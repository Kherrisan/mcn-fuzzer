//! Asynchronous generator synthesis. At most one agent task is in flight.
//!
//! Coverage mode consumes a frozen proposal from the shared mailbox and gives
//! it to the agent as the sole source of synthesis requests.

use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
    time::Duration,
};

use libafl::{
    stages::{Restartable, Stage},
    state::HasCorpus,
    Error, HasMetadata,
};
use libafl_bolts::impl_serdeany;
use serde::{Deserialize, Serialize};

use crate::{
    agents::{command_line, CommandLine},
    app::config::{Campaign, GeneratorSynthesisConfig},
    coverage::proposal::ProposalMailbox,
    engine::input::GeneratorInput,
    engine::stagnation::BitmapProgress,
    protocol::Binding,
};

mod agent;
pub(crate) mod artifacts;
mod prompts;
mod queue;
mod request;

use agent::Client;
use request::GeneratorRequest;

pub use prompts::{ExecutionMode, SynthesisPromptContext};
pub use queue::{SynthesisCandidate, SynthesisCandidates};
pub(crate) use request::build_generator_input;
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

/// Canonical path of the accepted request's initial `params.json`.
pub(crate) fn initial_params_path(generator: &Path) -> Result<PathBuf, Error> {
    if let Some(params) = generator.parent().map(|dir| dir.join("params.json")).filter(|p| p.is_file()) {
        return fs::canonicalize(params).map_err(Error::from);
    }
    // Legacy publications kept params.json in the request directory.
    let request_dir = generator.parent().and_then(Path::parent).ok_or_else(|| {
        Error::illegal_state(format!(
            "published generator {} has no request directory",
            generator.display()
        ))
    })?;
    let params = request_dir.join("params.json");
    if params.file_name().and_then(|name| name.to_str()) != Some("params.json") {
        return Err(Error::illegal_state(format!(
            "refusing to publish {} as the initial parameter file",
            params.display()
        )));
    }
    if !params.is_file() {
        return Err(Error::illegal_state(format!(
            "synthesis initial params.json is missing at {}",
            params.display()
        )));
    }
    fs::canonicalize(&params).map_err(|error| {
        Error::illegal_state(format!(
            "canonicalizing synthesis params.json {}: {error}",
            params.display()
        ))
    })
}

/// Validate every input before making any generated file visible in published/.
pub(crate) fn accept_artifacts(
    dir: &Path, request_id: &str,
    build: impl FnOnce(
        serde_json::Value,
        crate::engine::input::InstanceParams,
        Option<artifacts::FieldManifest>,
        PathBuf,
    ) -> Result<ValidatedGenerator, String>,
) -> Result<ValidatedGenerator, String> {
    let result = artifacts::check_result(dir, request_id)?;
    let dir = dir.canonicalize().map_err(|e| e.to_string())?;
    let published = dir.join("published");
    if published.exists() {
        return Err("published generator path already exists".into());
    }
    // Stage on the same filesystem, and validate the exact bytes we publish.
    let staging = dir.join(format!(".publishing-{}", std::process::id()));
    fs::create_dir(&staging).map_err(|e| format!("creating {}: {e}", staging.display()))?;
    let commit = (|| {
        let mut files = vec!["generator.py", "params.schema.json", "params.json"];
        if dir.join("mutable_fields.json").is_file() {
            files.push("mutable_fields.json");
        }
        for file in files {
            fs::copy(dir.join(file), staging.join(file))
                .map_err(|e| format!("staging {file}: {e}"))?;
        }
        let (params, manifest) = artifacts::check_bundle(&staging)?;
        let ready = build(result, params, manifest, published.join("generator.py"))?;
        fs::rename(&staging, &published).map_err(|e| format!("publishing generator: {e}"))?;
        Ok(ready)
    })();
    if commit.is_err() {
        let _ = fs::remove_dir_all(staging);
    }
    commit
}

pub(crate) fn validate_historical_request(
    out: &Path, request_id: &str, session: &str,
) -> Result<(), String> {
    request::GeneratorRequest {
        output_dir: out.to_path_buf(),
        id: request_id.to_string(),
        session: session.to_string(),
    }.accept().map(|_| ())
}

/// Stage placed before mutation. Disabled campaigns do not load a model.
pub struct GeneratorSynthesisStage {
    enabled: bool,
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
    introspection: Option<crate::coverage::introspection::IntrospectionHandle>,
}

impl GeneratorSynthesisStage {
    pub fn from_campaign(campaign: &Campaign, mailbox: ProposalMailbox) -> Result<Self, Error> {
        let mut stage = Self::disabled(campaign, mailbox);
        if !campaign.generator_synthesis.enabled {
            return Ok(stage);
        }
        let context = SynthesisPromptContext::from_campaign(campaign)?;
        let boot = crate::agents::current_boot().ok_or_else(|| {
            Error::unknown(
                "generator synthesis is enabled but the private HC core was not started"
                    .to_string(),
            )
        })?;
        stage.enabled = true;
        stage.binding = context.binding().clone();
        stage.config = campaign.generator_synthesis.clone();
        stage.prompt_context = Some(context);
        stage.session = boot.session.clone();
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
            introspection: None,
        }
    }

    pub(crate) fn set_introspection(
        &mut self,
        handle: crate::coverage::introspection::IntrospectionHandle,
    ) {
        self.introspection = Some(handle);
    }

    fn spawn_worker(&mut self, socket: PathBuf) -> Result<(), Error> {
        let terminal = crate::agents::campaign_terminal_name("synthesis", &self.campaign_base);
        let client = Client::spawn(WorkerConfig {
            thread_name: "synthesis",
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

    pub fn shutdown(&mut self) {
        if self.enabled && !self.exited {
            self.exited = true;
        }
        self.worker.take();
    }

    fn dispatch(&mut self) -> Result<(), Error> {
        if !self.enabled || !self.initialized || self.paused || self.inflight {
            return Ok(());
        }
        let Some(accepted) = self.mailbox.take() else { return Ok(()); };
        let request = accepted.request_id.clone();
        let output_dir = self.output.join("requests").join(&request);
        if output_dir.exists() {
            return Err(Error::illegal_state(format!(
                "synthesis request {} already exists",
                output_dir.display()
            )));
        }
        fs::create_dir_all(&output_dir)?;
        let synthesis_proposal = accepted.proposal.synthesis_copy(&self.binding);
        let body = serde_json::to_vec_pretty(&synthesis_proposal).map_err(|error| {
            Error::serialize(format!("writing synthesis proposal: {error}"))
        })?;
        fs::write(
            output_dir.join("proposal.json"),
            [body.as_slice(), b"\n"].concat(),
        )?;
        if let Some(handle) = &self.introspection {
            if let Err(error) = handle.synthesis_started(&request) {
                log::warn!("introspection synthesis {request}: {error}");
            }
        }
        let generation = GeneratorRequest {
            id: request.clone(), output_dir, session: accepted.proposal.session.clone(),
        };
        let prompt = match generation.prompt(
            &self.mode_hint,
            self.prompt_context.as_ref().ok_or_else(|| {
                Error::illegal_state("synthesis prompt context is missing".to_string())
            })?,
        ) {
            Ok(prompt) => prompt,
            Err(error) => {
                self.fail_request(&request);
                return Err(error);
            }
        };
        let coverage_mcp = crate::coverage::mcp::McpClient::connect().ok();
        crate::evaluation::request_eval::capture(
            &generation.output_dir, &prompt, coverage_mcp.as_ref(), self.introspection.as_ref(),
        );
        let worker = self
            .worker
            .as_ref()
            .ok_or_else(|| Error::unknown("synthesis worker is not running".to_string()))?;
        if worker
            .send(AgentCommand::Run {
                request: generation,
                prompt,
            })
            .is_err()
        {
            self.fail_request(&request);
            return Err(Error::unknown("synthesis worker stopped".to_string()));
        }
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
    S: HasCorpus<GeneratorInput> + HasMetadata,
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
            if self.mailbox.peek().is_some() {
                state
                    .metadata_or_insert_with(BitmapProgress::default)
                    .take_synthesis_request();
                self.dispatch()?;
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
                self.fail_current();
                self.finish_request(true);
                self.note_terminal_failure(&failure);
                self.note_failure(state, reason);
                Ok(())
            }
            Outcome::Artifact {
                request,
                result: Err(reason),
                ..
            } => {
                self.fail_request(&request);
                self.finish_request(false);
                self.note_failure(state, reason);
                Ok(())
            }
            Outcome::Artifact {
                result: Ok(ready), ..
            } => {
                if self.closed_requests.contains(&ready.request) {
                    self.finish_request(false);
                    self.note_failure(
                        state,
                        "ignored a late result for a closed request".to_string(),
                    );
                    return Ok(());
                }
                self.publish_ready(&ready)?;
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

    fn fail_current(&self) {
        let Some(request) = &self.current_request else {
            return;
        };
        self.fail_request(request);
    }

    fn fail_request(&self, request: &str) {
        let Some(handle) = &self.introspection else {
            return;
        };
        if let Err(error) = handle.mark_failed(request) {
            log::debug!("introspection failure for {request} was not applied: {error}");
        }
    }

    fn publish_ready(&self, ready: &ReadyInput) -> Result<(), Error> {
        let Some(handle) = &self.introspection else {
            return Ok(());
        };
        let request = ready.origin.request_id.as_str();
        let params_json = initial_params_path(&ready.input.generator)?;
        let artifacts = crate::coverage::introspection::GeneratorArtifacts {
            generator_py: ready.input.generator.clone(),
            params_json,
        };
        let outcome = handle
            .publish(request, artifacts, None)
            .map_err(Error::illegal_state);
        if outcome.is_ok() {
            // Present counts describe the published generator's original trace,
            // deduplicated by generator path so a repeated publish cannot
            // double count.
            let trace = crate::coverage::step_stats::TraceView::of(&ready.input);
            handle.step_present(&ready.input.generator, &trace);
        }
        outcome
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
