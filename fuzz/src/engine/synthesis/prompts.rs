//! Markdown prompt templates rendered with MiniJinja.

use std::{
    collections::BTreeSet,
    path::{absolute, Path, PathBuf},
};

use minijinja::{value::Serde, AutoEscape, Environment, UndefinedBehavior};
use serde::Serialize;
use serde_json::Value;

use crate::{
    app::config::Campaign,
    protocol::{load_model, Binding, IndexedModel, PartyRole, StepTrace},
};

const INITIALIZATION_TEMPLATE: &str = include_str!(concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/prompts/synthesis-init.md"
));
const GENERATION_TEMPLATE: &str = include_str!(concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/prompts/synthesis-generation.md"
));

/// Shared, read-only campaign facts. Agent environment values are deliberately excluded.
#[derive(Clone, Serialize)]
pub(super) struct CampaignContext {
    campaign_dir: PathBuf,
    workspace: PathBuf,
    model: PathBuf,
    documents_dir: PathBuf,
    python: PathBuf,
    pythonpath: PathBuf,
    generator_skill: PathBuf,
    generator_validator: PathBuf,
    target: TargetContext,
    budgets: Budgets,
}

#[derive(Clone, Serialize)]
struct TargetContext {
    binary: PathBuf,
    workdir: PathBuf,
    fixture: PathBuf,
    config_template: PathBuf,
    config_file: PathBuf,
    args: Vec<String>,
    host: String,
    configured_port: u16,
}

#[derive(Clone, Serialize)]
struct Budgets {
    initialization_secs: u64,
    generation_secs: u64,
    connect_ms: u64,
    io_ms: u64,
    case_ms: u64,
    exit_grace_ms: u64,
}

impl CampaignContext {
    pub(super) fn from_campaign(campaign: &Campaign) -> Result<Self, libafl::Error> {
        let model = absolute(&campaign.protocol_generation.model)?;
        Ok(Self {
            campaign_dir: absolute(&campaign.base)?,
            workspace: absolute(&campaign.protocol_generation.output)?,
            documents_dir: model
                .parent()
                .expect("absolute model path has a parent")
                .to_path_buf(),
            model,
            python: absolute(&campaign.campaign.python)?,
            pythonpath: absolute(&campaign.campaign.pythonpath)?,
            generator_skill: PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("skills/protocol-modules/SKILL.md"),
            generator_validator: PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("skills/protocol-modules/scripts/validate_generator.py"),
            target: TargetContext {
                binary: absolute(&campaign.target.binary)?,
                workdir: absolute(&campaign.target.workdir)?,
                fixture: absolute(&campaign.target.fixture)?,
                config_template: absolute(&campaign.target.config_template)?,
                config_file: absolute(campaign.server_config())?,
                args: campaign.target.args.clone(),
                host: campaign.target.host.clone(),
                configured_port: campaign.target.port,
            },
            budgets: Budgets {
                initialization_secs: campaign.protocol_generation.init_timeout_secs.max(1),
                generation_secs: campaign.protocol_generation.task_timeout_secs.max(1),
                connect_ms: campaign.timeouts.connect_ms,
                io_ms: campaign.timeouts.io_ms,
                case_ms: campaign.timeouts.case_ms,
                exit_grace_ms: campaign.timeouts.exit_grace_ms,
            },
        })
    }
}

/// Who launches the SUT. Production and evaluation share one template.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ExecutionMode {
    /// The fuzzer restores fixtures and owns the SUT process.
    Fuzzer,
    /// The evaluation framework starts an ordinary SUT process.
    Evaluation,
}

/// Prompt facts shared by the production stage and the evaluation binary.
///
/// This value does not know about HC, worker threads, or stage progress.
pub struct SynthesisPromptContext {
    campaign: CampaignContext,
    model: IndexedModel,
    binding: Binding,
    mode: ExecutionMode,
}

impl SynthesisPromptContext {
    /// Load the model and party binding described by a campaign snapshot.
    pub fn from_campaign(campaign: &Campaign) -> Result<Self, libafl::Error> {
        if campaign.protocol_generation.model.as_os_str().is_empty() {
            return Err(libafl::Error::illegal_argument(
                "protocol_generation.model is required to build a synthesis prompt".to_string(),
            ));
        }
        let model = load_model(&campaign.protocol_generation.model)?;
        let parties = campaign.parties.as_ref().ok_or_else(|| {
            libafl::Error::illegal_argument(
                "synthesis prompts require [parties].sut and [parties].generator".to_string(),
            )
        })?;
        let binding = Binding::new(parties.resolve(&model.party_names())?);
        Ok(Self {
            campaign: CampaignContext::from_campaign(campaign)?,
            model,
            binding,
            mode: ExecutionMode::Fuzzer,
        })
    }

    /// Describe SUT startup without changing the template.
    pub fn with_execution(mut self, mode: ExecutionMode) -> Self {
        self.mode = mode;
        self
    }

    pub fn model(&self) -> &IndexedModel {
        &self.model
    }

    pub fn binding(&self) -> &Binding {
        &self.binding
    }

    pub fn python(&self) -> &Path {
        &self.campaign.python
    }

    /// Installed-module guidance shared by initialization and generation.
    pub fn generator_skill(&self) -> &Path {
        &self.campaign.generator_skill
    }

    /// Initialization prompt. `hold` is the test-only `HOLD=` flag.
    pub fn initialization_prompt(
        &self,
        session: &str,
        hold: bool,
        output_dir: &Path,
    ) -> Result<String, libafl::Error> {
        let parties = self
            .model
            .described_parties()
            .into_iter()
            .map(|(name, description)| {
                serde_json::json!({
                    "name": name,
                    "description": description,
                    "role": role_label(self.binding.role(&name)),
                })
            })
            .collect::<Vec<_>>();
        let documents = self
            .model
            .document_paths()
            .iter()
            .map(|(rfc, path)| {
                Ok(serde_json::json!({
                    "rfc": rfc,
                    "path": absolute(path)?,
                }))
            })
            .collect::<Result<Vec<_>, libafl::Error>>()?;
        let ready_json = serde_json::to_string_pretty(&serde_json::json!({
            "status": "ready",
            "session": session,
        }))
        .map_err(|error| libafl::Error::serialize(error.to_string()))?;
        render_initialization(serde_json::json!({
            "campaign": self.campaign,
            "execution": execution_text(self.mode),
            "session": session,
            "hold": if hold { "1" } else { "0" },
            "output_dir": absolute(output_dir)?,
            "protocol_id": self.model.protocol_id,
            "protocol_name": self.model.protocol_name,
            "model_complete": self.model.complete,
            "parties": parties,
            "documents": documents,
            "ready_json": ready_json,
        }))
        .map_err(|error| libafl::Error::serialize(format!("render initialization prompt: {error}")))
    }

    /// One generation prompt for an already chosen trace.
    pub fn generation_prompt(
        &self,
        output_dir: &Path,
        request_id: &str,
        mode: &str,
        trace: &StepTrace,
    ) -> Result<String, libafl::Error> {
        let output_dir = absolute(output_dir)?;
        let roles = self
            .model
            .described_parties()
            .into_iter()
            .map(|(name, description)| {
                serde_json::json!({
                    "role": role_label(self.binding.role(&name)),
                    "name": name,
                    "description": description,
                })
            })
            .collect::<Vec<_>>();
        let result_ok_json = serde_json::json!({
            "request_id": request_id,
            "protocol_id": self.model.protocol_id,
            "trace_id": trace.id.0,
            "status": "ok",
            "added_steps": [],
        })
        .to_string();
        let result_error_json = serde_json::json!({
            "request_id": request_id,
            "protocol_id": self.model.protocol_id,
            "trace_id": trace.id.0,
            "status": "error",
            "error": "",
            "added_steps": [],
        })
        .to_string();
        render_generation(serde_json::json!({
            "campaign": self.campaign,
            "execution": execution_text(self.mode),
            "output_dir": output_dir,
            "request_id": request_id,
            "protocol_id": self.model.protocol_id,
            "protocol_name": self.model.protocol_name,
            "trace_id": trace.id.0,
            "mode": mode,
            "receipt": output_dir.join("receipt.json"),
            "roles": roles,
            "generator_parties": generator_parties(&self.model, &self.binding, trace),
            "result_ok_json": result_ok_json,
            "result_error_json": result_error_json,
        }))
        .map_err(|error| libafl::Error::serialize(format!("render generator prompt: {error}")))
    }
}

fn execution_text(mode: ExecutionMode) -> Value {
    let (summary, lifecycle) = match mode {
        ExecutionMode::Fuzzer => (
            "Rust and LibAFL run the real system under test (SUT), select target step traces, mutate input parameters, execute cases, and collect coverage. The fuzzer runs the real SUT; implement every necessary generator-side participant and supporting service that lets the selected interactions occur. Do not start, replace, or terminate a fuzzer-managed SUT.",
            "These paths describe runtime endpoints and fixture resources only. Do not use them to inspect or assess SUT implementation capabilities. The fuzzer restores fixtures, renders placeholders such as @PORT@, and launches the SUT. Do not edit these files or start, replace, or terminate a fuzzer-managed SUT.",
        ),
        ExecutionMode::Evaluation => (
            "The evaluation framework starts the real SUT as an ordinary process for this instance and replays the published generator against it. Implement every necessary generator-side participant and supporting service that lets the selected interactions occur. Do not start, replace, or terminate an evaluation-managed SUT.",
            "These paths describe runtime endpoints and fixture resources only. Do not use them to inspect or assess SUT implementation capabilities. The evaluation framework restores fixtures, renders placeholders such as @PORT@, and launches the SUT. Do not edit these files or start, replace, or terminate an evaluation-managed SUT.",
        ),
    };
    serde_json::json!({ "summary": summary, "lifecycle": lifecycle })
}

pub(super) fn generator_parties(
    model: &IndexedModel,
    binding: &Binding,
    trace: &StepTrace,
) -> Vec<String> {
    let mut names = BTreeSet::new();
    for step in &trace.steps {
        let mut parties = vec![step.sender_name.clone(), step.receiver_name.clone()];
        parties.extend(model.channel_party_names(&step.channel_name));
        for name in parties {
            if binding.role(&name) == PartyRole::Generator {
                names.insert(name);
            }
        }
    }
    names.into_iter().collect()
}

pub(super) fn role_label(role: PartyRole) -> &'static str {
    match role {
        PartyRole::Sut => "sut",
        PartyRole::Generator => "generator",
        PartyRole::Unavailable => "unavailable",
    }
}

fn render(template_name: &str, source: &str, context: Value) -> Result<String, minijinja::Error> {
    let mut environment = Environment::new();
    environment.set_undefined_behavior(UndefinedBehavior::Strict);
    environment.set_auto_escape_callback(|_| AutoEscape::None);
    environment.render_named_str(template_name, source, Serde(context))
}

pub(super) fn render_initialization(context: Value) -> Result<String, minijinja::Error> {
    render("synthesis-init.md", INITIALIZATION_TEMPLATE, context)
}

pub(super) fn render_generation(context: Value) -> Result<String, minijinja::Error> {
    render("synthesis-generation.md", GENERATION_TEMPLATE, context)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_prompt_fields_fail_rendering() {
        assert!(render_initialization(serde_json::json!({})).is_err());
        assert!(render_generation(serde_json::json!({})).is_err());
    }
}
