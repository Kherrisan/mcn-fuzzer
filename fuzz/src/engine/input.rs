//! Generator script path plus the parameter dictionary Rust is allowed to mutate.

use std::{
    collections::BTreeMap,
    fs::File,
    hash::{Hash, Hasher},
    io::{self, Read, Write},
    path::{Path, PathBuf},
};
#[cfg(feature = "stats")]
use std::{
    sync::{Arc, Mutex},
    time::Instant,
};

use libafl::{inputs::Input, Error};
use serde::{Deserialize, Serialize};

/// One concrete parameter value. Serialized as a plain JSON/CBOR value.
#[derive(Clone, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(untagged)]
pub enum ParamValue {
    /// Signed integer.
    Int(i64),
    /// Boolean.
    Bool(bool),
    /// Text.
    Str(String),
    /// Nested array.
    List(Vec<ParamValue>),
    /// Raw bytes.
    Bytes(Vec<u8>),
}

/// zlib bytes waiting to be written beside a saved input. Not part of the seed.
#[derive(Clone, Debug)]
pub struct PendingCapture {
    /// Random nonce for this process. A restarted process with the same sequence still gets a new file.
    pub session: u64,
    /// Monotonic sequence inside that process. Starts at 1.
    pub seq: u64,
    /// zlib level-1 payload. `to_file` stream-decompresses this and does not inspect the bytes.
    pub compressed: Vec<u8>,
    /// Sidecar write time, in microseconds. Shared with the case phase record.
    #[cfg(feature = "stats")]
    pub persist_us: Arc<Mutex<u128>>,
}

/// Script path plus the parameters passed to `generate(ctx, params)`.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct GeneratorInput {
    /// Path of the generator script. Relative paths are resolved at load time.
    pub generator: PathBuf,
    /// Parameter dictionary.
    pub params: BTreeMap<String, ParamValue>,
    /// Runtime capture export. Skipped by JSON, hashing, equality, and mutation.
    #[serde(skip)]
    pending: Option<PendingCapture>,
}

#[derive(Deserialize)]
struct GeneratorFile {
    generator: String,
    #[serde(default)]
    params: BTreeMap<String, ParamValue>,
}

impl GeneratorInput {
    /// Build a generator input.
    pub fn new(generator: PathBuf, params: BTreeMap<String, ParamValue>) -> Self {
        Self {
            generator,
            params,
            pending: None,
        }
    }

    /// Attach a capture that `to_file` will export. The scheduled parent input is not this value.
    pub fn attach_pending(&mut self, pending: PendingCapture) {
        self.pending = Some(pending);
    }

    /// Whether a capture sidecar will be written with this input.
    pub fn has_pending(&self) -> bool {
        self.pending.is_some()
    }

    /// Encode the `op=run` body.
    pub fn run_request(
        &self,
        client: u64,
        id: u64,
        worker_id: u64,
        case_id: u64,
        host: &str,
        port: u16,
        timeout_ms: u64,
        connect_timeout_ms: u64,
    ) -> Result<Vec<u8>, Error> {
        let request = RunRequest {
            op: "run",
            client,
            id,
            worker_id,
            case_id,
            generator: self.generator.display().to_string(),
            params: &self.params,
            host,
            port,
            timeout_ms,
            connect_timeout_ms,
        };
        let mut encoded = Vec::new();
        ciborium::ser::into_writer(&request, &mut encoded)
            .map_err(|error| Error::serialize(format!("generator request cbor: {error}")))?;
        Ok(encoded)
    }
}

#[derive(Serialize)]
struct RunRequest<'a> {
    op: &'static str,
    client: u64,
    id: u64,
    worker_id: u64,
    case_id: u64,
    generator: String,
    params: &'a BTreeMap<String, ParamValue>,
    host: &'a str,
    port: u16,
    timeout_ms: u64,
    connect_timeout_ms: u64,
}

/// `ping` or `reset`.
#[derive(Serialize)]
pub struct ControlRequest {
    /// Operation name.
    pub op: &'static str,
    /// Fuzzer process id.
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
#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct GeneratorResponse {
    /// Request id echoed by the server.
    pub id: u64,
    /// `ok`, `timeout`, `conn`, `eof`, or `generator`.
    pub status: String,
    /// Failure detail.
    #[serde(default)]
    pub error: Option<String>,
    /// `cancel_ack` or `terminal` when the server distinguishes them.
    #[serde(default)]
    pub kind: Option<String>,
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

impl PartialEq for GeneratorInput {
    fn eq(&self, other: &Self) -> bool {
        self.generator == other.generator && self.params == other.params
    }
}

impl Eq for GeneratorInput {}

impl Hash for GeneratorInput {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.generator.hash(state);
        self.params.hash(state);
    }
}

impl Input for GeneratorInput {
    fn to_file<P>(&self, path: P) -> Result<(), Error>
    where
        P: AsRef<Path>,
    {
        let path = path.as_ref();
        let encoded =
            serde_json::to_vec_pretty(self).map_err(|error| Error::serialize(error.to_string()))?;
        libafl_bolts::fs::write_file_atomic(path, &encoded)?;
        if let Some(pending) = &self.pending {
            #[cfg(feature = "stats")]
            let started = Instant::now();
            let name = path.file_name().ok_or_else(|| {
                Error::illegal_argument("generator input path has no file name".to_string())
            })?;
            let sidecar = path.with_file_name(format!(".{}.pcap", name.to_string_lossy()));
            // Stream the zlib payload to the sibling. Do not read it back or check its contents.
            let mut output = File::create(&sidecar)?;
            let mut decoder = flate2::read::ZlibDecoder::new(pending.compressed.as_slice());
            io::copy(&mut decoder, &mut output)?;
            output.flush()?;
            #[cfg(feature = "stats")]
            {
                *pending
                    .persist_us
                    .lock()
                    .unwrap_or_else(|error| error.into_inner()) = started.elapsed().as_micros();
            }
        }
        Ok(())
    }

    fn from_file<P>(path: P) -> Result<Self, Error>
    where
        P: AsRef<Path>,
    {
        let path = path.as_ref();
        let mut file = File::open(path)?;
        let mut bytes = Vec::new();
        file.read_to_end(&mut bytes)?;
        let parsed: GeneratorFile = serde_json::from_slice(&bytes).map_err(|error| {
            Error::serialize(format!("generator seed {}: {error}", path.display()))
        })?;
        if parsed.generator.is_empty() {
            return Err(Error::illegal_argument(format!(
                "generator seed {} has an empty script path",
                path.display()
            )));
        }
        let generator = resolve_generator(path, Path::new(&parsed.generator))?;
        Ok(GeneratorInput::new(generator, parsed.params))
    }

    fn generate_name(&self, _id: Option<libafl::corpus::CorpusId>) -> String {
        let hash = format!("{:016x}", libafl_bolts::generic_hash_std(self));
        match &self.pending {
            Some(pending) => format!("{hash}-{:016x}-{:016x}", pending.session, pending.seq),
            None => hash,
        }
    }
}

fn resolve_generator(seed: &Path, generator: &Path) -> Result<PathBuf, Error> {
    if generator.is_absolute() {
        return Ok(generator.to_path_buf());
    }
    let seed_dir = seed.parent().unwrap_or_else(|| Path::new("."));
    let beside = seed_dir.join(generator);
    if beside.exists() {
        return std::fs::canonicalize(&beside).map_err(|error| {
            Error::os_error(error, format!("generator script {}", beside.display()))
        });
    }
    if let Some(parent) = seed_dir.parent() {
        let campaign = parent.join(generator);
        if campaign.exists() {
            return std::fs::canonicalize(&campaign).map_err(|error| {
                Error::os_error(error, format!("generator script {}", campaign.display()))
            });
        }
    }
    Err(Error::illegal_argument(format!(
        "generator script {} from {} was not found",
        generator.display(),
        seed.display()
    )))
}

#[cfg(test)]
mod tests {
    use std::io::Write;

    use super::*;

    #[test]
    fn json_seed_roundtrips_and_resolves_a_relative_script() {
        let dir = std::env::temp_dir().join(format!("mcn-input-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("seeds")).unwrap();
        let script = dir.join("gen.py");
        std::fs::write(&script, "def generate(ctx, params):\n    pass\n").unwrap();
        let seed = dir.join("seeds").join("one.json");
        std::fs::write(
            &seed,
            r#"{"generator":"../gen.py","params":{"user":"ubuntu","tries":2,"ok":true}}"#,
        )
        .unwrap();
        let input = GeneratorInput::from_file(&seed).unwrap();
        assert_eq!(input.generator, std::fs::canonicalize(&script).unwrap());
        assert_eq!(
            input.params.get("user"),
            Some(&ParamValue::Str("ubuntu".to_string()))
        );
        assert_eq!(input.params.get("tries"), Some(&ParamValue::Int(2)));
        assert_eq!(input.params.get("ok"), Some(&ParamValue::Bool(true)));
        input.to_file(&seed).unwrap();
        let again = GeneratorInput::from_file(&seed).unwrap();
        assert_eq!(again, input);
        assert!(!again.has_pending());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn pending_capture_is_exported_and_ignored_by_identity() {
        let dir = std::env::temp_dir().join(format!("mcn-pcap-input-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let script = dir.join("gen.py");
        std::fs::write(&script, "def generate(ctx, params):\n    pass\n").unwrap();
        let seed = dir.join("one.json");
        std::fs::write(&seed, format!("{{\"generator\":\"{}\"}}", script.display())).unwrap();
        let input = GeneratorInput::from_file(&seed).unwrap();
        assert!(!input.has_pending());

        let raw = b"not-a-pcap-check";
        let mut encoder = flate2::write::ZlibEncoder::new(Vec::new(), flate2::Compression::new(1));
        encoder.write_all(raw).unwrap();
        let compressed = encoder.finish().unwrap();
        #[cfg(feature = "stats")]
        let persist = std::sync::Arc::new(std::sync::Mutex::new(0u128));
        let mut first = input.clone();
        first.attach_pending(PendingCapture {
            session: 0xabc,
            seq: 1,
            compressed: compressed.clone(),
            #[cfg(feature = "stats")]
            persist_us: std::sync::Arc::clone(&persist),
        });
        let mut second = input.clone();
        second.attach_pending(PendingCapture {
            session: 0xabc,
            seq: 2,
            compressed: compressed.clone(),
            #[cfg(feature = "stats")]
            persist_us: std::sync::Arc::new(std::sync::Mutex::new(0)),
        });
        let mut restarted = input.clone();
        restarted.attach_pending(PendingCapture {
            session: 0xdef,
            seq: 1,
            compressed,
            #[cfg(feature = "stats")]
            persist_us: std::sync::Arc::new(std::sync::Mutex::new(0)),
        });
        assert_eq!(first, second);
        assert_eq!(
            libafl_bolts::generic_hash_std(&first),
            libafl_bolts::generic_hash_std(&input)
        );
        assert_ne!(first.generate_name(None), second.generate_name(None));
        assert_ne!(first.generate_name(None), restarted.generate_name(None));
        assert!(first
            .generate_name(None)
            .ends_with("-0000000000000abc-0000000000000001"));
        assert_eq!(
            input.generate_name(None),
            format!("{:016x}", libafl_bolts::generic_hash_std(&input))
        );

        let saved = dir.join(first.generate_name(None));
        first.to_file(&saved).unwrap();
        let sidecar = dir.join(format!(
            ".{}.pcap",
            saved.file_name().unwrap().to_string_lossy()
        ));
        assert_eq!(std::fs::read(&sidecar).unwrap(), raw);
        #[cfg(feature = "stats")]
        assert!(*persist.lock().unwrap() > 0);
        let loaded = GeneratorInput::from_file(&saved).unwrap();
        assert!(!loaded.has_pending());
        assert_eq!(loaded, input);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
