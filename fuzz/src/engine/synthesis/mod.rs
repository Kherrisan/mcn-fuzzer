//! Asynchronous generator synthesis. At most one agent task is in flight.
//!
//! The background thread performs HC calls. LibAFL state is touched only on
//! the fuzzer thread, at a case boundary.

use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
    sync::mpsc,
    thread,
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
use serde_json::Value;

use crate::{
    agents::{command_line, CommandLine},
    app::config::{Campaign, ProtocolGenerationConfig},
    engine::input::{GeneratorInput, ParamValue},
    engine::stagnation::BitmapProgress,
    protocol::{
        write_receipt, Binding, IndexedModel, RandomTraceSelector, SelectionContext, TraceSelector,
    },
};

mod artifacts;
mod prompts;
mod request;
mod worker;

use request::GeneratorRequest;
use worker::AgentWorker;

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

/// Coarse session phase. Completion still follows `ready.json` and `result.json`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum SessionPhase {
    Initializing,
    Ready,
    Busy,
    Paused,
    Exited,
}

impl Default for SessionPhase {
    fn default() -> Self {
        Self::Initializing
    }
}

/// Latest agent session. Environment values are never stored here.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AgentSession {
    pub phase: SessionPhase,
    /// `launch`, `ready`, `send`, `result`, `blocked`, `timeout`, or `stop`.
    pub transition: String,
    pub terminal: Option<String>,
    /// Raw HC terminal `state`, such as `live`.
    pub hc_state: Option<String>,
    /// Raw HC terminal `status`. Non-strings are compact JSON.
    pub hc_status: Option<String>,
    /// Raw HC error or the campaign failure that accompanied it.
    pub hc_error: Option<String>,
}

impl Default for AgentSession {
    fn default() -> Self {
        Self {
            phase: SessionPhase::Initializing,
            transition: String::new(),
            terminal: None,
            hc_state: None,
            hc_status: None,
            hc_error: None,
        }
    }
}

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

/// Commands for the synthesis worker to control the campaign agent.
enum AgentCommand {
    Init {
        output_dir: PathBuf,
        prompt: String,
        launch: CommandLine,
    },
    Generate {
        request: GeneratorRequest,
        prompt: String,
    },
    Stop,
}

/// One `terminal.get` observation. Values are redacted before this is stored.
#[derive(Clone, Debug, Default)]
struct TerminalFact {
    terminal: Option<String>,
    state: Option<String>,
    status: Option<String>,
    error: Option<String>,
}

#[derive(Debug)]
struct AgentFailure {
    reason: String,
    fact: TerminalFact,
    timeout: bool,
}

#[derive(Debug)]
enum Outcome {
    Launched(TerminalFact),
    Ready(TerminalFact),
    Artifact(Result<ReadyInput, String>, TerminalFact),
    Failed(AgentFailure),
}

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
    GeneratorRequest {
        id: request_id.to_string(),
        output_dir: output_dir.to_path_buf(),
        protocol_id: protocol_id.to_string(),
        trace_id: trace_id.to_string(),
    }
    .accept()
}

/// Stage placed before mutation. Disabled campaigns do not load a model.
pub struct GeneratorSynthesisStage {
    enabled: bool,
    model: Option<IndexedModel>,
    binding: Binding,
    config: ProtocolGenerationConfig,
    prompt_context: Option<SynthesisPromptContext>,
    output: PathBuf,
    context: SelectionContext,
    inflight: bool,
    initialization_started: bool,
    initialized: bool,
    paused: bool,
    commands: Option<mpsc::Sender<AgentCommand>>,
    outcomes: Option<mpsc::Receiver<Outcome>>,
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
}

impl GeneratorSynthesisStage {
    pub fn from_campaign(campaign: &Campaign) -> Result<Self, Error> {
        if !campaign.protocol_generation.enabled {
            return Ok(Self::disabled(campaign));
        }
        let context = SynthesisPromptContext::from_campaign(campaign)?;
        let boot = crate::agents::current_boot().ok_or_else(|| {
            Error::unknown(
                "protocol generation is enabled but the private HC core was not started"
                    .to_string(),
            )
        })?;
        let mut stage = Self::disabled(campaign);
        stage.enabled = true;
        stage.model = Some(context.model().clone());
        stage.binding = context.binding().clone();
        stage.config = campaign.protocol_generation.clone();
        stage.prompt_context = Some(context);
        stage.session = boot.session.clone();
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
        stage.secrets = launched
            .env
            .iter()
            .map(|(_, value)| value.clone())
            .filter(|value| !value.is_empty())
            .collect();
        stage.launch = Some(launched);
        stage.spawn_worker(boot.socket, boot.session)?;
        Ok(stage)
    }

    fn disabled(campaign: &Campaign) -> Self {
        Self {
            enabled: false,
            model: None,
            binding: Binding::default(),
            config: campaign.protocol_generation.clone(),
            prompt_context: None,
            output: campaign.protocol_generation.output.clone(),
            context: SelectionContext::default(),
            inflight: false,
            initialization_started: false,
            initialized: false,
            paused: false,
            commands: None,
            outcomes: None,
            closed_requests: BTreeSet::new(),
            current_request: None,
            session: String::new(),
            launch: None,
            mode_hint: String::new(),
            init_hold: false,
            session_state: AgentSession::default(),
            exited: false,
            secrets: Vec::new(),
        }
    }

    fn spawn_worker(&mut self, socket: PathBuf, session: String) -> Result<(), Error> {
        let (command_tx, command_rx) = mpsc::channel();
        let (out_tx, out_rx) = mpsc::channel();
        let init_timeout = Duration::from_secs(self.config.init_timeout_secs.max(1));
        let task_timeout = Duration::from_secs(self.config.task_timeout_secs.max(1));
        let secrets = self.secrets.clone();
        thread::Builder::new()
            .name("mcn-synthesis".to_string())
            .spawn(move || {
                AgentWorker {
                    commands: command_rx,
                    outcomes: out_tx,
                    socket,
                    terminal: "generator".to_string(),
                    init_timeout,
                    task_timeout,
                    session,
                    secrets,
                }
                .run()
            })
            .map_err(|error| Error::unknown(format!("starting synthesis thread: {error}")))?;
        self.commands = Some(command_tx);
        self.outcomes = Some(out_rx);
        Ok(())
    }

    /// Queue initialization on the worker without waiting. No receipt is written.
    pub fn initialize(&mut self) -> Result<(), Error> {
        if !self.enabled || self.initialization_started {
            return Ok(());
        }
        let output_dir = self.output.join("init");
        fs::create_dir_all(&output_dir)?;
        let prompt = self.initialization_prompt(&output_dir)?;
        let launch = self.launch.clone().ok_or_else(|| {
            Error::illegal_argument("agent profile did not produce a command".to_string())
        })?;
        self.commands
            .as_ref()
            .ok_or_else(|| Error::unknown("synthesis worker is not running".to_string()))?
            .send(AgentCommand::Init {
                output_dir,
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
        self.initialize()?;
        let deadline = Instant::now() + Duration::from_secs(self.config.init_timeout_secs.max(1));
        while Instant::now() < deadline {
            match self
                .outcomes
                .as_ref()
                .unwrap()
                .recv_timeout(Duration::from_millis(200))
            {
                Ok(Outcome::Launched(fact)) => {
                    self.record_session("launch", SessionPhase::Initializing, &fact, None);
                }
                Ok(Outcome::Ready(fact)) => {
                    return self.accept_initialization(&fact);
                }
                Ok(Outcome::Failed(failure)) => {
                    self.note_terminal_failure(&failure);
                    return Err(Error::unknown(format!(
                        "agent initialization failed: {}",
                        failure.reason
                    )));
                }
                Ok(Outcome::Artifact(_, _)) => {}
                Err(mpsc::RecvTimeoutError::Timeout) => {}
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    self.paused = true;
                    self.record_session(
                        "blocked",
                        SessionPhase::Paused,
                        &TerminalFact::default(),
                        Some("synthesis worker disconnected".to_string()),
                    );
                    return Err(Error::unknown("synthesis worker disconnected".to_string()));
                }
            }
        }
        self.paused = true;
        self.record_session(
            "timeout",
            SessionPhase::Paused,
            &TerminalFact::default(),
            Some("agent initialization timed out".to_string()),
        );
        Err(Error::unknown("agent initialization timed out".to_string()))
    }

    /// Block until the first artifact is evaluated by `accept`.
    pub fn bootstrap_first<F>(&mut self, rand: &mut impl Rand, mut accept: F) -> Result<(), Error>
    where
        F: FnMut(GeneratorInput, TraceOrigin) -> Result<bool, Error>,
    {
        self.wait_initialized()?;
        self.dispatch_with(rand)?;
        let deadline = Instant::now() + Duration::from_secs(self.config.task_timeout_secs.max(1));
        while Instant::now() < deadline {
            if let Some(outcome) = self.recv_one(Duration::from_millis(200))? {
                if let Outcome::Artifact(Ok(ready), fact) = outcome {
                    self.finish_request(false);
                    self.record_session("result", SessionPhase::Ready, &fact, None);
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
                if let Outcome::Artifact(Err(reason), fact) = outcome {
                    self.finish_request(false);
                    self.record_session(
                        "blocked",
                        SessionPhase::Ready,
                        &fact,
                        Some(reason.clone()),
                    );
                    return Err(Error::unknown(format!(
                        "bootstrap synthesis failed: {reason}"
                    )));
                }
            }
        }
        self.finish_request(true);
        self.record_session(
            "timeout",
            SessionPhase::Paused,
            &TerminalFact::default(),
            Some("bootstrap synthesis produced no schedulable corpus".to_string()),
        );
        Err(Error::unknown(
            "bootstrap synthesis produced no schedulable corpus".to_string(),
        ))
    }

    pub fn shutdown(&mut self) {
        if self.enabled && !self.exited {
            self.exited = true;
            self.record_session("stop", SessionPhase::Exited, &TerminalFact::default(), None);
        }
        if let Some(commands) = &self.commands {
            let _ = commands.send(AgentCommand::Stop);
        }
    }

    fn dispatch_with(&mut self, rand: &mut impl Rand) -> Result<(), Error> {
        if !self.enabled || !self.initialized || self.paused || self.inflight {
            return Ok(());
        }
        let Some(model) = self.model.as_ref() else {
            return Ok(());
        };
        let trace = RandomTraceSelector.select(
            model,
            &self.binding,
            rand,
            &self.context,
            self.config.sample_attempts,
            self.config.trace_len_min,
            self.config.trace_len_max,
        );
        let Some(trace) = trace else {
            return Ok(());
        };
        let request = format!("{:016x}", rand.next());
        let output_dir = self.output.join("requests").join(&request);
        if output_dir.exists() {
            return Err(Error::illegal_state(format!(
                "synthesis request {} already exists",
                output_dir.display()
            )));
        }
        fs::create_dir_all(&output_dir)?;
        write_receipt(
            &output_dir.join("receipt.json"),
            model,
            &self.binding,
            &trace,
        )?;
        self.context.attempted.insert(trace.id.0.clone());
        self.context.selections += 1;
        self.context.by_trace.insert(trace.id.0.clone(), 1);
        let generation = GeneratorRequest {
            id: request.clone(),
            output_dir,
            protocol_id: model.protocol_id.clone(),
            trace_id: trace.id.0.clone(),
        };
        let prompt = generation.prompt(
            &self.mode_hint,
            self.prompt_context.as_ref().ok_or_else(|| {
                Error::illegal_state("synthesis prompt context is missing".to_string())
            })?,
            &trace,
        )?;
        self.commands
            .as_ref()
            .ok_or_else(|| Error::unknown("synthesis worker is not running".to_string()))?
            .send(AgentCommand::Generate {
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

    fn recv_one(&mut self, timeout: Duration) -> Result<Option<Outcome>, Error> {
        let Some(outcomes) = &self.outcomes else {
            return Ok(None);
        };
        match outcomes.recv_timeout(timeout) {
            Ok(outcome) => Ok(Some(outcome)),
            Err(mpsc::RecvTimeoutError::Timeout) => Ok(None),
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                self.outcomes = None;
                Ok(Some(Outcome::Failed(AgentFailure {
                    reason: "synthesis worker disconnected".to_string(),
                    fact: TerminalFact::default(),
                    timeout: false,
                })))
            }
        }
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
        while let Some(outcome) = self.recv_one(Duration::from_millis(1))? {
            self.consume(outcome, fuzzer, executor, state, manager)?;
        }
        state
            .metadata_or_insert_with(BitmapProgress::default)
            .set_synthesis_inflight(self.inflight);
        if self.paused {
            state
                .metadata_or_insert_with(BitmapProgress::default)
                .take_synthesis_request();
        } else if self.initialized
            && !self.inflight
            && state
                .metadata_or_insert_with(BitmapProgress::default)
                .take_synthesis_request()
        {
            self.dispatch_with(state.rand_mut())?;
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
            Outcome::Ready(fact) => {
                if let Err(error) = self.accept_initialization(&fact) {
                    self.note_failure(state, error.to_string());
                }
                self.mirror_session(state);
                Ok(())
            }
            Outcome::Failed(failure) => {
                let reason = failure.reason.clone();
                self.finish_request(true);
                self.note_terminal_failure(&failure);
                self.note_failure(state, reason);
                Ok(())
            }
            Outcome::Artifact(Err(reason), fact) => {
                self.finish_request(false);
                self.context.generated_err += 1;
                self.record_session("blocked", SessionPhase::Ready, &fact, Some(reason.clone()));
                self.note_failure(state, reason);
                Ok(())
            }
            Outcome::Artifact(Ok(ready), fact) => {
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
                        self.context.generated_ok += 1;
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
        let ready = fs::read(self.output.join("init/ready.json"))
            .map_err(|error| error.to_string())
            .and_then(|bytes| {
                serde_json::from_slice::<Value>(&bytes).map_err(|error| error.to_string())
            })
            .and_then(|ready| {
                if ready.get("status").and_then(Value::as_str) == Some("ready")
                    && ready.get("session").and_then(Value::as_str) == Some(self.session.as_str())
                {
                    Ok(())
                } else {
                    Err("ready.json is missing this campaign session".to_string())
                }
            });
        if let Err(reason) = ready {
            self.paused = true;
            self.record_session("blocked", SessionPhase::Paused, fact, Some(reason.clone()));
            return Err(Error::unknown(reason));
        }
        self.initialized = true;
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
        progress.attempted.clone_from(&self.context.attempted);
        progress.session = self.session_state.clone();
    }

    fn remember<S: HasMetadata>(&mut self, state: &mut S, ready: &ReadyInput) {
        let progress = state.metadata_or_insert_with(SynthesisProgress::default);
        progress.attempted.clone_from(&self.context.attempted);
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
        self.session_state.phase = phase;
        self.session_state.transition = transition.to_string();
        if let Some(terminal) = &fact.terminal {
            self.session_state.terminal = Some(self.redacted(terminal));
        }
        if let Some(state) = &fact.state {
            self.session_state.hc_state = Some(self.redacted(state));
        }
        if let Some(status) = &fact.status {
            self.session_state.hc_status = Some(self.redacted(status));
        }
        if let Some(error) = error.or(fact.error.clone()) {
            self.session_state.hc_error = Some(self.redacted(&error));
        } else if matches!(transition, "ready" | "result") {
            self.session_state.hc_error = None;
        }
        self.append_session_log();
    }

    fn append_session_log(&self) {
        let Ok(line) = serde_json::to_string(&self.session_state) else {
            return;
        };
        let path = self.output.join("session.jsonl");
        let Ok(mut file) = OpenOptions::new().create(true).append(true).open(path) else {
            return;
        };
        let _ = writeln!(file, "{line}");
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

pub(crate) fn redact_text(text: &str, secrets: &[String]) -> String {
    let mut out = text.to_string();
    for secret in secrets {
        if !secret.is_empty() {
            out = out.replace(secret, "[redacted]");
        }
    }
    out
}

fn wait_file(path: &Path, budget: Duration) -> Result<(), String> {
    let deadline = Instant::now() + budget;
    while Instant::now() < deadline {
        if path.is_file() {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(20));
    }
    Err(format!(
        "{} was not published before the deadline",
        path.display()
    ))
}

#[cfg(test)]
mod tests;
