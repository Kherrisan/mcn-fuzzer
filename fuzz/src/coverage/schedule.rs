//! Coverage-analysis scheduling driven by the MCP service.
//!
//! Rust no longer measures regions or maintains the protocol graph. It
//! registers a frozen snapshot with the service, asks the agent for a
//! proposal, validates the frozen file, commits the graph transaction, and
//! hands the proposal to synthesis.

use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use libafl::{
    stages::{Restartable, Stage},
    Error, HasMetadata,
};
use serde_json::{json, Value};

use super::{
    events::SharedLog,
    mcp::McpClient,
    proposal::{sha256_file, AcceptedProposal, Proposal, ProposalIdentity, ProposalMailbox},
};
use crate::{
    agents::{
        artifacts::{check_identity, read_json},
        command_line, current_boot, AgentClient, AgentCommand, AgentFailure, AgentInit,
        AgentOutcome, AgentSession, AgentTask, CommandLine, SessionInit, SessionPhase,
        WorkerConfig,
    },
    app::config::{AgentProvider, Campaign},
    protocol::{load_model, write_catalog, Binding, IndexedModel, PartyRole},
};

type Client = AgentClient<CoverageInit, AnalysisRequest>;
type Outcome = AgentOutcome<(), PreparedProposal>;

const INIT_PROMPT: &str = include_str!("../../prompts/coverage-analysis-init.md");
const TASK_PROMPT: &str = include_str!("../../prompts/coverage-analysis-task.md");

#[derive(Clone, Copy, PartialEq, Eq)]
enum Phase {
    Idle,
    Initializing,
    Running,
}

struct CoverageInit {
    ready: SessionInit,
    campaign: Box<Campaign>,
    model: IndexedModel,
    binding: Binding,
    handlers_path: PathBuf,
}

impl AgentInit for CoverageInit {
    type Output = ();
    fn output_dir(&self) -> &Path {
        &self.ready.output_dir
    }
    fn accept(&self) -> Result<(), String> {
        accept_handlers(self, &self.ready.output_dir)
    }
}

fn inbound_messages(model: &IndexedModel, binding: &Binding) -> BTreeSet<String> {
    model
        .steps
        .iter()
        .filter(|step| binding.role(&step.receiver_name) == PartyRole::Sut)
        .map(|step| step.message_name.clone())
        .collect()
}

/// Validate the initialized mapping and freeze an accepted copy for the service.
fn accept_handlers(context: &CoverageInit, output_dir: &Path) -> Result<(), String> {
    let value = read_json(&output_dir.join("handlers.json"))?;
    if value.get("version").and_then(Value::as_u64) != Some(1) {
        return Err("handlers.json must have version 1".into());
    }
    let mappings = value
        .get("mappings")
        .and_then(Value::as_array)
        .ok_or("handlers.json needs a mappings array")?;
    let by_hash: std::collections::BTreeMap<String, String> = context
        .model
        .steps
        .iter()
        .map(|step| (step.id().0, step.message_name.clone()))
        .collect();
    let mut covered = BTreeSet::new();
    for row in mappings {
        let message = row
            .get("message")
            .and_then(Value::as_str)
            .ok_or("handler mapping has no message")?;
        covered.insert(message.to_string());
        let handlers = row
            .get("handlers")
            .and_then(Value::as_array)
            .ok_or_else(|| format!("handler mapping for {message} has no handlers array"))?;
        let reason = row.get("reason").and_then(Value::as_str).unwrap_or("");
        if handlers.is_empty() && reason.trim().is_empty() {
            return Err(format!(
                "handler mapping for {message} needs a handler or an unmatched reason"
            ));
        }
        for handler in handlers {
            let symbol = handler
                .get("symbol")
                .and_then(Value::as_str)
                .ok_or_else(|| format!("handler for {message} has no symbol"))?;
            if symbol.trim().is_empty() {
                return Err(format!("handler for {message} has an empty symbol"));
            }
        }
        for step in row
            .get("step_ids")
            .and_then(Value::as_array)
            .ok_or_else(|| format!("handler mapping for {message} has no step_ids"))?
        {
            let step = step.as_str().unwrap_or("");
            let Some(expected) = by_hash.get(step) else {
                return Err(format!(
                    "handler mapping for {message} references unknown step {step}"
                ));
            };
            if expected != message {
                return Err(format!(
                    "handler mapping for {message} references step {step} of message {expected}"
                ));
            }
        }
    }
    for message in inbound_messages(&context.model, &context.binding) {
        if !covered.contains(&message) {
            return Err(format!(
                "handlers.json does not cover inbound message {message}"
            ));
        }
    }
    fs::copy(output_dir.join("handlers.json"), &context.handlers_path)
        .map_err(|error| format!("accepted handlers copy: {error}"))?;
    Ok(())
}

/// Proposal that passed identity and reference checks in the worker thread.
struct PreparedProposal {
    proposal: Proposal,
    path: PathBuf,
    sha256: String,
}

struct AnalysisRequest {
    id: String,
    snapshot: u64,
    session: String,
    build: String,
    protocol_id: String,
    output_dir: PathBuf,
    model: IndexedModel,
    binding: Binding,
}

impl AgentTask for AnalysisRequest {
    type Prepared = ();
    type Output = PreparedProposal;
    fn id(&self) -> &str {
        &self.id
    }
    fn output_dir(&self) -> &Path {
        &self.output_dir
    }
    fn prepare(&self) -> Result<(), String> {
        Ok(())
    }
    fn accept(&self, (): ()) -> Result<PreparedProposal, String> {
        let result = read_json(&self.output_dir.join("result.json"))?;
        check_identity(
            &result,
            &[
                ("request_id", &self.id),
                ("session", &self.session),
                ("build", &self.build),
                ("snapshot", &format!("{:06}", self.snapshot)),
                ("protocol_id", &self.protocol_id),
            ],
        )?;
        match result.get("status").and_then(Value::as_str).unwrap_or("") {
            "ok" => {}
            "no_proposal" => {
                return Err(format!(
                    "analysis produced no proposal: {}",
                    result.get("error").and_then(Value::as_str).unwrap_or("")
                ))
            }
            other => {
                return Err(format!(
                    "analysis status {other}: {}",
                    result.get("error").and_then(Value::as_str).unwrap_or("")
                ))
            }
        }
        let path = self.output_dir.join("proposal.json");
        let proposal = Proposal::read(&path)?;
        let identity = ProposalIdentity {
            session: self.session.clone(),
            request_id: self.id.clone(),
            build: self.build.clone(),
            snapshot: format!("{:06}", self.snapshot),
            protocol_id: self.protocol_id.clone(),
        };
        proposal.validate(&self.model, &self.binding, &identity)?;
        let sha256 = sha256_file(&path)?;
        if result.get("proposal_id").and_then(Value::as_str) != Some(proposal.proposal_id.as_str())
        {
            return Err("result.json proposal_id does not match proposal.json".into());
        }
        Ok(PreparedProposal {
            proposal,
            path,
            sha256,
        })
    }
}

struct Inflight {
    request: String,
    snapshot: u64,
    output_dir: PathBuf,
}

/// Stage that polls MCP analysis without blocking the mutational loop.
pub struct CoverageAnalysisStage {
    enabled: bool,
    campaign: Campaign,
    model: Option<IndexedModel>,
    binding: Binding,
    sut_parties: Vec<String>,
    session: String,
    fingerprint: String,
    analysis_root: PathBuf,
    catalog: PathBuf,
    handlers_path: PathBuf,
    log: Option<SharedLog>,
    mailbox: ProposalMailbox,
    mcp: Option<McpClient>,
    initialized: bool,
    init_started: bool,
    init_timeouts: u32,
    phase: Phase,
    inflight: Option<Inflight>,
    pending: Option<u64>,
    /// Newest snapshot that arrived before initialization finished.
    skipped_snapshot: Option<u64>,
    seen_snapshots: BTreeSet<u64>,
    worker: Option<Client>,
    launch: Option<CommandLine>,
    session_state: AgentSession,
    secrets: Vec<String>,
    exited: bool,
}

impl CoverageAnalysisStage {
    pub fn from_campaign(campaign: &Campaign, mailbox: ProposalMailbox) -> Result<Self, Error> {
        if !campaign.coverage.analysis_enabled {
            return Ok(Self::disabled(campaign, mailbox));
        }
        let boot = current_boot().ok_or_else(|| {
            Error::unknown(
                "coverage analysis is enabled but the private HC core was not started".to_string(),
            )
        })?;
        let model = load_model(&campaign.protocol_generation.model)?;
        let binding = Binding::new(
            campaign
                .parties
                .as_ref()
                .ok_or_else(|| Error::illegal_argument("coverage analysis requires [parties]"))?
                .resolve(&model.party_names())?,
        );
        let fingerprint = if campaign.coverage.binary.is_file() {
            super::build_fingerprint(
                &campaign.coverage.binary,
                &campaign.coverage.llvm_profdata,
                &campaign.coverage.llvm_cov,
            )?
        } else {
            return Err(Error::illegal_argument(
                "coverage analysis needs the coverage-built SUT binary".to_string(),
            ));
        };
        let analysis_root = campaign.coverage.output.join(&fingerprint).join("analysis");
        fs::create_dir_all(analysis_root.join("requests"))?;
        fs::create_dir_all(analysis_root.join("init"))?;
        let catalog = analysis_root.join("catalog.json");
        write_catalog(&model, &binding, &catalog)?;
        let handlers_path = analysis_root.join("init").join("handlers.accepted.json");
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
        if profile.provider != AgentProvider::Codex {
            return Err(Error::illegal_argument(format!(
                "coverage analysis profile {} uses {:?}; stdio MCP requires the codex provider",
                campaign.coverage.analysis_profile, profile.provider
            )));
        }
        let launch = command_line(profile, &std::path::absolute(analysis_root.join("init"))?)?;
        let launch = mcp_launch(launch, campaign)?;
        let secrets = launch.secrets();
        let log = SharedLog::open(
            analysis_root.join("events.jsonl"),
            boot.session.clone(),
            fingerprint.clone(),
        );
        log.append(
            "analysis_ready",
            json!({
                "coverage_output": campaign.coverage.output,
                "callgraph": campaign.coverage.callgraph_dot,
                "cfg_dir": campaign.coverage.cfg_dir,
                "manifest": campaign.coverage.build_manifest,
                "mcp_socket": crate::coverage::mcp::socket_path(),
            }),
        );
        let sut_parties = binding
            .entries()
            .filter(|(_, role)| *role == PartyRole::Sut)
            .map(|(name, _)| name.to_string())
            .collect();
        let mut stage = Self {
            enabled: true,
            campaign: campaign.clone(),
            model: Some(model),
            binding,
            sut_parties,
            session: boot.session.clone(),
            fingerprint,
            analysis_root,
            catalog,
            handlers_path,
            log: Some(log),
            mailbox,
            mcp: Some(McpClient::connect()?),
            initialized: false,
            init_started: false,
            init_timeouts: 0,
            phase: Phase::Idle,
            inflight: None,
            pending: None,
            skipped_snapshot: None,
            seen_snapshots: BTreeSet::new(),
            worker: None,
            launch: Some(launch),
            session_state: AgentSession::default(),
            secrets,
            exited: false,
        };
        stage.spawn_worker(boot.socket)?;
        Ok(stage)
    }

    fn disabled(campaign: &Campaign, mailbox: ProposalMailbox) -> Self {
        Self {
            enabled: false,
            campaign: campaign.clone(),
            model: None,
            binding: Binding::default(),
            sut_parties: Vec::new(),
            session: String::new(),
            fingerprint: String::new(),
            analysis_root: PathBuf::new(),
            catalog: PathBuf::new(),
            handlers_path: PathBuf::new(),
            log: None,
            mailbox,
            mcp: None,
            initialized: false,
            init_started: false,
            init_timeouts: 0,
            phase: Phase::Idle,
            inflight: None,
            pending: None,
            skipped_snapshot: None,
            seen_snapshots: BTreeSet::new(),
            worker: None,
            launch: None,
            session_state: AgentSession::default(),
            secrets: Vec::new(),
            exited: false,
        }
    }

    fn spawn_worker(&mut self, socket: PathBuf) -> Result<(), Error> {
        let terminal = crate::agents::campaign_terminal_name("coverage", &self.campaign.base);
        let client = Client::spawn(WorkerConfig {
            thread_name: "coverage-analysis",
            socket,
            terminal,
            init_timeout: Duration::from_secs(
                self.campaign.coverage.analysis_init_timeout_secs.max(1),
            ),
            task_timeout: Duration::from_secs(
                self.campaign.coverage.analysis_task_timeout_secs.max(1),
            ),
            secrets: self.secrets.clone(),
        })
        .map_err(|error| Error::unknown(format!("starting coverage worker: {error}")))?;
        self.worker = Some(client);
        Ok(())
    }

    pub fn event_log(&self) -> Option<SharedLog> {
        self.log.clone()
    }

    pub fn mailbox(&self) -> ProposalMailbox {
        self.mailbox.clone()
    }

    pub fn snapshot_dir(&self, snapshot: u64) -> PathBuf {
        self.analysis_root
            .parent()
            .expect("analysis root has a parent")
            .join("snapshots")
            .join(format!("{snapshot:06}"))
    }

    pub fn initialize(&mut self) -> Result<(), Error> {
        if !self.enabled || self.init_started || self.initialized {
            return Ok(());
        }
        let output_dir = self.analysis_root.join("init");
        fs::create_dir_all(&output_dir)?;
        for name in ["ready.json", "handlers.json", "handlers.accepted.json"] {
            let path = output_dir.join(name);
            if path.exists() {
                fs::remove_file(&path)?;
            }
        }
        let source = self.campaign.target.source_dir.as_ref().ok_or_else(|| {
            Error::illegal_argument("coverage analysis requires target.source_dir")
        })?;
        let model = self.model.as_ref().expect("enabled stage has a model");
        let prompt = render_init(self, model, source, &output_dir);
        let launch = self
            .launch
            .clone()
            .ok_or_else(|| Error::illegal_argument("analysis profile did not produce a command"))?;
        self.worker
            .as_ref()
            .ok_or_else(|| Error::unknown("analysis worker is not running"))?
            .send(AgentCommand::Init {
                context: CoverageInit {
                    ready: SessionInit {
                        output_dir,
                        session: self.session.clone(),
                    },
                    campaign: Box::new(self.campaign.clone()),
                    model: model.clone(),
                    binding: self.binding.clone(),
                    handlers_path: self.handlers_path.clone(),
                },
                prompt,
                launch,
            })
            .map_err(|_| Error::unknown("analysis worker stopped"))?;
        self.init_started = true;
        self.phase = Phase::Initializing;
        self.log_event("analysis_initializing", json!({}));
        Ok(())
    }

    /// Remember the newest unpublished snapshot. Does not interrupt an active request.
    pub fn note_snapshot(&mut self, snapshot: u64) -> Result<(), Error> {
        if !self.enabled || snapshot == 0 || !self.seen_snapshots.insert(snapshot) {
            return Ok(());
        }
        if !self.initialized {
            // Initialization can take minutes. Keep the newest snapshot so the
            // first analysis request can start immediately after ready.
            let replaced = self.skipped_snapshot.replace(snapshot);
            self.log_event(
                "analysis_snapshot_skipped_initializing",
                json!({"snapshot": snapshot, "replaced": replaced}),
            );
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
        );
        Ok(())
    }

    fn dispatch(&mut self) -> Result<(), Error> {
        let Some(snapshot) = self.pending.take() else {
            return Ok(());
        };
        let Some(model) = self.model.as_ref() else {
            return Ok(());
        };
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|value| value.as_nanos())
            .unwrap_or(0);
        let request = format!("{snapshot:06}-{nonce:x}");
        let output_dir = self.analysis_root.join("requests").join(&request);
        fs::create_dir_all(&output_dir)?;
        let snapshot_dir = self.snapshot_dir(snapshot);
        let identity = json!({
            "request_id": request,
            "session": self.session,
            "build": self.fingerprint,
            "snapshot": format!("{snapshot:06}"),
            "protocol_id": model.protocol_id,
        });
        fs::write(
            output_dir.join("request.json"),
            format!(
                "{}\n",
                serde_json::to_string_pretty(&json!({
                    "identity": identity,
                    "snapshot_dir": snapshot_dir,
                    "manifest": self.campaign.coverage.build_manifest,
                    "model": self.campaign.protocol_generation.model,
                    "catalog": self.catalog,
                    "handlers": self.handlers_path,
                    "output_dir": output_dir,
                }))
                .map_err(|error| Error::unknown(error.to_string()))?
            ),
        )?;
        let mcp = self
            .mcp
            .as_ref()
            .ok_or_else(|| Error::unknown("coverage MCP client is missing"))?;
        mcp.register(&json!({
            "request_id": request,
            "session": self.session,
            "build": self.fingerprint,
            "snapshot": format!("{snapshot:06}"),
            "protocol_id": model.protocol_id,
            "output_dir": output_dir,
            "snapshot_dir": snapshot_dir,
            "manifest": self.campaign.coverage.build_manifest,
            "model": self.campaign.protocol_generation.model,
            "catalog": self.catalog,
            "handlers": self.handlers_path,
            "source_dir": self.campaign.target.source_dir,
        }))?;
        let prompt = render_task(self, model, &request, &output_dir, snapshot, &snapshot_dir);
        self.worker
            .as_ref()
            .ok_or_else(|| Error::unknown("analysis worker is not running"))?
            .send(AgentCommand::Run {
                request: AnalysisRequest {
                    id: request.clone(),
                    snapshot,
                    session: self.session.clone(),
                    build: self.fingerprint.clone(),
                    protocol_id: model.protocol_id.clone(),
                    output_dir: output_dir.clone(),
                    model: model.clone(),
                    binding: self.binding.clone(),
                },
                prompt,
            })
            .map_err(|_| Error::unknown("analysis worker stopped"))?;
        self.phase = Phase::Running;
        self.inflight = Some(Inflight {
            request: request.clone(),
            snapshot,
            output_dir,
        });
        self.log_event(
            "analysis_dispatched",
            json!({"request": request, "snapshot": snapshot}),
        );
        Ok(())
    }

    fn accept_artifact(&mut self, request: String, result: Result<PreparedProposal, String>) {
        let Some(inflight) = self.inflight.take() else {
            return;
        };
        self.phase = Phase::Idle;
        if inflight.request != request {
            self.log_event(
                "analysis_result_ignored",
                json!({"request": request, "expected": inflight.request}),
            );
            return;
        }
        let proposal = match result {
            Ok(proposal) => proposal,
            Err(reason) => {
                self.abort(&inflight.request);
                self.log_event(
                    "analysis_failed",
                    json!({"request": inflight.request, "reason": reason}),
                );
                return;
            }
        };
        let Some(mcp) = self.mcp.as_ref() else {
            return;
        };
        match mcp.commit(
            &inflight.request,
            &proposal.proposal.proposal_id,
            &proposal.sha256,
        ) {
            Ok(committed) => {
                let accepted = AcceptedProposal {
                    proposal: proposal.proposal.clone(),
                    path: proposal.path.clone(),
                    sha256: proposal.sha256.clone(),
                };
                let replaced = self.mailbox.replace(accepted);
                self.log_event(
                    "proposal_accepted",
                    json!({
                        "request": inflight.request,
                        "snapshot": inflight.snapshot,
                        "proposal_id": proposal.proposal.proposal_id,
                        "trace_id": proposal.proposal.trace_id,
                        "goal_handlers": proposal.proposal.goal.handler_ids,
                        "steps": proposal.proposal.trace.iter().map(|step| step.step_id.clone()).collect::<Vec<_>>(),
                        "replaced": replaced.map(|item| item.proposal.proposal_id),
                        "commit": committed,
                    }),
                );
            }
            Err(error) => {
                self.abort(&inflight.request);
                self.log_event(
                    "proposal_commit_failed",
                    json!({"request": inflight.request, "reason": error.to_string()}),
                );
            }
        }
    }

    fn abort(&self, request: &str) {
        if let Some(mcp) = &self.mcp {
            if let Err(error) = mcp.abort(request) {
                log::debug!("coverage analysis abort {request}: {error}");
            }
        }
    }

    fn log_event(&self, kind: &str, body: Value) {
        if let Some(log) = &self.log {
            log.append(kind, body);
        }
    }

    fn consume_failure(&mut self, failure: AgentFailure) -> Result<(), Error> {
        match self.phase {
            Phase::Initializing => {
                self.init_started = false;
                self.init_timeouts += 1;
                self.log_event(
                    "analysis_init_failed",
                    json!({"attempt": self.init_timeouts, "reason": failure.reason}),
                );
                if self.init_timeouts >= crate::agents::INIT_TIMEOUT_ATTEMPTS {
                    return Err(Error::unknown(format!(
                        "coverage analysis initialization timed out {} times: {}",
                        self.init_timeouts, failure.reason
                    )));
                }
            }
            Phase::Running => {
                let inflight = self.inflight.take();
                self.phase = Phase::Idle;
                if let Some(inflight) = inflight {
                    self.abort(&inflight.request);
                    self.log_event(
                        "analysis_failed",
                        json!({"request": inflight.request, "reason": failure.reason}),
                    );
                }
            }
            Phase::Idle => {
                self.log_event("analysis_worker_failed", json!({"reason": failure.reason}));
            }
        }
        Ok(())
    }

    pub fn shutdown(&mut self) {
        if !self.enabled || self.exited {
            return;
        }
        self.exited = true;
        self.worker.take();
        self.log_event("analysis_stopped", json!({}));
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
        if let Ok(progress) = state.metadata::<super::SourceCoverageProgress>() {
            self.note_snapshot(progress.snapshot)?;
        }
        while let Some(outcome) = self
            .worker
            .as_mut()
            .and_then(|worker| worker.recv_timeout(Duration::from_millis(1)))
        {
            match outcome {
                AgentOutcome::Failed(failure) => self.consume_failure(failure)?,
                AgentOutcome::Artifact {
                    request, result, ..
                } => self.accept_artifact(request, result),
                AgentOutcome::Ready((), fact) => {
                    self.initialized = true;
                    self.init_started = false;
                    self.init_timeouts = 0;
                    self.phase = Phase::Idle;
                    if self.pending.is_none() {
                        if let Some(snapshot) = self.skipped_snapshot.take() {
                            self.pending = Some(snapshot);
                            self.log_event(
                                "analysis_snapshot_requeued",
                                json!({"snapshot": snapshot}),
                            );
                        }
                    }
                    self.session_state = AgentSession {
                        phase: SessionPhase::Ready,
                        transition: "ready".into(),
                        terminal: fact.terminal.clone(),
                        hc_state: fact.state.clone(),
                        hc_status: fact.status.clone(),
                        hc_error: fact.error.clone(),
                    };
                    self.log_event("analysis_initialized", json!({"terminal": fact.terminal}));
                }
                AgentOutcome::Launched(fact) => {
                    self.log_event("analysis_launched", json!({"terminal": fact.terminal}));
                }
            }
        }
        if !self.initialized && !self.init_started {
            self.initialize()?;
        }
        if self.initialized && self.inflight.is_none() {
            self.dispatch()?;
        }
        Ok(())
    }
}

fn mcp_launch(mut launch: CommandLine, campaign: &Campaign) -> Result<CommandLine, Error> {
    let socket = crate::coverage::mcp::socket_path().ok_or_else(|| {
        Error::unknown(
            "coverage analysis is enabled but the MCP service is unavailable".to_string(),
        )
    })?;
    let bridge = std::path::absolute(
        campaign
            .campaign
            .pythonpath
            .join("coverage_analysis")
            .join("server.py"),
    )?;
    let args = serde_json::json!([
        bridge.display().to_string(),
        "stdio",
        "--socket",
        socket.display().to_string()
    ]);
    let python = std::path::absolute(&campaign.campaign.python)?;
    let command = serde_json::to_string(&python.display().to_string())
        .map_err(|error| Error::unknown(error.to_string()))?;
    launch.argv.extend([
        "-c".to_string(),
        format!("mcp_servers.coverage.command={command}"),
        "-c".to_string(),
        format!("mcp_servers.coverage.args={args}"),
    ]);
    Ok(launch)
}

fn render_init(
    stage: &CoverageAnalysisStage,
    model: &IndexedModel,
    source: &Path,
    output_dir: &Path,
) -> String {
    let steps: Vec<_> = model
        .steps
        .iter()
        .map(|step| {
            json!({
                "step_id": step.id().0,
                "name": step.name,
                "message": step.message_name,
                "channel": step.channel_name,
                "sender": step.sender_name,
                "receiver": step.receiver_name,
                "condition": step.condition,
            })
        })
        .collect();
    INIT_PROMPT
        .replace("{{ session_id }}", &stage.session)
        .replace(
            "{{ campaign.workspace }}",
            &output_dir.display().to_string(),
        )
        .replace("{{ source_dir }}", &source.display().to_string())
        .replace(
            "{{ model }}",
            &stage
                .campaign
                .protocol_generation
                .model
                .display()
                .to_string(),
        )
        .replace("{{ catalog }}", &stage.catalog.display().to_string())
        .replace("{{ sut_parties }}", &json!(stage.sut_parties).to_string())
        .replace("{{ steps }}", &json!(steps).to_string())
        .replace(
            "{{ handlers_path }}",
            &stage.handlers_path.display().to_string(),
        )
        .replace(
            "{{ execution.summary }}",
            "Coverage analysis reads the SUT source directory and protocol model.",
        )
}

fn render_task(
    stage: &CoverageAnalysisStage,
    model: &IndexedModel,
    request: &str,
    output_dir: &Path,
    snapshot: u64,
    snapshot_dir: &Path,
) -> String {
    TASK_PROMPT
        .replace("{{ session_id }}", &stage.session)
        .replace("{{ request_id }}", request)
        .replace("{{ output_dir }}", &output_dir.display().to_string())
        .replace("{{ snapshot }}", &format!("{snapshot:06}"))
        .replace("{{ snapshot_dir }}", &snapshot_dir.display().to_string())
        .replace("{{ build }}", &stage.fingerprint)
        .replace(
            "{{ manifest }}",
            &stage.campaign.coverage.build_manifest.display().to_string(),
        )
        .replace(
            "{{ callgraph_dot }}",
            &stage.campaign.coverage.callgraph_dot.display().to_string(),
        )
        .replace(
            "{{ cfg_dir }}",
            &stage.campaign.coverage.cfg_dir.display().to_string(),
        )
        .replace(
            "{{ binary }}",
            &stage.campaign.coverage.binary.display().to_string(),
        )
        .replace(
            "{{ model }}",
            &stage
                .campaign
                .protocol_generation
                .model
                .display()
                .to_string(),
        )
        .replace("{{ catalog }}", &stage.catalog.display().to_string())
        .replace("{{ handlers }}", &stage.handlers_path.display().to_string())
        .replace("{{ protocol_id }}", &model.protocol_id)
        .replace(
            "{{ source_dir }}",
            &stage
                .campaign
                .target
                .source_dir
                .as_ref()
                .map(|path| path.display().to_string())
                .unwrap_or_default(),
        )
}
