//! Coverage-analysis scheduling driven by the MCP service.
//!
//! Rust no longer measures regions or maintains the protocol graph. It
//! registers a frozen snapshot with the service, asks the agent for a
//! proposal. Python validates that file in submit_proposal. Rust checks the
//! result identity, hashes the frozen bytes, commits the graph transaction,
//! and hands the proposal to synthesis.

use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use libafl::{
    stages::{Restartable, Stage},
    Error, HasMetadata,
};
use serde_json::{json, Value};

use super::{
    events::SharedLog,
    introspection::{http::IntrospectionHttp, CampaignIdentity, IntrospectionHandle, ProposalId},
    mcp::McpClient,
    proposal::{sha256_file, AcceptedProposal, Proposal, ProposalMailbox},
};
use crate::{
    agents::{
        artifacts::{check_identity, read_json},
        command_line, current_boot, AgentClient, AgentCommand, AgentFailure, AgentInit,
        AgentOutcome, AgentTask, CommandLine, SessionInit, WorkerConfig,
    },
    app::config::{AgentProvider, Campaign},
    engine::stagnation::BitmapProgress,
    protocol::{load_model, Binding, IndexedModel, PartyRole},
};

type Client = AgentClient<CoverageInit, AnalysisRequest>;

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
        .filter(|step| {
            step.receiver_name()
                .is_some_and(|name| binding.role(name) == PartyRole::Sut)
        })
        .filter_map(|step| step.message_name().map(str::to_string))
        .collect()
}

/// Validate the initialized mapping and freeze an accepted copy for the service.
fn accept_handlers(context: &CoverageInit, output_dir: &Path) -> Result<(), String> {
    let value = read_json(&output_dir.join("handlers.json"))?;
    let mappings = value
        .get("mappings")
        .and_then(Value::as_array)
        .ok_or("handlers.json needs a mappings array")?;
    let by_name: std::collections::BTreeMap<String, String> = context
        .model
        .steps
        .iter()
        .filter_map(|step| {
            step.message_name()
                .map(|message| (step.name.clone(), message.to_string()))
        })
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
        if row.get("step_ids").is_some() {
            return Err(format!(
                "handler mapping for {message} uses removed step_ids; use step_names"
            ));
        }
        for step in row
            .get("step_names")
            .and_then(Value::as_array)
            .ok_or_else(|| format!("handler mapping for {message} has no step_names"))?
        {
            let step = step.as_str().unwrap_or("");
            let Some(expected) = by_name.get(step) else {
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
    output_dir: PathBuf,
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
            ],
        )?;
        match result.get("status").and_then(Value::as_str).unwrap_or("") {
            "ok" => {}
            "no_proposal" => {
                return Err(format!(
                    "analysis produced no proposal: {}",
                    result.get("error").and_then(Value::as_str).unwrap_or("")
                ));
            }
            other => {
                return Err(format!(
                    "analysis status {other}: {}",
                    result.get("error").and_then(Value::as_str).unwrap_or("")
                ));
            }
        }
        let path = self.output_dir.join("proposal.json");
        let proposal = Proposal::read(&path)?;
        let sha256 = sha256_file(&path)?;
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
}

/// Stage that polls MCP analysis without blocking the mutational loop.
pub struct CoverageAnalysisStage {
    enabled: bool,
    campaign: Campaign,
    model: Option<IndexedModel>,
    binding: Binding,
    sut_parties: Vec<String>,
    generator_parties: Vec<String>,
    unavailable_parties: Vec<String>,
    session: String,
    fingerprint: String,
    analysis_root: PathBuf,
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
    retried_snapshot: Option<u64>,
    /// Newest snapshot that arrived before initialization finished.
    skipped_snapshot: Option<u64>,
    seen_snapshots: BTreeSet<u64>,
    worker: Option<Client>,
    launch: Option<CommandLine>,
    secrets: Vec<String>,
    exited: bool,
    introspection: Option<IntrospectionHandle>,
    introspection_http: Option<IntrospectionHttp>,
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
        let model = load_model(&campaign.generator_synthesis.model)?;
        let binding = Binding::new(
            campaign
                .parties
                .as_ref()
                .ok_or_else(|| Error::illegal_argument("coverage analysis requires [parties]"))?
                .resolve(&model.party_names())?,
        );
        if !campaign.coverage.binary.is_file() {
            return Err(Error::illegal_argument(
                "coverage analysis needs the coverage-built SUT binary".to_string(),
            ));
        }
        let fingerprint = "source-coverage".to_string();
        let analysis_root = campaign.coverage.output.join("analysis");
        fs::create_dir_all(analysis_root.join("requests"))?;
        fs::create_dir_all(analysis_root.join("init"))?;
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
        // Codex workspace-write uses this process cwd. init/ and requests/ are
        // both children, so a later task can publish requests/<id>/result.json.
        let introspection = IntrospectionHandle::open(
            campaign_identity(campaign, &boot.session),
            campaign.coverage.index_map.as_deref(),
        );
        let listener = crate::coverage::introspection::take_host_listener().ok_or_else(|| {
            Error::unknown(
                "coverage analysis is enabled but the introspection listener was not bound on 127.0.0.1 before CLONE_NEWNET"
                    .to_string(),
            )
        })?;
        introspection.set_step_model(&model);
        let introspection_http = IntrospectionHttp::start(introspection.store(), listener)
            .map_err(|error| {
                Error::unknown(format!("starting campaign introspection MCP: {error}"))
            })?;
        let launch = command_line(
            profile,
            &std::path::absolute(analysis_agent_cwd(&analysis_root))?,
        )?;
        let launch = mcp_launch(launch, campaign, introspection_http.url())?;
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
                "mcp_socket": crate::coverage::mcp::socket_path(),
                "introspection_url": introspection_http.url(),
            }),
        );
        let mut sut_parties = Vec::new();
        let mut generator_parties = Vec::new();
        let mut unavailable_parties = Vec::new();
        for (name, role) in binding.entries() {
            match role {
                PartyRole::Sut => sut_parties.push(name.to_string()),
                PartyRole::Generator => generator_parties.push(name.to_string()),
                PartyRole::Unavailable => unavailable_parties.push(name.to_string()),
            }
        }
        // Campaign-wide register facts belong to the service, not to each
        // request. Write them before the client starts the service.
        let session_path = analysis_root.join("session.json");
        fs::write(
            &session_path,
            serde_json::to_vec_pretty(&json!({
                "session": boot.session,
                "build": fingerprint,
                "model": campaign.generator_synthesis.model,
                "handlers": handlers_path,
                "llvm_cov": campaign.coverage.llvm_cov,
                "source_dir": campaign.target.source_dir,
                "sut_parties": sut_parties,
                "generator_parties": generator_parties,
                "unavailable_parties": unavailable_parties,
                "configure_options": campaign.target.configure_options,
                "cli_args": campaign.target.cli_args,
            }))
            .map_err(|error| Error::unknown(format!("serialising session config: {error}")))?,
        )
        .map_err(|error| Error::unknown(format!("writing session config: {error}")))?;
        let mut stage = Self {
            enabled: true,
            campaign: campaign.clone(),
            model: Some(model),
            binding,
            sut_parties,
            generator_parties,
            unavailable_parties,
            session: boot.session.clone(),
            fingerprint,
            analysis_root,
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
            retried_snapshot: None,
            skipped_snapshot: None,
            seen_snapshots: BTreeSet::new(),
            worker: None,
            launch: Some(launch),
            secrets,
            exited: false,
            introspection: Some(introspection),
            introspection_http: Some(introspection_http),
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
            generator_parties: Vec::new(),
            unavailable_parties: Vec::new(),
            session: String::new(),
            fingerprint: String::new(),
            analysis_root: PathBuf::new(),
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
            retried_snapshot: None,
            skipped_snapshot: None,
            seen_snapshots: BTreeSet::new(),
            worker: None,
            launch: None,
            secrets: Vec::new(),
            exited: false,
            introspection: None,
            introspection_http: None,
        }
    }

    pub(crate) fn introspection_handle(&self) -> Option<IntrospectionHandle> {
        self.introspection.clone()
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
        if self.model.is_none() {
            return Ok(());
        }
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|value| value.as_nanos())
            .unwrap_or(0);
        let request = format!("{snapshot:06}-{nonce:x}");
        let output_dir = self.analysis_root.join("requests").join(&request);
        fs::create_dir_all(&output_dir)?;
        let snapshot_dir = self.snapshot_dir(snapshot);
        let (sut_stdout_log, sut_stderr_log) = snapshot_sut_logs(&snapshot_dir, snapshot)?;
        let identity = json!({
            "request_id": request,
            "session": self.session,
            "build": self.fingerprint,
            "snapshot": format!("{snapshot:06}"),
        });
        fs::write(
            output_dir.join("request.json"),
            format!(
                "{}\n",
                serde_json::to_string_pretty(&json!({
                    "identity": identity,
                    "snapshot_dir": snapshot_dir,
                    "sut_stdout_log": sut_stdout_log,
                    "sut_stderr_log": sut_stderr_log,
                    "model": self.campaign.generator_synthesis.model,
                    "handlers": self.handlers_path,
                    "configure_options": self.campaign.target.configure_options,
                    "cli_args": self.campaign.target.cli_args,
                    "output_dir": output_dir,
                }))
                .map_err(|error| Error::unknown(error.to_string()))?
            ),
        )?;
        let mcp = self
            .mcp
            .as_ref()
            .ok_or_else(|| Error::unknown("coverage MCP client is missing"))?;
        // Only per-request facts travel here: session, build, model, handlers,
        // parties, and llvm-cov were read by the service at startup from
        // analysis/session.json.
        mcp.register(&json!({
            "request_id": request,
            "snapshot": format!("{snapshot:06}"),
            "output_dir": output_dir,
            "snapshot_dir": snapshot_dir,
        }))?;
        let prompt = render_task(
            self,
            &request,
            &output_dir,
            snapshot,
            &snapshot_dir,
            &sut_stdout_log,
            &sut_stderr_log,
        );
        if let Some(handle) = &self.introspection {
            if let Err(error) =
                handle.register_coverage(&request, &self.session, &format!("{snapshot:06}"))
            {
                log::warn!("introspection register {request}: {error}");
            }
        }
        crate::evaluation::request_eval::capture(
            &output_dir, &prompt, Some(mcp), self.introspection.as_ref(),
        );
        if self
            .worker
            .as_ref()
            .ok_or_else(|| Error::unknown("analysis worker is not running"))?
            .send(AgentCommand::Run {
                request: AnalysisRequest {
                    id: request.clone(),
                    snapshot,
                    session: self.session.clone(),
                    build: self.fingerprint.clone(),
                    output_dir: output_dir.clone(),
                },
                prompt,
            })
            .is_err()
        {
            if let Some(handle) = &self.introspection {
                let _ = handle.mark_failed(&request);
            }
            return Err(Error::unknown("analysis worker stopped"));
        }
        self.phase = Phase::Running;
        self.inflight = Some(Inflight {
            request: request.clone(),
            snapshot,
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
                self.fail_request(&inflight.request);
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
        match mcp.commit(&inflight.request, &proposal.sha256) {
            Ok(committed) => {
                let accepted = AcceptedProposal {
                    proposal: proposal.proposal.clone(),
                    path: proposal.path.clone(),
                    request_id: inflight.request.clone(),
                };
                if let Some(handle) = &self.introspection {
                    if let Err(error) = handle.accept(
                        &ProposalId::coverage(&inflight.request),
                        Arc::new(proposal.proposal.clone()),
                    ) {
                        log::warn!("introspection accept {}: {error}", inflight.request);
                    }
                }
                let replaced = self.mailbox.replace(accepted);
                if let Some(replaced) = &replaced {
                    if let Some(handle) = &self.introspection {
                        if let Err(error) =
                            handle.mark_skipped(&ProposalId::coverage(&replaced.request_id))
                        {
                            log::warn!(
                                "introspection skip replaced {}: {error}",
                                replaced.request_id
                            );
                        }
                    }
                }
                self.log_event(
                    "proposal_accepted",
                    json!({
                        "request": inflight.request,
                        "snapshot": inflight.snapshot,
                        "goal_handlers": proposal.proposal.goal.handler_ids,
                        "steps": proposal.proposal.trace.iter().map(|step| step.name.clone()).collect::<Vec<_>>(),
                        "replaced": replaced.map(|item| item.request_id),
                        "commit": committed,
                    }),
                );
            }
            Err(error) => {
                self.fail_request(&inflight.request);
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

    fn consume_init_timeout(&mut self, failure: AgentFailure) -> Result<(), Error> {
        self.init_timeouts += 1;
        self.log_event(
            "analysis_init_timeout",
            json!({"count": self.init_timeouts, "reason": failure.reason}),
        );
        if self.init_timeouts >= crate::agents::INIT_TIMEOUT_LIMIT {
            return Err(Error::unknown(format!(
                "coverage analysis initialization timed out {} times: {}",
                self.init_timeouts, failure.reason
            )));
        }
        Ok(())
    }

    fn consume_failure(&mut self, failure: AgentFailure) -> Result<(), Error> {
        match self.phase {
            Phase::Initializing => {
                return Err(Error::unknown(format!(
                    "coverage analysis initialization failed: {}",
                    failure.reason
                )));
            }
            Phase::Running => {
                let inflight = self.inflight.take();
                self.phase = Phase::Idle;
                if let Some(inflight) = inflight {
                    self.fail_request(&inflight.request);
                    self.abort(&inflight.request);
                    self.log_event(
                        "analysis_failed",
                        json!({"request": inflight.request, "reason": failure.reason}),
                    );
                    if failure.prompt_wait_timed_out
                        && self.pending.is_none()
                        && self.retried_snapshot != Some(inflight.snapshot)
                    {
                        self.retried_snapshot = Some(inflight.snapshot);
                        self.pending = Some(inflight.snapshot);
                        self.log_event(
                            "analysis_snapshot_requeued",
                            json!({"snapshot": inflight.snapshot, "reason": "prompt_wait_timeout"}),
                        );
                    }
                }
            }
            Phase::Idle => {
                self.log_event("analysis_worker_failed", json!({"reason": failure.reason}));
            }
        }
        Ok(())
    }

    pub(crate) fn poll_agent(&mut self) -> Result<(), Error> {
        if !self.enabled {
            return Ok(());
        }
        while let Some(outcome) = self
            .worker
            .as_mut()
            .and_then(|worker| worker.recv_timeout(Duration::from_millis(1)))
        {
            match outcome {
                AgentOutcome::InitTimedOut(failure) => self.consume_init_timeout(failure)?,
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
        Ok(())
    }

    pub fn shutdown(&mut self) {
        if !self.enabled || self.exited {
            return;
        }
        self.exited = true;
        self.worker.take();
        if let Some(handle) = &self.introspection {
            handle.cancel_incomplete();
        }
        if let Some(http) = self.introspection_http.take() {
            http.shutdown();
        }
        self.log_event("analysis_stopped", json!({}));
    }

    fn fail_request(&self, request: &str) {
        let Some(handle) = &self.introspection else {
            return;
        };
        if let Err(error) = handle.mark_failed(request) {
            log::debug!("introspection failure for {request} was not applied: {error}");
        }
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
        self.poll_agent()?;
        if self.initialized && self.inflight.is_none() {
            self.dispatch()?;
            // No analysis can fill the mailbox for this trigger. Resume the
            // stagnation counter after failures, no_proposal, or an empty replay.
            if self.inflight.is_none() {
                if let Ok(progress) = state.metadata_mut::<BitmapProgress>() {
                    if progress.synthesis_due() && self.mailbox.peek().is_none() {
                        progress.take_synthesis_request();
                    }
                }
            }
        }
        Ok(())
    }
}

/// Working directory of the coverage agent. Codex treats it as the writable
/// workspace, so it must be the analysis root rather than `init/`.
fn analysis_agent_cwd(analysis_root: &Path) -> &Path {
    analysis_root
}

fn campaign_identity(campaign: &Campaign, session: &str) -> CampaignIdentity {
    let run_id = campaign
        .output_root
        .as_ref()
        .and_then(|path| path.file_name())
        .map(|name| name.to_string_lossy().into_owned())
        .filter(|name| !name.is_empty())
        .unwrap_or_else(|| session.to_string());
    CampaignIdentity {
        run_id,
        started_at_unix_ms: SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|value| value.as_millis() as u64)
            .unwrap_or(0),
    }
}

fn introspection_url_arg(url: &str) -> Result<String, Error> {
    let encoded = serde_json::to_string(url).map_err(|error| Error::unknown(error.to_string()))?;
    Ok(format!("mcp_servers.campaign_introspection.url={encoded}"))
}

fn mcp_launch(
    mut launch: CommandLine,
    campaign: &Campaign,
    introspection_url: &str,
) -> Result<CommandLine, Error> {
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
        "-c".to_string(),
        introspection_url_arg(introspection_url)?,
    ]);
    Ok(launch)
}

fn inject_target_config(prompt: String, target: &crate::app::config::TargetConfig) -> String {
    prompt
        .replace(
            "{{ configure_options }}",
            &json!(target.configure_options).to_string(),
        )
        .replace("{{ cli_args }}", &json!(target.cli_args).to_string())
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
                "name": step.name,
                "action": step.action.name(),
                "message": step.message_name(),
                "channel": step.channel_name,
                "sender": step.sender_name(),
                "receiver": step.receiver_name(),
                "party": step.party_name(),
                "condition": step.condition,
            })
        })
        .collect();
    let prompt = INIT_PROMPT
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
                .generator_synthesis
                .model
                .display()
                .to_string(),
        )
        .replace("{{ sut_parties }}", &json!(stage.sut_parties).to_string())
        .replace(
            "{{ generator_parties }}",
            &json!(stage.generator_parties).to_string(),
        )
        .replace(
            "{{ unavailable_parties }}",
            &json!(stage.unavailable_parties).to_string(),
        )
        .replace("{{ steps }}", &json!(steps).to_string())
        .replace(
            "{{ handlers_path }}",
            &stage.handlers_path.display().to_string(),
        )
        .replace(
            "{{ execution.summary }}",
            "Coverage analysis reads the SUT source directory and protocol model.",
        );
    inject_target_config(prompt, &stage.campaign.target)
}

fn snapshot_sut_logs(snapshot_dir: &Path, snapshot: u64) -> Result<(PathBuf, PathBuf), Error> {
    let stdout = snapshot_dir.join("sut.stdout.log");
    let stderr = snapshot_dir.join("sut.stderr.log");
    for path in [&stdout, &stderr] {
        if !path.is_file() {
            return Err(Error::unknown(format!(
                "coverage snapshot {snapshot:06} is missing required SUT output log {}",
                path.display()
            )));
        }
    }
    Ok((stdout, stderr))
}

fn render_task(
    stage: &CoverageAnalysisStage,
    request: &str,
    output_dir: &Path,
    snapshot: u64,
    snapshot_dir: &Path,
    sut_stdout_log: &Path,
    sut_stderr_log: &Path,
) -> String {
    let prompt = TASK_PROMPT
        .replace("{{ session_id }}", &stage.session)
        .replace("{{ request_id }}", request)
        .replace("{{ output_dir }}", &output_dir.display().to_string())
        .replace("{{ snapshot }}", &format!("{snapshot:06}"))
        .replace("{{ snapshot_dir }}", &snapshot_dir.display().to_string())
        .replace("{{ build }}", &stage.fingerprint)
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
                .generator_synthesis
                .model
                .display()
                .to_string(),
        )
        .replace("{{ handlers }}", &stage.handlers_path.display().to_string())
        .replace("{{ sut_parties }}", &json!(stage.sut_parties).to_string())
        .replace(
            "{{ generator_parties }}",
            &json!(stage.generator_parties).to_string(),
        )
        .replace(
            "{{ unavailable_parties }}",
            &json!(stage.unavailable_parties).to_string(),
        )
        .replace(
            "{{ sut_stdout_log }}",
            &sut_stdout_log.display().to_string(),
        )
        .replace(
            "{{ sut_stderr_log }}",
            &sut_stderr_log.display().to_string(),
        )
        .replace(
            "{{ source_dir }}",
            &stage
                .campaign
                .target
                .source_dir
                .as_ref()
                .map(|path| path.display().to_string())
                .unwrap_or_default(),
        );
    inject_target_config(prompt, &stage.campaign.target)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::agents::TerminalFact;

    fn test_campaign() -> Campaign {
        toml::from_str(
            r#"
[target]
binary = "b"
workdir = "w"
port = 0
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
"#,
        )
        .unwrap()
    }

    #[test]
    fn init_timeout_count_is_per_stage_and_only_the_third_is_fatal() {
        let campaign = test_campaign();
        let mut first = CoverageAnalysisStage::disabled(&campaign, ProposalMailbox::default());
        let mut second = CoverageAnalysisStage::disabled(&campaign, ProposalMailbox::default());
        for stage in [&mut first, &mut second] {
            stage.enabled = true;
            stage.init_started = true;
            stage.phase = Phase::Initializing;
        }
        let (commands, requests) = std::sync::mpsc::channel();
        let (outcomes, receiver) = std::sync::mpsc::channel();
        first.worker = Some(Client::from_channels(commands, receiver));
        for count in 1..=3 {
            outcomes
                .send(AgentOutcome::InitTimedOut(AgentFailure::new(
                    "ready.json was not published before the deadline".into(),
                    TerminalFact::default(),
                )))
                .unwrap();
            let result = first.poll_agent();
            assert_eq!(result.is_err(), count == 3);
            assert_eq!(first.init_timeouts, count);
            assert_eq!(second.init_timeouts, 0);
            assert!(first.init_started);
            assert!(!first.initialized);
            assert!(requests.try_recv().is_err(), "must not send another init");
            if let Err(error) = result {
                assert!(error
                    .to_string()
                    .contains("initialization timed out 3 times"));
            }
        }
        second
            .consume_init_timeout(AgentFailure::new(
                "ready.json was not published".into(),
                TerminalFact::default(),
            ))
            .unwrap();
        assert_eq!(second.init_timeouts, 1);
    }

    #[test]
    fn unsent_request_retries_once_then_releases_the_synthesis_trigger() {
        let mut stage = CoverageAnalysisStage::disabled(&test_campaign(), ProposalMailbox::default());
        stage.enabled = true;
        stage.initialized = true;
        let mut state = libafl::state::NopState::<()>::new();
        let mut progress = BitmapProgress::new(1).unwrap();
        progress.set_mutation_pending(true);
        progress.observe(&[0], true);
        progress.finish_source_coverage();
        assert!(progress.synthesis_due());
        state.add_metadata(progress);

        let failure = || {
            let mut failure =
                AgentFailure::new("prompt wait timed out".into(), TerminalFact::default());
            failure.prompt_wait_timed_out = true;
            failure
        };
        stage.phase = Phase::Running;
        stage.inflight = Some(Inflight {
            request: "first".into(),
            snapshot: 2,
        });
        stage.consume_failure(failure()).unwrap();
        assert_eq!(stage.pending.take(), Some(2));

        // A redispatched request for the same snapshot uses a fresh identity.
        stage.phase = Phase::Running;
        stage.inflight = Some(Inflight {
            request: "retry".into(),
            snapshot: 2,
        });
        let (commands, _requests) = std::sync::mpsc::channel();
        let (outcomes, receiver) = std::sync::mpsc::channel();
        stage.worker = Some(Client::from_channels(commands, receiver));
        stage.perform(&mut (), &mut (), &mut state, &mut ()).unwrap();
        assert!(state.metadata::<BitmapProgress>().unwrap().synthesis_due());

        outcomes.send(AgentOutcome::Failed(failure())).unwrap();
        stage.perform(&mut (), &mut (), &mut state, &mut ()).unwrap();
        assert!(stage.pending.is_none());
        let progress = state.metadata_mut::<BitmapProgress>().unwrap();
        assert!(!progress.synthesis_due());
        progress.set_mutation_pending(true);
        progress.observe(&[0], true);
        assert!(progress.source_coverage_due());
    }

    #[test]
    fn snapshot_sut_logs_are_required_and_absolute() {
        let dir = std::env::temp_dir().join(format!("mcn-sut-log-paths-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let error = snapshot_sut_logs(&dir, 7).unwrap_err();
        assert!(format!("{error:?}").contains("sut.stdout.log"), "{error:?}");
        fs::write(dir.join("sut.stdout.log"), b"out").unwrap();
        let error = snapshot_sut_logs(&dir, 7).unwrap_err();
        assert!(format!("{error:?}").contains("sut.stderr.log"), "{error:?}");
        fs::write(dir.join("sut.stderr.log"), b"err").unwrap();
        let (stdout, stderr) = snapshot_sut_logs(&dir, 7).unwrap();
        assert_eq!(stdout, dir.join("sut.stdout.log"));
        assert_eq!(stderr, dir.join("sut.stderr.log"));
        assert!(stdout.is_absolute());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn prompt_injects_build_configuration_and_cli_args() {
        let campaign: Campaign = toml::from_str(
            r#"
[target]
binary = "b"
workdir = "w"
port = 0
configure_options = ["no-asm", "no-shared"]
cli_args = ["-tls1_3", "-www"]
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
"#,
        )
        .unwrap();
        let prompt = inject_target_config(
            "CONFIGURE_OPTIONS={{ configure_options }} CLI_ARGS={{ cli_args }}".into(),
            &campaign.target,
        );
        assert!(prompt.contains(r#"["no-asm","no-shared"]"#), "{prompt}");
        assert!(prompt.contains(r#"["-tls1_3","-www"]"#), "{prompt}");
        assert!(!prompt.contains("{{"), "{prompt}");
    }

    #[test]
    fn analysis_agent_cwd_contains_init_and_request_output() {
        let root = Path::new("/output/coverage/build/analysis");
        let cwd = analysis_agent_cwd(root);
        assert_eq!(cwd, root);
        let init = root.join("init/ready.json");
        let result = root.join("requests/000001-abc/result.json");
        assert!(init.starts_with(cwd));
        assert!(result.starts_with(cwd));
        assert_ne!(cwd, root.join("init"));
    }

    #[test]
    fn introspection_url_is_a_json_string_config() {
        let arg = introspection_url_arg("http://127.0.0.1:9/mcp").unwrap();
        assert_eq!(
            arg,
            "mcp_servers.campaign_introspection.url=\"http://127.0.0.1:9/mcp\""
        );
    }
}
