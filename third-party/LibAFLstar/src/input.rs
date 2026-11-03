//! Fuzzer input. Raw bytes keep the original socket path. A generator input
//! carries the absolute path of a Python script and the parameter dictionary
//! that script receives.

use std::{
    collections::BTreeMap,
    fs::File,
    hash::{Hash, Hasher},
    io::Read,
    ops::RangeBounds,
    path::{Path, PathBuf},
    vec::Drain,
    borrow::Cow,
};

use libafl::{
    inputs::{BytesInput, HasMutatorBytes, HasTargetBytes, Input, ResizableMutator},
    Error,
};
use libafl_bolts::{ownedref::OwnedSlice, AsSlice, HasLen, Named};
use serde::{Deserialize, Serialize};

use crate::generator_mutator::mutate_generator;

/// Which way an input is delivered to the target.
pub enum Delivery<'a> {
    /// Write these bytes to the target socket, stdin, or shared memory.
    Bytes(&'a [u8]),
    /// Ask the generator server to run this script.
    Generator(&'a GeneratorInput),
}

/// Implemented by inputs the forkserver executor can deliver.
pub trait DeliverInput {
    /// How `run_target` should deliver this input.
    fn delivery(&self) -> Delivery<'_>;
}

impl DeliverInput for BytesInput {
    fn delivery(&self) -> Delivery<'_> {
        Delivery::Bytes(self.mutator_bytes())
    }
}

/// One concrete parameter value. Serialized as a plain CBOR/JSON value, not as
/// an externally tagged enum, so the Python side sees ints, strings, bools,
/// byte strings, and arrays.
#[derive(Clone, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(untagged)]
pub enum ParamValue {
    /// Signed integer.
    Int(i64),
    /// Boolean.
    Bool(bool),
    /// Text.
    Str(String),
    /// Nested array of the same value kinds.
    List(Vec<ParamValue>),
    /// Raw bytes. In JSON this is an array of numbers only when it is not a
    /// list of mixed values; CBOR byte strings decode here.
    Bytes(Vec<u8>),
}

/// Script path plus the parameters Rust is allowed to mutate.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct GeneratorInput {
    /// Absolute path of the generator script.
    pub generator: PathBuf,
    /// Parameter dictionary passed to `generate(ctx, params)`.
    pub params: BTreeMap<String, ParamValue>,
    /// JSON view used when a byte mutator reads this input out of the corpus.
    #[serde(skip)]
    pub(crate) wire: Vec<u8>,
}

/// Either a legacy byte seed or a generator invocation.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum StarInput {
    /// JSON object with a `generator` field.
    Generator(GeneratorInput),
    /// Raw protocol bytes.
    Bytes(BytesInput),
}

#[derive(Deserialize)]
struct GeneratorFile {
    generator: String,
    #[serde(default)]
    params: BTreeMap<String, ParamValue>,
}

impl Input for StarInput {
    fn to_file<P>(&self, path: P) -> Result<(), Error>
    where
        P: AsRef<Path>,
    {
        match self {
            StarInput::Bytes(bytes) => bytes.to_file(path),
            StarInput::Generator(generator) => {
                let encoded = serde_json::to_vec(generator)
                    .map_err(|error| Error::serialize(error.to_string()))?;
                libafl_bolts::fs::write_file_atomic(path, &encoded)?;
                Ok(())
            }
        }
    }

    fn from_file<P>(path: P) -> Result<Self, Error>
    where
        P: AsRef<Path>,
    {
        let mut file = File::open(path)?;
        let mut bytes = Vec::new();
        file.read_to_end(&mut bytes)?;
        if let Ok(text) = std::str::from_utf8(&bytes) {
            if let Ok(parsed) = serde_json::from_str::<GeneratorFile>(text) {
                if !parsed.generator.is_empty() {
                    return Ok(StarInput::Generator(GeneratorInput::new(
                        PathBuf::from(parsed.generator),
                        parsed.params,
                    )));
                }
            }
        }
        Ok(StarInput::Bytes(BytesInput::new(bytes)))
    }

    fn generate_name(&self, _id: Option<libafl::corpus::CorpusId>) -> String {
        let owned = self.target_bytes();
        format!("{:016x}", libafl_bolts::hash_std(owned.as_slice()))
    }
}

impl Hash for GeneratorInput {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.generator.hash(state);
        self.params.hash(state);
    }
}

impl Hash for StarInput {
    fn hash<H: Hasher>(&self, state: &mut H) {
        match self {
            StarInput::Generator(generator) => {
                0u8.hash(state);
                generator.hash(state);
            }
            StarInput::Bytes(bytes) => {
                1u8.hash(state);
                bytes.hash(state);
            }
        }
    }
}

impl HasTargetBytes for StarInput {
    fn target_bytes(&self) -> OwnedSlice<'_, u8> {
        match self {
            StarInput::Bytes(bytes) => bytes.target_bytes(),
            StarInput::Generator(generator) => {
                let encoded = serde_json::to_vec(generator).unwrap_or_default();
                OwnedSlice::from(encoded)
            }
        }
    }
}

impl HasLen for StarInput {
    fn len(&self) -> usize {
        self.mutator_bytes().len()
    }
}

impl HasMutatorBytes for StarInput {
    fn mutator_bytes(&self) -> &[u8] {
        match self {
            StarInput::Bytes(bytes) => bytes.mutator_bytes(),
            StarInput::Generator(generator) => generator.wire.as_slice(),
        }
    }

    fn mutator_bytes_mut(&mut self) -> &mut [u8] {
        match self {
            StarInput::Bytes(bytes) => bytes.mutator_bytes_mut(),
            StarInput::Generator(generator) => generator.wire.as_mut_slice(),
        }
    }
}

impl ResizableMutator<u8> for StarInput {
    fn resize(&mut self, new_len: usize, value: u8) {
        match self {
            StarInput::Bytes(bytes) => bytes.resize(new_len, value),
            StarInput::Generator(generator) => generator.wire.resize(new_len, value),
        }
    }

    fn extend<'a, It: IntoIterator<Item = &'a u8>>(&mut self, iter: It) {
        match self {
            StarInput::Bytes(bytes) => ResizableMutator::extend(bytes, iter),
            StarInput::Generator(generator) => {
                <Vec<u8> as std::iter::Extend<u8>>::extend(
                    &mut generator.wire,
                    iter.into_iter().copied(),
                );
            }
        }
    }

    fn splice<R, It>(&mut self, range: R, replace_with: It) -> std::vec::Splice<'_, It::IntoIter>
    where
        R: RangeBounds<usize>,
        It: IntoIterator<Item = u8>,
    {
        match self {
            StarInput::Bytes(bytes) => bytes.splice(range, replace_with),
            StarInput::Generator(generator) => generator.wire.splice(range, replace_with),
        }
    }

    fn drain<R>(&mut self, range: R) -> Drain<'_, u8>
    where
        R: RangeBounds<usize>,
    {
        match self {
            StarInput::Bytes(bytes) => bytes.drain(range),
            StarInput::Generator(generator) => generator.wire.drain(range),
        }
    }
}

impl DeliverInput for StarInput {
    fn delivery(&self) -> Delivery<'_> {
        match self {
            StarInput::Bytes(bytes) => Delivery::Bytes(bytes.mutator_bytes()),
            StarInput::Generator(generator) => Delivery::Generator(generator),
        }
    }
}

impl GeneratorInput {
    /// Build a generator input and cache its JSON byte view.
    pub fn new(generator: PathBuf, params: BTreeMap<String, ParamValue>) -> Self {
        let mut input = Self {
            generator,
            params,
            wire: Vec::new(),
        };
        input.refresh_wire();
        input
    }

    /// Rebuild the JSON byte view after the parameter dictionary changes.
    pub fn refresh_wire(&mut self) {
        self.wire = serde_json::to_vec(self).unwrap_or_default();
    }

    /// Encode the `op=run` body. Host, port, timeout, client, and id are added
    /// by the executor because they are not part of the mutated input.
    pub fn run_request(&self, client: u64, id: u64, host: &str, port: u16, timeout_ms: u64) -> Result<Vec<u8>, Error> {
        let request = RunRequest {
            op: "run",
            client,
            id,
            generator: self.generator.display().to_string(),
            params: &self.params,
            host,
            port,
            timeout_ms,
        };
        let mut encoded = Vec::new();
        ciborium::ser::into_writer(&request, &mut encoded).map_err(|error| {
            Error::serialize(format!("generator request cbor: {error}"))
        })?;
        Ok(encoded)
    }
}

#[derive(Serialize)]
struct RunRequest<'a> {
    op: &'static str,
    client: u64,
    id: u64,
    generator: String,
    params: &'a BTreeMap<String, ParamValue>,
    host: &'a str,
    port: u16,
    timeout_ms: u64,
}

/// Control messages that do not carry a generator input.
#[derive(Serialize)]
pub struct ControlRequest {
    /// `ping` or `reset`.
    pub op: &'static str,
    /// Fuzzer pid.
    pub client: u64,
    /// Monotonic id.
    pub id: u64,
}

impl ControlRequest {
    /// CBOR-encode this control request.
    pub fn encode(&self) -> Result<Vec<u8>, Error> {
        let mut encoded = Vec::new();
        ciborium::ser::into_writer(self, &mut encoded)
            .map_err(|error| Error::serialize(format!("control request cbor: {error}")))?;
        Ok(encoded)
    }
}

/// One response from the generator server.
#[derive(Debug, Deserialize, PartialEq)]
pub struct GeneratorResponse {
    /// Request id echoed by the server.
    pub id: u64,
    /// `ok`, `timeout`, `conn`, or `generator`.
    pub status: String,
    /// Failure detail.
    #[serde(default)]
    pub error: Option<String>,
    /// Bytes written on every channel.
    #[serde(default)]
    pub sent: u64,
    /// Bytes read on every channel.
    #[serde(default)]
    pub recv: u64,
}

impl GeneratorResponse {
    /// Decode a response body.
    pub fn decode(bytes: &[u8]) -> Result<Self, Error> {
        ciborium::de::from_reader(bytes)
            .map_err(|error| Error::serialize(format!("generator response cbor: {error}")))
    }
}

/// Runs the byte mutator or the parameter mutator according to [`StarInput`].
pub struct DispatchMutator<M> {
    name: Cow<'static, str>,
    inner: M,
}

impl<M> DispatchMutator<M>
where
    M: Named,
{
    /// Wrap the protocol mutator already constructed by a fuzzer binary.
    pub fn new(inner: M) -> Self {
        let name = Cow::Owned(format!("DispatchMutator[{}]", inner.name()));
        Self { name, inner }
    }
}

impl<M> Named for DispatchMutator<M> {
    fn name(&self) -> &Cow<'static, str> {
        &self.name
    }
}

impl<M, S> libafl::mutators::Mutator<StarInput, S> for DispatchMutator<M>
where
    M: libafl::mutators::Mutator<StarInput, S>,
    S: libafl::state::HasRand,
    S::Rand: libafl_bolts::rands::Rand,
{
    fn mutate(
        &mut self,
        state: &mut S,
        input: &mut StarInput,
    ) -> Result<libafl::mutators::MutationResult, Error> {
        if let StarInput::Generator(generator) = input {
            let result = mutate_generator(state.rand_mut(), generator);
            generator.refresh_wire();
            return Ok(result);
        }
        self.inner.mutate(state, input)
    }

    fn post_exec(
        &mut self,
        state: &mut S,
        corpus_idx: Option<libafl::corpus::CorpusId>,
    ) -> Result<(), Error> {
        self.inner.post_exec(state, corpus_idx)
    }
}

#[cfg(test)]
mod tests {
    use std::io::Write;

    use super::*;
    use crate::generator_mutator::mutate_generator;
    use libafl_bolts::rands::StdRand;

    #[test]
    fn raw_seed_stays_bytes() {
        let mut file = tempfile_path("user");
        write!(file.1, "USER\r\n").unwrap();
        let input = StarInput::from_file(&file.0).unwrap();
        match input {
            StarInput::Bytes(bytes) => assert_eq!(bytes.mutator_bytes(), b"USER\r\n"),
            StarInput::Generator(_) => panic!("raw seed classified as generator"),
        }
    }

    #[test]
    fn json_seed_is_generator_and_roundtrips() {
        let script = "/tmp/ftp_cmd.py";
        let mut file = tempfile_path("gen.json");
        write!(
            file.1,
            r#"{{"generator":"{script}","params":{{"verb":"USER","arg":"ubuntu"}}}}"#
        )
        .unwrap();
        let input = StarInput::from_file(&file.0).unwrap();
        let StarInput::Generator(generator) = input.clone() else {
            panic!("json seed classified as bytes");
        };
        assert_eq!(generator.generator, PathBuf::from(script));
        assert_eq!(
            generator.params.get("verb"),
            Some(&ParamValue::Str("USER".to_string()))
        );
        input.to_file(&file.0).unwrap();
        let again = StarInput::from_file(&file.0).unwrap();
        assert_eq!(again, input);
    }

    #[test]
    fn generator_mutation_keeps_the_script_path() {
        let mut generator = GeneratorInput::new(
            PathBuf::from("/tmp/ftp_cmd.py"),
            BTreeMap::from([
                ("verb".to_string(), ParamValue::Str("USER".to_string())),
                ("arg".to_string(), ParamValue::Str("ubuntu".to_string())),
            ]),
        );
        let before = generator.params.clone();
        let mut rand = StdRand::with_seed(1);
        let mutated = mutate_generator(&mut rand, &mut generator);
        assert_eq!(mutated, libafl::mutators::MutationResult::Mutated);
        assert_eq!(generator.generator, PathBuf::from("/tmp/ftp_cmd.py"));
        assert_ne!(generator.params, before);
    }

    fn tempfile_path(name: &str) -> (PathBuf, File) {
        let path = std::env::temp_dir().join(format!("libaflstar-{name}-{}", std::process::id()));
        let file = File::create(&path).unwrap();
        (path, file)
    }
}
