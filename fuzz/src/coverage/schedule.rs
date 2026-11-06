//! Coverage-analysis scheduling. HC I/O stays off the fuzz thread.
//!
//! At most one request is in flight. A newer snapshot replaces the one waiting
//! to be dispatched and does not cancel the active request. Ordinary fuzz
//! executions are not stalled or counted here.

use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
    time::Duration,
};

use libafl::{
    stages::{Restartable, Stage},
    Error, HasMetadata,
};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use super::analysis::{
    accept_analysis, initialization_prompt, task_prompt, AnalysisLog, SharedLog,
};
use super::{build_fingerprint, graphs::GraphFiles, SourceCoverageProgress};
use crate::{
    agents::{
        command_line, current_boot, redact_text, AgentClient, AgentInit, AgentOutcome,
        AgentSession, AgentTask, CommandLine, SessionInit, SessionPhase, TerminalFact,
        WorkerConfig,
    },
    app::config::Campaign,
    protocol::{load_model, DepEdge, IndexedModel, PartyRole, ProtocolExplorationGraph},
};

use crate::agents::artifacts::{check_identity, read_json, ArtifactSeal};

type SharedGraph = std::sync::Arc<std::sync::Mutex<ProtocolExplorationGraph>>;

type Command = crate::agents::AgentCommand<CoverageInit, AnalysisRequest>;
type Outcome = AgentOutcome<Vec<Value>, Finished>;
type Client = AgentClient<CoverageInit, AnalysisRequest>;

struct CoverageInit {
    ready: SessionInit,
    campaign: Box<Campaign>,
}

impl AgentInit for CoverageInit {
    type Output = Vec<Value>;
    fn output_dir(&self) -> &Path {
        &self.ready.output_dir
    }
    fn prepare(&self) -> Result<(), String> {
        verify_source_digest(&self.campaign).map_err(|error| error.to_string())
    }
    fn accept(&self) -> Result<Vec<Value>, String> {
        accept_initialized_handlers(
            &self.ready.output_dir,
            &self.campaign.protocol_generation.model,
            &self.ready.session,
        )
    }
}

#[derive(Debug)]
struct Finished {
    request: String,
    snapshot: u64,
    result: Value,
    dependencies: Vec<DepEdge>,
    deltas: Vec<(String, f64, String)>,
    handlers: Vec<Value>,
    regions: Vec<Value>,
}

type MeasurementSeal = ArtifactSeal;

struct MeasurePaths {
    snapshot: PathBuf,
    graphs: GraphFiles,
    manifest: PathBuf,
    binary: PathBuf,
    model: PathBuf,
    handlers: PathBuf,
    source: PathBuf,
}

/// Immutable identity and artifact paths sent to the background worker.
struct AnalysisRequest {
    id: String,
    snapshot: u64,
    session: String,
    build: String,
    input_protocol: String,
    output_dir: PathBuf,
    measure: MeasurePaths,
    seal: MeasurementSeal,
}

impl AnalysisRequest {
    fn prepare(&self) -> Result<MeasurementSeal, String> {
        self.measure.graphs.verify()?;
        self.seal.verify(&self.output_dir)?;
        Ok(self.seal.clone())
    }

    fn accept(&self, seal: &MeasurementSeal) -> Result<Finished, String> {
        let finished = read_finished(&self.id, &self.output_dir, seal)?;
        check_identity(
            &finished.result,
            &[
                ("session", &self.session),
                ("request", &self.id),
                ("build", &self.build),
                ("input_protocol", &self.input_protocol),
            ],
        )?;
        if finished.snapshot != self.snapshot {
            return Err(
                "analysis result.json snapshot does not match the dispatched request".into(),
            );
        }
        if finished.result.get("status").and_then(Value::as_str) != Some("ok") {
            return Err(finished
                .result
                .get("error")
                .and_then(Value::as_str)
                .unwrap_or("coverage analysis failed")
                .into());
        }
        Ok(finished)
    }
}

impl AgentTask for AnalysisRequest {
    type Prepared = MeasurementSeal;
    type Output = Finished;
    fn id(&self) -> &str {
        &self.id
    }
    fn output_dir(&self) -> &Path {
        &self.output_dir
    }
    fn prepare(&self) -> Result<MeasurementSeal, String> {
        AnalysisRequest::prepare(self)
    }
    fn accept(&self, seal: MeasurementSeal) -> Result<Finished, String> {
        AnalysisRequest::accept(self, &seal)
    }
    fn result_grace(&self) -> Duration {
        // The task budget stays the campaign timeout. This only covers an
        // agent that finishes the two JSON files and publishes result.json
        // in the seconds after that budget.
        Duration::from_secs(45)
    }
}

struct Inflight {
    request: String,
    snapshot: u64,
    output_dir: PathBuf,
}

/// Stage that polls analysis without blocking the mutational loop.
pub struct CoverageAnalysisStage {
    enabled: bool,
    campaign: Campaign,
    model: Option<IndexedModel>,
    sut_parties: Vec<String>,
    graph: Option<SharedGraph>,
    graph_files: Option<GraphFiles>,
    log: Option<SharedLog>,
    session: String,
    fingerprint: String,
    initialized: bool,
    init_started: bool,
    init_timeouts: u32,
    paused: bool,
    exited: bool,
    inflight: Option<Inflight>,
    pending: Option<u64>,
    worker: Option<Client>,
    launch: Option<CommandLine>,
    session_state: AgentSession,
    secrets: Vec<String>,
    seen_snapshots: BTreeSet<u64>,
}

impl CoverageAnalysisStage {
    pub fn from_campaign(campaign: &Campaign) -> Result<Self, Error> {
        if !campaign.coverage.analysis_enabled {
            return Ok(Self::disabled(campaign));
        }
        let boot = current_boot().ok_or_else(|| {
            Error::unknown(
                "coverage analysis is enabled but the private HC core was not started".to_string(),
            )
        })?;
        let model = load_model(&campaign.protocol_generation.model)?;
        let roles = campaign
            .parties
            .as_ref()
            .ok_or_else(|| Error::illegal_argument("coverage analysis requires [parties]"))?
            .resolve(&model.party_names())?;
        let fingerprint = if campaign.coverage.binary.is_file() {
            build_fingerprint(
                &campaign.coverage.binary,
                &campaign.coverage.llvm_profdata,
                &campaign.coverage.llvm_cov,
            )?
        } else {
            "missing-coverage-binary".to_string()
        };
        let profile = campaign
            .agents
            .profiles
            .get(&campaign.coverage.analysis_profile)
            .ok_or_else(|| {
                Error::illegal_argument(format!(
                    "agent profile {} is not defined",
                    campaign.coverage.analysis_profile
                ))
            })?;
        verify_source_digest(campaign)?;
        let output = analysis_root(campaign, &fingerprint);
        fs::create_dir_all(&output)?;
        let graph_files = GraphFiles::prepare(campaign, &output.join("graphs"))?;
        let launch = command_line(profile, &std::path::absolute(&output)?)?;
        let secrets = launch.secrets();
        let log = ArcLog::open(
            output.join("events.jsonl"),
            boot.session.clone(),
            fingerprint.clone(),
        );
        let mut stage = Self {
            enabled: true,
            campaign: campaign.clone(),
            model: Some(model),
            sut_parties: roles
                .into_iter()
                .filter_map(|(name, role)| (role == PartyRole::Sut).then_some(name))
                .collect(),
            graph: None,
            graph_files: Some(graph_files),
            log: Some(log),
            session: boot.session.clone(),
            fingerprint,
            initialized: false,
            init_started: false,
            init_timeouts: 0,
            paused: false,
            exited: false,
            inflight: None,
            pending: None,
            worker: None,
            launch: Some(launch),
            session_state: AgentSession::default(),
            secrets,
            seen_snapshots: BTreeSet::new(),
        };
        stage.spawn_worker(boot.socket)?;
        Ok(stage)
    }

    fn disabled(campaign: &Campaign) -> Self {
        Self {
            enabled: false,
            campaign: campaign.clone(),
            model: None,
            sut_parties: Vec::new(),
            graph: None,
            graph_files: None,
            log: None,
            session: String::new(),
            fingerprint: String::new(),
            initialized: false,
            init_started: false,
            init_timeouts: 0,
            paused: false,
            exited: false,
            inflight: None,
            pending: None,
            worker: None,
            launch: None,
            session_state: AgentSession::default(),
            secrets: Vec::new(),
            seen_snapshots: BTreeSet::new(),
        }
    }

    pub fn snapshot_dir(&self, snapshot: u64) -> PathBuf {
        self.campaign
            .coverage
            .output
            .join(&self.fingerprint)
            .join("snapshots")
            .join(format!("{snapshot:06}"))
    }

    pub fn event_log(&self) -> Option<SharedLog> {
        self.log.clone()
    }

    pub fn use_graph(&mut self, graph: SharedGraph) {
        self.graph = Some(graph);
    }

    pub fn load_graph(&mut self) -> Result<(), Error> {
        let Some(model) = &self.model else {
            return Ok(());
        };
        self.graph = Some(std::sync::Arc::new(std::sync::Mutex::new(
            ProtocolExplorationGraph::from_model(model),
        )));
        let nodes = self
            .graph
            .as_ref()
            .map(|graph| {
                graph
                    .lock()
                    .unwrap_or_else(|error| error.into_inner())
                    .node_count()
            })
            .unwrap_or(0);
        if let Some(log) = &self.log {
            let mut log = log.lock().unwrap_or_else(|error| error.into_inner());
            log.append("graph_initialized", json!({"nodes": nodes}))
                .map_err(Error::unknown)?;
        }
        Ok(())
    }

    pub fn initialize(&mut self) -> Result<(), Error> {
        if !self.enabled || self.init_started {
            return Ok(());
        }
        let output_dir = analysis_root(&self.campaign, &self.fingerprint).join("init");
        fs::create_dir_all(&output_dir)?;
        for name in ["ready.json", "handlers.json", "handlers.accepted.json"] {
            let path = output_dir.join(name);
            if path.exists() {
                fs::remove_file(path)?;
            }
        }
        let source = self.campaign.target.source_dir.as_ref().ok_or_else(|| {
            Error::illegal_argument("coverage analysis requires target.source_dir")
        })?;
        let prompt = initialization_prompt(
            &self.session,
            &output_dir.display().to_string(),
            &source.display().to_string(),
            &self
                .campaign
                .protocol_generation
                .model
                .display()
                .to_string(),
            &self.sut_parties,
        );
        let launch = self
            .launch
            .clone()
            .ok_or_else(|| Error::illegal_argument("analysis profile did not produce a command"))?;
        self.worker
            .as_ref()
            .ok_or_else(|| Error::unknown("analysis worker is not running"))?
            .send(Command::Init {
                context: CoverageInit {
                    ready: SessionInit {
                        output_dir,
                        session: self.session.clone(),
                    },
                    campaign: Box::new(self.campaign.clone()),
                },
                prompt,
                launch,
            })
            .map_err(|_| Error::unknown("analysis worker stopped"))?;
        self.init_started = true;
        self.log_event("analysis_initializing", json!({}))?;
        Ok(())
    }

    /// Remember the newest unpublished snapshot. Does not interrupt an active request.
    pub fn note_snapshot(&mut self, snapshot: u64) -> Result<(), Error> {
        if !self.enabled || snapshot == 0 || !self.seen_snapshots.insert(snapshot) {
            return Ok(());
        }
        if !self.initialized {
            self.log_event(
                "analysis_snapshot_skipped_initializing",
                json!({
                    "snapshot": snapshot,
                }),
            )?;
            return Ok(());
        }
        if self.paused {
            return Ok(());
        }
        if self
            .inflight
            .as_ref()
            .is_some_and(|item| item.snapshot == snapshot)
        {
            return Ok(());
        }
        if self.pending == Some(snapshot) {
            return Ok(());
        }
        let replaced = self.pending.replace(snapshot);
        self.log_event(
            if replaced.is_some() {
                "analysis_snapshot_replaced"
            } else {
                "analysis_snapshot_queued"
            },
            json!({"snapshot": snapshot, "replaced": replaced}),
        )
    }

    /// Poll HC outcomes and maybe dispatch. Returns quickly.
    pub fn poll(&mut self) -> Result<(), Error> {
        if !self.enabled {
            return Ok(());
        }
        while let Some(outcome) = self.recv_one(Duration::from_millis(1)) {
            self.consume(outcome)?;
        }
        self.maybe_dispatch()
    }

    fn maybe_dispatch(&mut self) -> Result<(), Error> {
        if !self.initialized || self.paused || self.inflight.is_some() {
            return Ok(());
        }
        let Some(snapshot) = self.pending.take() else {
            return Ok(());
        };
        let session_hash = format!("{:x}", Sha256::digest(self.session.as_bytes()));
        let request = format!("{}-{snapshot:06}", &session_hash[..8]);
        let root = analysis_root(&self.campaign, &self.fingerprint);
        let output_dir = root.join("requests").join(&request);
        if output_dir.exists() {
            self.pending = Some(snapshot);
            return Err(Error::illegal_state(format!(
                "analysis request {} already exists",
                output_dir.display()
            )));
        }
        fs::create_dir_all(&output_dir)?;
        let input = json!({
            "session": self.session,
            "request": request,
            "build": self.fingerprint,
            "snapshot": format!("{snapshot:06}"),
            "input_protocol": self.model.as_ref().map(|model| model.protocol_id.clone()),
        });
        fs::write(
            output_dir.join("input.json"),
            serde_json::to_vec_pretty(&input)
                .map_err(|error| Error::serialize(error.to_string()))?,
        )?;
        let snapshot_dir = self.snapshot_dir(snapshot);
        let graphs = self
            .graph_files
            .as_ref()
            .expect("enabled analysis has JSON graphs");
        let prompt = task_prompt(crate::coverage::analysis::TaskPaths {
            session: &self.session,
            request: &request,
            output: &output_dir.display().to_string(),
            snapshot: &format!("{snapshot:06}"),
            snapshot_dir: &snapshot_dir.display().to_string(),
            build: &self.fingerprint,
            callgraph_json: &graphs.callgraph.display().to_string(),
            cfg_json: &graphs.cfg.display().to_string(),
            manifest: &self.campaign.coverage.build_manifest.display().to_string(),
            binary: &self.campaign.coverage.binary.display().to_string(),
            model: &self
                .campaign
                .protocol_generation
                .model
                .display()
                .to_string(),
            source_dir: &self
                .campaign
                .target
                .source_dir
                .as_ref()
                .ok_or_else(|| {
                    Error::illegal_argument("coverage analysis requires target.source_dir")
                })?
                .display()
                .to_string(),
        });
        let measure = MeasurePaths {
            snapshot: snapshot_dir.clone(),
            graphs: graphs.clone(),
            manifest: self.campaign.coverage.build_manifest.clone(),
            binary: self.campaign.coverage.binary.clone(),
            model: self.campaign.protocol_generation.model.clone(),
            handlers: root.join("init/handlers.accepted.json"),
            source: self.campaign.target.source_dir.clone().ok_or_else(|| {
                Error::illegal_argument("coverage analysis requires target.source_dir")
            })?,
        };
        let stats = match super::measure::run(&super::measure::MeasureRequest {
            snapshot: &measure.snapshot,
            callgraph: &measure.graphs.callgraph,
            cfg: &measure.graphs.cfg,
            manifest: &measure.manifest,
            binary: &measure.binary,
            model: &measure.model,
            handlers: &measure.handlers,
            source: &measure.source,
            output_dir: &output_dir,
        }) {
            Ok(stats) => stats,
            Err(error) => {
                let _ = fs::remove_dir_all(&output_dir);
                self.log_event(
                    "analysis_measure_failed",
                    json!({"snapshot": snapshot, "request": request, "reason": error}),
                )?;
                return Ok(());
            }
        };
        self.log_event(
            "analysis_measured",
            json!({
                "snapshot": snapshot,
                "request": request,
                "elapsed_ms": stats.elapsed_ms,
                "handlers": stats.handlers,
                "regions": stats.regions,
                "missed_branches": stats.missed_branches,
                "functions": stats.functions,
                "unknown": stats.unknown,
            }),
        )?;
        if let Err(error) = write_task_input(
            &output_dir.join("input.json"),
            &input,
            self.model.as_ref(),
            &output_dir.join("regions.json"),
        ) {
            let _ = fs::remove_dir_all(&output_dir);
            self.log_event(
                "analysis_measure_failed",
                json!({"snapshot": snapshot, "request": request, "reason": error}),
            )?;
            return Ok(());
        }
        let seal = match seal_measurements(&output_dir) {
            Ok(seal) => seal,
            Err(error) => {
                self.log_event(
                    "analysis_measure_failed",
                    json!({"snapshot": snapshot, "request": request, "reason": error}),
                )?;
                return Ok(());
            }
        };
        self.worker
            .as_ref()
            .ok_or_else(|| Error::unknown("analysis worker is not running"))?
            .send(Command::Run {
                request: AnalysisRequest {
                    id: request.clone(),
                    snapshot,
                    session: self.session.clone(),
                    build: self.fingerprint.clone(),
                    input_protocol: self
                        .model
                        .as_ref()
                        .expect("enabled analysis has a model")
                        .protocol_id
                        .clone(),
                    output_dir: output_dir.clone(),
                    measure,
                    seal,
                },
                prompt,
            })
            .map_err(|_| Error::unknown("analysis worker stopped"))?;
        self.inflight = Some(Inflight {
            request,
            snapshot,
            output_dir,
        });
        self.record_session("send", SessionPhase::Busy, &TerminalFact::default(), None);
        self.log_event("analysis_dispatched", json!({"snapshot": snapshot, "request": self.inflight.as_ref().map(|item| &item.request)}))?;
        Ok(())
    }

    fn consume(&mut self, outcome: Outcome) -> Result<(), Error> {
        match outcome {
            Outcome::Launched(fact) => {
                self.record_session("launch", SessionPhase::Initializing, &fact, None);
                self.log_event("analysis_launched", json!({}))
            }
            Outcome::Ready(handlers, fact) => {
                if let Some(log) = &self.log {
                    let mut log = log.lock().unwrap_or_else(|error| error.into_inner());
                    super::analysis::log_handlers(&mut log, "init", &handlers)
                        .map_err(Error::unknown)?;
                }
                self.initialized = true;
                self.init_timeouts = 0;
                self.record_session("ready", SessionPhase::Ready, &fact, None);
                self.log_event("analysis_ready", json!({}))
            }
            Outcome::Failed(failure) => {
                if !self.initialized {
                    let reason = redact_text(&failure.reason, &self.secrets);
                    if !failure.timeout {
                        self.paused = true;
                        self.record_session(
                            "blocked",
                            SessionPhase::Paused,
                            &failure.fact,
                            Some(reason.clone()),
                        );
                        self.log_event("analysis_init_failed", json!({"reason": reason}))?;
                        return Err(Error::unknown(format!(
                            "coverage analysis initialization failed: {reason}"
                        )));
                    }
                    self.init_timeouts = self.init_timeouts.saturating_add(1);
                    self.record_session(
                        "timeout",
                        SessionPhase::Paused,
                        &failure.fact,
                        Some(reason.clone()),
                    );
                    self.log_event(
                        "analysis_init_timeout",
                        json!({
                            "attempt": self.init_timeouts,
                            "limit": crate::agents::INIT_TIMEOUT_ATTEMPTS,
                            "reason": reason,
                        }),
                    )?;
                    if self.init_timeouts >= crate::agents::INIT_TIMEOUT_ATTEMPTS {
                        let error = format!(
                            "coverage analysis initialization timed out {} consecutive times",
                            self.init_timeouts
                        );
                        self.paused = true;
                        self.log_event(
                            "analysis_init_timeouts_exhausted",
                            json!({"error": error}),
                        )?;
                        return Err(Error::unknown(error));
                    }
                    self.paused = false;
                    self.init_started = false;
                    self.log_event(
                        "analysis_init_retry",
                        json!({"next_attempt": self.init_timeouts + 1}),
                    )?;
                    self.initialize()?;
                    return Ok(());
                }
                self.paused = true;
                let inflight = self.inflight.take();
                let reason = redact_text(&failure.reason, &self.secrets);
                self.record_session(
                    if failure.timeout {
                        "timeout"
                    } else {
                        "blocked"
                    },
                    SessionPhase::Paused,
                    &failure.fact,
                    Some(reason.clone()),
                );
                self.log_event(
                    if failure.timeout {
                        "analysis_timeout"
                    } else {
                        "analysis_failed"
                    },
                    json!({
                        "reason": reason,
                        "request": inflight.as_ref().map(|item| &item.request),
                        "snapshot": inflight.as_ref().map(|item| item.snapshot),
                        "output_dir": inflight.as_ref().map(|item| &item.output_dir),
                    }),
                )
            }
            Outcome::Artifact {
                request,
                result,
                fact,
            } => {
                if !self
                    .inflight
                    .as_ref()
                    .is_some_and(|item| item.request == request)
                {
                    return self.log_event(
                        "analysis_result_ignored",
                        json!({"request": request, "reason": "late result for a closed request"}),
                    );
                }
                self.inflight = None;
                match result {
                    Ok(finished) => {
                        self.record_session("result", SessionPhase::Ready, &fact, None);
                        self.apply_finished(finished)
                    }
                    Err(reason) => {
                        let reason = redact_text(&reason, &self.secrets);
                        self.record_session(
                            "blocked",
                            SessionPhase::Ready,
                            &fact,
                            Some(reason.clone()),
                        );
                        self.log_event(
                            "analysis_rejected",
                            json!({"request": request, "reason": reason}),
                        )
                    }
                }
            }
        }
    }

    fn record_session(
        &mut self,
        transition: &str,
        phase: SessionPhase,
        fact: &TerminalFact,
        error: Option<String>,
    ) {
        let path = analysis_root(&self.campaign, &self.fingerprint).join("session.jsonl");
        self.session_state
            .record(transition, phase, fact, error, &self.secrets);
        self.session_state.append_log(&path);
    }

    fn apply_finished(&mut self, finished: Finished) -> Result<(), Error> {
        let Some(graph) = &self.graph else {
            return self.log_event(
                "model_rejected",
                json!({"reason": "exploration graph is missing", "request": finished.request}),
            );
        };
        let Some(log) = &self.log else {
            return Ok(());
        };
        let Some(model) = &self.model else {
            return Ok(());
        };
        let mut graph = graph.lock().unwrap_or_else(|error| error.into_inner());
        let mut log = log.lock().unwrap_or_else(|error| error.into_inner());
        if let Err(error) = crate::coverage::analysis::record_measurements(
            &mut log,
            &finished.request,
            &format!("{:06}", finished.snapshot),
            &finished.handlers,
            &finished.regions,
        ) {
            log::error!("coverage analysis measurement log failed: {error}");
            return Err(Error::unknown(error));
        }
        match accept_analysis(
            &mut graph,
            model,
            &finished.request,
            &self.session,
            &self.fingerprint,
            &format!("{:06}", finished.snapshot),
            &model.protocol_id,
            &finished.result,
            &finished.dependencies,
            &finished.deltas,
            &mut log,
        ) {
            Ok(_) => Ok(()),
            Err(error) => {
                log::error!("coverage analysis result rejected: {error}");
                Ok(())
            }
        }
    }

    fn recv_one(&mut self, timeout: Duration) -> Option<Outcome> {
        self.worker
            .as_mut()
            .and_then(|worker| worker.recv_timeout(timeout))
    }

    fn spawn_worker(&mut self, socket: PathBuf) -> Result<(), Error> {
        self.worker = Some(
            AgentClient::spawn(WorkerConfig {
                thread_name: "mcn-coverage-analysis",
                socket,
                terminal: "coverage-analysis".into(),
                init_timeout: Duration::from_secs(
                    self.campaign.coverage.analysis_init_timeout_secs.max(1),
                ),
                task_timeout: Duration::from_secs(
                    self.campaign.coverage.analysis_task_timeout_secs.max(1),
                ),
                secrets: self.secrets.clone(),
            })
            .map_err(|error| Error::unknown(format!("starting coverage worker: {error}")))?,
        );
        Ok(())
    }

    fn log_event(&mut self, kind: &str, mut body: Value) -> Result<(), Error> {
        let Some(log) = &self.log else {
            return Ok(());
        };
        if let Value::Object(map) = &mut body {
            map.entry("session").or_insert(json!(self.session));
            map.entry("build").or_insert(json!(self.fingerprint));
        }
        let mut log = log.lock().unwrap_or_else(|error| error.into_inner());
        log.append(kind, body).map(|_| ()).map_err(Error::unknown)
    }

    fn shutdown(&mut self) {
        if self.enabled && !self.exited {
            self.exited = true;
            self.record_session("stop", SessionPhase::Exited, &TerminalFact::default(), None);
            let _ = self.log_event(
                "analysis_stop",
                json!({"inflight": self.inflight.is_some()}),
            );
        }
        self.worker.take();
    }
}

impl Drop for CoverageAnalysisStage {
    fn drop(&mut self) {
        self.shutdown();
    }
}

impl<S> Restartable<S> for CoverageAnalysisStage {
    fn should_restart(&mut self, _state: &mut S) -> Result<bool, Error> {
        Ok(true)
    }
    fn clear_progress(&mut self, _state: &mut S) -> Result<(), Error> {
        Ok(())
    }
}

impl<E, EM, S, Z> Stage<E, EM, S, Z> for CoverageAnalysisStage
where
    S: HasMetadata,
{
    fn perform(
        &mut self,
        _fuzzer: &mut Z,
        _executor: &mut E,
        state: &mut S,
        _manager: &mut EM,
    ) -> Result<(), Error> {
        if !self.enabled {
            return Ok(());
        }
        self.poll()?;
        if let Ok(progress) = state.metadata::<SourceCoverageProgress>() {
            self.note_snapshot(progress.snapshot)?;
        }
        self.poll()
    }
}

fn analysis_root(campaign: &Campaign, fingerprint: &str) -> PathBuf {
    campaign.coverage.output.join(fingerprint).join("analysis")
}

struct ArcLog;
impl ArcLog {
    fn open(path: PathBuf, session: String, build: String) -> SharedLog {
        std::sync::Arc::new(std::sync::Mutex::new(AnalysisLog::open(
            path, session, build,
        )))
    }
}

/// Accept only a complete source-backed mapping from this initialization session.
fn accept_initialized_handlers(
    output: &Path,
    model: &Path,
    session: &str,
) -> Result<Vec<Value>, String> {
    SessionInit {
        output_dir: output.to_path_buf(),
        session: session.into(),
    }
    .accept()?;
    let mut document = read_json(&output.join("handlers.json"))?;
    if document.get("version").and_then(Value::as_u64) != Some(1)
        || document.get("session").and_then(Value::as_str) != Some(session)
    {
        return Err("initialization handlers.json must have version 1 and this session".into());
    }
    let model = read_json(model)?;
    let messages = model
        .get("messages")
        .and_then(Value::as_array)
        .ok_or("model has no messages")?;
    let rows = document
        .get("mappings")
        .and_then(Value::as_array)
        .ok_or("handlers.json has no mappings")?;
    if rows.len() != messages.len() {
        return Err("handlers.json must map every model message exactly once".into());
    }
    for message in messages {
        let name = message
            .get("name")
            .and_then(Value::as_str)
            .ok_or("model message missing name")?;
        let matching: Vec<_> = rows
            .iter()
            .filter(|row| row.get("message").and_then(Value::as_str) == Some(name))
            .collect();
        if matching.len() != 1 {
            return Err(format!(
                "handlers.json must map message {name} exactly once"
            ));
        }
        let row = matching[0];
        let handlers = row
            .get("handlers")
            .and_then(Value::as_array)
            .ok_or("message mapping missing handlers array")?;
        let nonempty = |value: Option<&Value>| {
            value
                .and_then(Value::as_str)
                .is_some_and(|text| !text.trim().is_empty())
        };
        if handlers.is_empty() && !nonempty(row.get("reason")) {
            return Err(format!("unmatched message {name} requires a reason"));
        }
        for handler in handlers {
            for key in ["symbol", "location", "evidence"] {
                if !nonempty(handler.get(key)) {
                    return Err(format!("handler for {name} requires {key}"));
                }
            }
        }
    }
    let mut mappings = rows.clone();
    let steps = model
        .get("steps")
        .and_then(Value::as_array)
        .ok_or("model has no steps")?;
    for row in &mut mappings {
        let matching: Vec<_> = steps
            .iter()
            .filter(|step| step.get("message_name") == row.get("message"))
            .collect();
        row["step_ids"] = json!(matching
            .iter()
            .filter_map(|step| step.get("id"))
            .collect::<Vec<_>>());
        row["step_names"] = json!(matching
            .iter()
            .filter_map(|step| step.get("name"))
            .collect::<Vec<_>>());
    }
    document["mappings"] = json!(mappings);
    // Snapshot tools consume the accepted mapping, never a later agent edit.
    fs::write(
        output.join("handlers.accepted.json"),
        serde_json::to_vec_pretty(&document).map_err(|error| error.to_string())?,
    )
    .map_err(|error| error.to_string())?;
    Ok(mappings)
}

fn verify_source_digest(campaign: &Campaign) -> Result<(), Error> {
    let source = campaign.target.source_dir.as_ref().ok_or_else(|| {
        Error::illegal_argument("coverage analysis requires target.source_dir".to_string())
    })?;
    let manifest = read_json(&campaign.coverage.build_manifest).map_err(Error::unknown)?;
    let actual = super::measure::source_digest(source).map_err(Error::illegal_argument)?;
    let expected = manifest
        .get("source_sha256")
        .and_then(Value::as_str)
        .unwrap_or("");
    if actual != expected {
        return Err(Error::illegal_argument(format!(
            "source tree does not match manifest source_sha256\nmanifest {expected}\nsource   {actual}"
        )));
    }
    Ok(())
}

/// Rewrite `input.json` after measurement with the step table and a compact
/// region summary. The coverage agent reads this file instead of regions.json.
fn write_task_input(
    path: &Path,
    identity: &Value,
    model: Option<&IndexedModel>,
    regions_path: &Path,
) -> Result<(), String> {
    let model = model.ok_or("coverage analysis requires a protocol model")?;
    let regions_doc = read_json(regions_path)?;
    let handlers = regions_doc
        .get("handlers")
        .and_then(Value::as_array)
        .ok_or("regions.json is missing handlers")?;
    let summary: Vec<Value> = handlers
        .iter()
        .filter(|row| {
            row.get("branch_directions")
                .and_then(Value::as_u64)
                .unwrap_or(0)
                > 0
        })
        .map(|row| {
            json!({
                "handler": row.get("handler").cloned().unwrap_or(Value::Null),
                "branch_directions": row.get("branch_directions").and_then(Value::as_u64).unwrap_or(0),
                "function_count": row.get("function_count").and_then(Value::as_u64).unwrap_or(0),
                "unknown": row.get("unknown").and_then(Value::as_array).map(Vec::len).unwrap_or(0),
                "zero": row.get("zero").cloned().unwrap_or(json!(false)),
                "complete": row.get("complete").cloned().unwrap_or(json!(false)),
                "locations": summary_locations(row, 3),
            })
        })
        .collect();
    let steps: Vec<Value> = model
        .steps
        .iter()
        .map(|step| {
            json!({
                "id": step.id().0,
                "name": step.name,
                "message_name": step.message_name,
            })
        })
        .collect();
    let mut document = identity.clone();
    document["steps"] = json!(steps);
    document["region_summary"] = json!(summary);
    let bytes = serde_json::to_vec_pretty(&document).map_err(|error| error.to_string())?;
    fs::write(path, bytes).map_err(|error| format!("writing {}: {error}", path.display()))
}

fn summary_locations(row: &Value, limit: usize) -> Vec<Value> {
    let mut locations = Vec::new();
    let Some(regions) = row.get("regions").and_then(Value::as_array) else {
        return locations;
    };
    for region in regions {
        let Some(items) = region.get("locations").and_then(Value::as_array) else {
            continue;
        };
        for location in items {
            if location.is_null() || locations.iter().any(|seen| seen == location) {
                continue;
            }
            locations.push(location.clone());
            if locations.len() == limit {
                return locations;
            }
        }
    }
    locations
}

fn seal_measurements(output_dir: &Path) -> Result<MeasurementSeal, String> {
    ArtifactSeal::capture(output_dir, &["handlers.json", "regions.json"])
}

fn read_finished(
    request: &str,
    output_dir: &Path,
    seal: &MeasurementSeal,
) -> Result<Finished, String> {
    let handlers_path = output_dir.join("handlers.json");
    let regions_path = output_dir.join("regions.json");
    if !handlers_path.is_file() || !regions_path.is_file() {
        return Err(
            "handlers.json and regions.json were not published by the measure tool".to_string(),
        );
    }
    seal.verify(output_dir)?;
    let result = read_json(&output_dir.join("result.json"))?;
    let snapshot = result
        .get("snapshot")
        .and_then(Value::as_str)
        .unwrap_or("0")
        .parse::<u64>()
        .unwrap_or(0);
    let dependencies = read_dependencies(&output_dir.join("protocol-refined.json"))?;
    let deltas = read_deltas(&output_dir.join("priority-delta.json"))?;
    let (handlers, regions) = read_measurements(output_dir)?;
    Ok(Finished {
        request: request.to_string(),
        snapshot,
        result,
        dependencies,
        deltas,
        handlers,
        regions,
    })
}

fn read_measurements(output_dir: &Path) -> Result<(Vec<Value>, Vec<Value>), String> {
    let handlers_doc = read_json(&output_dir.join("handlers.json"))?;
    let regions_doc = read_json(&output_dir.join("regions.json"))?;
    if handlers_doc.get("version").and_then(Value::as_u64) != Some(1)
        || regions_doc.get("version").and_then(Value::as_u64) != Some(1)
    {
        return Err("handler and region reports must be version 1".to_string());
    }
    let profile = handlers_doc
        .get("profile_sha256")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or("handlers.json is missing profile_sha256")?;
    if regions_doc.get("profile_sha256").and_then(Value::as_str) != Some(profile) {
        return Err("handler profile_sha256 does not match the measure tool".to_string());
    }
    let handlers = handlers_doc
        .get("mappings")
        .and_then(Value::as_array)
        .cloned()
        .ok_or("handlers.json is missing mappings")?;
    let regions = regions_doc
        .get("handlers")
        .and_then(Value::as_array)
        .cloned()
        .ok_or("regions.json is missing handlers")?;
    if handlers.is_empty() {
        return Err("handlers.json has no message mappings".to_string());
    }
    if regions.len() != handlers.len() {
        return Err(
            "regions.json handlers length does not match the initialized handler mappings"
                .to_string(),
        );
    }
    Ok((handlers, regions))
}

fn read_dependencies(path: &Path) -> Result<Vec<DepEdge>, String> {
    let value = read_json(path)?;
    let items = value
        .get("step_dependencies")
        .and_then(Value::as_array)
        .ok_or("protocol-refined.json requires a step_dependencies array")?;
    items
        .iter()
        .map(|item| {
            Ok(DepEdge {
                prior: item
                    .get("prior_step")
                    .and_then(Value::as_str)
                    .ok_or("dependency is missing prior_step")?
                    .to_string(),
                dependent: item
                    .get("dependent_step")
                    .and_then(Value::as_str)
                    .ok_or("dependency is missing dependent_step")?
                    .to_string(),
                condition: item
                    .get("condition")
                    .and_then(Value::as_str)
                    .ok_or("dependency is missing condition")?
                    .to_string(),
                field_relations: Vec::new(),
            })
        })
        .collect()
}

fn read_deltas(path: &Path) -> Result<Vec<(String, f64, String)>, String> {
    let value = read_json(path)?;
    let items = value
        .get("steps")
        .and_then(Value::as_array)
        .ok_or("priority-delta.json requires a steps array")?;
    let mut deltas = Vec::new();
    for item in items {
        let step_id = item
            .get("step_id")
            .and_then(Value::as_str)
            .ok_or("priority delta is missing step_id")?
            .to_string();
        let delta = item
            .get("delta")
            .and_then(Value::as_f64)
            .ok_or("priority delta is missing delta")?;
        if !delta.is_finite() {
            return Err(format!("priority delta for {step_id} is not finite"));
        }
        let reason = item
            .get("reason")
            .and_then(Value::as_str)
            .ok_or("priority delta is missing reason")?
            .to_string();
        deltas.push((step_id, delta, reason));
    }
    Ok(deltas)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::agents::AgentFailure;
    use crate::agents::{install_boot_for_test, BootRecord, MockCore};
    use crate::app::config::Campaign;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::Instant;
    use std::{sync::mpsc, thread};

    static TEMP: AtomicU64 = AtomicU64::new(0);

    fn campaign_dir() -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "mcn-analysis-sched-{}-{}",
            std::process::id(),
            TEMP.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join("cfg")).unwrap();
        fs::create_dir_all(dir.join("src")).unwrap();
        fs::write(
            dir.join("src").join("ftpserv.c"),
            b"int ftpM(void){return 0;}",
        )
        .unwrap();
        fs::write(dir.join("binary"), b"coverage-binary").unwrap();
        fs::write(dir.join("cg.dot"), "digraph { n1 [label=\"{ftpM}\"]; }\n").unwrap();
        fs::write(dir.join("cfg").join("ftpM.dot"), "digraph { a -> b; }\n").unwrap();
        let model = serde_json::json!({
            "meta": {"schema_version": "7.0.0", "protocol": "p", "complete": false, "documents": [{"doc_id": "d", "rfc": "RFC 1", "input_sha256": "a", "document_sha256": "b", "path": "doc.json"}]},
            "parties": [{"name": "client", "description": "c"}, {"name": "server", "description": "s"}],
            "messages": [{"name": "m", "associated_fields": []}],
            "channels": [{"name": "c", "description": "c", "transport": "TCP", "connection": "connection_oriented", "endpoint_party_names": ["client", "server"], "initiator_party_name": "client", "acceptor_party_name": "server", "endpoint_sources": []}],
            "steps": [
                {"id": "1", "name": "a", "message_name": "m", "channel_name": "c", "sender_name": "client", "receiver_name": "server", "condition": "go"},
                {"id": "2", "name": "b", "message_name": "m", "channel_name": "c", "sender_name": "server", "receiver_name": "client", "condition": "go"}
            ],
            "step_dependencies": [],
            "unresolved": []
        });
        fs::write(
            dir.join("protocol.json"),
            serde_json::to_vec(&model).unwrap(),
        )
        .unwrap();
        let status = std::process::Command::new("/usr/bin/python3")
            .env(
                "PYTHONPATH",
                PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("python"),
            )
            .arg("-c")
            .arg(
                "from coverage_analysis.manifest import write_manifest\n\
                 from pathlib import Path\n\
                 import sys\n\
                 root = Path(sys.argv[1])\n\
                 write_manifest(root/'manifest.json', root/'binary', root/'src', root, ['cg.dot', 'cfg/ftpM.dot'])\n",
            )
            .arg(&dir)
            .status()
            .unwrap();
        assert!(status.success(), "manifest was not sealed");
        let python = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(".venv/bin/python");
        let toml = format!(
            r#"
[target]
binary = "/bin/true"
source_dir = "{dir}/src"
workdir = "{dir}/work"
fixture = "{dir}"
config_template = "{dir}/protocol.json"
port = 0
[timeouts]
connect_ms = 1
io_ms = 1
case_ms = 1
exit_grace_ms = 1
[campaign]
seeds = "{dir}"
corpus = "{dir}/corpus"
crashes = "{dir}/crashes"
python = "{python}"
pythonpath = "{pythonpath}"
iterations = 1
[coverage]
enabled = true
binary = "{dir}/binary"
output = "{dir}/coverage"
analysis_enabled = true
analysis_profile = "coverage-analyzer"
analysis_init_timeout_secs = 2
analysis_task_timeout_secs = 2
callgraph_dot = "{dir}/cg.dot"
cfg_dir = "{dir}/cfg"
build_manifest = "{dir}/manifest.json"
[protocol_generation]
enabled = false
model = "{dir}/protocol.json"
[parties]
sut = ["server"]
generator = ["client"]
[agents]
hc_executable = "/bin/true"
[agents.profiles.coverage-analyzer]
provider = "codex"
executable = "/bin/true"
"#,
            dir = dir.display(),
            python = python.display(),
            pythonpath = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("python")
                .display()
        );
        let path = dir.join("campaign.toml");
        fs::write(&path, toml).unwrap();
        dir
    }

    #[test]
    fn analysis_stays_async_and_keeps_only_the_latest_snapshot() {
        let dir = campaign_dir();
        let socket = dir.join("hc.sock");
        let core = MockCore::spawn(socket.clone()).unwrap();
        install_boot_for_test(BootRecord {
            socket,
            session: "sched-session".into(),
        });
        let campaign = Campaign::load(&dir.join("campaign.toml")).unwrap();
        let mut stage = CoverageAnalysisStage::from_campaign(&campaign).unwrap();
        stage.load_graph().unwrap();
        let graphs = stage.graph_files.as_ref().unwrap();
        assert!(graphs.callgraph.is_file());
        assert!(graphs.cfg.is_file());
        // Once startup finished, all request preparation must use JSON alone.
        fs::remove_file(&campaign.coverage.callgraph_dot).unwrap();
        fs::remove_file(campaign.coverage.cfg_dir.join("ftpM.dot")).unwrap();
        for snapshot in [1u64, 2, 3] {
            let snap = stage.snapshot_dir(snapshot);
            fs::create_dir_all(&snap).unwrap();
            let compressed =
                flate2::write::ZlibEncoder::new(Vec::new(), flate2::Compression::default());
            let mut compressed = compressed;
            use std::io::Write as _;
            compressed.write_all(b"profile-bytes").unwrap();
            fs::write(
                snap.join("coverage.profdata.zlib"),
                compressed.finish().unwrap(),
            )
            .unwrap();
            fs::write(
                snap.join("coverage.json"),
                r#"{"data":[{"functions":[{"name":"ftpM","count":1,"filenames":["ftpserv.c"],"regions":[],"branches":[[1,1,1,2,0,1,0,0,4]]}]}]}"#,
            )
            .unwrap();
        }
        fs::write(dir.join("handlers.fixture.json"), serde_json::to_vec(&json!({
            "version": 1, "session": "sched-session", "mappings": [{
                "message": "m", "handlers": [{"symbol": "ftpM", "location": "ftpserv.c:1", "evidence": "fixture dispatch invokes ftpM"}]
            }]
        })).unwrap()).unwrap();
        stage.initialize().unwrap();
        let start = Instant::now();
        let mut execs = 0u64;
        while !stage.initialized && start.elapsed() < Duration::from_secs(2) {
            execs += 1;
            stage.poll().unwrap();
        }
        assert!(stage.initialized, "analysis did not become ready");
        assert!(execs >= 1);
        let profile = fs::read(stage.snapshot_dir(2).join("coverage.profdata.zlib")).unwrap();
        stage.note_snapshot(1).unwrap();
        stage.note_snapshot(2).unwrap();
        assert_eq!(stage.pending, Some(2));
        stage.poll().unwrap();
        assert!(stage
            .inflight
            .as_ref()
            .is_some_and(|item| item.snapshot == 2));
        stage.note_snapshot(3).unwrap();
        assert_eq!(stage.pending, Some(3));
        assert!(stage
            .inflight
            .as_ref()
            .is_some_and(|item| item.snapshot == 2));
        let during = Instant::now();
        let mut during_execs = 0u64;
        while during.elapsed() < Duration::from_millis(150) {
            during_execs += 1;
            stage.poll().unwrap();
        }
        assert!(
            during_execs > 10,
            "fuzz polling blocked on the in-flight analysis"
        );
        let log_path = stage
            .event_log()
            .expect("log")
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .path()
            .to_path_buf();
        let deadline = Instant::now() + Duration::from_secs(8);
        let mut text = fs::read_to_string(&log_path).unwrap_or_default();
        while Instant::now() < deadline
            && !text.contains("unknown_boundary")
            && !text.contains("analysis_failed")
        {
            stage.poll().unwrap();
            text = fs::read_to_string(&log_path).unwrap_or_default();
            thread::sleep(Duration::from_millis(10));
        }
        assert!(
            text.contains("handler_mapping")
                && text.contains("uncovered_region")
                && text.contains("unknown_boundary")
                && (text.contains("cannot be mapped") || text.contains("no CFG")),
            "{text}"
        );
        let model = stage.model.as_ref().expect("model");
        let score = stage
            .graph
            .as_ref()
            .expect("graph")
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .score_of(&model.steps[0].id().0);
        assert_eq!(score, Some(0.0));
        assert!(text.contains("model_accepted"));
        let after = fs::read(stage.snapshot_dir(2).join("coverage.profdata.zlib")).unwrap();
        assert_eq!(profile, after);
        drop(stage);
        drop(core);
        let _ = fs::remove_dir_all(dir);
    }

    /// A stage with a channel but no HC worker, for testing the stage boundary.
    fn channel_stage(
        dir: &Path,
    ) -> (
        CoverageAnalysisStage,
        mpsc::Receiver<Command>,
        mpsc::Sender<Outcome>,
    ) {
        let campaign = Campaign::load(&dir.join("campaign.toml")).unwrap();
        let mut stage = CoverageAnalysisStage::disabled(&campaign);
        stage.enabled = true;
        stage.initialized = true;
        stage.model = Some(load_model(&campaign.protocol_generation.model).unwrap());
        stage.session = "test-session".into();
        stage.fingerprint = "test-build".into();
        let root = analysis_root(&campaign, &stage.fingerprint);
        fs::create_dir_all(&root).unwrap();
        stage.graph_files = Some(GraphFiles::prepare(&campaign, &root.join("graphs")).unwrap());
        stage.log = Some(ArcLog::open(
            root.join("events.jsonl"),
            stage.session.clone(),
            stage.fingerprint.clone(),
        ));
        fs::create_dir_all(root.join("init")).unwrap();
        fs::write(
            root.join("init/handlers.accepted.json"),
            r#"{"version":1,"mappings":[{"message":"m","handlers":[{"symbol":"ftpM","location":"ftpserv.c:1","evidence":"fixture dispatch invokes ftpM"}]}]}"#,
        )
        .unwrap();
        for snapshot in [1u64, 7, 8, 9] {
            let snap = stage.snapshot_dir(snapshot);
            fs::create_dir_all(&snap).unwrap();
            let mut compressed =
                flate2::write::ZlibEncoder::new(Vec::new(), flate2::Compression::default());
            use std::io::Write as _;
            compressed.write_all(b"profile-bytes").unwrap();
            fs::write(
                snap.join("coverage.profdata.zlib"),
                compressed.finish().unwrap(),
            )
            .unwrap();
            fs::write(
                snap.join("coverage.json"),
                r#"{"data":[{"functions":[{"name":"ftpM","count":0,"filenames":["ftpserv.c"],"regions":[],"branches":[]}]}]}"#,
            )
            .unwrap();
        }
        let (sender, receiver) = mpsc::channel();
        let (outcome_tx, outcomes) = mpsc::channel();
        stage.worker = Some(AgentClient::from_channels(sender, outcomes));
        (stage, receiver, outcome_tx)
    }

    fn take_request(receiver: &mpsc::Receiver<Command>) -> AnalysisRequest {
        match receiver.recv_timeout(Duration::from_secs(1)).unwrap() {
            Command::Run { request, .. } => request,
            _ => panic!("expected an analysis request"),
        }
    }

    #[test]
    fn task_input_summarizes_only_missed_branch_handlers() {
        let dir = campaign_dir();
        let model = load_model(&dir.join("protocol.json")).unwrap();
        let regions = dir.join("regions.json");
        fs::write(
            &regions,
            serde_json::to_vec(&json!({
                "version": 1,
                "handlers": [
                    {
                        "handler": "covered",
                        "branch_directions": 0,
                        "function_count": 1,
                        "unknown": [],
                        "zero": true,
                        "complete": true,
                        "regions": []
                    },
                    {
                        "handler": "missed",
                        "branch_directions": 4,
                        "function_count": 2,
                        "unknown": [{"reason": "x"}, {"reason": "y"}],
                        "zero": false,
                        "complete": false,
                        "regions": [{
                            "locations": ["a.c:1:1", null, "a.c:2:1", "a.c:3:1", "a.c:4:1"]
                        }]
                    }
                ]
            }))
            .unwrap(),
        )
        .unwrap();
        let input_path = dir.join("input.json");
        write_task_input(
            &input_path,
            &json!({
                "session": "s",
                "request": "r",
                "build": "b",
                "snapshot": "000007",
                "input_protocol": "p"
            }),
            Some(&model),
            &regions,
        )
        .unwrap();
        let input = read_json(&input_path).unwrap();
        assert_eq!(input["session"], "s");
        assert_eq!(input["request"], "r");
        assert_eq!(input["steps"].as_array().unwrap().len(), 2);
        let summary = input["region_summary"].as_array().unwrap();
        assert_eq!(summary.len(), 1);
        assert_eq!(summary[0]["handler"], "missed");
        assert_eq!(summary[0]["branch_directions"], 4);
        assert_eq!(summary[0]["unknown"], 2);
        assert_eq!(summary[0]["complete"], false);
        assert_eq!(
            summary[0]["locations"],
            json!(["a.c:1:1", "a.c:2:1", "a.c:3:1"])
        );
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn request_rejects_modified_startup_graph_json() {
        let dir = campaign_dir();
        let (mut stage, receiver, _outcomes) = channel_stage(&dir);
        stage.note_snapshot(1).unwrap();
        stage.poll().unwrap();
        let request = take_request(&receiver);
        fs::write(&request.measure.graphs.cfg, "{}").unwrap();
        let error = request.prepare().unwrap_err();
        assert!(error.contains("cfg.json no longer matches"), "{error}");
        drop(stage);
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn request_acceptance_checks_the_dispatched_identity_and_status() {
        let dir = campaign_dir();
        let (mut stage, receiver, _outcomes) = channel_stage(&dir);
        stage.note_snapshot(7).unwrap();
        stage.poll().unwrap();
        let request = take_request(&receiver);
        let input = read_json(&request.output_dir.join("input.json")).unwrap();
        let steps = input["steps"].as_array().expect("step table");
        assert_eq!(steps.len(), 2);
        assert_eq!(steps[0]["name"], "a");
        assert!(steps[0]["id"].as_str().is_some_and(|id| id.len() == 8));
        assert!(input["region_summary"].is_array());
        assert!(input.get("handlers").is_none());
        let (handlers, regions) = sample_measurements();
        fs::write(request.output_dir.join("handlers.json"), handlers).unwrap();
        fs::write(request.output_dir.join("regions.json"), regions).unwrap();
        let seal = seal_measurements(&request.output_dir).unwrap();
        fs::write(
            request.output_dir.join("protocol-refined.json"),
            br#"{"step_dependencies":[]}"#,
        )
        .unwrap();
        fs::write(
            request.output_dir.join("priority-delta.json"),
            br#"{"steps":[]}"#,
        )
        .unwrap();
        let mut result = read_json(&request.output_dir.join("input.json")).unwrap();
        result["status"] = json!("ok");
        for key in [
            "session",
            "request",
            "build",
            "input_protocol",
            "snapshot",
            "status",
        ] {
            let mut stale = result.clone();
            stale[key] = json!("stale");
            fs::write(
                request.output_dir.join("result.json"),
                serde_json::to_vec(&stale).unwrap(),
            )
            .unwrap();
            assert!(request.accept(&seal).is_err(), "accepted stale {key}");
        }
        fs::write(
            request.output_dir.join("result.json"),
            serde_json::to_vec(&result).unwrap(),
        )
        .unwrap();
        assert_eq!(request.accept(&seal).unwrap().snapshot, 7);
        drop(stage);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn artifact_rejection_keeps_session_ready_and_ignores_duplicate_and_late_results() {
        let dir = campaign_dir();
        let (mut stage, receiver, _outcomes) = channel_stage(&dir);
        stage.note_snapshot(7).unwrap();
        stage.poll().unwrap();
        let first = take_request(&receiver);
        stage
            .consume(Outcome::Artifact {
                request: first.id.clone(),
                result: Err("invalid artifact".into()),
                fact: TerminalFact::default(),
            })
            .unwrap();
        assert!(!stage.paused);
        assert!(stage.inflight.is_none());
        assert_eq!(stage.session_state.phase, SessionPhase::Ready);
        stage.note_snapshot(7).unwrap();
        stage.poll().unwrap();
        assert!(matches!(
            receiver.try_recv(),
            Err(mpsc::TryRecvError::Empty)
        ));
        stage.note_snapshot(8).unwrap();
        stage.poll().unwrap();
        let second = take_request(&receiver);
        assert_ne!(first.id, second.id);
        stage
            .consume(Outcome::Artifact {
                request: first.id,
                result: Err("late".into()),
                fact: TerminalFact::default(),
            })
            .unwrap();
        assert_eq!(stage.inflight.as_ref().unwrap().request, second.id);
        stage.secrets = vec!["private-value".into()];
        stage
            .consume(Outcome::Failed(AgentFailure::new(
                "agent timed out private-value".into(),
                TerminalFact {
                    terminal: Some("coverage-analysis".into()),
                    state: Some("live".into()),
                    status: Some("private-value".into()),
                    error: None,
                },
            )))
            .unwrap();
        assert!(stage.paused);
        assert_eq!(stage.session_state.phase, SessionPhase::Paused);
        assert_eq!(stage.session_state.hc_state.as_deref(), Some("live"));
        stage.note_snapshot(9).unwrap();
        stage.poll().unwrap();
        assert!(matches!(
            receiver.try_recv(),
            Err(mpsc::TryRecvError::Empty)
        ));
        let root = analysis_root(&stage.campaign, &stage.fingerprint);
        let sessions = fs::read_to_string(root.join("session.jsonl")).unwrap();
        let events = fs::read_to_string(root.join("events.jsonl")).unwrap();
        assert!(events.contains("analysis_result_ignored"));
        assert!(events.contains("analysis_rejected"));
        assert!(!events.contains("private-value"));
        assert!(!sessions.contains("private-value"));
        drop(stage);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn initialization_requires_complete_source_backed_handlers_from_this_session() {
        let dir = campaign_dir();
        let init = dir.join("init");
        fs::create_dir_all(&init).unwrap();
        fs::write(
            init.join("ready.json"),
            br#"{"status":"ready","session":"s"}"#,
        )
        .unwrap();
        let mut document = json!({"version": 1, "session": "s", "mappings": []});
        let write = |document: &Value| {
            fs::write(
                init.join("handlers.json"),
                serde_json::to_vec(document).unwrap(),
            )
            .unwrap()
        };
        write(&document);
        assert!(
            accept_initialized_handlers(&init, &dir.join("protocol.json"), "s")
                .unwrap_err()
                .contains("every model message")
        );
        document["mappings"] = json!([{"message":"m", "handlers":[{"symbol":"generic_dispatch", "location":"ftpserv.c:10"}]}]);
        write(&document);
        assert!(
            accept_initialized_handlers(&init, &dir.join("protocol.json"), "s")
                .unwrap_err()
                .contains("evidence")
        );
        document["mappings"][0]["handlers"][0]["evidence"] =
            json!("dispatch case m: generic_dispatch(ctx)");
        write(&document);
        let mappings = accept_initialized_handlers(&init, &dir.join("protocol.json"), "s").unwrap();
        assert_eq!(mappings[0]["step_ids"], json!(["1", "2"]));
        assert!(init.join("handlers.accepted.json").is_file());
        assert!(
            accept_initialized_handlers(&init, &dir.join("protocol.json"), "other")
                .unwrap_err()
                .contains("session")
        );
        document["mappings"][0]["handlers"] = json!([]);
        write(&document);
        assert!(
            accept_initialized_handlers(&init, &dir.join("protocol.json"), "s")
                .unwrap_err()
                .contains("reason")
        );
        document["mappings"][0]["reason"] =
            json!("message has no dispatch branch in this implementation");
        write(&document);
        accept_initialized_handlers(&init, &dir.join("protocol.json"), "s").unwrap();
        let _ = fs::remove_dir_all(&dir);
    }

    fn sample_measurements() -> (String, String) {
        let handlers = r#"{"version":1,"profile_sha256":"abc","mappings":[{"message":"m","handlers":[{"symbol":"ftpM"}],"unmatched":false}],"unmatched":[]}"#.to_string();
        let regions = r#"{"version":1,"profile_sha256":"abc","handlers":[{"handler":"m","regions":[],"unknown":[{"function":"ftpM","reason":"indirect"}],"zero":true}]}"#.to_string();
        (handlers, regions)
    }

    #[test]
    fn read_finished_rejects_a_verbal_result_without_measure_artifacts() {
        let dir = std::env::temp_dir().join(format!(
            "mcn-finished-missing-{}",
            TEMP.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        fs::write(
            dir.join("result.json"),
            br#"{"status":"ok","session":"s","request":"r","build":"b","snapshot":"000002","input_protocol":"p","diagnosis":"the handler is probably ftpM"}"#,
        )
        .unwrap();
        fs::write(
            dir.join("protocol-refined.json"),
            br#"{"step_dependencies":[]}"#,
        )
        .unwrap();
        fs::write(
            dir.join("priority-delta.json"),
            br#"{"steps":[{"step_id":"1","delta":1.0}]}"#,
        )
        .unwrap();
        let error = read_finished("r", &dir, &MeasurementSeal::default()).unwrap_err();
        assert!(error.contains("handlers.json"), "{error}");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn read_finished_rejects_rewritten_measure_artifacts() {
        let dir = std::env::temp_dir().join(format!(
            "mcn-finished-rewrite-{}",
            TEMP.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let (handlers, regions) = sample_measurements();
        fs::write(dir.join("handlers.json"), &handlers).unwrap();
        fs::write(dir.join("regions.json"), &regions).unwrap();
        let seal = seal_measurements(&dir).unwrap();
        fs::write(
            dir.join("regions.json"),
            br#"{"version":1,"profile_sha256":"abc","handlers":[]}"#,
        )
        .unwrap();
        fs::write(
            dir.join("result.json"),
            br#"{"status":"ok","snapshot":"000002"}"#,
        )
        .unwrap();
        let error = read_finished("r", &dir, &seal).unwrap_err();
        assert!(error.contains("no longer matches"), "{error}");
        fs::write(dir.join("regions.json"), &regions).unwrap();
        fs::write(
            dir.join("handlers.json"),
            br#"{"version":1,"profile_sha256":"abc","mappings":[{"message":"other","handlers":[]}]}"#,
        )
        .unwrap();
        let error = read_finished("r", &dir, &seal).unwrap_err();
        assert!(error.contains("no longer matches"), "{error}");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn read_finished_rejects_handler_changes_after_initialization() {
        let dir = std::env::temp_dir().join(format!(
            "mcn-finished-supplement-{}",
            TEMP.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let (handlers, regions) = sample_measurements();
        fs::write(dir.join("handlers.json"), &handlers).unwrap();
        fs::write(dir.join("regions.json"), &regions).unwrap();
        let seal = seal_measurements(&dir).unwrap();
        fs::write(
            dir.join("handlers.json"),
            br#"{"version":1,"profile_sha256":"abc","mappings":[{"message":"m","handlers":[{"symbol":"ftpM","location":"ftpserv.c:10","evidence":"dispatch calls ftpM"}]},{"message":"extra","handlers":[{"symbol":"ftpExtra","location":"ftpserv.c:20","evidence":"switch case"}]}]}"#,
        )
        .unwrap();
        fs::write(
            dir.join("result.json"),
            br#"{"status":"ok","snapshot":"000002"}"#,
        )
        .unwrap();
        let error = read_finished("r", &dir, &seal).unwrap_err();
        assert!(error.contains("no longer matches"), "{error}");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn read_finished_returns_the_sealed_tool_mappings() {
        let dir = std::env::temp_dir().join(format!(
            "mcn-finished-ok-{}",
            TEMP.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let (handlers, regions) = sample_measurements();
        fs::write(dir.join("handlers.json"), &handlers).unwrap();
        fs::write(dir.join("regions.json"), &regions).unwrap();
        let seal = seal_measurements(&dir).unwrap();
        fs::write(
            dir.join("result.json"),
            br#"{"status":"ok","session":"s","request":"r","build":"b","snapshot":"000002","input_protocol":"p"}"#,
        )
        .unwrap();
        fs::write(
            dir.join("protocol-refined.json"),
            br#"{"step_dependencies":[]}"#,
        )
        .unwrap();
        fs::write(
            dir.join("priority-delta.json"),
            br#"{"steps":[{"step_id":"11111111","delta":2.5,"reason":"uncovered"}]}"#,
        )
        .unwrap();
        let finished = read_finished("r", &dir, &seal).unwrap();
        assert_eq!(finished.snapshot, 2);
        assert_eq!(finished.handlers.len(), 1);
        assert_eq!(finished.regions.len(), 1);
        assert_eq!(finished.deltas.len(), 1);
        assert_eq!(
            finished.regions[0]["unknown"][0]["reason"].as_str(),
            Some("indirect")
        );
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn changed_source_fails_the_manifest_check_and_load_requires_source_dir() {
        let dir = campaign_dir();
        let campaign = Campaign::load(&dir.join("campaign.toml")).unwrap();
        verify_source_digest(&campaign).unwrap();
        fs::write(
            dir.join("src").join("ftpserv.c"),
            b"int ftpM(void){return 1;}",
        )
        .unwrap();
        let error = verify_source_digest(&campaign).unwrap_err();
        assert!(format!("{error:?}").contains("source_sha256"), "{error:?}");
        let text = fs::read_to_string(dir.join("campaign.toml")).unwrap();
        fs::write(
            dir.join("campaign.toml"),
            text.replace("source_dir = ", "# source_dir = "),
        )
        .unwrap();
        let error = Campaign::load(&dir.join("campaign.toml")).unwrap_err();
        assert!(
            format!("{error:?}").contains("target.source_dir"),
            "{error:?}"
        );
        let _ = fs::remove_dir_all(&dir);
    }
}
