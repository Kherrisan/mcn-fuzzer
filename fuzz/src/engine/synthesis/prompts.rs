//! Markdown prompt templates rendered with MiniJinja.

use std::{
    fs,
    path::{absolute, Path, PathBuf},
};

use minijinja::{value::Serde, AutoEscape, Environment, UndefinedBehavior};
use serde::Serialize;
use serde_json::Value;

use crate::{
    app::config::Campaign,
    protocol::{load_model, Binding, IndexedModel, PartyRole},
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
    python: PathBuf,
    pythonpath: PathBuf,
    generator_skill: PathBuf,
    generator_validator: PathBuf,
    catalog: PathBuf,
    proposal_tool: PathBuf,
    extra_files: Vec<PathBuf>,
    target: TargetContext,
    budgets: Budgets,
}

#[derive(Clone, Serialize)]
struct TargetContext {
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
        let model = absolute(&campaign.generator_synthesis.model)?;
        let mut extra_files = Vec::new();
        for (index, path) in campaign.generator_synthesis.extra_files.iter().enumerate() {
            let absolute_path = absolute(path)?;
            let context = format!(
                "generator_synthesis.extra_files[{index}] {}",
                absolute_path.display()
            );
            let metadata = fs::metadata(&absolute_path)
                .map_err(|error| libafl::Error::illegal_argument(format!("{context}: {error}")))?;
            if !metadata.is_file() {
                return Err(libafl::Error::illegal_argument(format!(
                    "{context}: not a regular file"
                )));
            }
            fs::File::open(&absolute_path)
                .map_err(|error| libafl::Error::illegal_argument(format!("{context}: {error}")))?;
            extra_files.push(absolute_path);
        }
        Ok(Self {
            campaign_dir: absolute(&campaign.base)?,
            workspace: absolute(&campaign.generator_synthesis.output)?,
            model,
            python: absolute(&campaign.campaign.python)?,
            pythonpath: absolute(&campaign.campaign.pythonpath)?,
            generator_skill: PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("skills/protocol-modules/SKILL.md"),
            generator_validator: PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("skills/protocol-modules/scripts/validate_generator.py"),
            catalog: absolute(&campaign.generator_synthesis.output)?.join("catalog.json"),
            proposal_tool: absolute(&campaign.campaign.pythonpath)?
                .join("coverage_analysis")
                .join("server.py"),
            extra_files,
            target: TargetContext {
                host: campaign.target.host.clone(),
                configured_port: campaign.target.port,
            },
            budgets: Budgets {
                initialization_secs: campaign.generator_synthesis.init_timeout_secs.max(1),
                generation_secs: campaign.generator_synthesis.task_timeout_secs.max(1),
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
    /// The fuzzer runs the optional cleanup script before each case and owns the SUT process.
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
        if campaign.generator_synthesis.model.as_os_str().is_empty() {
            return Err(libafl::Error::illegal_argument(
                "generator_synthesis.model is required to build a synthesis prompt".to_string(),
            ));
        }
        let model = load_model(&campaign.generator_synthesis.model)?;
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

    pub fn catalog(&self) -> &Path {
        &self.campaign.catalog
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
            "protocol_name": self.model.protocol_name,
            "model_complete": self.model.complete,
            "parties": parties,
            "ready_json": ready_json,
        }))
        .map_err(|error| libafl::Error::serialize(format!("render initialization prompt: {error}")))
    }

    /// One generation prompt. A coverage proposal is copied into `output_dir`;
    /// autonomous mode asks the agent to write and validate its own proposal.
    pub fn generation_prompt(
        &self,
        output_dir: &Path,
        request_id: &str,
        session: &str,
        mode: &str,
        proposal: Option<&Path>,
        autonomous: bool,
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
        let proposal_path = proposal.map(|path| absolute(path)).transpose()?;
        let result_ok_json = serde_json::json!({
            "request_id": request_id,
            "status": "ok",
            "added_steps": [],
        })
        .to_string();
        let result_error_json = serde_json::json!({
            "request_id": request_id,
            "status": "error",
            "error": "",
            "added_steps": [],
        })
        .to_string();
        let draft_path = output_dir.join("proposal.draft.json");
        render_generation(serde_json::json!({
            "campaign": self.campaign,
            "execution": execution_text(self.mode),
            "output_dir": output_dir,
            "request_id": request_id,
            "session": session,
            "protocol_name": self.model.protocol_name,
            "mode": mode,
            "autonomous": autonomous,
            "proposal": proposal_path,
            "proposal_draft": draft_path,
            "proposal_tool": self.campaign.proposal_tool,
            "catalog": self.campaign.catalog,
            "roles": roles,
            "generator_parties": self
                .model
                .described_parties()
                .into_iter()
                .filter(|(name, _)| self.binding.role(name) == PartyRole::Generator)
                .map(|(name, _)| name)
                .collect::<Vec<_>>(),
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
            "These paths describe runtime endpoints only. Do not use them to inspect or assess SUT implementation capabilities. The fuzzer runs the configured cleanup script before each case and launches the SUT. Do not edit campaign files or start, replace, or terminate a fuzzer-managed SUT.",
        ),
        ExecutionMode::Evaluation => (
            "The evaluation framework starts the real SUT as an ordinary process for this instance and replays the published generator against it. Implement every necessary generator-side participant and supporting service that lets the selected interactions occur. Do not start, replace, or terminate an evaluation-managed SUT.",
            "These paths describe runtime endpoints only. Do not use them to inspect or assess SUT implementation capabilities. The evaluation framework runs the configured cleanup script before each case and launches the SUT. Do not edit campaign files or start, replace, or terminate an evaluation-managed SUT.",
        ),
    };
    serde_json::json!({ "summary": summary, "lifecycle": lifecycle })
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

#[cfg(test)]
mod extra_file_tests {
    use super::*;
    use std::{
        fs,
        os::unix::fs::{symlink, PermissionsExt},
        path::Path,
    };

    fn campaign(dir: &Path, extra: &[&str]) -> Campaign {
        let model = dir.join("protocol.json");
        fs::write(
            &model,
            serde_json::json!({
                "meta": {"protocol": "demo", "complete": true, "documents": []},
                "parties": [{"name": "client", "description": "c"}, {"name": "server", "description": "s"}],
                "messages": [{"name": "hello", "associated_fields": []}],
                "channels": [{"name": "control", "description": "c", "transport": "TCP",
                              "connection": "connection_oriented",
                              "endpoint_party_names": ["client", "server"],
                              "initiator_party_name": "client", "acceptor_party_name": "server",
                              "endpoint_sources": []}],
                "steps": [{"name": "hello", "action": "send", "message_name": "hello",
                           "channel_name": "control", "sender_name": "client",
                           "receiver_name": "server", "condition": "start"}],
                "step_dependencies": [],
                "unresolved": []
            })
            .to_string(),
        )
        .unwrap();
        let quoted = extra
            .iter()
            .map(|name| format!("{:?}", name))
            .collect::<Vec<_>>()
            .join(", ");
        let path = dir.join("campaign.toml");
        fs::write(
            &path,
            format!(
                r#"
[target]
binary = "b"
workdir = "w"
cleanup_script = "cleanup.sh"
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
[parties]
sut = ["server"]
generator = ["client"]
[generator_synthesis]
enabled = false
model = "protocol.json"
output = "out"
extra_files = [{quoted}]
"#
            ),
        )
        .unwrap();
        Campaign::load(&path).unwrap()
    }

    #[test]
    fn extra_files_are_validated_and_listed_as_absolute_paths() {
        let dir = std::env::temp_dir().join(format!("mcn-extra-prompt-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("real.conf"), b"account=ubuntu\n").unwrap();
        symlink(dir.join("real.conf"), dir.join("linked.conf")).unwrap();
        let campaign = campaign(&dir, &["real.conf", "linked.conf"]);
        let context = SynthesisPromptContext::from_campaign(&campaign)
            .unwrap()
            .with_execution(ExecutionMode::Fuzzer);
        let init = context
            .initialization_prompt("s", false, &dir.join("init"))
            .unwrap();
        let generation = context
            .generation_prompt(&dir.join("request"), "r", "s", "", None, false)
            .unwrap();
        for prompt in [&init, &generation] {
            assert!(prompt.contains(&dir.join("real.conf").display().to_string()));
            assert!(prompt.contains(&dir.join("linked.conf").display().to_string()));
            assert!(prompt.contains("deployment") || prompt.contains("account"));
            assert!(
                !prompt.contains("account=ubuntu"),
                "file content was injected"
            );
            assert!(!prompt.contains("Protocol document directory"));
            assert!(!prompt.contains("SUT binary:"));
            assert!(prompt.contains("Do not read RFC documents"));
            assert!(prompt.contains("internet searches"));
            assert!(prompt.contains("never infer"));
            assert!(prompt.contains("even when malformed"));
            assert!(prompt.contains("generator_server/template.py"));
            assert!(prompt.contains("generator_server/params.schema.json"));
        }
        assert!(init.contains("EXTRA_REFERENCE_FILES"));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn missing_directory_and_unreadable_extra_files_are_explicit_errors() {
        let dir = std::env::temp_dir().join(format!("mcn-extra-errors-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        fs::create_dir_all(dir.join("directory.conf")).unwrap();
        fs::write(dir.join("secret.conf"), b"x").unwrap();
        let mut permissions = fs::metadata(dir.join("secret.conf")).unwrap().permissions();
        permissions.set_mode(0o000);
        fs::set_permissions(dir.join("secret.conf"), permissions).unwrap();

        for (name, needle) in [
            ("missing.conf", "not found"),
            ("directory.conf", "not a regular file"),
        ] {
            let campaign = campaign(&dir, &[name]);
            let error = match SynthesisPromptContext::from_campaign(&campaign) {
                Ok(_) => panic!("expected an extra_files error for {name}"),
                Err(error) => error,
            };
            let message = format!("{error:?}");
            assert!(
                message.contains("generator_synthesis.extra_files[0]"),
                "{message}"
            );
            assert!(message.contains(name), "{message}");
            assert!(
                message.contains(needle) || error.to_string().contains("No such file"),
                "{message}"
            );
        }
        if unsafe { libc::geteuid() } != 0 {
            let campaign = campaign(&dir, &["secret.conf"]);
            let error = match SynthesisPromptContext::from_campaign(&campaign) {
                Ok(_) => panic!("expected an unreadable extra_files error"),
                Err(error) => error,
            };
            let message = format!("{error:?}");
            assert!(
                message.contains("generator_synthesis.extra_files[0]"),
                "{message}"
            );
            assert!(message.contains("secret.conf"), "{message}");
        }
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn empty_extra_files_render_the_no_files_message() {
        let dir = std::env::temp_dir().join(format!("mcn-extra-empty-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let campaign = campaign(&dir, &[]);
        let context = SynthesisPromptContext::from_campaign(&campaign).unwrap();
        let init = context
            .initialization_prompt("s", false, &dir.join("init"))
            .unwrap();
        let generation = context
            .generation_prompt(&dir.join("request"), "r", "s", "", None, false)
            .unwrap();
        assert!(init.contains("none provided"));
        assert!(generation.contains("none provided"));
        let _ = fs::remove_dir_all(&dir);
    }
}
