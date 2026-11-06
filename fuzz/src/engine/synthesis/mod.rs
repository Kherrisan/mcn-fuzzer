//! Asynchronous generator synthesis. At most one agent task is in flight.
//!
//! Coverage mode consumes a frozen proposal from the shared mailbox and gives
//! it to the agent unchanged. Autonomous mode (analysis disabled, or seedless
//! bootstrap) asks the agent to write and validate its own proposal first.

use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, Instant},
};

use libafl::{
    corpus::Corpus,
    stages::{Restartable, Stage},
    state::{HasCorpus, HasRand},
    Error, HasMetadata,
};
use libafl_bolts::{impl_serdeany, rands::Rand};
use serde::{Deserialize, Serialize};

use crate::{
    agents::{command_line, CommandLine},
    app::config::{Campaign, ProtocolGenerationConfig},
    coverage::proposal::ProposalMailbox,
    engine::input::{GeneratorInput, ParamValue},
    engine::stagnation::BitmapProgress,
    protocol::{write_catalog, Binding, IndexedModel},
};

mod agent;
mod artifacts;
mod prompts;
mod request;

use agent::Client;
use request::GeneratorRequest;

pub use prompts::{ExecutionMode, SynthesisPromptContext};
pub use request::ValidatedGenerator;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct TraceOrigin {
    pub protocol_id: String,
    pub trace_id: String,
    pub step_ids: Vec<String>,
    pub added_steps: Vec<String>,
}

impl_serdeany!(TraceOrigin);

pub(crate) use crate::agents::redact_text;
use crate::agents::{
    AgentFailure, AgentInit, AgentOutcome, SessionInit, TerminalFact, WorkerConfig,
};
pub use crate::agents::{AgentSession, SessionPhase};

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct SynthesisProgress {
    pub attempted: BTreeSet<String>,
    pub paused: bool,
    pub published: BTreeMap<String, String>,
    pub failures: Vec<String>,
    /// Latest session snapshot. Absent in older metadata files.
    #[serde(default)]
    pub session: AgentSession,
}

impl_serdeany!(SynthesisProgress);

type AgentCommand = crate::agents::AgentCommand<SessionInit, GeneratorRequest>;
type Outcome = AgentOutcome<(), ReadyInput>;

#[derive(Debug)]
pub(crate) struct ReadyInput {
    request: String,
    input: GeneratorInput,
    origin: TraceOrigin,
}

/// Run the production artifact checks and publish `generator.py` plus its schema.
pub fn validate_generator_artifacts(
    output_dir: &Path,
    request_id: &str,
    protocol_id: &str,
    trace_id: &str,
) -> Result<ValidatedGenerator, String> {
    artifacts::check(output_dir, request_id, protocol_id, trace_id, "").and_then(
        |(result, params)| {
            let published = output_dir.join("published");
            if published.exists() {
                return Err("published generator path already exists".to_string());
            }
            fs::create_dir_all(&published).map_err(|error| error.to_string())?;
            fs::copy(
                output_dir.join("generator.py"),
                published.join("generator.py"),
            )
            .map_err(|error| error.to_string())?;
            fs::copy(
                output_dir.join("params.schema.json"),
                published.join("params.schema.json"),
            )
            .map_err(|error| error.to_string())?;
            let generator = fs::canonicalize(published.join("generator.py"))
                .map_err(|error| error.to_string())?;
            let added = result
                .get("added_steps")
                .and_then(serde_json::Value::as_array)
                .map(|items| {
                    items
                        .iter()
                        .filter_map(serde_json::Value::as_str)
                        .map(str::to_string)
                        .collect()
                })
                .unwrap_or_default();
            Ok(ValidatedGenerator {
                input: GeneratorInput::new(generator, params),
                origin: TraceOrigin {
                    protocol_id: protocol_id.to_string(),
                    trace_id: trace_id.to_string(),
                    step_ids: Vec::new(),
                    added_steps: added,
                },
            })
        },
    )
}

/// Stage placed before mutation. Disabled campaigns do not load a model.
pub struct GeneratorSynthesisStage {
    enabled: bool,
    model: Option<Arc<IndexedModel>>,
    binding: Binding,
    config: ProtocolGenerationConfig,
    prompt_context: Option<SynthesisPromptContext>,
    output: PathBuf,
    inflight: bool,
    initialization_started: bool,
    initialized: bool,
    init_timeouts: u32,
    paused: bool,
    worker: Option<Client>,
    closed_requests: BTreeSet<String>,
    current_request: Option<String>,
    session: String,
    launch: Option<CommandLine>,
    /// Test-only prompt mode. Empty in production prompts.
    mode_hint: String,
    /// Test-only. When set, initialization must not publish `ready.json`.
    init_hold: bool,
    session_state: AgentSession,
    exited: bool,
    /// Env values used only to redact text before it is stored. Never written.
    secrets: Vec<String>,
    mailbox: ProposalMailbox,
    analysis_enabled: bool,
}

impl GeneratorSynthesisStage {
    pub fn from_campaign(campaign: &Campaign, mailbox: ProposalMailbox) -> Result<Self, Error> {
        let mut stage = Self::disabled(campaign, mailbox);
        if !campaign.protocol_generation.enabled {
            return Ok(stage);
        }
        let context = SynthesisPromptContext::from_campaign(campaign)?;
        let catalog_model = context.model().clone();
        let catalog_binding = context.binding().clone();
        let boot = crate::agents::current_boot().ok_or_else(|| {
            Error::unknown(
                "protocol generation is enabled but the private HC core was not started"
                    .to_string(),
            )
        })?;
        stage.enabled = true;
        stage.model = Some(Arc::new(context.model().clone()));
        stage.binding = context.binding().clone();
        stage.config = campaign.protocol_generation.clone();
        stage.prompt_context = Some(context);
        stage.session = boot.session.clone();
        stage.analysis_enabled = campaign.coverage.analysis_enabled;
        write_catalog(
            &catalog_model,
            &catalog_binding,
            &campaign.protocol_generation.output.join("catalog.json"),
        )?;
        let launched = command_line(
            campaign
                .agents
                .profiles
                .get(&campaign.protocol_generation.profile)
                .ok_or_else(|| {
                    Error::illegal_argument(format!(
                        "agent profile {} is not defined",
                        campaign.protocol_generation.profile
                    ))
                })?,
            &std::path::absolute(&campaign.protocol_generation.output)?,
        )?;
        stage.secrets = launched.secrets();
        stage.launch = Some(launched);
        stage.spawn_worker(boot.socket)?;
        Ok(stage)
    }

    fn disabled(campaign: &Campaign, mailbox: ProposalMailbox) -> Self {
        Self {
            enabled: false,
            model: None,
            binding: Binding::default(),
            config: campaign.protocol_generation.clone(),
            prompt_context: None,
            output: campaign.protocol_generation.output.clone(),
            inflight: false,
            initialization_started: false,
            initialized: false,
            init_timeouts: 0,
            paused: false,
            worker: None,
            closed_requests: BTreeSet::new(),
            current_request: None,
            session: String::new(),
            launch: None,
            mode_hint: String::new(),
            init_hold: false,
            session_state: AgentSession::default(),
            exited: false,
            secrets: Vec::new(),
            mailbox,
            analysis_enabled: campaign.coverage.analysis_enabled,
        }
    }

    fn spawn_worker(&mut self, socket: PathBuf) -> Result<(), Error> {
        let terminal = format!("synthesis-{}", std::process::id());
        let client = Client::spawn(WorkerConfig {
            thread_name: "generator-synthesis",
            socket,
            terminal,
            init_timeout: Duration::from_secs(self.config.init_timeout_secs.max(1)),
            task_timeout: Duration::from_secs(self.config.task_timeout_secs.max(1)),
            secrets: self.secrets.clone(),
        })
        .map_err(|error| Error::unknown(format!("starting synthesis worker: {error}")))?;
        self.worker = Some(client);
        Ok(())
    }

    pub fn initialize(&mut self) -> Result<(), Error> {
        if !self.enabled || self.initialization_started || self.initialized {
            return Ok(());
        }
        let output_dir = self.output.join("init");
        fs::create_dir_all(&output_dir)?;
        let ready = output_dir.join("ready.json");
        if ready.exists() {
            fs::remove_file(ready)?;
        }
        let prompt = self.initialization_prompt(&output_dir)?;
        let launch = self.launch.clone().ok_or_else(|| {
            Error::illegal_argument("agent profile did not produce a command".to_string())
        })?;
        self.worker
            .as_ref()
            .ok_or_else(|| Error::unknown("synthesis worker is not running".to_string()))?
            .send(AgentCommand::Init {
                context: SessionInit {
                    output_dir,
                    session: self.session.clone(),
                },
                prompt,
                launch,
            })
            .map_err(|_| {
                Error::unknown("synthesis worker stopped before initialization".to_string())
            })?;
        self.initialization_started = true;
        Ok(())
    }

    /// Seedless bootstrap needs an initialized agent before it can create a corpus.
    fn wait_initialized(&mut self) -> Result<(), Error> {
        if !self.enabled || self.initialized {
            return Ok(());
        }
        loop {
            self.initialize()?;
            match self.recv_one(Duration::from_millis(200)) {
                Some(Outcome::Launched(fact)) => {
                    self.record_session("launch", SessionPhase::Initializing, &fact, None)
                }
                Some(Outcome::Ready((), fact)) => return self.accept_initialization(&fact),
                Some(Outcome::Failed(failure)) if failure.timeout => {
                    self.init_timeouts = self.init_timeouts.saturating_add(1);
                    self.note_terminal_failure(&failure);
                    if self.init_timeouts >= crate::agents::INIT_TIMEOUT_ATTEMPTS {
                        return Err(Error::unknown(format!(
                            "synthesis initialization timed out {} consecutive times",
                            self.init_timeouts
                        )));
                    }
                    self.paused = false;
                    self.initialization_started = false;
                    self.record_session(
                        "init_retry",
                        SessionPhase::Initializing,
                        &failure.fact,
                        Some(format!(
                            "initialization timed out; starting attempt {} of {}",
                            self.init_timeouts + 1,
                            crate::agents::INIT_TIMEOUT_ATTEMPTS
                        )),
                    );
                }
                Some(Outcome::Failed(failure)) => {
                    self.note_terminal_failure(&failure);
                    return Err(Error::unknown(format!(
                        "agent initialization failed: {}",
                        failure.reason
                    )));
                }
                Some(Outcome::Artifact { .. }) | None => {}
            }
        }
    }

    /// Block until the first artifact is evaluated by `accept`.
    pub fn bootstrap_first<F>(&mut self, rand: &mut impl Rand, mut accept: F) -> Result<(), Error>
    where
        F: FnMut(GeneratorInput, TraceOrigin) -> Result<bool, Error>,
    {
        self.wait_initialized()?;
        self.dispatch_with(rand, true)?;
        let deadline = Instant::now() + Duration::from_secs(self.config.task_timeout_secs.max(1));
        while Instant::now() < deadline {
            if let Some(outcome) = self.recv_one(Duration::from_millis(200)) {
                if let Outcome::Artifact {
                    result: Ok(ready), ..
                } = outcome
                {
                    self.finish_request(false);
                    let kept = accept(ready.input, ready.origin)?;
                    if !kept {
                        return Err(Error::unknown(
                            "the first synthesized generator was not accepted into the corpus"
                                .to_string(),
                        ));
                    }
                    return Ok(());
                }
                if let Outcome::Failed(failure) = outcome {
                    let reason = failure.reason.clone();
                    self.finish_request(true);
                    self.note_terminal_failure(&failure);
                    return Err(Error::unknown(format!(
                        "bootstrap synthesis failed: {reason}"
                    )));
                }
                if let Outcome::Artifact {
                    result: Err(reason),
                    ..
                } = outcome
                {
                    self.finish_request(false);
                    return Err(Error::unknown(format!(
                        "bootstrap synthesis failed: {reason}"
                    )));
                }
            }
        }
        self.finish_request(true);
        Err(Error::unknown(
            "bootstrap synthesis produced no schedulable corpus".to_string(),
        ))
    }

    pub fn shutdown(&mut self) {
        if self.enabled && !self.exited {
            self.exited = true;
            self.record_session("stop", SessionPhase::Exited, &TerminalFact::default(), None);
        }
        self.worker.take();
    }

    /// `allow_autonomous` is only true for the seedless bootstrap.
    fn dispatch_with(&mut self, rand: &mut impl Rand, allow_autonomous: bool) -> Result<(), Error> {
        if !self.enabled || !self.initialized || self.paused || self.inflight {
            return Ok(());
        }
        let Some(model) = self.model.as_ref() else {
            return Ok(());
        };
        let proposal = if self.analysis_enabled && !allow_autonomous {
            let Some(proposal) = self.mailbox.take() else {
                return Ok(());
            };
            Some(proposal)
        } else {
            None
        };
        let autonomous = proposal.is_none();
        let (request, output_dir, proposal_id, trace_id, session, build, snapshot) =
            if let Some(accepted) = &proposal {
                let request = accepted.proposal.proposal_id.clone();
                let output_dir = self.output.join("requests").join(&request);
                if output_dir.exists() {
                    return Err(Error::illegal_state(format!(
                        "synthesis request {} already exists",
                        output_dir.display()
                    )));
                }
                fs::create_dir_all(&output_dir)?;
                fs::copy(&accepted.path, output_dir.join("proposal.json"))
                    .map_err(|error| Error::unknown(format!("copying proposal: {error}")))?;
                (
                    request,
                    output_dir,
                    Some(accepted.proposal.proposal_id.clone()),
                    Some(accepted.proposal.trace_id.clone()),
                    self.session.clone(),
                    accepted.proposal.build.clone(),
                    accepted.proposal.snapshot.clone(),
                )
            } else {
                let request = format!("{:016x}", rand.next());
                let output_dir = self.output.join("requests").join(&request);
                if output_dir.exists() {
                    return Err(Error::illegal_state(format!(
                        "synthesis request {} already exists",
                        output_dir.display()
                    )));
                }
                fs::create_dir_all(&output_dir)?;
                (
                    request,
                    output_dir,
                    None,
                    None,
                    self.session.clone(),
                    String::new(),
                    String::new(),
                )
            };
        let generation = GeneratorRequest {
            id: request.clone(),
            output_dir,
            protocol_id: model.protocol_id.clone(),
            session,
            build,
            snapshot,
            proposal_id,
            trace_id,
            model: Arc::clone(model),
            binding: self.binding.clone(),
            autonomous,
        };
        let prompt = generation.prompt(
            &self.mode_hint,
            self.prompt_context.as_ref().ok_or_else(|| {
                Error::illegal_state("synthesis prompt context is missing".to_string())
            })?,
        )?;
        self.worker
            .as_ref()
            .ok_or_else(|| Error::unknown("synthesis worker is not running".to_string()))?
            .send(AgentCommand::Run {
                request: generation,
                prompt,
            })
            .map_err(|_| Error::unknown("synthesis worker stopped".to_string()))?;
        self.inflight = true;
        self.current_request = Some(request);
        let fact = TerminalFact {
            terminal: self.session_state.terminal.clone(),
            ..TerminalFact::default()
        };
        self.record_session("send", SessionPhase::Busy, &fact, None);
        Ok(())
    }

    fn recv_one(&mut self, timeout: Duration) -> Option<Outcome> {
        self.worker
            .as_mut()
            .and_then(|worker| worker.recv_timeout(timeout))
    }
}

impl Drop for GeneratorSynthesisStage {
    fn drop(&mut self) {
        self.shutdown();
    }
}

impl<S> Restartable<S> for GeneratorSynthesisStage {
    fn should_restart(&mut self, _state: &mut S) -> Result<bool, Error> {
        Ok(true)
    }
    fn clear_progress(&mut self, _state: &mut S) -> Result<(), Error> {
        Ok(())
    }
}

impl<E, EM, S, Z> Stage<E, EM, S, Z> for GeneratorSynthesisStage
where
    S: HasCorpus<GeneratorInput> + HasRand + HasMetadata,
    S::Rand: Rand,
    Z: libafl::fuzzer::Evaluator<E, EM, GeneratorInput, S>,
{
    fn perform(
        &mut self,
        fuzzer: &mut Z,
        executor: &mut E,
        state: &mut S,
        manager: &mut EM,
    ) -> Result<(), Error> {
        state
            .metadata_or_insert_with(BitmapProgress::default)
            .set_synthesis_inflight(self.inflight);
        if !self.enabled {
            state
                .metadata_or_insert_with(BitmapProgress::default)
                .take_synthesis_request();
            return Ok(());
        }
        while let Some(outcome) = self.recv_one(Duration::from_millis(1)) {
            self.consume(outcome, fuzzer, executor, state, manager)?;
        }
        state
            .metadata_or_insert_with(BitmapProgress::default)
            .set_synthesis_inflight(self.inflight);
        let synthesis_due = state
            .metadata_or_insert_with(BitmapProgress::default)
            .synthesis_due();
        if self.paused {
            state
                .metadata_or_insert_with(BitmapProgress::default)
                .take_synthesis_request();
        } else if !self.initialized {
            if synthesis_due {
                self.record_session(
                    "init_deferred",
                    SessionPhase::Initializing,
                    &TerminalFact::default(),
                    Some("synthesis request skipped while initialization is pending".to_string()),
                );
            }
        } else if !self.inflight
            && state
                .metadata_or_insert_with(BitmapProgress::default)
                .synthesis_due()
        {
            // Coverage mode must keep the trigger pending until a proposal
            // arrives; taking it early would drop the only dispatch signal.
            if self.analysis_enabled && self.mailbox.peek().is_none() {
                self.record_session(
                    "waiting_proposal",
                    SessionPhase::Ready,
                    &TerminalFact::default(),
                    Some("synthesis trigger retained until a proposal is accepted".to_string()),
                );
            } else {
                state
                    .metadata_or_insert_with(BitmapProgress::default)
                    .take_synthesis_request();
                self.dispatch_with(state.rand_mut(), false)?;
            }
        }
        state
            .metadata_or_insert_with(BitmapProgress::default)
            .set_synthesis_inflight(self.inflight);
        self.mirror_session(state);
        Ok(())
    }
}

impl GeneratorSynthesisStage {
    fn consume<E, EM, S, Z>(
        &mut self,
        outcome: Outcome,
        fuzzer: &mut Z,
        executor: &mut E,
        state: &mut S,
        manager: &mut EM,
    ) -> Result<(), Error>
    where
        S: HasCorpus<GeneratorInput> + HasMetadata,
        Z: libafl::fuzzer::Evaluator<E, EM, GeneratorInput, S>,
    {
        match outcome {
            Outcome::Launched(fact) => {
                self.record_session("launch", SessionPhase::Initializing, &fact, None);
                self.mirror_session(state);
                Ok(())
            }
            Outcome::Ready((), fact) => {
                if let Err(error) = self.accept_initialization(&fact) {
                    self.note_failure(state, error.to_string());
                }
                self.mirror_session(state);
                Ok(())
            }
            Outcome::Failed(failure) => {
                if !self.initialized {
                    let reason = self.redacted(&failure.reason);
                    if !failure.timeout {
                        self.note_terminal_failure(&failure);
                        self.note_failure(state, reason.clone());
                        return Err(Error::unknown(format!(
                            "synthesis initialization failed: {reason}"
                        )));
                    }
                    self.init_timeouts = self.init_timeouts.saturating_add(1);
                    self.note_terminal_failure(&failure);
                    self.note_failure(state, reason.clone());
                    if self.init_timeouts >= crate::agents::INIT_TIMEOUT_ATTEMPTS {
                        let error = format!(
                            "synthesis initialization timed out {} consecutive times",
                            self.init_timeouts
                        );
                        self.paused = true;
                        self.record_session(
                            "blocked",
                            SessionPhase::Paused,
                            &failure.fact,
                            Some(error.clone()),
                        );
                        return Err(Error::unknown(error));
                    }
                    self.paused = false;
                    self.initialization_started = false;
                    self.record_session(
                        "init_retry",
                        SessionPhase::Initializing,
                        &failure.fact,
                        Some(format!(
                            "initialization timed out; starting attempt {} of {}",
                            self.init_timeouts + 1,
                            crate::agents::INIT_TIMEOUT_ATTEMPTS
                        )),
                    );
                    self.initialize()?;
                    state
                        .metadata_or_insert_with(BitmapProgress::default)
                        .take_synthesis_request();
                    self.mirror_session(state);
                    return Ok(());
                }
                let reason = failure.reason.clone();
                self.finish_request(true);
                self.note_terminal_failure(&failure);
                self.note_failure(state, reason);
                Ok(())
            }
            Outcome::Artifact {
                result: Err(reason),
                fact,
                ..
            } => {
                self.finish_request(false);
                self.record_session("blocked", SessionPhase::Ready, &fact, Some(reason.clone()));
                self.note_failure(state, reason);
                Ok(())
            }
            Outcome::Artifact {
                result: Ok(ready),
                fact,
                ..
            } => {
                if self.closed_requests.contains(&ready.request) {
                    self.finish_request(false);
                    self.record_session(
                        "blocked",
                        SessionPhase::Ready,
                        &fact,
                        Some("ignored a late result for a closed request".to_string()),
                    );
                    self.note_failure(
                        state,
                        "ignored a late result for a closed request".to_string(),
                    );
                    return Ok(());
                }
                self.finish_request(false);
                self.record_session("result", SessionPhase::Ready, &fact, None);
                match fuzzer.evaluate_input(state, executor, manager, &ready.input) {
                    Ok((_exit, corpus_id)) => {
                        if let Some(id) = corpus_id {
                            if let Ok(case) = state.corpus_mut().get(id) {
                                case.borrow_mut().add_metadata(ready.origin.clone());
                            }
                            self.remember(state, &ready);
                        }
                        Ok(())
                    }
                    Err(error) => {
                        self.note_failure(
                            state,
                            format!("evaluating synthesized input failed: {error}"),
                        );
                        Ok(())
                    }
                }
            }
        }
    }

    fn accept_initialization(&mut self, fact: &TerminalFact) -> Result<(), Error> {
        let ready = SessionInit {
            output_dir: self.output.join("init"),
            session: self.session.clone(),
        }
        .accept();
        if let Err(reason) = ready {
            self.paused = true;
            self.record_session("blocked", SessionPhase::Paused, fact, Some(reason.clone()));
            return Err(Error::unknown(reason));
        }
        self.initialized = true;
        self.init_timeouts = 0;
        self.record_session("ready", SessionPhase::Ready, fact, None);
        Ok(())
    }

    fn finish_request(&mut self, pause: bool) {
        self.inflight = false;
        if pause {
            self.paused = true;
        }
        if let Some(request) = self.current_request.take() {
            self.closed_requests.insert(request);
        }
    }

    fn note_failure<S: HasMetadata>(&mut self, state: &mut S, reason: String) {
        let reason = self.redacted(&reason);
        let progress = state.metadata_or_insert_with(SynthesisProgress::default);
        progress.failures.push(reason);
        progress.paused = self.paused;
        progress.session = self.session_state.clone();
    }

    fn remember<S: HasMetadata>(&mut self, state: &mut S, ready: &ReadyInput) {
        let progress = state.metadata_or_insert_with(SynthesisProgress::default);
        progress.attempted.insert(ready.origin.trace_id.clone());
        progress.published.insert(
            ready.origin.trace_id.clone(),
            ready.input.generator.display().to_string(),
        );
        progress.session = self.session_state.clone();
    }

    fn mirror_session<S: HasMetadata>(&self, state: &mut S) {
        if !self.enabled {
            return;
        }
        let progress = state.metadata_or_insert_with(SynthesisProgress::default);
        progress.paused = self.paused;
        progress.session = self.session_state.clone();
    }

    fn note_terminal_failure(&mut self, failure: &AgentFailure) {
        self.paused = true;
        let transition = if failure.timeout {
            "timeout"
        } else {
            "blocked"
        };
        self.record_session(
            transition,
            SessionPhase::Paused,
            &failure.fact,
            Some(failure.reason.clone()),
        );
    }

    /// Record one transition. `error` replaces the stored HC error when `Some`.
    fn record_session(
        &mut self,
        transition: &str,
        phase: SessionPhase,
        fact: &TerminalFact,
        error: Option<String>,
    ) {
        if !self.enabled {
            return;
        }
        self.session_state
            .record(transition, phase, fact, error, &self.secrets);
        self.session_state
            .append_log(&self.output.join("session.jsonl"));
    }

    fn redacted(&self, text: &str) -> String {
        redact_text(text, &self.secrets)
    }

    fn initialization_prompt(&self, output_dir: &Path) -> Result<String, Error> {
        self.prompt_context
            .as_ref()
            .ok_or_else(|| Error::illegal_state("synthesis prompt context is missing".to_string()))?
            .initialization_prompt(&self.session, self.init_hold, output_dir)
    }
}

#[cfg(test)]
mod tests;
