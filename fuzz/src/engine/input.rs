//! Generator script path plus the per-instance parameter map Rust mutates.

use std::{
    collections::{BTreeMap, BTreeSet},
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
    /// An explicitly unset shared value.
    Null,
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

/// Semantic kind of a mutable field.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum FieldKind {
    #[default]
    String,
    Bytes,
    Integer,
    Boolean,
    List,
}

/// Wire encoding for `FieldKind::Bytes` values.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum BytesEncoding {
    #[default]
    Hex,
    Base64,
}

/// Kind and encoding for one mutable field, carried beside its value.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FieldSpec {
    #[serde(default)]
    pub kind: FieldKind,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub encoding: Option<BytesEncoding>,
}

impl FieldSpec {
    /// Encoding used for byte values, defaulting to hex.
    pub fn bytes_encoding(&self) -> BytesEncoding {
        self.encoding.unwrap_or_default()
    }
}

/// Mutable field values for one step instance: field name to value.
pub type InstanceFields = BTreeMap<String, ParamValue>;

/// Per-instance mutable values: step instance name to its fields.
pub type InstanceParams = BTreeMap<String, InstanceFields>;

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

/// One concrete step instance selected by Rust for execution.
#[derive(Clone, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StepInstance {
    /// Unique instance name inside this trace.
    pub name: String,
    /// Model step name this instance implements.
    pub step: String,
    /// Model action: send, listen, connect, or close.
    pub action: String,
    #[serde(default)]
    pub condition: String,
    #[serde(default)]
    pub message: Option<String>,
    pub channel: String,
    #[serde(default)]
    pub sender: Option<String>,
    #[serde(default)]
    pub receiver: Option<String>,
    #[serde(default)]
    pub party: Option<String>,
    /// Fields explicitly removed by Rust; distinct from "not overridden".
    #[serde(default)]
    pub omitted_fields: Vec<String>,
    /// Dependency bindings from target field to `instance.field` source.
    #[serde(default)]
    pub inputs: BTreeMap<String, String>,
    /// Dependencies deliberately broken by the selected mutation strategy.
    #[serde(default)]
    pub broken_dependencies: Vec<String>,
}

/// One concrete dependency connecting two step instances.
#[derive(Clone, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DependencyInstance {
    pub prior: String,
    pub dependent: String,
    #[serde(default)]
    pub kind: String,
    #[serde(default)]
    pub condition: String,
    #[serde(default)]
    pub fields: Vec<String>,
    #[serde(default)]
    pub origin: String,
}

/// The mutable concrete trace and its instance-level dependencies.
#[derive(Clone, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TraceInstance {
    #[serde(default, rename = "step_trace")]
    pub steps: Vec<StepInstance>,
    #[serde(default)]
    pub dependencies: Vec<DependencyInstance>,
}

impl TraceInstance {
    /// Validate step uniqueness and dependency field materialization.
    pub fn validate(&self) -> Result<(), String> {
        let steps = self
            .steps
            .iter()
            .enumerate()
            .map(|(index, step)| (step.name.as_str(), index))
            .collect::<BTreeMap<_, _>>();
        if steps.len() != self.steps.len() {
            return Err("duplicate step instance name".into());
        }
        let mut bindings = BTreeSet::new();
        for dependency in &self.dependencies {
            let Some(prior) = steps.get(dependency.prior.as_str()) else {
                return Err(format!(
                    "dependency prior {} is not a step instance",
                    dependency.prior
                ));
            };
            let Some(dependent) = steps.get(dependency.dependent.as_str()) else {
                return Err(format!(
                    "dependency dependent {} is not a step instance",
                    dependency.dependent
                ));
            };
            if prior >= dependent {
                return Err(format!(
                    "dependency {} -> {} is not in trace order",
                    dependency.prior, dependency.dependent
                ));
            }
            if dependency.kind == "data" && dependency.fields.is_empty() {
                return Err(format!(
                    "data dependency {} -> {} has no field binding",
                    dependency.prior, dependency.dependent
                ));
            }
            for field in &dependency.fields {
                if field.trim().is_empty() {
                    return Err(format!(
                        "dependency {} -> {} has an empty field binding",
                        dependency.prior, dependency.dependent
                    ));
                }
                if !bindings.insert((dependency.dependent.clone(), field.clone())) {
                    return Err(format!("dependent field {} is bound more than once", field));
                }
                let expected = format!("{}.{field}", dependency.prior);
                let actual = self
                    .steps
                    .iter()
                    .find(|step| step.name == dependency.dependent)
                    .and_then(|step| step.inputs.get(field));
                if actual != Some(&expected) {
                    return Err(format!(
                        "step {} input {} does not match dependency binding {expected}",
                        dependency.dependent, field
                    ));
                }
            }
        }
        Ok(())
    }
}

/// Script path plus the concrete step trace passed to `generate(ctx, step_trace)`.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GeneratorInput {
    /// Path of the generator script. Relative paths are resolved at load time.
    pub generator: PathBuf,
    /// Concrete step and dependency instances.
    #[serde(flatten)]
    pub trace: TraceInstance,
    /// Mutable values keyed by step instance name, passed read-only to handlers.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub params: InstanceParams,
    /// Kind metadata for mutable values, keyed like `params`.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub param_kinds: BTreeMap<String, BTreeMap<String, FieldSpec>>,
    /// Runtime capture export. Skipped by JSON, hashing, equality, and mutation.
    #[serde(skip)]
    pending: Option<PendingCapture>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct GeneratorFile {
    generator: String,
    #[serde(flatten)]
    trace: TraceInstance,
    #[serde(default)]
    params: InstanceParams,
    #[serde(default)]
    param_kinds: BTreeMap<String, BTreeMap<String, FieldSpec>>,
}

impl GeneratorInput {
    /// Build a generator input.
    pub fn new(generator: PathBuf, params: InstanceParams) -> Self {
        Self {
            generator,
            trace: TraceInstance::default(),
            params,
            param_kinds: BTreeMap::new(),
            pending: None,
        }
    }

    /// Build an execution input from a concrete trace.
    pub fn with_trace(
        generator: PathBuf,
        step_trace: Vec<StepInstance>,
        params: InstanceParams,
    ) -> Self {
        Self {
            generator,
            trace: TraceInstance {
                steps: step_trace,
                ..TraceInstance::default()
            },
            params,
            param_kinds: BTreeMap::new(),
            pending: None,
        }
    }

    /// Validate the trace plus its per-instance parameter bindings.
    pub fn validate(&self) -> Result<(), String> {
        self.trace.validate()?;
        let steps = self
            .trace
            .steps
            .iter()
            .map(|step| step.name.as_str())
            .collect::<BTreeSet<_>>();
        for (instance, fields) in &self.params {
            if !steps.contains(instance.as_str()) {
                return Err(format!(
                    "params key {instance} is not a step instance"
                ));
            }
            if fields.is_empty() {
                return Err(format!("params for {instance} is empty"));
            }
            let omitted = self
                .trace
                .steps
                .iter()
                .find(|step| &step.name == instance)
                .map(|step| step.omitted_fields.as_slice())
                .unwrap_or(&[]);
            for field in fields.keys() {
                if omitted.iter().any(|name| name == field) {
                    return Err(format!(
                        "step {instance} has field {field} both explicit and omitted"
                    ));
                }
            }
        }
        for (instance, fields) in &self.param_kinds {
            let Some(values) = self.params.get(instance) else {
                return Err(format!("param kind instance {instance} has no params"));
            };
            for field in fields.keys() {
                if !values.contains_key(field) {
                    return Err(format!(
                        "param kind {instance}.{field} has no params value"
                    ));
                }
            }
        }
        Ok(())
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
            step_trace: &self.trace.steps,
            dependencies: &self.trace.dependencies,
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
    step_trace: &'a [StepInstance],
    dependencies: &'a [DependencyInstance],
    params: &'a InstanceParams,
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
        self.generator == other.generator
            && self.trace == other.trace
            && self.params == other.params
            && self.param_kinds == other.param_kinds
    }
}

impl Eq for GeneratorInput {}

impl Hash for GeneratorInput {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.generator.hash(state);
        self.trace.hash(state);
        self.params.hash(state);
        self.param_kinds.hash(state);
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
        Ok(GeneratorInput {
            generator,
            trace: parsed.trace,
            params: parsed.params,
            param_kinds: parsed.param_kinds,
            pending: None,
        })
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
    fn unknown_input_fields_are_parse_errors() {
        let dir = std::env::temp_dir().join(format!("mcn-input-strict-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("gen.py"), "def generate(ctx, params):\n    pass\n").unwrap();
        let seed = dir.join("old.json");
        std::fs::write(
            &seed,
            r#"{"version":3,"generator":"gen.py","params":{"session_1":{"user":"ubuntu"}}}"#,
        )
        .unwrap();
        let error = GeneratorInput::from_file(&seed).unwrap_err();
        assert!(format!("{error:?}").contains("version"), "{error:?}");
        let _ = std::fs::remove_dir_all(&dir);
    }

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
            r#"{"generator":"../gen.py","params":{"session_1":{"user":"ubuntu","tries":2,"ok":true}}}"#,
        )
        .unwrap();
        let input = GeneratorInput::from_file(&seed).unwrap();
        assert_eq!(input.generator, std::fs::canonicalize(&script).unwrap());
        let session = input.params.get("session_1").expect("instance params");
        assert_eq!(
            session.get("user"),
            Some(&ParamValue::Str("ubuntu".to_string()))
        );
        assert_eq!(session.get("tries"), Some(&ParamValue::Int(2)));
        assert_eq!(session.get("ok"), Some(&ParamValue::Bool(true)));
        input.to_file(&seed).unwrap();
        let again = GeneratorInput::from_file(&seed).unwrap();
        assert_eq!(again, input);
        assert!(!again.has_pending());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn step_trace_and_params_roundtrip() {
        let dir = std::env::temp_dir().join(format!("mcn-step-input-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let script = dir.join("gen.py");
        std::fs::write(&script, "def generate(ctx, step_trace):\n    pass\n").unwrap();
        let instance = StepInstance {
            name: "hello_1".into(),
            step: "hello".into(),
            action: "send".into(),
            condition: "start".into(),
            message: Some("hello".into()),
            channel: "control".into(),
            sender: Some("client".into()),
            receiver: Some("server".into()),
            party: None,
            omitted_fields: vec!["optional".into()],
            inputs: BTreeMap::from([("token".into(), "hello_1.field".into())]),
            broken_dependencies: vec![],
        };
        let mut input = GeneratorInput::with_trace(
            std::fs::canonicalize(&script).unwrap(),
            vec![instance],
            BTreeMap::from([(
                "hello_1".into(),
                BTreeMap::from([("username".into(), ParamValue::Str("ubuntu".into()))]),
            )]),
        );
        input.param_kinds.insert(
            "hello_1".into(),
            BTreeMap::from([(
                "username".into(),
                FieldSpec {
                    kind: FieldKind::String,
                    encoding: None,
                },
            )]),
        );

        input.validate().unwrap();
        let path = dir.join("input.json");
        input.to_file(&path).unwrap();
        let saved: serde_json::Value =
            serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert!(saved.get("parties").is_none());
        assert!(saved.get("channels").is_none());
        let body = input
            .run_request(1, 2, 3, 4, "127.0.0.1", 21, 1000, 1000)
            .unwrap();
        let request: serde_json::Value = ciborium::de::from_reader(body.as_slice()).unwrap();
        assert!(request.get("parties").is_none());
        assert!(request.get("channels").is_none());
        assert_eq!(request["params"]["hello_1"]["username"], "ubuntu");
        let again = GeneratorInput::from_file(&path).unwrap();
        assert_eq!(again, input);
        assert_eq!(again.trace.steps[0].name, "hello_1");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn field_kinds_and_encodings_roundtrip() {
        assert_eq!(
            serde_json::from_str::<FieldKind>("\"bytes\"").unwrap(),
            FieldKind::Bytes
        );
        assert_eq!(
            serde_json::from_str::<BytesEncoding>("\"base64\"").unwrap(),
            BytesEncoding::Base64
        );
        let spec: FieldSpec =
            serde_json::from_str(r#"{"kind":"bytes","encoding":"hex"}"#).unwrap();
        assert_eq!(spec.kind, FieldKind::Bytes);
        assert_eq!(spec.bytes_encoding(), BytesEncoding::Hex);
        assert_eq!(serde_json::to_string(&FieldKind::List).unwrap(), "\"list\"");
    }

    #[test]
    fn pending_capture_is_exported_and_ignored_by_identity() {
        let dir = std::env::temp_dir().join(format!("mcn-pcap-input-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let script = dir.join("gen.py");
        std::fs::write(&script, "def generate(ctx, params):\n    pass\n").unwrap();
        let seed = dir.join("one.json");
        std::fs::write(
            &seed,
            format!("{{\"generator\":\"{}\"}}", script.display()),
        )
        .unwrap();
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
