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
    stages::{Restartable, Stage},
    state::{HasCorpus, HasRand},
    Error, HasMetadata,
};
use libafl_bolts::{impl_serdeany, rands::Rand};
use serde::{Deserialize, Serialize};

use crate::{
    agents::{command_line, CommandLine},
    app::config::{Campaign, GeneratorSynthesisConfig},
    coverage::proposal::ProposalMailbox,
    engine::input::{GeneratorInput, ParamValue},
    engine::stagnation::BitmapProgress,
    protocol::{write_catalog, Binding, IndexedModel},
};

mod agent;
mod artifacts;
mod prompts;
mod queue;
mod request;

use agent::Client;
use request::GeneratorRequest;

pub use prompts::{ExecutionMode, SynthesisPromptContext};
pub use queue::{SynthesisCandidate, SynthesisCandidates};
pub use request::ValidatedGenerator;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct TraceOrigin {
    pub request_id: String,
    pub session: String,
    pub step_names: Vec<String>,
    pub added_steps: Vec<String>,
}

impl_serdeany!(TraceOrigin);

pub(crate) use crate::agents::redact_text;
use crate::agents::{AgentFailure, AgentInit, AgentOutcome, SessionInit, WorkerConfig};

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct SynthesisProgress {
    pub attempted: BTreeSet<String>,
    pub paused: bool,
    pub published: BTreeMap<String, String>,
    pub failures: Vec<String>,
}

impl_serdeany!(SynthesisProgress);

type AgentCommand = crate::agents::AgentCommand<SessionInit, GeneratorRequest>;
type Outcome = AgentOutcome<(), ReadyInput>;

#[derive(Debug)]
pub(crate) struct ReadyInput {
    pub(crate) request: String,
    pub(crate) input: GeneratorInput,
    pub(crate) origin: TraceOrigin,
}

/// Run the production artifact checks and publish `generator.py` plus its schema.
pub fn validate_generator_artifacts(
    output_dir: &Path,
    request_id: &str,
) -> Result<ValidatedGenerator, String> {
    artifacts::check(output_dir, request_id).and_then(|(result, params)| {
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
        let generator =
            fs::canonicalize(published.join("generator.py")).map_err(|error| error.to_string())?;
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
                request_id: request_id.to_string(),
                session: String::new(),
                step_names: Vec::new(),
                added_steps: added,
            },
        })
    })
}

/// Stage placed before mutation. Disabled campaigns do not load a model.
pub struct GeneratorSynthesisStage {
    enabled: bool,
    model: Option<Arc<IndexedModel>>,
    binding: Binding,
    config: GeneratorSynthesisConfig,
    prompt_context: Option<SynthesisPromptContext>,
    output: PathBuf,
    campaign_base: PathBuf,
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
    exited: bool,
    /// Env values used only to redact text before it is stored. Never written.
    secrets: Vec<String>,
    mailbox: ProposalMailbox,
    analysis_enabled: bool,
}

impl GeneratorSynthesisStage {
    pub fn from_campaign(campaign: &Campaign, mailbox: ProposalMailbox) -> Result<Self, Error> {
        let mut stage = Self::disabled(campaign, mailbox);
        if !campaign.generator_synthesis.enabled {
            return Ok(stage);
        }
        let context = SynthesisPromptContext::from_campaign(campaign)?;
        let catalog_model = context.model().clone();
        let catalog_binding = context.binding().clone();
        let boot = crate::agents::current_boot().ok_or_else(|| {
            Error::unknown(
                "generator synthesis is enabled but the private HC core was not started"
                    .to_string(),
            )
        })?;
        stage.enabled = true;
        stage.model = Some(Arc::new(context.model().clone()));
        stage.binding = context.binding().clone();
        stage.config = campaign.generator_synthesis.clone();
        stage.prompt_context = Some(context);
        stage.session = boot.session.clone();
        stage.analysis_enabled = campaign.coverage.analysis_enabled;
        write_catalog(
            &catalog_model,
            &catalog_binding,
            &campaign.generator_synthesis.output.join("catalog.json"),
        )?;
        let launched = command_line(
            campaign
                .agents
                .profiles
                .get(&campaign.generator_synthesis.profile)
                .ok_or_else(|| {
                    Error::illegal_argument(format!(
                        "agent profile {} is not defined",
                        campaign.generator_synthesis.profile
                    ))
                })?,
            &std::path::absolute(&campaign.generator_synthesis.output)?,
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
            config: campaign.generator_synthesis.clone(),
            prompt_context: None,
            output: campaign.generator_synthesis.output.clone(),
            campaign_base: campaign.base.clone(),
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
            exited: false,
            secrets: Vec::new(),
            mailbox,
            analysis_enabled: campaign.coverage.analysis_enabled,
        }
    }

    fn spawn_worker(&mut self, socket: PathBuf) -> Result<(), Error> {
        let terminal = crate::agents::campaign_terminal_name("synthesis", &self.campaign_base);
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
    fn wait_initialized(
        &mut self,
        poll: &mut impl FnMut() -> Result<(), Error>,
    ) -> Result<(), Error> {
        if !self.enabled || self.initialized {
            return Ok(());
        }
        loop {
            poll()?;
            self.initialize()?;
            match self.recv_one(Duration::from_millis(200)) {
                Some(Outcome::Launched(_)) => {}
                Some(Outcome::Ready((), _)) => return self.accept_initialization(),
                Some(Outcome::InitTimedOut(failure)) => self.consume_init_timeout(failure)?,
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

    /// Block until the first candidate artifact is published. The caller
    /// enqueues it for baseline + mutation exploration; it is not evaluated here.
    pub(crate) fn bootstrap_first(
        &mut self,
        rand: &mut impl Rand,
        mut poll: impl FnMut() -> Result<(), Error>,
    ) -> Result<ReadyInput, Error> {
        self.wait_initialized(&mut poll)?;
        self.dispatch_with(rand, true)?;
        let deadline = Instant::now() + Duration::from_secs(self.config.task_timeout_secs.max(1));
        while Instant::now() < deadline {
            poll()?;
            if let Some(outcome) = self.recv_one(Duration::from_millis(200)) {
                if let Outcome::Artifact {
                    result: Ok(ready), ..
                } = outcome
                {
                    self.finish_request(false);
                    return Ok(ready);
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
        }
        self.worker.take();
    }

    /// `allow_autonomous` is only true for the seedless bootstrap.
    fn dispatch_with(&mut self, rand: &mut impl Rand, allow_autonomous: bool) -> Result<(), Error> {
        if !self.enabled || !self.initialized || self.paused || self.inflight {
            return Ok(());
        }
        if self.model.is_none() {
            return Ok(());
        }
        let proposal = if self.analysis_enabled && !allow_autonomous {
            let Some(proposal) = self.mailbox.take() else {
                return Ok(());
            };
            Some(proposal)
        } else {
            None
        };
        let autonomous = proposal.is_none();
        let (request, output_dir, session) = if let Some(accepted) = &proposal {
            let request = accepted.request_id.clone();
            let output_dir = self.output.join("requests").join(&request);
            if output_dir.exists() {
                return Err(Error::illegal_state(format!(
                    "synthesis request {} already exists",
                    output_dir.display()
                )));
            }
            fs::create_dir_all(&output_dir)?;
            let synthesis_proposal = accepted
                .proposal
                .synthesis_copy()
                .map_err(Error::illegal_state)?;
            let body = serde_json::to_vec_pretty(&synthesis_proposal).map_err(|error| {
                Error::serialize(format!("writing synthesis proposal: {error}"))
            })?;
            fs::write(
                output_dir.join("proposal.json"),
                [body.as_slice(), b"\n"].concat(),
            )?;
            (request, output_dir, accepted.proposal.session.clone())
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
            (request, output_dir, self.session.clone())
        };
        let generation = GeneratorRequest {
            id: request.clone(),
            output_dir,
            session,
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
        _fuzzer: &mut Z,
        _executor: &mut E,
        state: &mut S,
        _manager: &mut EM,
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
            self.consume(outcome, state)?;
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
                .synthesis_due()
        {
            // Coverage mode must keep the trigger pending until a proposal
            // arrives; taking it early would drop the only dispatch signal.
            if !(self.analysis_enabled && self.mailbox.peek().is_none()) {
                state
                    .metadata_or_insert_with(BitmapProgress::default)
                    .take_synthesis_request();
                self.dispatch_with(state.rand_mut(), false)?;
            }
        }
        state
            .metadata_or_insert_with(BitmapProgress::default)
            .set_synthesis_inflight(self.inflight);
        Ok(())
    }
}

impl GeneratorSynthesisStage {
    fn consume<S>(&mut self, outcome: Outcome, state: &mut S) -> Result<(), Error>
    where
        S: HasCorpus<GeneratorInput> + HasMetadata,
    {
        match outcome {
            Outcome::Launched(_) => Ok(()),
            Outcome::Ready((), _) => {
                if let Err(error) = self.accept_initialization() {
                    self.note_failure(state, error.to_string());
                }
                Ok(())
            }
            Outcome::InitTimedOut(failure) => {
                let result = self.consume_init_timeout(failure);
                        result
            }
            Outcome::Failed(failure) => {
                if !self.initialized {
                    let reason = self.redacted(&failure.reason);
                    self.note_terminal_failure(&failure);
                    self.note_failure(state, reason.clone());
                    return Err(Error::unknown(format!(
                        "synthesis initialization failed: {reason}"
                    )));
                }
                let reason = failure.reason.clone();
                self.finish_request(true);
                self.note_terminal_failure(&failure);
                self.note_failure(state, reason);
                Ok(())
            }
            Outcome::Artifact {
                result: Err(reason),
                ..
            } => {
                self.finish_request(false);
                self.note_failure(state, reason);
                Ok(())
            }
            Outcome::Artifact {
                result: Ok(ready),
                ..
            } => {
                if self.closed_requests.contains(&ready.request) {
                    self.finish_request(false);
                    self.note_failure(
                        state,
                        "ignored a late result for a closed request".to_string(),
                    );
                    return Ok(());
                }
                self.finish_request(false);
                // Accepted artifacts enter the mutation FIFO, never the
                // corpus as initial parameters. The ordinary mutation stage
                // runs the baseline once and then mutates through feedback.
                self.remember(state, &ready);
                let candidates = state.metadata_or_insert_with(SynthesisCandidates::default);
                candidates.enqueue(ready.request.clone(), ready.input, ready.origin);
                if let Err(error) = candidates.save(&self.output.join("candidates.json")) {
                    log::warn!("persisting synthesis candidates failed: {error:?}");
                }
                log::info!(
                    "synthesis candidate {} queued for mutation exploration",
                    ready.request
                );
                Ok(())
            }
        }
    }

    fn consume_init_timeout(&mut self, failure: AgentFailure) -> Result<(), Error> {
        self.init_timeouts = self.init_timeouts.saturating_add(1);
        let reason = format!(
            "synthesis initialization timed out {} times (limit {}): {}",
            self.init_timeouts,
            crate::agents::INIT_TIMEOUT_LIMIT,
            self.redacted(&failure.reason)
        );
        self.paused = self.init_timeouts >= crate::agents::INIT_TIMEOUT_LIMIT;
        if self.paused {
            return Err(Error::unknown(reason));
        }
        Ok(())
    }

    fn accept_initialization(&mut self) -> Result<(), Error> {
        let ready = SessionInit {
            output_dir: self.output.join("init"),
            session: self.session.clone(),
        }
        .accept();
        if let Err(reason) = ready {
            self.paused = true;
            return Err(Error::unknown(reason));
        }
        self.initialized = true;
        self.init_timeouts = 0;
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
    }

    fn remember<S: HasMetadata>(&mut self, state: &mut S, ready: &ReadyInput) {
        let progress = state.metadata_or_insert_with(SynthesisProgress::default);
        progress.attempted.insert(ready.origin.request_id.clone());
        progress.published.insert(
            ready.origin.request_id.clone(),
            ready.input.generator.display().to_string(),
        );
    }

    fn note_terminal_failure(&mut self, _failure: &AgentFailure) {
        self.paused = true;
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
