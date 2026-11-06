//! Campaign configuration. Paths in the TOML file are relative to that file.

use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
};

use libafl::Error;
use serde::Deserialize;

use crate::protocol::PartyRole;

fn default_host() -> String {
    "127.0.0.1".to_string()
}

fn default_iterations() -> u64 {
    10
}

fn default_map_size() -> usize {
    65_536
}

fn default_config_output() -> String {
    "fftp.conf".to_string()
}

fn default_args() -> Vec<String> {
    vec!["@CONFIG@".to_string()]
}

fn default_cert() -> String {
    "certs/my.crt".to_string()
}

fn default_key() -> String {
    "certs/my.pem".to_string()
}

fn default_file_limit() -> u64 {
    67_108_864
}

fn default_drain_quiet_ms() -> u64 {
    5
}

fn default_drain_timeout_ms() -> u64 {
    100
}

/// How long each phase of one case may run.
#[derive(Clone, Debug, Deserialize)]
pub struct Timeouts {
    /// Budget for the first TCP connect, in milliseconds.
    pub connect_ms: u64,
    /// Budget for one blocking socket read or write, in milliseconds.
    pub io_ms: u64,
    /// Budget for the whole case, in milliseconds.
    pub case_ms: u64,
    /// How long to wait for the target to exit after the generator closes.
    pub exit_grace_ms: u64,
}

impl Default for Timeouts {
    fn default() -> Self {
        Self {
            connect_ms: 2_000,
            io_ms: 1_000,
            case_ms: 10_000,
            exit_grace_ms: 5_000,
        }
    }
}

/// One forkserver target. LightFTP keeps the historical defaults.
#[derive(Clone, Debug, Deserialize)]
pub struct TargetConfig {
    /// PCGUARD forkserver ELF.
    pub binary: PathBuf,
    /// Optional SUT source directory. Coverage analysis reads it. Synthesis prompts do not.
    #[serde(default)]
    pub source_dir: Option<PathBuf>,
    /// Directory restored from `fixture` before every case.
    pub workdir: PathBuf,
    /// Snapshot copied over `workdir`.
    pub fixture: PathBuf,
    /// Config template. Placeholders are replaced when it is written.
    pub config_template: PathBuf,
    /// File name written under `workdir`. LightFTP uses `fftp.conf`.
    #[serde(default = "default_config_output")]
    pub config_output: String,
    /// Arguments after the program. `@CONFIG@` is the rendered config path.
    #[serde(default = "default_args")]
    pub args: Vec<String>,
    /// Certificate path relative to `fixture`.
    #[serde(default = "default_cert")]
    pub cert: String,
    /// Private key path relative to `fixture`.
    #[serde(default = "default_key")]
    pub key: String,
    /// CA path relative to `fixture`. Omitted keeps the LightFTP CA lookup.
    #[serde(default)]
    pub ca: Option<String>,
    #[serde(default = "default_host")]
    pub host: String,
    /// Control port. `0` picks a free port once for the campaign.
    pub port: u16,
    #[serde(default)]
    pub extra_env: Vec<(String, String)>,
    /// Shared PCGUARD map capacity in bytes. The handshake rejects a smaller map.
    #[serde(default = "default_map_size")]
    pub map_size: usize,
    #[serde(default)]
    pub debug_child: bool,
}

/// Values substituted into arguments, environment values, and the config template.
#[derive(Clone, Debug)]
pub struct TargetPlaceholders {
    /// Campaign control port.
    pub port: u16,
    /// Directory the target uses as its root.
    pub root: PathBuf,
    /// Rendered config file.
    pub config: PathBuf,
    /// Certificate file.
    pub cert: PathBuf,
    /// Private key file.
    pub key: PathBuf,
    /// CA bundle.
    pub ca: PathBuf,
}

impl TargetPlaceholders {
    /// Replace `@PORT@`, `@ROOT@`, `@CONFIG@`, `@CERT@`, `@KEY@`, and `@CA@`.
    pub fn apply(&self, text: &str) -> String {
        text.replace("@PORT@", &self.port.to_string())
            .replace("@ROOT@", &self.root.display().to_string())
            .replace("@CONFIG@", &self.config.display().to_string())
            .replace("@CERT@", &self.cert.display().to_string())
            .replace("@KEY@", &self.key.display().to_string())
            .replace("@CA@", &self.ca.display().to_string())
    }
}

/// Corpus, seeds, and the Python interpreter.
#[derive(Clone, Debug, Deserialize)]
pub struct CampaignPaths {
    pub seeds: PathBuf,
    pub corpus: PathBuf,
    pub crashes: PathBuf,
    pub python: PathBuf,
    pub pythonpath: PathBuf,
    /// CPU pin for the forkserver. Omitted means the process is not pinned.
    /// Coverage does not depend on this.
    #[serde(default)]
    pub core: Option<usize>,
    #[serde(default = "default_iterations")]
    pub iterations: u64,
    /// Stop the campaign after this many wall-clock seconds. Takes precedence over iterations.
    #[serde(default)]
    pub timeout_secs: Option<u64>,
    /// Consecutive ordinary executions without a newly occupied bitmap slot.
    #[serde(default = "default_bitmap_stagnation_limit")]
    pub bitmap_stagnation_limit: u64,
}

fn default_bitmap_stagnation_limit() -> u64 {
    crate::engine::stagnation::DEFAULT_BITMAP_STAGNATION_LIMIT
}

/// Opt-in loopback capture. Omitted means capture stays off.
#[derive(Clone, Debug, Deserialize)]
pub struct CaptureConfig {
    /// When false, the executor skips the pcap handle, capture thread, and preflight.
    #[serde(default)]
    pub enabled: bool,
    /// Maximum bytes kept from one case, including the PCAP global header.
    #[serde(default = "default_file_limit")]
    pub file_limit: u64,
    /// Quiet period after the last packet before End completes, in milliseconds.
    #[serde(default = "default_drain_quiet_ms")]
    pub drain_quiet_ms: u64,
    /// Maximum End drain, in milliseconds.
    #[serde(default = "default_drain_timeout_ms")]
    pub drain_timeout_ms: u64,
}

impl Default for CaptureConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            file_limit: default_file_limit(),
            drain_quiet_ms: default_drain_quiet_ms(),
            drain_timeout_ms: default_drain_timeout_ms(),
        }
    }
}

fn default_llvm_profdata() -> String {
    "llvm-profdata-18".to_string()
}

fn default_llvm_cov() -> String {
    "llvm-cov-18".to_string()
}

fn default_coverage_output() -> PathBuf {
    PathBuf::from("source-coverage")
}

/// LLVM source-coverage replay and optional coverage analysis.
///
/// Omitted or `enabled = false` leaves PCGUARD fuzz unchanged.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CoverageConfig {
    /// Replay saved inputs through a forkserver-only coverage binary.
    #[serde(default)]
    pub enabled: bool,
    /// Coverage-built SUT. Relative paths are resolved from the campaign file.
    #[serde(default)]
    pub binary: PathBuf,
    /// Directory that receives snapshots, checkpoints, and analysis output.
    #[serde(default = "default_coverage_output")]
    pub output: PathBuf,
    /// `llvm-profdata` executable. LLVM 18 on this machine.
    #[serde(default = "default_llvm_profdata")]
    pub llvm_profdata: String,
    /// `llvm-cov` executable.
    #[serde(default = "default_llvm_cov")]
    pub llvm_cov: String,
    /// Run the coverage-analysis agent. Requires [`Self::enabled`].
    #[serde(default)]
    pub analysis_enabled: bool,
    /// Profile name under `[agents.profiles]`.
    #[serde(default)]
    pub analysis_profile: String,
    #[serde(default = "default_init_timeout")]
    pub analysis_init_timeout_secs: u64,
    #[serde(default = "default_task_timeout")]
    pub analysis_task_timeout_secs: u64,
    #[serde(default)]
    pub callgraph_dot: PathBuf,
    #[serde(default)]
    pub cfg_dir: PathBuf,
    #[serde(default)]
    pub build_manifest: PathBuf,
    /// Runtime-only CLI override. Full JSON export is off unless requested on the command line.
    #[serde(skip)]
    pub export_json: bool,
}

/// Previous name of [`CoverageConfig`].
pub type SourceCoverageConfig = CoverageConfig;

impl Default for CoverageConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            binary: PathBuf::new(),
            output: default_coverage_output(),
            llvm_profdata: default_llvm_profdata(),
            llvm_cov: default_llvm_cov(),
            analysis_enabled: false,
            analysis_profile: String::new(),
            analysis_init_timeout_secs: default_init_timeout(),
            analysis_task_timeout_secs: default_task_timeout(),
            callgraph_dot: PathBuf::new(),
            cfg_dir: PathBuf::new(),
            build_manifest: PathBuf::new(),
            export_json: false,
        }
    }
}

/// Which coding-agent executable a profile launches.
#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum AgentProvider {
    Codex,
    Claude,
    Pi,
}

/// One named agent launch profile. The generation task stores only the profile name.
#[derive(Clone, Debug, Deserialize)]
pub struct AgentProfile {
    pub provider: AgentProvider,
    pub executable: PathBuf,
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub effort: Option<String>,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub env: Vec<(String, String)>,
}

/// Named profiles. Missing from the campaign means no agents are configured.
#[derive(Clone, Debug, Deserialize)]
pub struct AgentCatalog {
    /// Private HC executable shared by synthesis and coverage analysis.
    #[serde(default = "default_hc")]
    pub hc_executable: PathBuf,
    #[serde(default)]
    pub profiles: std::collections::BTreeMap<String, AgentProfile>,
}

impl Default for AgentCatalog {
    fn default() -> Self {
        Self {
            hc_executable: default_hc(),
            profiles: std::collections::BTreeMap::new(),
        }
    }
}

fn default_init_timeout() -> u64 {
    300
}

fn default_task_timeout() -> u64 {
    600
}

fn default_trace_min() -> usize {
    1
}

fn default_trace_max() -> usize {
    8
}

fn default_sample_attempts() -> u32 {
    64
}

/// Optional protocol-driven generator synthesis. Omitted or disabled leaves the campaign unchanged.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProtocolGenerationConfig {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default)]
    pub model: PathBuf,
    #[serde(default)]
    pub profile: String,
    #[serde(default = "default_synthesis_output")]
    pub output: PathBuf,
    /// Rejected. Roles now come from `[parties]`.
    #[serde(
        default,
        rename = "bindings",
        deserialize_with = "reject_legacy_bindings"
    )]
    pub(crate) _legacy_bindings: (),
    #[serde(default = "default_init_timeout")]
    pub init_timeout_secs: u64,
    #[serde(default = "default_task_timeout")]
    pub task_timeout_secs: u64,
    #[serde(default = "default_trace_min")]
    pub trace_len_min: usize,
    #[serde(default = "default_trace_max")]
    pub trace_len_max: usize,
    #[serde(default = "default_sample_attempts")]
    pub sample_attempts: u32,
    /// Rejected. The executable now lives on `[agents].hc_executable`.
    #[serde(
        default,
        rename = "hc_executable",
        deserialize_with = "reject_legacy_hc"
    )]
    pub(crate) _legacy_hc: (),
}

fn default_hc() -> PathBuf {
    PathBuf::from("hc")
}

fn reject_legacy_hc<'de, D>(deserializer: D) -> Result<(), D::Error>
where
    D: serde::Deserializer<'de>,
{
    serde::de::IgnoredAny::deserialize(deserializer)?;
    Err(serde::de::Error::custom(
        "protocol_generation.hc_executable has moved to [agents].hc_executable",
    ))
}

fn reject_legacy_source_coverage<'de, D>(deserializer: D) -> Result<(), D::Error>
where
    D: serde::Deserializer<'de>,
{
    serde::de::IgnoredAny::deserialize(deserializer)?;
    Err(serde::de::Error::custom(
        "[source_coverage] has been renamed to [coverage]; move enabled, binary, output, llvm_profdata, and llvm_cov, and set analysis_enabled separately",
    ))
}

fn reject_legacy_bindings<'de, D>(deserializer: D) -> Result<(), D::Error>
where
    D: serde::Deserializer<'de>,
{
    serde::de::IgnoredAny::deserialize(deserializer)?;
    Err(serde::de::Error::custom(
        "protocol_generation.bindings has been removed; set [parties].sut and [parties].generator to a non-empty party list or \"*\"",
    ))
}

/// One side of `[parties]`: a non-empty name list, or `"*"` for the complement.
#[derive(Clone, Debug, PartialEq, Eq, Deserialize)]
#[serde(untagged)]
pub enum PartySelector {
    /// Explicit protocol `party.name` values.
    Names(Vec<String>),
    /// The string `"*"`, or any other string rejected by [`PartiesConfig::check_shape`].
    Wildcard(String),
}

#[derive(Clone, Debug)]
enum NormalizedParties {
    All,
    Names(BTreeSet<String>),
}

/// Campaign assignment of protocol parties to the SUT and the generator environment.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PartiesConfig {
    pub sut: PartySelector,
    pub generator: PartySelector,
}

impl PartiesConfig {
    /// Reject selectors that are not a non-empty name list or `"*"`.
    pub fn check_shape(&self) -> Result<(), Error> {
        let sut = normalize_selector(&self.sut, "sut")?;
        let generator = normalize_selector(&self.generator, "generator")?;
        if matches!(
            (&sut, &generator),
            (NormalizedParties::All, NormalizedParties::All)
        ) {
            return Err(Error::illegal_argument(
                "parties.sut and parties.generator are both \"*\"; one side must name its parties"
                    .to_string(),
            ));
        }
        if let (NormalizedParties::Names(sut), NormalizedParties::Names(generator)) =
            (&sut, &generator)
        {
            if let Some(name) = sut.intersection(generator).next() {
                return Err(Error::illegal_argument(format!(
                    "party \"{name}\" is assigned to both parties.sut and parties.generator"
                )));
            }
        }
        Ok(())
    }

    /// Expand `"*"` against the model and map every assigned party to a role.
    ///
    /// Parties in neither set stay out of the map and are treated as unavailable.
    pub fn resolve(&self, known: &BTreeSet<String>) -> Result<BTreeMap<String, PartyRole>, Error> {
        self.check_shape()?;
        let sut = normalize_selector(&self.sut, "sut")?;
        let generator = normalize_selector(&self.generator, "generator")?;
        let sut_names = materialize(&sut, &generator, known, "sut")?;
        let generator_names = materialize(&generator, &sut, known, "generator")?;
        if sut_names.is_empty() {
            return Err(Error::illegal_argument(
                "parties.sut resolves to an empty party set".to_string(),
            ));
        }
        if generator_names.is_empty() {
            return Err(Error::illegal_argument(
                "parties.generator resolves to an empty party set".to_string(),
            ));
        }
        let mut roles = BTreeMap::new();
        for name in sut_names {
            roles.insert(name, PartyRole::Sut);
        }
        for name in generator_names {
            roles.insert(name, PartyRole::Generator);
        }
        Ok(roles)
    }
}

fn normalize_selector(selector: &PartySelector, side: &str) -> Result<NormalizedParties, Error> {
    match selector {
        PartySelector::Wildcard(text) if text == "*" => Ok(NormalizedParties::All),
        PartySelector::Wildcard(_) => Err(Error::illegal_argument(format!(
            "parties.{side} must be a non-empty list of party names or \"*\""
        ))),
        PartySelector::Names(names) => {
            if names.is_empty() {
                return Err(Error::illegal_argument(format!(
                    "parties.{side} must be a non-empty list of party names or \"*\""
                )));
            }
            let mut unique = BTreeSet::new();
            for name in names {
                if name.is_empty() {
                    return Err(Error::illegal_argument(format!(
                        "parties.{side} contains an empty party name"
                    )));
                }
                if !unique.insert(name.clone()) {
                    return Err(Error::illegal_argument(format!(
                        "parties.{side} lists \"{name}\" more than once"
                    )));
                }
            }
            Ok(NormalizedParties::Names(unique))
        }
    }
}

fn materialize(
    side: &NormalizedParties,
    other: &NormalizedParties,
    known: &BTreeSet<String>,
    label: &str,
) -> Result<BTreeSet<String>, Error> {
    match side {
        NormalizedParties::Names(names) => {
            for name in names {
                if !known.contains(name) {
                    return Err(Error::illegal_argument(format!(
                        "unknown party \"{name}\" in parties.{label}"
                    )));
                }
            }
            Ok(names.clone())
        }
        NormalizedParties::All => {
            let explicit = match other {
                NormalizedParties::Names(names) => names,
                NormalizedParties::All => {
                    return Err(Error::illegal_argument(
                        "parties.sut and parties.generator are both \"*\"; one side must name its parties"
                            .to_string(),
                    ));
                }
            };
            Ok(known.difference(explicit).cloned().collect())
        }
    }
}

fn default_synthesis_output() -> PathBuf {
    PathBuf::from("synthesis")
}

impl Default for ProtocolGenerationConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            model: PathBuf::new(),
            profile: String::new(),
            output: PathBuf::from("synthesis"),
            _legacy_bindings: (),
            init_timeout_secs: default_init_timeout(),
            task_timeout_secs: default_task_timeout(),
            trace_len_min: default_trace_min(),
            trace_len_max: default_trace_max(),
            sample_attempts: default_sample_attempts(),
            _legacy_hc: (),
        }
    }
}

/// One campaign file.
#[derive(Clone, Debug, Deserialize)]
pub struct Campaign {
    pub target: TargetConfig,
    pub timeouts: Timeouts,
    pub campaign: CampaignPaths,
    /// PCAP capture. Missing from the file keeps the historical no-capture path.
    #[serde(default)]
    pub capture: CaptureConfig,
    /// LLVM source coverage and optional analysis. Missing from the file keeps it off.
    #[serde(default)]
    pub coverage: CoverageConfig,
    /// Rejected. The table was renamed to `[coverage]`.
    #[serde(
        default,
        rename = "source_coverage",
        deserialize_with = "reject_legacy_source_coverage"
    )]
    pub(crate) _legacy_source_coverage: (),
    /// Protocol generator synthesis. Missing from the file keeps it off.
    #[serde(default)]
    pub protocol_generation: ProtocolGenerationConfig,
    /// Named agent profiles referenced by protocol generation.
    #[serde(default)]
    pub agents: AgentCatalog,
    /// SUT and generator party sets. Required only when protocol generation is enabled.
    #[serde(default)]
    pub parties: Option<PartiesConfig>,
    /// Directory that contained the TOML file. Not read from the file.
    #[serde(skip)]
    pub base: PathBuf,
}

impl Campaign {
    /// Load a TOML campaign and resolve its paths.
    pub fn load(path: &Path) -> Result<Self, Error> {
        let text = fs::read_to_string(path)
            .map_err(|error| Error::unknown(format!("reading {}: {error}", path.display())))?;
        let mut campaign: Campaign = toml::from_str(&text)
            .map_err(|error| Error::serialize(format!("parsing {}: {error}", path.display())))?;
        reject_reserved_target_env(&campaign.target.extra_env)?;
        if campaign.campaign.bitmap_stagnation_limit == 0 {
            return Err(Error::illegal_argument(
                "campaign.bitmap_stagnation_limit must be greater than 0".to_string(),
            ));
        }
        if campaign.campaign.timeout_secs == Some(0) {
            return Err(Error::illegal_argument(
                "campaign.timeout_secs must be greater than 0".to_string(),
            ));
        }
        let base = path
            .parent()
            .unwrap_or_else(|| Path::new("."))
            .to_path_buf();
        campaign.target.binary = resolve(&base, &campaign.target.binary);
        campaign.target.source_dir = campaign
            .target
            .source_dir
            .as_ref()
            .map(|path| resolve(&base, path));
        campaign.target.workdir = resolve(&base, &campaign.target.workdir);
        campaign.target.fixture = resolve(&base, &campaign.target.fixture);
        campaign.target.config_template = resolve(&base, &campaign.target.config_template);
        campaign.campaign.seeds = resolve(&base, &campaign.campaign.seeds);
        campaign.campaign.corpus = resolve(&base, &campaign.campaign.corpus);
        campaign.campaign.crashes = resolve(&base, &campaign.campaign.crashes);
        campaign.campaign.python = resolve(&base, &campaign.campaign.python);
        campaign.campaign.pythonpath = resolve(&base, &campaign.campaign.pythonpath);
        if !campaign.coverage.binary.as_os_str().is_empty() {
            campaign.coverage.binary = resolve(&base, &campaign.coverage.binary);
        }
        campaign.coverage.output = resolve(&base, &campaign.coverage.output);
        if !campaign.coverage.callgraph_dot.as_os_str().is_empty() {
            campaign.coverage.callgraph_dot = resolve(&base, &campaign.coverage.callgraph_dot);
        }
        if !campaign.coverage.cfg_dir.as_os_str().is_empty() {
            campaign.coverage.cfg_dir = resolve(&base, &campaign.coverage.cfg_dir);
        }
        if !campaign.coverage.build_manifest.as_os_str().is_empty() {
            campaign.coverage.build_manifest = resolve(&base, &campaign.coverage.build_manifest);
        }
        if !campaign.agents.hc_executable.as_os_str().is_empty() {
            campaign.agents.hc_executable = resolve(&base, &campaign.agents.hc_executable);
        }
        if campaign.coverage.analysis_enabled && !campaign.coverage.enabled {
            return Err(Error::illegal_argument(
                "coverage.analysis_enabled requires coverage.enabled".to_string(),
            ));
        }
        if campaign.coverage.analysis_enabled {
            if campaign.coverage.analysis_profile.is_empty()
                || !campaign
                    .agents
                    .profiles
                    .contains_key(&campaign.coverage.analysis_profile)
            {
                return Err(Error::illegal_argument(
                    "coverage.analysis_profile must name a profile in [agents.profiles]"
                        .to_string(),
                ));
            }
            for (label, path) in [
                ("callgraph_dot", &campaign.coverage.callgraph_dot),
                ("cfg_dir", &campaign.coverage.cfg_dir),
                ("build_manifest", &campaign.coverage.build_manifest),
            ] {
                if path.as_os_str().is_empty() {
                    return Err(Error::illegal_argument(format!(
                        "coverage.{label} is required when analysis is enabled"
                    )));
                }
            }
            if campaign.parties.is_none() {
                return Err(Error::illegal_argument(
                    "coverage analysis requires [parties].sut and [parties].generator".to_string(),
                ));
            }
            if campaign
                .target
                .source_dir
                .as_ref()
                .is_none_or(|path| path.as_os_str().is_empty())
            {
                return Err(Error::illegal_argument(
                    "coverage analysis requires target.source_dir".to_string(),
                ));
            }
        }
        if !campaign.protocol_generation.model.as_os_str().is_empty() {
            campaign.protocol_generation.model =
                resolve(&base, &campaign.protocol_generation.model);
        }
        campaign.protocol_generation.output = resolve(&base, &campaign.protocol_generation.output);
        if campaign.protocol_generation.enabled && campaign.parties.is_none() {
            return Err(Error::illegal_argument(
                "protocol generation requires [parties].sut and [parties].generator".to_string(),
            ));
        }
        if let Some(parties) = &campaign.parties {
            parties.check_shape()?;
            let model_path = &campaign.protocol_generation.model;
            if model_path.as_os_str().is_empty() {
                return Err(Error::illegal_argument(
                    "protocol_generation.model is required to resolve [parties]".to_string(),
                ));
            }
            let model = crate::protocol::load_model(model_path)?;
            parties.resolve(&model.party_names())?;
        }
        campaign.base = base;
        Ok(campaign)
    }

    /// Generated server configuration path.
    pub fn server_config(&self) -> PathBuf {
        self.target.workdir.join(&self.target.config_output)
    }

    /// Certificate copied with the fixture.
    pub fn cert_path(&self) -> PathBuf {
        self.target.fixture.join(&self.target.cert)
    }

    /// Private key copied with the fixture.
    pub fn key_path(&self) -> PathBuf {
        self.target.fixture.join(&self.target.key)
    }

    /// CA bundle. An omitted path keeps the previous LightFTP lookup.
    pub fn ca_path(&self) -> PathBuf {
        if let Some(ca) = &self.target.ca {
            return self.target.fixture.join(ca);
        }
        let system = PathBuf::from("/etc/ssl/certs/ca-certificates.crt");
        if system.is_file() {
            system
        } else {
            self.target.fixture.join("certs").join("my.pem")
        }
    }

    /// Placeholder values for this campaign's fixed port.
    pub fn placeholders(&self, port: u16) -> TargetPlaceholders {
        TargetPlaceholders {
            port,
            root: self.target.workdir.clone(),
            config: self.server_config(),
            cert: self.cert_path(),
            key: self.key_path(),
            ca: self.ca_path(),
        }
    }
}

/// Keys the fuzzer owns. `extra_env` must not set them.
///
/// `AFL_LLVM_ONLY_FSRV` and `AFL_GCC_ONLY_FSRV` disable PCGUARD counters when
/// they are present, including when the value is `0`. `__AFL_SHM_ID`,
/// `__AFL_SHM_ID_SIZE`, and `AFL_MAP_SIZE` select the shared coverage map.
pub fn reject_reserved_target_env(extra_env: &[(String, String)]) -> Result<(), Error> {
    for (key, value) in extra_env {
        if key == "AFL_LLVM_ONLY_FSRV" || key == "AFL_GCC_ONLY_FSRV" {
            return Err(Error::illegal_argument(format!(
                "target.extra_env must not set {key} (got {value:?}). \
                 The variable disables PCGUARD counters whenever it is present, including when it is 0 or 1. \
                 Remove the key."
            )));
        }
        if key == "__AFL_SHM_ID" || key == "__AFL_SHM_ID_SIZE" || key == "AFL_MAP_SIZE" {
            return Err(Error::illegal_argument(format!(
                "target.extra_env must not set {key} (got {value:?}). \
                 The fuzzer allocates the shared coverage map and sets this itself. \
                 Size the allocation with target.map_size; a campaign value would replace the real map."
            )));
        }
    }
    Ok(())
}

fn resolve(base: &Path, path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        base.join(path)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample(extra: &str, core_line: &str) -> String {
        format!(
            r#"
[target]
binary = "b"
workdir = "w"
fixture = "f"
config_template = "c"
port = 0
{extra}
[timeouts]
connect_ms = 1
io_ms = 1
case_ms = 1
exit_grace_ms = 1
[campaign]
seeds = "s"
corpus = "co"
crashes = "cr"
python = "python"
pythonpath = "pp"
{core_line}
"#
        )
    }

    #[test]
    fn omitted_core_does_not_pin() {
        let campaign: Campaign = toml::from_str(&sample("", "")).unwrap();
        assert_eq!(campaign.campaign.core, None);
        let pinned: Campaign = toml::from_str(&sample("", "core = 0")).unwrap();
        assert_eq!(pinned.campaign.core, Some(0));
        assert_eq!(campaign.campaign.bitmap_stagnation_limit, 256);
    }

    #[test]
    fn bitmap_stagnation_limit_resolves_from_campaign_and_rejects_zero() {
        let dir =
            std::env::temp_dir().join(format!("mcn-stagnation-config-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("campaign.toml");
        fs::write(&path, sample("", "bitmap_stagnation_limit = 7")).unwrap();
        assert_eq!(
            Campaign::load(&path)
                .unwrap()
                .campaign
                .bitmap_stagnation_limit,
            7
        );
        fs::write(&path, sample("", "bitmap_stagnation_limit = 0")).unwrap();
        let error = Campaign::load(&path).unwrap_err();
        assert!(format!("{error:?}").contains("bitmap_stagnation_limit must be greater than 0"));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn removed_stage_intervals_are_configuration_errors() {
        let legacy = format!("{}\n[source_coverage]\nenabled = false\n", sample("", ""));
        let error = toml::from_str::<Campaign>(&legacy).unwrap_err();
        assert!(error.to_string().contains("[source_coverage]"), "{error}");
        for section in ["coverage", "protocol_generation"] {
            let text = format!("{}\n[{section}]\ninterval = 1\n", sample("", ""));
            let error = toml::from_str::<Campaign>(&text).unwrap_err();
            assert!(
                error.to_string().contains("unknown field `interval`"),
                "{error}"
            );
        }
    }

    #[test]
    fn omitted_launch_fields_keep_lightftp_defaults() {
        let campaign: Campaign = toml::from_str(&sample("", "")).unwrap();
        assert_eq!(campaign.target.config_output, "fftp.conf");
        assert_eq!(campaign.target.args, vec!["@CONFIG@".to_string()]);
        assert_eq!(campaign.target.cert, "certs/my.crt");
        assert_eq!(campaign.target.key, "certs/my.pem");
        assert_eq!(campaign.target.ca, None);
        assert!(!campaign.capture.enabled);
        assert_eq!(campaign.capture.file_limit, 67_108_864);
        assert_eq!(campaign.capture.drain_quiet_ms, 5);
        assert_eq!(campaign.capture.drain_timeout_ms, 100);
        let rendered = campaign.placeholders(2200).apply(&campaign.target.args[0]);
        assert!(rendered.ends_with("fftp.conf"), "{rendered}");
        let conf = campaign
            .placeholders(2200)
            .apply("port=@PORT@ cert=@CERT@ key=@KEY@ ca=@CA@ root=@ROOT@ cfg=@CONFIG@");
        assert!(conf.contains("port=2200"), "{conf}");
        assert!(conf.contains("certs/my.crt"), "{conf}");
        assert!(conf.contains("certs/my.pem"), "{conf}");
        assert!(conf.contains("fftp.conf"), "{conf}");
    }

    #[test]
    fn reserved_env_values_are_rejected() {
        for (key, value) in [
            ("AFL_LLVM_ONLY_FSRV", "0"),
            ("AFL_LLVM_ONLY_FSRV", "1"),
            ("AFL_GCC_ONLY_FSRV", "1"),
            ("__AFL_SHM_ID", "99"),
            ("__AFL_SHM_ID_SIZE", "65536"),
            ("AFL_MAP_SIZE", "65536"),
        ] {
            let text = sample(&format!("extra_env = [[\"{key}\", \"{value}\"]]\n"), "");
            let error = reject_reserved_target_env(
                &toml::from_str::<Campaign>(&text).unwrap().target.extra_env,
            )
            .expect_err(key);
            let message = format!("{error:?}");
            assert!(message.contains(key), "{message}");
        }
    }

    #[test]
    fn load_rejects_forkserver_only_set_to_zero() {
        let dir = std::env::temp_dir().join(format!("mcn-cfg-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("campaign.toml");
        fs::write(
            &path,
            sample("extra_env = [[\"AFL_LLVM_ONLY_FSRV\", \"0\"]]\n", ""),
        )
        .unwrap();
        let error = Campaign::load(&path).expect_err("reserved env");
        let message = format!("{error:?}");
        assert!(message.contains("AFL_LLVM_ONLY_FSRV"), "{message}");
        assert!(message.contains('0'), "{message}");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn openssl_campaign_points_openssl_conf_at_rendered_config() {
        let campaign =
            Campaign::load(Path::new("targets/openssl/campaign.toml")).expect("openssl campaign");
        let placeholders = campaign.placeholders(4433);
        let value = campaign
            .target
            .extra_env
            .iter()
            .find(|(key, _)| key == "OPENSSL_CONF")
            .map(|(_, value)| placeholders.apply(value));
        let expected = placeholders.config.display().to_string();
        assert_eq!(value.as_deref(), Some(expected.as_str()));
        assert!(expected.ends_with("openssl.cnf"));
        assert!(campaign.target.args.iter().any(|arg| arg == "-naccept"));
    }

    #[test]
    fn ordinary_extra_env_is_kept() {
        let text = sample("extra_env = [[\"MCN_TEST_CRASH\", \"client\"]]\n", "");
        let campaign: Campaign = toml::from_str(&text).unwrap();
        assert!(reject_reserved_target_env(&campaign.target.extra_env).is_ok());
        assert_eq!(
            campaign.target.extra_env,
            vec![("MCN_TEST_CRASH".to_string(), "client".to_string())]
        );
    }

    #[test]
    fn omitted_protocol_generation_stays_off_and_profiles_keep_env_pairs() {
        let plain: Campaign = toml::from_str(&sample("", "")).unwrap();
        assert!(!plain.protocol_generation.enabled);
        assert!(plain.protocol_generation.model.as_os_str().is_empty());
        assert_eq!(plain.protocol_generation.init_timeout_secs, 300);
        assert_eq!(plain.coverage.analysis_init_timeout_secs, 300);
        assert_eq!(plain.protocol_generation.task_timeout_secs, 600);
        assert!(plain.agents.profiles.is_empty());
        let text = format!(
            "{}\n[protocol_generation]\nenabled = false\nmodel = \"protocol.json\"\nprofile = \"codex\"\n[agents.profiles.codex]\nprovider = \"codex\"\nexecutable = \"/bin/true\"\nmodel = \"gpt\"\neffort = \"low\"\nargs = [\"--sandbox\", \"workspace-write\"]\nenv = [[\"NO_COLOR\", \"1\"]]\n",
            sample("", "")
        );
        let campaign: Campaign = toml::from_str(&text).unwrap();
        assert!(!campaign.protocol_generation.enabled);
        assert_eq!(campaign.protocol_generation.profile, "codex");
        let profile = campaign.agents.profiles.get("codex").unwrap();
        assert_eq!(profile.env, vec![("NO_COLOR".to_string(), "1".to_string())]);
        let line = crate::agents::command_line(profile, Path::new("/tmp/synthesis")).unwrap();
        assert_eq!(
            line.argv,
            vec![
                "/bin/true".to_string(),
                "--no-daemon".to_string(),
                "--model".to_string(),
                "gpt".to_string(),
                "--effort".to_string(),
                "low".to_string(),
                "--sandbox".to_string(),
                "workspace-write".to_string(),
            ]
        );
        assert!(!line
            .argv
            .iter()
            .any(|arg| arg == "--print" || arg == "exec"));
    }

    fn party_model(dir: &Path) -> PathBuf {
        let path = dir.join("protocol.json");
        std::fs::write(
            &path,
            br#"{"meta":{"schema_version":"7.0.0","protocol":"p","complete":false,"documents":[{"doc_id":"d","rfc":"RFC 1","input_sha256":"a","document_sha256":"b","path":"doc.json"}]},"parties":[{"name":"alpha","description":"a"},{"name":"beta","description":"b"},{"name":"gamma","description":"c"}],"messages":[{"name":"m","associated_fields":[]}],"channels":[{"name":"c","description":"c","transport":"TCP","connection":"connection_oriented","endpoint_party_names":["alpha","beta"],"initiator_party_name":"alpha","acceptor_party_name":"beta","endpoint_sources":[]}],"steps":[{"id":"1","name":"s","message_name":"m","channel_name":"c","sender_name":"alpha","receiver_name":"beta","condition":"go"}],"step_dependencies":[],"unresolved":[]}"#,
        )
        .unwrap();
        path
    }

    fn party_campaign(dir: &Path, parties: &str, enabled: bool) -> PathBuf {
        let path = dir.join("campaign.toml");
        let model = if enabled { "protocol.json" } else { "" };
        let enabled = if enabled { "true" } else { "false" };
        std::fs::write(
            &path,
            format!(
                "{}\n[protocol_generation]\nenabled = {enabled}\nmodel = \"{model}\"\nprofile = \"codex\"\n{parties}\n",
                sample("", "")
            ),
        )
        .unwrap();
        path
    }

    fn assert_party_error(toml_parties: &str, needle: &str) {
        let dir = std::env::temp_dir().join(format!(
            "mcn-parties-{}-{}",
            std::process::id(),
            needle.len()
        ));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        party_model(&dir);
        let path = party_campaign(&dir, toml_parties, true);
        let error = Campaign::load(&path).expect_err(needle);
        let message = format!("{error:?}");
        assert!(message.contains(needle), "{message}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn party_lists_and_wildcard_expand_before_cases() {
        let dir = std::env::temp_dir().join(format!("mcn-parties-ok-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let model_path = party_model(&dir);
        let explicit = party_campaign(
            &dir,
            "[parties]\nsut = [\"alpha\"]\ngenerator = [\"beta\"]\n",
            true,
        );
        let campaign = Campaign::load(&explicit).unwrap();
        let known = crate::protocol::load_model(&model_path)
            .unwrap()
            .party_names();
        let roles = campaign.parties.unwrap().resolve(&known).unwrap();
        assert_eq!(roles.get("alpha"), Some(&crate::protocol::PartyRole::Sut));
        assert_eq!(
            roles.get("beta"),
            Some(&crate::protocol::PartyRole::Generator)
        );
        assert!(roles.get("gamma").is_none());

        let wildcard = party_campaign(
            &dir,
            "[parties]\nsut = [\"alpha\"]\ngenerator = \"*\"\n",
            true,
        );
        let campaign = Campaign::load(&wildcard).unwrap();
        let roles = campaign.parties.unwrap().resolve(&known).unwrap();
        assert_eq!(roles.get("alpha"), Some(&crate::protocol::PartyRole::Sut));
        assert_eq!(
            roles.get("beta"),
            Some(&crate::protocol::PartyRole::Generator)
        );
        assert_eq!(
            roles.get("gamma"),
            Some(&crate::protocol::PartyRole::Generator)
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn party_configuration_rejects_conflicts_and_legacy_bindings() {
        assert_party_error(
            "[parties]\nsut = \"*\"\ngenerator = \"*\"\n",
            "one side must name its parties",
        );
        assert_party_error(
            "[parties]\nsut = [\"alpha\"]\ngenerator = [\"alpha\"]\n",
            "both parties.sut and parties.generator",
        );
        assert_party_error(
            "[parties]\nsut = [\"missing\"]\ngenerator = [\"alpha\"]\n",
            "unknown party",
        );
        assert_party_error(
            "[parties]\nsut = [\"alpha\", \"alpha\"]\ngenerator = [\"beta\"]\n",
            "more than once",
        );
        assert_party_error(
            "[parties]\nsut = []\ngenerator = [\"beta\"]\n",
            "non-empty list",
        );
        assert_party_error(
            "[parties]\nsut = [\"alpha\"]\ngenerator = \"all\"\n",
            "parties.generator must be a non-empty list",
        );
        assert_party_error(
            "[parties]\nsut = [\"\"]\ngenerator = [\"beta\"]\n",
            "empty party name",
        );
        assert_party_error(
            "[parties]\nsut = [\"alpha\", \"beta\", \"gamma\"]\ngenerator = \"*\"\n",
            "parties.generator resolves to an empty party set",
        );
        assert_party_error("", "requires [parties]");
        let dir = std::env::temp_dir().join(format!("mcn-parties-legacy-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("campaign.toml");
        std::fs::write(
            &path,
            format!(
                "{}\n[protocol_generation]\nenabled = false\nbindings = [[\"alpha\", \"sut\"]]\n",
                sample("", "")
            ),
        )
        .unwrap();
        let error = Campaign::load(&path).expect_err("legacy bindings");
        let message = format!("{error:?}");
        assert!(
            message.contains("protocol_generation.bindings has been removed"),
            "{message}"
        );
        assert!(message.contains("[parties]"), "{message}");
        let omitted = party_campaign(&dir, "", false);
        let campaign = Campaign::load(&omitted).unwrap();
        assert!(campaign.parties.is_none());
        assert!(!campaign.protocol_generation.enabled);
        let _ = std::fs::remove_dir_all(&dir);
    }

    fn party_campaign_model(dir: &Path, parties: &str, enabled: bool, model: &str) -> PathBuf {
        let path = dir.join("campaign.toml");
        let enabled = if enabled { "true" } else { "false" };
        std::fs::write(
            &path,
            format!(
                "{}\n[protocol_generation]\nenabled = {enabled}\nmodel = \"{model}\"\nprofile = \"codex\"\n{parties}\n",
                sample("", "")
            ),
        )
        .unwrap();
        path
    }

    #[test]
    fn disabled_parties_still_resolve_before_cases() {
        let dir = std::env::temp_dir().join(format!("mcn-parties-disabled-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        party_model(&dir);
        let unknown = party_campaign_model(
            &dir,
            "[parties]\nsut = [\"missing\"]\ngenerator = [\"alpha\"]\n",
            false,
            "protocol.json",
        );
        let error = Campaign::load(&unknown).expect_err("unknown party while disabled");
        let message = format!("{error:?}");
        assert!(message.contains("unknown party"), "{message}");
        let empty = party_campaign_model(
            &dir,
            "[parties]\nsut = [\"alpha\", \"beta\", \"gamma\"]\ngenerator = \"*\"\n",
            false,
            "protocol.json",
        );
        let error = Campaign::load(&empty).expect_err("empty wildcard while disabled");
        let message = format!("{error:?}");
        assert!(
            message.contains("parties.generator resolves to an empty party set"),
            "{message}"
        );
        let missing = party_campaign_model(
            &dir,
            "[parties]\nsut = [\"alpha\"]\ngenerator = [\"beta\"]\n",
            false,
            "",
        );
        let error = Campaign::load(&missing).expect_err("missing model");
        let message = format!("{error:?}");
        assert!(
            message.contains("protocol_generation.model is required to resolve [parties]"),
            "{message}"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }
}
