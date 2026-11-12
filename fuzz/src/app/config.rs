//! Campaign configuration. Input paths are relative to the TOML file; output paths use the CLI campaign root when provided.

use std::{
    collections::{BTreeMap, BTreeSet},
    env, fs,
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
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

fn default_args() -> Vec<String> {
    vec!["fftp.conf".to_string()]
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
    /// Directory in which the target runs.
    pub workdir: PathBuf,
    /// Optional shell script run before startup and before every testcase.
    #[serde(default)]
    pub cleanup_script: Option<PathBuf>,
    /// Arguments after the program. `@PORT@` and `@ROOT@` are substituted.
    #[serde(default = "default_args")]
    pub args: Vec<String>,
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

/// Values substituted into arguments and environment values.
#[derive(Clone, Debug)]
pub struct TargetPlaceholders {
    /// Campaign control port.
    pub port: u16,
    /// Directory the target uses as its root.
    pub root: PathBuf,
}

impl TargetPlaceholders {
    /// Replace `@PORT@` and `@ROOT@`.
    pub fn apply(&self, text: &str) -> String {
        text.replace("@PORT@", &self.port.to_string())
            .replace("@ROOT@", &self.root.display().to_string())
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
    /// Supervisor-provided MCP endpoint. Absent starts a private service.
    #[serde(default)]
    pub mcp_socket: Option<PathBuf>,
    /// How long to wait for the MCP service socket, in seconds.
    #[serde(default = "default_mcp_ready")]
    pub mcp_ready_timeout_secs: u64,
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
            mcp_socket: None,
            mcp_ready_timeout_secs: default_mcp_ready(),
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

fn default_mcp_ready() -> u64 {
    30
}

/// Optional protocol-driven generator synthesis. Omitted or disabled leaves the campaign unchanged.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GeneratorSynthesisConfig {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default)]
    pub model: PathBuf,
    #[serde(default)]
    pub profile: String,
    #[serde(default = "default_synthesis_output")]
    pub output: PathBuf,
    /// Extra read-only campaign files offered to the synthesizer.
    #[serde(default)]
    pub extra_files: Vec<PathBuf>,
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
    /// Rejected. Traces now come from a proposal.
    #[serde(
        default,
        rename = "trace_len_min",
        deserialize_with = "reject_legacy_trace"
    )]
    pub(crate) _legacy_trace_min: (),
    /// Rejected. Traces now come from a proposal.
    #[serde(
        default,
        rename = "trace_len_max",
        deserialize_with = "reject_legacy_trace"
    )]
    pub(crate) _legacy_trace_max: (),
    /// Rejected. Traces now come from a proposal.
    #[serde(
        default,
        rename = "sample_attempts",
        deserialize_with = "reject_legacy_trace"
    )]
    pub(crate) _legacy_sample_attempts: (),
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

fn reject_legacy_trace<'de, D>(deserializer: D) -> Result<(), D::Error>
where
    D: serde::Deserializer<'de>,
{
    serde::de::IgnoredAny::deserialize(deserializer)?;
    Err(serde::de::Error::custom(
        "generator_synthesis trace selection settings are obsolete; traces come from a proposal",
    ))
}

fn reject_legacy_hc<'de, D>(deserializer: D) -> Result<(), D::Error>
where
    D: serde::Deserializer<'de>,
{
    serde::de::IgnoredAny::deserialize(deserializer)?;
    Err(serde::de::Error::custom(
        "generator_synthesis.hc_executable has moved to [agents].hc_executable",
    ))
}

fn reject_legacy_generator_synthesis_table<'de, D>(deserializer: D) -> Result<(), D::Error>
where
    D: serde::Deserializer<'de>,
{
    serde::de::IgnoredAny::deserialize(deserializer)?;
    Err(serde::de::Error::custom(
        "[protocol_generation] has been renamed to [generator_synthesis]; move enabled, model, profile, output, init_timeout_secs, task_timeout_secs, and extra_files",
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
        "generator_synthesis.bindings has been removed; set [parties].sut and [parties].generator to a non-empty party list or \"*\"",
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

impl Default for GeneratorSynthesisConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            model: PathBuf::new(),
            profile: String::new(),
            output: PathBuf::from("synthesis"),
            extra_files: Vec::new(),
            _legacy_bindings: (),
            init_timeout_secs: default_init_timeout(),
            task_timeout_secs: default_task_timeout(),
            _legacy_trace_min: (),
            _legacy_trace_max: (),
            _legacy_sample_attempts: (),
            _legacy_hc: (),
        }
    }
}

/// Create a unique campaign output root below `parent` (or the current directory).
///
/// The leaf name is `mcn-fuzzer-YYYYMMDD-HHMM`; collisions within the same
/// minute receive `-2`, `-3`, ... suffixes. Timestamps are UTC.
pub fn create_campaign_root(parent: Option<&Path>) -> Result<PathBuf, Error> {
    let parent = match parent {
        Some(path) if path.is_absolute() => path.to_path_buf(),
        Some(path) => env::current_dir()
            .map_err(|error| Error::unknown(format!("current directory: {error}")))?
            .join(path),
        None => env::current_dir()
            .map_err(|error| Error::unknown(format!("current directory: {error}")))?,
    };
    fs::create_dir_all(&parent).map_err(|error| {
        Error::unknown(format!(
            "creating output parent {}: {error}",
            parent.display()
        ))
    })?;
    let (year, month, day, hour, minute) = utc_minute(SystemTime::now());
    let base = format!("mcn-fuzzer-{year:04}{month:02}{day:02}-{hour:02}{minute:02}");
    for suffix in 1u32.. {
        let name = if suffix == 1 {
            base.clone()
        } else {
            format!("{base}-{suffix}")
        };
        let path = parent.join(name);
        match fs::create_dir(&path) {
            Ok(()) => return Ok(path),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => {
                return Err(Error::unknown(format!(
                    "creating campaign root {}: {error}",
                    path.display()
                )))
            }
        }
    }
    unreachable!("unbounded campaign-root suffix loop")
}

fn utc_minute(time: SystemTime) -> (i64, u32, u32, u32, u32) {
    let seconds = time
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs() as i64)
        .unwrap_or(0);
    let days = seconds.div_euclid(86_400);
    let second_of_day = seconds.rem_euclid(86_400) as u32;
    let (year, month, day) = civil_from_days(days);
    (
        year,
        month,
        day,
        second_of_day / 3_600,
        (second_of_day % 3_600) / 60,
    )
}

fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let mut year = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = mp + if mp < 10 { 3 } else { -9 };
    year += i64::from(month <= 2);
    (year, month as u32, day as u32)
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
    /// Rejected. The table was renamed to `[generator_synthesis]`.
    #[serde(
        default,
        rename = "protocol_generation",
        deserialize_with = "reject_legacy_generator_synthesis_table"
    )]
    pub(crate) _legacy_generator_synthesis: (),
    /// Protocol generator synthesis. Missing from the file keeps it off.
    #[serde(default)]
    pub generator_synthesis: GeneratorSynthesisConfig,
    /// Named agent profiles referenced by generator synthesis.
    #[serde(default)]
    pub agents: AgentCatalog,
    /// SUT and generator party sets. Required only when generator synthesis is enabled.
    #[serde(default)]
    pub parties: Option<PartiesConfig>,
    /// Directory that contained the TOML file. Not read from the file.
    #[serde(skip)]
    pub base: PathBuf,
    /// CLI-created campaign root. Not read from the file.
    #[serde(skip)]
    pub output_root: Option<PathBuf>,
}

impl Campaign {
    /// Load a TOML campaign and resolve its paths.
    pub fn load(path: &Path) -> Result<Self, Error> {
        Self::load_with_output_root(path, None)
    }

    /// Load a campaign, resolving output fields relative to `output_root`.
    ///
    /// Input fields (`seeds`, binary, model, documents, ...) remain relative to
    /// the campaign file. Output fields (`corpus`, `crashes`, coverage output,
    /// synthesis output) use `output_root` when present.
    pub fn load_with_output_root(path: &Path, output_root: Option<&Path>) -> Result<Self, Error> {
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
        let output_base = output_root.unwrap_or(&base).to_path_buf();
        campaign.target.binary = resolve(&base, &campaign.target.binary);
        campaign.target.source_dir = campaign
            .target
            .source_dir
            .as_ref()
            .map(|path| resolve(&base, path));
        campaign.target.workdir = resolve(&base, &campaign.target.workdir);
        campaign.target.cleanup_script = campaign
            .target
            .cleanup_script
            .as_ref()
            .map(|path| resolve(&base, path));
        campaign.campaign.seeds = resolve(&base, &campaign.campaign.seeds);
        campaign.campaign.corpus = resolve(&output_base, &campaign.campaign.corpus);
        campaign.campaign.crashes = resolve(&output_base, &campaign.campaign.crashes);
        campaign.campaign.python = resolve(&base, &campaign.campaign.python);
        campaign.campaign.pythonpath = resolve(&base, &campaign.campaign.pythonpath);
        if !campaign.coverage.binary.as_os_str().is_empty() {
            campaign.coverage.binary = resolve(&base, &campaign.coverage.binary);
        }
        campaign.coverage.output = resolve(&output_base, &campaign.coverage.output);
        if !campaign.coverage.callgraph_dot.as_os_str().is_empty() {
            campaign.coverage.callgraph_dot = resolve(&base, &campaign.coverage.callgraph_dot);
        }
        if !campaign.coverage.cfg_dir.as_os_str().is_empty() {
            campaign.coverage.cfg_dir = resolve(&base, &campaign.coverage.cfg_dir);
        }
        if !campaign.coverage.build_manifest.as_os_str().is_empty() {
            campaign.coverage.build_manifest = resolve(&base, &campaign.coverage.build_manifest);
        }
        if let Some(socket) = &campaign.coverage.mcp_socket {
            campaign.coverage.mcp_socket = Some(resolve(&base, socket));
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
        if !campaign.generator_synthesis.model.as_os_str().is_empty() {
            campaign.generator_synthesis.model =
                resolve(&base, &campaign.generator_synthesis.model);
        }
        campaign.generator_synthesis.output =
            resolve(&output_base, &campaign.generator_synthesis.output);
        campaign.generator_synthesis.extra_files = campaign
            .generator_synthesis
            .extra_files
            .iter()
            .map(|path| resolve(&base, path))
            .collect();
        if campaign.generator_synthesis.enabled && campaign.parties.is_none() {
            return Err(Error::illegal_argument(
                "generator synthesis requires [parties].sut and [parties].generator".to_string(),
            ));
        }
        if let Some(parties) = &campaign.parties {
            parties.check_shape()?;
            let model_path = &campaign.generator_synthesis.model;
            if model_path.as_os_str().is_empty() {
                return Err(Error::illegal_argument(
                    "generator_synthesis.model is required to resolve [parties]".to_string(),
                ));
            }
            let model = crate::protocol::load_model(model_path)?;
            parties.resolve(&model.party_names())?;
        }
        campaign.base = base;
        campaign.output_root = output_root.map(Path::to_path_buf);
        Ok(campaign)
    }

    /// Placeholder values for this campaign's port.
    pub fn placeholders(&self, port: u16) -> TargetPlaceholders {
        TargetPlaceholders {
            port,
            root: self.target.workdir.clone(),
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
    fn output_root_receives_relative_output_fields() {
        let dir = std::env::temp_dir().join(format!("mcn-output-{}", std::process::id()));
        let root = dir.join("run-root");
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("campaign.toml");
        fs::write(&path, sample("", "")).unwrap();
        let campaign = Campaign::load_with_output_root(&path, Some(&root)).unwrap();
        assert_eq!(campaign.output_root.as_deref(), Some(root.as_path()));
        assert_eq!(campaign.campaign.corpus, root.join("co"));
        assert_eq!(campaign.campaign.crashes, root.join("cr"));
        assert_eq!(campaign.coverage.output, root.join("source-coverage"));
        assert_eq!(campaign.generator_synthesis.output, root.join("synthesis"));
        assert_eq!(campaign.target.workdir, dir.join("w"));
        assert_eq!(campaign.campaign.seeds, dir.join("s"));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn campaign_root_uses_a_unique_timestamped_directory() {
        let dir = std::env::temp_dir().join(format!("mcn-campaign-root-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let first = create_campaign_root(Some(&dir)).unwrap();
        let second = create_campaign_root(Some(&dir)).unwrap();
        let name = first.file_name().unwrap().to_string_lossy();
        assert!(name.starts_with("mcn-fuzzer-"));
        assert_eq!(name.matches('-').count(), 3);
        assert_eq!(
            second.file_name().unwrap().to_string_lossy(),
            format!("{name}-2")
        );
        assert!(first.is_dir() && second.is_dir());
        let _ = fs::remove_dir_all(&dir);
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
        for section in ["coverage", "generator_synthesis"] {
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
        assert_eq!(campaign.target.args, vec!["fftp.conf".to_string()]);
        assert_eq!(campaign.target.cleanup_script, None);
        assert!(!campaign.capture.enabled);
        assert_eq!(campaign.capture.file_limit, 67_108_864);
        assert_eq!(campaign.capture.drain_quiet_ms, 5);
        assert_eq!(campaign.capture.drain_timeout_ms, 100);
        let rendered = campaign.placeholders(2200).apply(&campaign.target.args[0]);
        assert!(rendered.ends_with("fftp.conf"), "{rendered}");
        let conf = campaign.placeholders(2200).apply("port=@PORT@ root=@ROOT@");
        assert!(conf.contains("port=2200"), "{conf}");
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
    fn openssl_campaign_uses_explicit_runtime_paths() {
        let campaign =
            Campaign::load(Path::new("targets/openssl/campaign.toml")).expect("openssl campaign");
        let _placeholders = campaign.placeholders(4433);
        assert!(campaign.target.args.iter().any(|arg| arg == "-naccept"));
        assert!(campaign
            .target
            .args
            .iter()
            .any(|arg| arg.contains("@PORT@")));
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
    fn generator_synthesis_resolves_relative_and_absolute_extra_files() {
        let dir = std::env::temp_dir().join(format!("mcn-cfg-extra-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join("conf")).unwrap();
        fs::write(dir.join("conf/a.conf"), b"account=ubuntu\n").unwrap();
        fs::write(dir.join("conf/b.conf"), b"permission=upload\n").unwrap();
        let absolute = dir.join("conf/b.conf");
        let path = dir.join("campaign.toml");
        let text = format!(
            "{}\n[generator_synthesis]\nenabled = false\noutput = \"out\"\nextra_files = [\"conf/a.conf\", {:?}]\n",
            sample("", ""),
            absolute.display().to_string()
        );
        fs::write(&path, text).unwrap();
        let campaign = Campaign::load(&path).unwrap();
        assert_eq!(
            campaign.generator_synthesis.extra_files,
            vec![dir.join("conf/a.conf"), absolute]
        );
        assert!(!campaign.generator_synthesis.enabled);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn old_protocol_generation_table_is_rejected_with_a_migration_hint() {
        for text in [
            format!("{}\n[protocol_generation]\nenabled = false\n", sample("", "")),
            format!(
                "{}\n[generator_synthesis]\nenabled = false\n[protocol_generation]\nenabled = false\n",
                sample("", "")
            ),
        ] {
            let error = toml::from_str::<Campaign>(&text).unwrap_err();
            let message = error.to_string();
            assert!(message.contains("[protocol_generation]"), "{message}");
            assert!(message.contains("[generator_synthesis]"), "{message}");
        }
    }

    #[test]
    fn generator_synthesis_rejects_unknown_keys() {
        let text = format!(
            "{}\n[generator_synthesis]\nenabled = false\nextra_filez = []\n",
            sample("", "")
        );
        let error = toml::from_str::<Campaign>(&text).unwrap_err();
        assert!(error.to_string().contains("extra_filez"), "{error}");
    }

    #[test]
    fn omitted_generator_synthesis_stays_off_and_profiles_keep_env_pairs() {
        let plain: Campaign = toml::from_str(&sample("", "")).unwrap();
        assert!(!plain.generator_synthesis.enabled);
        assert!(plain.generator_synthesis.model.as_os_str().is_empty());
        assert!(plain.generator_synthesis.extra_files.is_empty());
        assert_eq!(plain.generator_synthesis.init_timeout_secs, 300);
        assert_eq!(plain.coverage.analysis_init_timeout_secs, 300);
        assert_eq!(plain.generator_synthesis.task_timeout_secs, 600);
        assert!(plain.agents.profiles.is_empty());
        let text = format!(
            "{}\n[generator_synthesis]\nenabled = false\nmodel = \"protocol.json\"\nprofile = \"codex\"\n[agents.profiles.codex]\nprovider = \"codex\"\nexecutable = \"/bin/true\"\nmodel = \"gpt\"\neffort = \"low\"\nargs = [\"--sandbox\", \"workspace-write\"]\nenv = [[\"NO_COLOR\", \"1\"]]\n",
            sample("", "")
        );
        let campaign: Campaign = toml::from_str(&text).unwrap();
        assert!(!campaign.generator_synthesis.enabled);
        assert_eq!(campaign.generator_synthesis.profile, "codex");
        let profile = campaign.agents.profiles.get("codex").unwrap();
        assert_eq!(profile.env, vec![("NO_COLOR".to_string(), "1".to_string())]);
        let line = crate::agents::command_line(profile, Path::new("/tmp/synthesis")).unwrap();
        assert_eq!(
            line.argv,
            vec![
                "/bin/true".to_string(),
                "--no-daemon".to_string(),
                "-c".to_string(),
                "check_for_update_on_startup=false".to_string(),
                "--model".to_string(),
                "gpt".to_string(),
                "-c".to_string(),
                "model_reasoning_effort=low".to_string(),
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
            br#"{"meta":{"protocol":"p","complete":false,"documents":[{"doc_id":"d","rfc":"RFC 1","path":"doc.json"}]},"parties":[{"name":"alpha","description":"a"},{"name":"beta","description":"b"},{"name":"gamma","description":"c"}],"messages":[{"name":"m","associated_fields":[]}],"channels":[{"name":"c","description":"c","transport":"TCP","connection":"connection_oriented","endpoint_party_names":["alpha","beta"],"initiator_party_name":"alpha","acceptor_party_name":"beta","endpoint_sources":[]}],"steps":[{"name":"s","action": "send", "message_name":"m","channel_name":"c","sender_name":"alpha","receiver_name":"beta","condition":"go"}],"step_dependencies":[],"unresolved":[]}"#,
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
                "{}\n[generator_synthesis]\nenabled = {enabled}\nmodel = \"{model}\"\nprofile = \"codex\"\n{parties}\n",
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
                "{}\n[generator_synthesis]\nenabled = false\nbindings = [[\"alpha\", \"sut\"]]\n",
                sample("", "")
            ),
        )
        .unwrap();
        let error = Campaign::load(&path).expect_err("legacy bindings");
        let message = format!("{error:?}");
        assert!(
            message.contains("generator_synthesis.bindings has been removed"),
            "{message}"
        );
        assert!(message.contains("[parties]"), "{message}");
        let omitted = party_campaign(&dir, "", false);
        let campaign = Campaign::load(&omitted).unwrap();
        assert!(campaign.parties.is_none());
        assert!(!campaign.generator_synthesis.enabled);
        let _ = std::fs::remove_dir_all(&dir);
    }

    fn party_campaign_model(dir: &Path, parties: &str, enabled: bool, model: &str) -> PathBuf {
        let path = dir.join("campaign.toml");
        let enabled = if enabled { "true" } else { "false" };
        std::fs::write(
            &path,
            format!(
                "{}\n[generator_synthesis]\nenabled = {enabled}\nmodel = \"{model}\"\nprofile = \"codex\"\n{parties}\n",
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
            message.contains("generator_synthesis.model is required to resolve [parties]"),
            "{message}"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }
}
