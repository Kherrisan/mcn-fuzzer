//! Replay one historical agent turn against private, point-in-time MCP state.

mod session;

use std::{
    fs,
    io::{BufRead, BufReader},
    net::TcpListener,
    os::unix::process::CommandExt,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use clap::{Args, ValueEnum};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::{
    agents::{
        artifacts::{check_identity, read_json},
        command_line,
    },
    app::config::Campaign,
    coverage::{
        introspection::{
            http::IntrospectionHttp, CampaignIdentity, IntrospectionHandle, ProposalId,
            ProposalIdentity, ProposalOrigin, ProposalSnapshot, ProposalStatus,
        },
        mcp::McpClient,
        proposal::{sha256_file, Proposal},
    },
};

#[derive(Clone, Copy, ValueEnum)]
pub enum Stage {
    Analysis,
    Synthesis,
}

#[derive(Args)]
pub struct Options {
    #[arg(long)]
    campaign: PathBuf,
    #[arg(long)]
    run_dir: PathBuf,
    #[arg(long)]
    stage: Stage,
    #[arg(long)]
    request_id: String,
    #[arg(long)]
    out: PathBuf,
    #[arg(long, conflicts_with_all = ["replace_from", "replace_with"])]
    prompt: Option<PathBuf>,
    #[arg(long, requires = "replace_with")]
    replace_from: Option<PathBuf>,
    #[arg(long, requires = "replace_from")]
    replace_with: Option<PathBuf>,
}

#[derive(Serialize, Deserialize)]
struct InitialState {
    coverage: Value,
    introspection: Vec<ProposalSnapshot>,
    #[serde(default)]
    step_stats: Option<crate::coverage::step_stats::StepStats>,
    #[serde(default)]
    gaps: Vec<String>,
}

pub(crate) fn capture(
    directory: &Path,
    prompt: &str,
    mcp: Option<&McpClient>,
    introspection: Option<&IntrospectionHandle>,
) {
    let save = || -> Result<(), String> {
        fs::write(directory.join("request-prompt.md"), prompt).map_err(error)?;
        let mut gaps = Vec::new();
        let coverage = match mcp.map(|client| {
            client
                .with_timeout(Duration::from_secs(2))
                .call(json!({"op":"eval_state"}))
        }) {
            Some(Ok(state)) => state,
            Some(Err(reason)) => {
                gaps.push(format!("coverage state capture failed: {reason}"));
                Value::Null
            }
            None => Value::Null,
        };
        let step_stats = introspection
            .and_then(IntrospectionHandle::step_stats_snapshot);
        let introspection = introspection
            .map(IntrospectionHandle::snapshot)
            .transpose()?
            .unwrap_or_default();
        write_json(
            &directory.join("eval-state.json"),
            &InitialState {
                coverage,
                introspection,
                step_stats,
                gaps,
            },
        )
    };
    if let Err(reason) = save() {
        log::warn!(
            "could not record evaluation state at {}: {reason}",
            directory.display()
        );
    }
}

fn error(value: impl std::fmt::Display) -> String {
    value.to_string()
}

fn write_json(path: &Path, value: &impl Serialize) -> Result<(), String> {
    let bytes = serde_json::to_vec_pretty(value).map_err(error)?;
    fs::write(path, bytes).map_err(error)
}

fn field<'a>(prompt: &'a str, key: &str) -> Option<&'a str> {
    prompt
        .lines()
        .find_map(|line| line.strip_prefix(key)?.strip_prefix('='))
}

fn unix_ms(time: SystemTime) -> u64 {
    time.duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

fn read_lines(path: &Path) -> Result<Vec<Value>, String> {
    BufReader::new(fs::File::open(path).map_err(error)?)
        .lines()
        .map(|line| serde_json::from_str(&line.map_err(error)?).map_err(error))
        .collect()
}

fn legacy_state(campaign: &Campaign, cutoff: u64, request: &str) -> Result<InitialState, String> {
    let root = &campaign.coverage.output;
    let mut coverage = json!({"loaded": {}, "checkpoint": null});
    let mut introspection = Vec::new();
    let log = root.join("mcp-state/service.jsonl");
    if log.is_file() {
        for row in read_lines(&log)? {
            if row["time_ms"].as_u64().unwrap_or(u64::MAX) >= cutoff || row["ok"] != true {
                continue;
            }
            match row["tool"].as_str().unwrap_or("") {
                "load_protocol" => coverage["loaded"]["protocol"] = json!({"loaded":true}),
                "load_handlers" => coverage["loaded"]["handlers"] = json!({"loaded":true}),
                "load_cfg" => coverage["loaded"]["cfg"] = json!({"loaded":true}),
                "load_callgraph" => coverage["loaded"]["callgraph"] = json!({"loaded":true}),
                "load_coverage_json" => {
                    coverage["loaded"]["coverage_request"] = row["request_id"].clone();
                }
                _ => {}
            }
        }
    }
    let events = root.join("analysis/events.jsonl");
    if events.is_file() {
        for row in read_lines(&events)? {
            if row["time_ms"].as_u64().unwrap_or(u64::MAX) >= cutoff
                || row["kind"] != "proposal_accepted"
            {
                continue;
            }
            let body = &row["body"];
            let old = body["request"]
                .as_str()
                .ok_or("accepted event lacks request")?;
            let dir = request_dir(&root.join("analysis"), old)?;
            let mut checkpoint = read_json(&dir.join("graph-checkpoint.json"))?;
            checkpoint["revision"] = body["commit"]["graph_revision"].clone();
            coverage["checkpoint"] = checkpoint;
            let proposal = Proposal::read(&dir.join("proposal.json")).map_err(error)?;
            let identity = ProposalIdentity {
                id: ProposalId::coverage(old),
                origin: ProposalOrigin::Coverage,
                analysis_request_id: Some(old.into()),
                session: proposal.session.clone(),
                snapshot: Some(proposal.snapshot.clone()),
                created_at_unix_ms: row["time_ms"].as_u64().unwrap_or(0),
            };
            let synthesis_dir = request_dir(&campaign.generator_synthesis.output, old)?;
            let result = synthesis_dir.join("result.json");
            let has_result = result
                .metadata()
                .and_then(|m| m.modified())
                .is_ok_and(|time| unix_ms(time) < cutoff);
            let status = if has_result {
                if read_json(&result)?["status"] == "ok" {
                    ProposalStatus::Synthesized
                } else {
                    ProposalStatus::Failed
                }
            } else if old == request && synthesis_dir.is_dir() {
                ProposalStatus::Synthesizing
            } else {
                ProposalStatus::Queued
            };
            introspection.push(ProposalSnapshot::legacy(identity, status, proposal));
        }
    }
    Ok(InitialState {
        coverage, introspection,
        step_stats: None,
        gaps: vec!["Legacy run: execution feedback, artifact ownership and some intermediate proposal states were not recorded at dispatch".into()],
    })
}

fn request_dir(root: &Path, id: &str) -> Result<PathBuf, String> {
    if id.is_empty()
        || !id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
    {
        return Err(format!("invalid request id {id:?}"));
    }
    Ok(root.join("requests").join(id))
}

struct Service(Child);

impl Drop for Service {
    fn drop(&mut self) {
        let _ = nix::sys::signal::killpg(
            nix::unistd::Pid::from_raw(self.0.id() as i32),
            nix::sys::signal::Signal::SIGKILL,
        );
        let _ = self.0.wait();
    }
}

fn start_coverage(
    campaign: &Campaign,
    out: &Path,
) -> Result<(Service, McpClient, PathBuf), String> {
    // A short socket path also supports long benchmark output paths.
    let socket = PathBuf::from(format!(
        "/tmp/mcn-eval-{}-{}.sock",
        std::process::id(),
        unix_ms(SystemTime::now())
    ));
    let log = fs::File::create(out.join("coverage-service.log")).map_err(error)?;
    let mut command = Command::new(&campaign.campaign.python);
    command
        .args(["-m", "coverage_analysis.server", "serve", "--socket"])
        .arg(&socket)
        .arg("--state-dir")
        .arg(out.join("coverage-state"))
        .env("PYTHONPATH", &campaign.campaign.pythonpath)
        .process_group(0)
        .stdout(Stdio::from(log.try_clone().map_err(error)?))
        .stderr(Stdio::from(log));
    if let Some(source) = &campaign.target.source_dir {
        command.arg("--source-root").arg(source);
    }
    let mut service = Service(command.spawn().map_err(error)?);
    let client = McpClient::at(socket.clone());
    let deadline =
        Instant::now() + Duration::from_secs(campaign.coverage.mcp_ready_timeout_secs.max(1));
    while client.health().is_err() {
        if service.0.try_wait().map_err(error)?.is_some() || Instant::now() >= deadline {
            let _ = fs::remove_file(&socket);
            return Err("coverage MCP did not become ready; see coverage-service.log".into());
        }
        thread::sleep(Duration::from_millis(25));
    }
    Ok((service, client, socket))
}

fn tool(client: &McpClient, name: &str, arguments: Value) -> Result<(), String> {
    client
        .call(json!({"op":"tool", "name":name, "arguments":arguments}))
        .map(|_| ())
        .map_err(error)
}

fn isolate_snapshot(source: &Path, out: &Path) -> Result<PathBuf, String> {
    let destination = out
        .join("snapshots")
        .join(source.file_name().ok_or("snapshot has no name")?);
    fs::create_dir_all(&destination).map_err(error)?;
    for entry in fs::read_dir(source).map_err(error)? {
        let entry = entry.map_err(error)?;
        if entry.path().is_file() {
            fs::copy(entry.path(), destination.join(entry.file_name())).map_err(error)?;
        }
    }
    Ok(destination)
}

fn register(
    client: &McpClient,
    campaign: &Campaign,
    original: &Path,
    snapshot_dir: &Path,
    out: &Path,
    prompt: &str,
) -> Result<Value, String> {
    let manifest = read_json(&original.join("request.json"))?;
    let mut request = manifest["identity"].clone();
    let obj = request
        .as_object_mut()
        .ok_or("request.json lacks identity")?;
    obj.extend(
        serde_json::from_value::<serde_json::Map<String, Value>>(json!({
            "output_dir": out,
            "snapshot_dir": snapshot_dir,
            "model": campaign.generator_synthesis.model,
            "handlers": manifest["handlers"],
            "source_dir": campaign.target.source_dir,
            "llvm_cov": campaign.coverage.llvm_cov,
            "sut_parties": parse_prompt_array(prompt, "SUT_PARTIES")?,
            "generator_parties": parse_prompt_array(prompt, "GENERATOR_PARTIES")?,
            "unavailable_parties": parse_prompt_array(prompt, "UNAVAILABLE_PARTIES")?,
        }))
        .map_err(error)?,
    );
    client.register(&request).map_err(error)
}

fn parse_prompt_array(prompt: &str, key: &str) -> Result<Value, String> {
    serde_json::from_str(field(prompt, key).unwrap_or("[]")).map_err(error)
}

pub fn run(options: &Options) -> Result<(), String> {
    let started = Instant::now();
    let run_dir = options.run_dir.canonicalize().map_err(error)?;
    let campaign_path = options.campaign.canonicalize().map_err(error)?;
    let campaign =
        Campaign::load_with_output_root(&campaign_path, Some(&run_dir)).map_err(error)?;
    let coverage_root = campaign.coverage.output.join("analysis");
    let (root, profile_name, timeout) = match options.stage {
        Stage::Analysis => (
            &coverage_root,
            &campaign.coverage.analysis_profile,
            campaign.coverage.analysis_task_timeout_secs,
        ),
        Stage::Synthesis => (
            &campaign.generator_synthesis.output,
            &campaign.generator_synthesis.profile,
            campaign.generator_synthesis.task_timeout_secs,
        ),
    };
    let profile = campaign
        .agents
        .profiles
        .get(profile_name)
        .ok_or_else(|| format!("unknown campaign profile {profile_name}"))?;
    let original = request_dir(root, &options.request_id)?
        .canonicalize()
        .map_err(error)?;
    let source = session::find(root, &original, &options.request_id, options.stage)?;
    let mut prompt = source.prompt.clone();
    if let Some(path) = &options.prompt {
        prompt = fs::read_to_string(path).map_err(error)?;
    }
    if let (Some(from), Some(to)) = (&options.replace_from, &options.replace_with) {
        let from = fs::read_to_string(from).map_err(error)?;
        let to = fs::read_to_string(to).map_err(error)?;
        if from.is_empty() || prompt.matches(&from).count() != 1 {
            return Err("replacement text must match exactly once".into());
        }
        prompt = prompt.replacen(&from, &to, 1);
    }
    let saved = original.join("eval-state.json");
    let mut state: InitialState = if saved.is_file() {
        serde_json::from_value(read_json(&saved)?).map_err(error)?
    } else {
        let cutoff = if matches!(options.stage, Stage::Analysis) {
            unix_ms(
                original
                    .join("request.json")
                    .metadata()
                    .and_then(|m| m.modified())
                    .map_err(error)?,
            )
        } else {
            source.started_at_ms
        };
        legacy_state(&campaign, cutoff, &options.request_id)?
    };

    if state.coverage.is_null() && !state.gaps.is_empty() {
        state.coverage =
            legacy_state(&campaign, source.started_at_ms, &options.request_id)?.coverage;
    }
    let autonomous = field(&source.prompt, "PROPOSAL_DRAFT").is_some();
    let previous_workspace = field(&source.prompt, "WORKSPACE")
        .map(Path::new)
        .unwrap_or(root);
    fs::create_dir(&options.out).map_err(error)?;
    let out = options.out.canonicalize().map_err(error)?;
    let mut launch = command_line(profile, &out).map_err(error)?;
    if matches!(options.stage, Stage::Synthesis) {
        copy_helpers(
            &previous_workspace.join("lib"),
            &out.join("lib"),
            source.started_at_ms,
        )?;
    }
    if !launch.codex {
        return Err(
            "eval-request requires a Codex profile with app-server thread/fork support".into(),
        );
    }
    let (_service, client, socket) = start_coverage(&campaign, &out)?;
    let _socket_cleanup = SocketCleanup(socket.clone());
    let registry = IntrospectionHandle::open(
        CampaignIdentity {
            run_id: run_dir.display().to_string(),
            started_at_unix_ms: source.started_at_ms,
        },
        campaign.coverage.index_map.as_deref(),
    );
    write_json(&out.join("mcp-initial-state.json"), &state)?;
    registry.restore(state.introspection)?;
    if let Some(stats) = state.step_stats.clone() {
        registry.restore_step_stats(stats);
    } else if let Ok(model) = crate::protocol::load_model(&campaign.generator_synthesis.model) {
        registry.set_step_model(&model);
    }
    let loaded = &state.coverage["loaded"];
    if loaded["protocol"]["loaded"] == true || state.coverage["checkpoint"].is_object() {
        let mut arguments = json!({"protocol_path": campaign.generator_synthesis.model});
        if state.coverage["checkpoint"].is_object() {
            let checkpoint = out.join("initial-graph.json");
            write_json(&checkpoint, &state.coverage["checkpoint"])?;
            arguments["checkpoint_path"] = json!(checkpoint);
        }
        tool(&client, "load_protocol", arguments)?;
    }
    for (key, name, arguments) in [
        (
            "cfg",
            "load_cfg",
            json!({"cfg_dir": campaign.coverage.cfg_dir}),
        ),
        (
            "callgraph",
            "load_callgraph",
            json!({"callgraph_dot": campaign.coverage.callgraph_dot}),
        ),
        (
            "handlers",
            "load_handlers",
            json!({"handlers_path": coverage_root.join("init/handlers.accepted.json")}),
        ),
    ] {
        if loaded[key]["loaded"] == true {
            tool(&client, name, arguments)?;
        }
    }
    let current_identity = if matches!(options.stage, Stage::Analysis) {
        let manifest = read_json(&original.join("request.json"))?;
        let snapshot = PathBuf::from(
            manifest["snapshot_dir"]
                .as_str()
                .ok_or("missing snapshot_dir")?,
        );
        let isolated = isolate_snapshot(&snapshot, &out)?;
        prompt = prompt.replace(
            snapshot.to_str().ok_or("invalid snapshot path")?,
            isolated.to_str().ok_or("invalid output path")?,
        );
        if !registry
            .with(|writer| writer.get(&ProposalId::coverage(&options.request_id)))
            .is_some()
        {
            registry.register_coverage(
                &options.request_id,
                manifest["identity"]["session"]
                    .as_str()
                    .ok_or("missing session")?,
                manifest["identity"]["snapshot"]
                    .as_str()
                    .ok_or("missing snapshot")?,
            )?;
        }
        Some(register(
            &client,
            &campaign,
            &original,
            &isolated,
            &out,
            &source.prompt,
        )?)
    } else {
        if !autonomous {
            fs::copy(original.join("proposal.json"), out.join("proposal.json")).map_err(error)?;
            let id = ProposalId::coverage(&options.request_id);
            if registry.with(|writer| writer.get(&id)).is_none() {
                let dir = request_dir(&coverage_root, &options.request_id)?;
                let proposal = Proposal::read(&dir.join("proposal.json")).map_err(error)?;
                registry.register_coverage(
                    &options.request_id,
                    &proposal.session,
                    &proposal.snapshot,
                )?;
                registry.accept(&id, std::sync::Arc::new(proposal))?;
                registry.synthesis_started(&options.request_id)?;
            }
        } else if registry
            .with(|writer| writer.get(&ProposalId::synthesis(&options.request_id)))
            .is_none()
        {
            let session = field(&source.prompt, "SESSION").unwrap_or("eval");
            registry.register_autonomous(&options.request_id, session)?;
        }
        None
    };
    let previous = loaded["coverage_request"]
        .as_str()
        .or_else(|| loaded["coverage"]["request_id"].as_str());
    if let Some(previous) = previous {
        let directory = request_dir(&coverage_root, previous)?;
        let manifest = read_json(&directory.join("request.json"))?;
        let snapshot = PathBuf::from(
            manifest["snapshot_dir"]
                .as_str()
                .ok_or("missing prior snapshot")?,
        );
        let isolated = isolate_snapshot(&snapshot, &out)?;
        if current_identity.is_none() || previous != options.request_id {
            let historical_prompt = directory.join("request-prompt.md");
            let prior_prompt = if historical_prompt.is_file() {
                fs::read_to_string(historical_prompt).map_err(error)?
            } else {
                session::find(&coverage_root, &directory, previous, Stage::Analysis)?.prompt
            };
            register(
                &client,
                &campaign,
                &directory,
                &isolated,
                &out,
                &prior_prompt,
            )?;
        }
        tool(
            &client,
            "load_coverage_json",
            json!({"request_id": previous, "snapshot_dir": isolated}),
        )?;
    }
    let http = IntrospectionHttp::start(
        registry.store(),
        TcpListener::bind("127.0.0.1:0").map_err(error)?,
    )?;
    let bridge = campaign
        .campaign
        .pythonpath
        .join("coverage_analysis/server.py");
    launch.argv.extend([
        "-c".into(),
        format!(
            "mcp_servers.coverage.command={}",
            json!(campaign.campaign.python)
        ),
        "-c".into(),
        format!(
            "mcp_servers.coverage.args={}",
            json!([
                bridge,
                PathBuf::from("stdio"),
                PathBuf::from("--socket"),
                socket
            ])
        ),
        "-c".into(),
        format!(
            "mcp_servers.campaign_introspection.url={}",
            json!(http.url())
        ),
    ]);
    if let Ok(rows) = read_lines(&coverage_root.join("events.jsonl")) {
        if let Some(old_url) = rows
            .iter()
            .find_map(|row| row["body"]["introspection_url"].as_str())
        {
            prompt = prompt.replace(old_url, http.url());
        }
    }
    let rewrite_root = if matches!(options.stage, Stage::Synthesis) {
        previous_workspace
    } else {
        &original
    };
    prompt = rebase_prompt(&prompt, &original, rewrite_root, &out)?;
    fs::write(out.join("request-prompt.md"), &prompt).map_err(error)?;
    let ready_at_ms = unix_ms(SystemTime::now());
    let turn = session::run(
        &launch,
        &source,
        &prompt,
        Duration::from_secs(timeout.max(1)),
        &out,
    );
    let receipt = read_json(&out.join("turn-started.json")).unwrap_or(Value::Null);
    let sent_at = receipt["sent_at_unix_ms"].as_u64();
    let ready_ms = started.elapsed().as_millis().saturating_sub(u128::from(
        unix_ms(SystemTime::now()).saturating_sub(sent_at.unwrap_or(ready_at_ms)),
    ));
    let result_ms = out
        .join("result.json")
        .metadata()
        .and_then(|m| m.modified())
        .ok()
        .and_then(|time| unix_ms(time).checked_sub(sent_at?));
    let validation = turn.as_ref().map_err(Clone::clone).and_then(|finished| {
        if finished["status"] != "completed" {
            return Err(format!(
                "agent turn ended with status {}",
                finished["status"]
            ));
        }
        if let Some(identity) = current_identity {
            validate_analysis(&client, &out, &identity)
        } else {
            crate::engine::synthesis::validate_historical_request(
                &out,
                &options.request_id,
                field(&source.prompt, "SESSION").unwrap_or("eval"),
                autonomous,
            )
            .map(|_| "ok".into())
        }
    });
    write_json(
        &out.join("evaluation.json"),
        &json!({
            "request_id": options.request_id, "original_thread": source.thread, "original_turn": source.target,
            "profile": profile_name, "prep_ms": ready_ms, "result_ms": result_ms, "turn_start": receipt,
            "turn": turn.as_ref().ok(), "validation": validation.as_ref().ok(), "error": validation.as_ref().err(),
        }),
    )?;
    validation.map(|_| ())
}

fn copy_helpers(source: &Path, destination: &Path, cutoff: u64) -> Result<(), String> {
    if !source.is_dir() {
        return Ok(());
    }
    for entry in fs::read_dir(source).map_err(error)? {
        let entry = entry.map_err(error)?;
        let ty = entry.file_type().map_err(error)?;
        let target = destination.join(entry.file_name());
        if ty.is_dir() {
            copy_helpers(&entry.path(), &target, cutoff)?;
        } else if ty.is_file()
            && unix_ms(
                entry
                    .metadata()
                    .and_then(|meta| meta.modified())
                    .map_err(error)?,
            ) < cutoff
        {
            fs::create_dir_all(destination).map_err(error)?;
            fs::copy(entry.path(), target).map_err(error)?;
        }
    }
    Ok(())
}

fn rebase_prompt(
    prompt: &str,
    request: &Path,
    workspace: &Path,
    out: &Path,
) -> Result<String, String> {
    let request = request.to_str().ok_or("invalid original request path")?;
    let workspace = workspace
        .to_str()
        .ok_or("invalid original workspace path")?;
    let out = out.to_str().ok_or("invalid output path")?;
    // Replace original request paths first, without rewriting bytes already emitted.
    let mut result = String::new();
    let mut rest = prompt;
    while let Some(position) = rest.find(workspace) {
        result.push_str(&rest[..position]);
        rest = &rest[position..];
        let prefix = if rest.starts_with(request) {
            request
        } else {
            workspace
        };
        rest = &rest[prefix.len()..];
        result.push_str(out);
    }
    result.push_str(rest);
    Ok(result)
}

struct SocketCleanup(PathBuf);
impl Drop for SocketCleanup {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

fn validate_analysis(client: &McpClient, out: &Path, identity: &Value) -> Result<String, String> {
    let result = read_json(&out.join("result.json"))?;
    let keys = ["request_id", "session", "build", "snapshot"];
    let expected = keys
        .iter()
        .map(|&key| Ok((key, identity[key].as_str().ok_or("incomplete identity")?)))
        .collect::<Result<Vec<_>, String>>()?;
    check_identity(&result, &expected)?;
    match result["status"].as_str() {
        Some("no_proposal") => Ok("no_proposal".into()),
        Some("ok") => {
            let path = out.join("proposal.json");
            Proposal::read(&path).map_err(error)?;
            client
                .commit(
                    identity["request_id"]
                        .as_str()
                        .ok_or("missing request id")?,
                    &sha256_file(&path)?,
                )
                .map_err(error)?;
            Ok("ok".into())
        }
        _ => Err(format!("analysis failed: {}", result["error"])),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rebinds_output_and_workspace_once() {
        let prompt = "WORKSPACE=/run/synthesis\nOUTPUT_DIR=/run/synthesis/requests/one\nPROPOSAL=/run/synthesis/requests/one/proposal.json\n";
        let out = Path::new("/run/synthesis/eval");
        let actual = rebase_prompt(
            prompt,
            Path::new("/run/synthesis/requests/one"),
            Path::new("/run/synthesis"),
            out,
        )
        .unwrap();
        assert_eq!(actual, "WORKSPACE=/run/synthesis/eval\nOUTPUT_DIR=/run/synthesis/eval\nPROPOSAL=/run/synthesis/eval/proposal.json\n");
    }

    #[test]
    fn snapshots_proposal_statistics_independently_from_the_live_registry() {
        let registry = IntrospectionHandle::open(
            CampaignIdentity {
                run_id: "test".into(),
                started_at_unix_ms: 1,
            },
            None,
        );
        registry
            .register_coverage("request", "session", "000001")
            .unwrap();
        let id = ProposalId::coverage("request");
        let record = registry.with(|writer| writer.get(&id)).unwrap();
        record.execution_started();
        let snapshot = serde_json::to_vec(&registry.snapshot().unwrap()).unwrap();
        record.execution_started();
        let copy = IntrospectionHandle::open(
            CampaignIdentity {
                run_id: "test".into(),
                started_at_unix_ms: 1,
            },
            None,
        );
        copy.restore(serde_json::from_slice(&snapshot).unwrap())
            .unwrap();
        let restored = copy.store().get_proposal(&id, None, None).unwrap();
        assert_eq!(restored["data"]["stats"]["executions"]["started"], 1);
        assert_eq!(restored["data"]["status"], "proposing");
    }
}
