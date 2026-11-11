//! Generator script path plus the parameter dictionary Rust is allowed to mutate.

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

/// Version of the concrete step-trace execution input.
pub const STEP_TRACE_VERSION: u32 = 3;

/// One concrete step instance selected by Rust for execution.
#[derive(Clone, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
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
    /// Final field values decided by Rust. Missing means use dependency/global
    /// context in the generator.
    #[serde(default)]
    pub fields: BTreeMap<String, ParamValue>,
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

/// One concrete party instance, bound to its model party type.
#[derive(Clone, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct PartyInstance {
    pub name: String,
    #[serde(rename = "type")]
    pub r#type: String,
}

/// One concrete channel instance. Endpoints are party instance names, exactly
/// like the model channel's endpoint_party_names relationship.
#[derive(Clone, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct ChannelInstance {
    pub name: String,
    #[serde(rename = "type")]
    pub r#type: String,
    #[serde(default)]
    pub endpoints: Vec<String>,
}

/// A field-level dependency binding between two step instances.
#[derive(Clone, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct FieldBinding {
    pub prior_field: String,
    pub dependent_field: String,
    #[serde(default)]
    pub derive: String,
}

/// One concrete dependency connecting two step instances.
#[derive(Clone, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct DependencyInstance {
    pub prior: String,
    pub dependent: String,
    #[serde(default)]
    pub kind: String,
    #[serde(default)]
    pub condition: String,
    #[serde(default)]
    pub fields: Vec<FieldBinding>,
    #[serde(default)]
    pub origin: String,
}

/// The mutable concrete trace. Model binding relationships are mirrored here:
/// channel endpoints are party instances, steps reference a channel and party
/// instances, and dependencies reference step instances.
#[derive(Clone, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct TraceInstance {
    #[serde(default)]
    pub parties: Vec<PartyInstance>,
    #[serde(default)]
    pub channels: Vec<ChannelInstance>,
    #[serde(default, rename = "step_trace")]
    pub steps: Vec<StepInstance>,
    #[serde(default)]
    pub dependencies: Vec<DependencyInstance>,
}

impl TraceInstance {
    /// Validate model-shaped bindings and dependency field materialization.
    pub fn validate(&self) -> Result<(), String> {
        let parties = self
            .parties
            .iter()
            .map(|party| (party.name.as_str(), party))
            .collect::<BTreeMap<_, _>>();
        if parties.len() != self.parties.len() {
            return Err("duplicate party instance name".into());
        }
        let channels = self
            .channels
            .iter()
            .map(|channel| (channel.name.as_str(), channel))
            .collect::<BTreeMap<_, _>>();
        if channels.len() != self.channels.len() {
            return Err("duplicate channel instance name".into());
        }
        for channel in &self.channels {
            let mut endpoints = BTreeSet::new();
            for endpoint in &channel.endpoints {
                if !endpoints.insert(endpoint) {
                    return Err(format!(
                        "channel {} repeats endpoint {endpoint}",
                        channel.name
                    ));
                }
                if !parties.contains_key(endpoint.as_str()) {
                    return Err(format!(
                        "channel {} endpoint {endpoint} is not a party instance",
                        channel.name
                    ));
                }
            }
        }
        let steps = self
            .steps
            .iter()
            .enumerate()
            .map(|(index, step)| (step.name.as_str(), index))
            .collect::<BTreeMap<_, _>>();
        if steps.len() != self.steps.len() {
            return Err("duplicate step instance name".into());
        }
        for step in &self.steps {
            let Some(channel) = channels.get(step.channel.as_str()) else {
                return Err(format!(
                    "step {} references unknown channel {}",
                    step.name, step.channel
                ));
            };
            let party_ok = |name: Option<&str>, label: &str| -> Result<(), String> {
                let Some(name) = name else {
                    return Err(format!("step {} needs {label}", step.name));
                };
                if !parties.contains_key(name) {
                    return Err(format!(
                        "step {} {label} {name} is not a party instance",
                        step.name
                    ));
                }
                if !channel.endpoints.iter().any(|endpoint| endpoint == name) {
                    return Err(format!(
                        "step {} {label} {name} is not an endpoint of channel {}",
                        step.name, channel.name
                    ));
                }
                Ok(())
            };
            if step.action == "send" {
                party_ok(step.sender.as_deref(), "sender")?;
                party_ok(step.receiver.as_deref(), "receiver")?;
                if step.party.is_some() {
                    return Err(format!("send step {} must not name a party", step.name));
                }
            } else {
                party_ok(step.party.as_deref(), "party")?;
                if step.sender.is_some() || step.receiver.is_some() {
                    return Err(format!(
                        "network step {} must not name sender/receiver",
                        step.name
                    ));
                }
            }
            if step
                .fields
                .keys()
                .any(|field| step.omitted_fields.contains(field))
            {
                return Err(format!(
                    "step {} has a field both explicit and omitted",
                    step.name
                ));
            }
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
                if field.prior_field.is_empty() || field.dependent_field.is_empty() {
                    return Err(format!(
                        "dependency {} -> {} has an empty field binding",
                        dependency.prior, dependency.dependent
                    ));
                }
                if !bindings.insert((dependency.dependent.clone(), field.dependent_field.clone())) {
                    return Err(format!(
                        "dependent field {} is bound more than once",
                        field.dependent_field
                    ));
                }
                let expected = format!("{}.{}", dependency.prior, field.prior_field);
                let actual = self
                    .steps
                    .iter()
                    .find(|step| step.name == dependency.dependent)
                    .and_then(|step| step.inputs.get(&field.dependent_field));
                if actual != Some(&expected) {
                    return Err(format!(
                        "step {} input {} does not match dependency binding {expected}",
                        dependency.dependent, field.dependent_field
                    ));
                }
            }
        }
        Ok(())
    }
}

/// A value available once per testcase, independent of any step instance.
#[derive(Clone, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct GlobalContextValue {
    pub name: String,
    #[serde(default)]
    pub kind: String,
    #[serde(default)]
    pub value: Option<ParamValue>,
    /// `constant`, `environment`, or `generated`.
    #[serde(default)]
    pub source: String,
    /// Deterministic seed for generated values, replayed with the input.
    #[serde(default)]
    pub seed: Option<u64>,
}

impl GlobalContextValue {
    pub fn as_param(&self) -> Option<&ParamValue> {
        self.value.as_ref()
    }
}

/// Script path plus the concrete step trace passed to `generate(ctx, step_trace)`.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct GeneratorInput {
    /// Execution-input format version. Legacy inputs without it are rejected.
    #[serde(default = "default_version")]
    pub version: u32,
    /// Path of the generator script. Relative paths are resolved at load time.
    pub generator: PathBuf,
    /// Concrete trace: parties, channels, step instances, dependency instances.
    #[serde(flatten)]
    pub trace: TraceInstance,
    /// Per-testcase values not owned by a step.
    #[serde(default)]
    pub global_context: Vec<GlobalContextValue>,
    /// Legacy parameter dictionary kept for old local seeds and global defaults.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub params: BTreeMap<String, ParamValue>,
    /// Runtime capture export. Skipped by JSON, hashing, equality, and mutation.
    #[serde(skip)]
    pending: Option<PendingCapture>,
}

fn default_version() -> u32 {
    STEP_TRACE_VERSION
}

#[derive(Deserialize)]
struct GeneratorFile {
    #[serde(default)]
    version: Option<u32>,
    generator: String,
    #[serde(default)]
    global_context: Vec<GlobalContextValue>,
    #[serde(flatten)]
    trace: TraceInstance,
    #[serde(default)]
    params: BTreeMap<String, ParamValue>,
}

impl GeneratorInput {
    /// Build a generator input.
    pub fn new(generator: PathBuf, params: BTreeMap<String, ParamValue>) -> Self {
        Self {
            version: STEP_TRACE_VERSION,
            generator,
            trace: TraceInstance::default(),
            global_context: Vec::new(),
            params,
            pending: None,
        }
    }

    /// Build an execution input from a concrete trace.
    pub fn with_trace(
        generator: PathBuf,
        step_trace: Vec<StepInstance>,
        global_context: Vec<GlobalContextValue>,
    ) -> Self {
        Self {
            version: STEP_TRACE_VERSION,
            generator,
            trace: TraceInstance {
                steps: step_trace,
                ..TraceInstance::default()
            },
            global_context,
            params: BTreeMap::new(),
            pending: None,
        }
    }

    /// Old corpus entries without a version are intentionally not inferred.
    pub fn require_current_version(&self) -> Result<(), Error> {
        if self.version != STEP_TRACE_VERSION {
            return Err(Error::illegal_argument(format!(
                "generator input version {} is not supported; re-run synthesis for step_trace version {}",
                self.version, STEP_TRACE_VERSION
            )));
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
            version: self.version,
            step_trace: &self.trace.steps,
            dependencies: &self.trace.dependencies,
            parties: &self.trace.parties,
            channels: &self.trace.channels,
            global_context: &self.global_context,
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
    version: u32,
    step_trace: &'a [StepInstance],
    dependencies: &'a [DependencyInstance],
    parties: &'a [PartyInstance],
    channels: &'a [ChannelInstance],
    global_context: &'a [GlobalContextValue],
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
        self.version == other.version
            && self.generator == other.generator
            && self.trace == other.trace
            && self.global_context == other.global_context
            && self.params == other.params
    }
}

impl Eq for GeneratorInput {}

impl Hash for GeneratorInput {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.version.hash(state);
        self.generator.hash(state);
        self.trace.hash(state);
        self.global_context.hash(state);
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
        let version = parsed.version.ok_or_else(|| {
            Error::illegal_argument(format!(
                "generator seed {} is legacy params-only input; re-run synthesis for step_trace version {STEP_TRACE_VERSION}",
                path.display()
            ))
        })?;
        if version != STEP_TRACE_VERSION {
            return Err(Error::illegal_argument(format!(
                "generator seed {} uses input version {version}; re-run synthesis for step_trace version {STEP_TRACE_VERSION}",
                path.display()
            )));
        }
        Ok(GeneratorInput {
            version,
            generator,
            trace: parsed.trace,
            global_context: parsed.global_context,
            params: parsed.params,
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
    fn json_seed_roundtrips_and_resolves_a_relative_script() {
        let dir = std::env::temp_dir().join(format!("mcn-input-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("seeds")).unwrap();
        let script = dir.join("gen.py");
        std::fs::write(&script, "def generate(ctx, params):\n    pass\n").unwrap();
        let seed = dir.join("seeds").join("one.json");
        std::fs::write(
            &seed,
            r#"{"version":3,"generator":"../gen.py","params":{"user":"ubuntu","tries":2,"ok":true}}"#,
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
    fn step_trace_and_global_context_roundtrip_and_legacy_is_rejected() {
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
            fields: BTreeMap::from([("name".into(), ParamValue::Str("x".into()))]),
            omitted_fields: vec!["optional".into()],
            inputs: BTreeMap::from([("token".into(), "hello_1.field".into())]),
            broken_dependencies: vec![],
        };
        let global = GlobalContextValue {
            name: "username".into(),
            kind: "string".into(),
            value: Some(ParamValue::Str("ubuntu".into())),
            source: "constant".into(),
            seed: None,
        };
        let mut input = GeneratorInput::with_trace(
            std::fs::canonicalize(&script).unwrap(),
            vec![instance],
            vec![global],
        );
        input.trace.parties = vec![
            PartyInstance {
                name: "client".into(),
                r#type: "client".into(),
            },
            PartyInstance {
                name: "server".into(),
                r#type: "server".into(),
            },
        ];
        input.trace.channels = vec![ChannelInstance {
            name: "control".into(),
            r#type: "control".into(),
            endpoints: vec!["client".into(), "server".into()],
        }];

        input.trace.validate().unwrap();
        let path = dir.join("input.json");
        input.to_file(&path).unwrap();
        let again = GeneratorInput::from_file(&path).unwrap();
        assert_eq!(again, input);
        assert_eq!(again.trace.steps[0].name, "hello_1");
        let legacy = dir.join("legacy.json");
        std::fs::write(&legacy, r#"{"generator":"gen.py","params":{}}"#).unwrap();
        let error = GeneratorInput::from_file(&legacy).unwrap_err();
        assert!(format!("{error:?}").contains("legacy"), "{error:?}");
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
        std::fs::write(
            &seed,
            format!("{{\"version\":3,\"generator\":\"{}\"}}", script.display()),
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
