//! One `profile × case × repeat` snapshot. Later commands trust its digest.

use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::{
    app::config::Campaign,
    engine::executor::{restore_tree, write_server_config},
    engine::synthesis::{ExecutionMode, SynthesisPromptContext},
    protocol::{covers_sut_and_generator, write_receipt, StepTrace},
};

use super::config::Suite;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Task {
    pub version: u32,
    pub profile: String,
    pub provider: String,
    pub case_name: String,
    pub repeat: u32,
    pub instance_dir: PathBuf,
    pub request_id: String,
    pub logical_session: String,
    pub protocol_id: String,
    pub trace_id: String,
    pub step_names: Vec<String>,
    pub digest: String,
    pub sources: Sources,
    pub target: TargetSnap,
    pub python: PathBuf,
    pub pythonpath: PathBuf,
    pub executable: PathBuf,
    pub model_name: Option<String>,
    pub effort: Option<String>,
    pub profile_args: Vec<String>,
    pub cli_version: Option<String>,
    pub provider_session: Option<String>,
    pub validation_digest: Option<String>,
    pub stages: BTreeMap<String, StageRecord>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Sources {
    pub suite: PathBuf,
    pub campaign: PathBuf,
    pub model: PathBuf,
    pub init_template: PathBuf,
    pub generation_template: PathBuf,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub generator_skill: Option<PathBuf>,
    pub binary: PathBuf,
    pub fixture: PathBuf,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct TargetSnap {
    pub binary: PathBuf,
    pub args: Vec<String>,
    #[serde(default)]
    pub extra_env: Vec<(String, String)>,
    pub host: String,
    pub fixture: PathBuf,
    pub workdir_snapshot: PathBuf,
    pub config_template: PathBuf,
    pub config_output: String,
    pub cert: String,
    pub key: String,
    pub ca: Option<String>,
    pub case_ms: u64,
    pub connect_ms: u64,
    pub io_ms: u64,
    pub ready_ms: u64,
    pub sut_exit_ms: u64,
    pub term_grace_ms: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct StageRecord {
    pub status: String,
    pub duration_ms: Option<u64>,
    pub failure_kind: Option<String>,
    pub message: Option<String>,
    pub artifacts: Vec<PathBuf>,
}

impl StageRecord {
    pub fn not_run() -> Self {
        Self {
            status: "not_run".to_string(),
            duration_ms: None,
            failure_kind: None,
            message: None,
            artifacts: Vec::new(),
        }
    }

    pub fn passed(elapsed_ms: u64, artifacts: Vec<PathBuf>) -> Self {
        Self {
            status: "passed".to_string(),
            duration_ms: Some(elapsed_ms),
            failure_kind: None,
            message: None,
            artifacts,
        }
    }

    pub fn failed(elapsed_ms: u64, kind: &str, message: impl Into<String>) -> Self {
        Self {
            status: "failed".to_string(),
            duration_ms: Some(elapsed_ms),
            failure_kind: Some(kind.to_string()),
            message: Some(message.into()),
            artifacts: Vec::new(),
        }
    }
}

impl Task {
    pub fn load(path: &Path) -> Result<Self, String> {
        let bytes =
            fs::read(path).map_err(|error| format!("reading {}: {error}", path.display()))?;
        serde_json::from_slice(&bytes)
            .map_err(|error| format!("parsing {}: {error}", path.display()))
    }

    pub fn save(&self) -> Result<(), String> {
        let path = self.instance_dir.join("task.json");
        let text = serde_json::to_string_pretty(self).map_err(|error| error.to_string())?;
        fs::write(path, text).map_err(|error| error.to_string())
    }

    pub fn workspace(&self) -> PathBuf {
        self.instance_dir.join("workspace")
    }

    pub fn prepared(&self) -> PathBuf {
        self.instance_dir.join("prepared")
    }

    pub fn request_dir(&self) -> PathBuf {
        self.workspace().join("requests").join(&self.request_id)
    }

    pub fn ensure_digest(&self) -> Result<(), String> {
        let current = source_digest(&self.sources)?;
        if current != self.digest {
            return Err("task sources changed since prepare; run prepare again".to_string());
        }
        Ok(())
    }
}

pub fn prepare_instance(
    suite: &Suite,
    campaign: &Campaign,
    profile_name: &str,
    case_name: &str,
    repeat: u32,
    out: &Path,
) -> Result<Task, String> {
    let case = suite
        .cases
        .iter()
        .find(|case| case.name == case_name)
        .ok_or_else(|| format!("suite has no case {case_name}"))?;
    let profile = campaign
        .agents
        .profiles
        .get(profile_name)
        .ok_or_else(|| format!("agent profile {profile_name} is not defined"))?;
    let instance = out
        .join(profile_name)
        .join(case_name)
        .join(repeat.to_string());
    if instance.join("task.json").exists() {
        return Err(format!(
            "instance {} is already prepared",
            instance.display()
        ));
    }
    fs::create_dir_all(instance.join("prepared/prompts")).map_err(|error| error.to_string())?;
    fs::create_dir_all(instance.join("agent")).map_err(|error| error.to_string())?;
    let fixture = instance.join("prepared/fixture");
    restore_tree(&campaign.target.fixture, &fixture).map_err(|error| error.to_string())?;
    let workdir = instance.join("prepared/workdir");
    restore_tree(&campaign.target.fixture, &workdir).map_err(|error| error.to_string())?;
    let mut local = campaign.clone();
    local.parties = Some(suite.parties());
    local.target.fixture = fixture.clone();
    local.target.workdir = workdir.clone();
    local.protocol_generation.output = instance.join("workspace");
    let context = SynthesisPromptContext::from_campaign(&local)
        .map_err(|error| error.to_string())?
        .with_execution(ExecutionMode::Evaluation);
    let names = suite.sequence(case);
    let trace = trace_from_names(context.model(), context.binding(), &names)?;
    let request_id = format!("{case_name}-r{repeat}");
    let logical_session = stable_uuid(&format!(
        "{}:{profile_name}:{case_name}:{repeat}:{}",
        context.model().protocol_id,
        instance.display()
    ));
    write_receipt(
        &instance.join("prepared/receipt.json"),
        context.model(),
        context.binding(),
        &trace,
    )
    .map_err(|error| error.to_string())?;
    let trace_doc = serde_json::json!({
        "trace_id": trace.id.0,
        "steps": trace.steps.iter().map(|step| serde_json::json!({
            "name": step.name,
            "source_id": step.source_id,
            "step_id": step.id().0,
            "sender": step.sender_name,
            "receiver": step.receiver_name,
            "channel": step.channel_name,
        })).collect::<Vec<_>>(),
    });
    fs::write(
        instance.join("trace.json"),
        serde_json::to_vec_pretty(&trace_doc).map_err(|error| error.to_string())?,
    )
    .map_err(|error| error.to_string())?;
    let init_dir = instance.join("workspace/init");
    let request_dir = instance.join("workspace/requests").join(&request_id);
    let init = context
        .initialization_prompt(&logical_session, false, &init_dir)
        .map_err(|error| error.to_string())?;
    let generation = context
        .generation_prompt(&request_dir, &request_id, "", &trace)
        .map_err(|error| error.to_string())?;
    fs::write(instance.join("prepared/prompts/init.md"), init)
        .map_err(|error| error.to_string())?;
    fs::write(instance.join("prepared/prompts/generation.md"), generation)
        .map_err(|error| error.to_string())?;
    let snapshot = workdir.join(&campaign.target.config_output);
    write_server_config(
        &campaign.target.config_template,
        &snapshot,
        &crate::app::config::TargetPlaceholders {
            port: 0,
            root: workdir.clone(),
            config: snapshot.clone(),
            cert: workdir.join(&campaign.target.cert),
            key: workdir.join(&campaign.target.key),
            ca: campaign
                .target
                .ca
                .clone()
                .map(|ca| workdir.join(ca))
                .unwrap_or_else(|| workdir.join("certs/my.pem")),
        },
    )
    .map_err(|error| error.to_string())?;
    let sources = Sources {
        suite: absolute(&suite.path)?,
        campaign: absolute(&suite.campaign)?,
        model: absolute(&campaign.protocol_generation.model)?,
        init_template: PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("prompts/synthesis-init.md"),
        generation_template: PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("prompts/synthesis-generation.md"),
        generator_skill: Some(context.generator_skill().to_path_buf()),
        binary: absolute(&campaign.target.binary)?,
        fixture: absolute(&campaign.target.fixture)?,
    };
    let digest = source_digest(&sources)?;
    let mut stages = BTreeMap::new();
    stages.insert(
        "prepare".to_string(),
        StageRecord::passed(
            0,
            vec![
                instance.join("trace.json"),
                instance.join("prepared/receipt.json"),
                instance.join("prepared/prompts/init.md"),
                instance.join("prepared/prompts/generation.md"),
            ],
        ),
    );
    for name in ["generate", "validate", "replay"] {
        stages.insert(name.to_string(), StageRecord::not_run());
    }
    let task = Task {
        version: 1,
        profile: profile_name.to_string(),
        provider: format!("{:?}", profile.provider).to_lowercase(),
        case_name: case_name.to_string(),
        repeat,
        instance_dir: absolute(&instance)?,
        request_id,
        logical_session,
        protocol_id: context.model().protocol_id.clone(),
        trace_id: trace.id.0,
        step_names: names,
        digest,
        sources,
        target: TargetSnap {
            binary: absolute(&campaign.target.binary)?,
            args: campaign.target.args.clone(),
            extra_env: campaign.target.extra_env.clone(),
            host: campaign.target.host.clone(),
            fixture,
            workdir_snapshot: workdir,
            config_template: absolute(&campaign.target.config_template)?,
            config_output: campaign.target.config_output.clone(),
            cert: campaign.target.cert.clone(),
            key: campaign.target.key.clone(),
            ca: campaign.target.ca.clone(),
            case_ms: campaign.timeouts.case_ms,
            connect_ms: campaign.timeouts.connect_ms,
            io_ms: campaign.timeouts.io_ms,
            ready_ms: suite.ready_ms,
            sut_exit_ms: suite.sut_exit_ms,
            term_grace_ms: suite.term_grace_ms,
        },
        python: absolute(&campaign.campaign.python)?,
        pythonpath: absolute(&campaign.campaign.pythonpath)?,
        executable: absolute(&profile.executable).unwrap_or_else(|_| profile.executable.clone()),
        model_name: profile.model.clone(),
        effort: profile.effort.clone(),
        profile_args: profile.args.clone(),
        cli_version: None,
        provider_session: None,
        validation_digest: None,
        stages,
    };
    task.save()?;
    Ok(task)
}

pub fn trace_from_names(
    model: &crate::protocol::IndexedModel,
    binding: &crate::protocol::Binding,
    names: &[String],
) -> Result<StepTrace, String> {
    let mut steps = Vec::new();
    for name in names {
        let matches: Vec<_> = model
            .steps
            .iter()
            .filter(|step| step.name == *name)
            .collect();
        if matches.is_empty() {
            return Err(format!("step name {name} was not found"));
        }
        if matches.len() != 1 {
            return Err(format!("step name {name} is not unique"));
        }
        let step = matches[0];
        if !model.carries_step(step, binding) {
            return Err(format!(
                "step {name} is not executable with this party binding"
            ));
        }
        steps.push(step.clone());
    }
    if !covers_sut_and_generator(&steps, binding) {
        let detail = steps
            .iter()
            .map(|step| {
                format!(
                    "{} {}->{:?}/{:?}",
                    step.name,
                    step.sender_name,
                    binding.role(&step.sender_name),
                    binding.role(&step.receiver_name)
                )
            })
            .collect::<Vec<_>>()
            .join(", ");
        return Err(format!(
            "trace does not cover both a SUT party and a generator party ({detail})"
        ));
    }
    Ok(StepTrace::new(steps))
}

pub fn source_digest(sources: &Sources) -> Result<String, String> {
    let mut hasher = Sha256::new();
    hash_file(&mut hasher, &sources.suite)?;
    hash_file(&mut hasher, &sources.campaign)?;
    hash_file(&mut hasher, &sources.model)?;
    hash_file(&mut hasher, &sources.init_template)?;
    hash_file(&mut hasher, &sources.generation_template)?;
    if let Some(skill) = &sources.generator_skill {
        hash_tree(
            &mut hasher,
            skill
                .parent()
                .ok_or("generator skill has no parent directory")?,
        )?;
    }
    hash_file(&mut hasher, &sources.binary)?;
    hash_tree(&mut hasher, &sources.fixture)?;
    Ok(format!("{:x}", hasher.finalize()))
}

fn hash_file(hasher: &mut Sha256, path: &Path) -> Result<(), String> {
    let bytes = fs::read(path).map_err(|error| format!("hashing {}: {error}", path.display()))?;
    hasher.update(path.to_string_lossy().as_bytes());
    hasher.update(bytes);
    Ok(())
}

fn hash_tree(hasher: &mut Sha256, root: &Path) -> Result<(), String> {
    let mut files = Vec::new();
    collect(root, root, &mut files)?;
    files.sort();
    for (name, path) in files {
        hasher.update(name.as_bytes());
        hasher.update(fs::read(&path).map_err(|error| error.to_string())?);
    }
    Ok(())
}

fn collect(root: &Path, dir: &Path, out: &mut Vec<(String, PathBuf)>) -> Result<(), String> {
    for entry in fs::read_dir(dir).map_err(|error| error.to_string())? {
        let entry = entry.map_err(|error| error.to_string())?;
        let path = entry.path();
        if path.is_dir() {
            collect(root, &path, out)?;
        } else {
            let name = path
                .strip_prefix(root)
                .map_err(|error| error.to_string())?
                .to_string_lossy()
                .to_string();
            out.push((name, path));
        }
    }
    Ok(())
}

pub fn stable_uuid(text: &str) -> String {
    let hex = format!("{:x}", Sha256::digest(text.as_bytes()));
    format!(
        "{}-{}-{}-{}-{}",
        &hex[0..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..32]
    )
}

pub fn absolute(path: &Path) -> Result<PathBuf, String> {
    std::path::absolute(path).map_err(|error| error.to_string())
}

pub fn run_id() -> String {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis())
        .unwrap_or(0);
    format!("{millis}")
}

/// Digest of the files replay actually executes.
pub fn execution_digest(dir: &Path) -> Result<String, String> {
    let mut hasher = Sha256::new();
    for name in [
        "published/generator.py",
        "published/params.schema.json",
        "input.json",
    ] {
        let path = dir.join(name);
        if !path.is_file() {
            return Err(format!("validated artifact {} is missing", name));
        }
        hash_file(&mut hasher, &path)?;
    }
    Ok(format!("{:x}", hasher.finalize()))
}
