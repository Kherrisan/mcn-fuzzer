//! Run one published generator against an ordinary SUT process.

use std::{
    fs,
    net::TcpListener,
    path::PathBuf,
    thread,
    time::{Duration, Instant},
};

use serde_json::{json, Value};

use crate::{
    app::config::TargetPlaceholders,
    engine::executor::run_cleanup_script,
    engine::input::{DependencyInstance, ParamValue, StepInstance},
    engine::synthesis::TraceOrigin,
};

use super::{
    process::{listens_on, SpawnSpec, Supervisor},
    task::{execution_digest, Task},
};

#[derive(serde::Deserialize)]
struct StoredInput {
    generator: PathBuf,
    #[serde(default)]
    step_trace: Vec<crate::engine::input::StepInstance>,
    #[serde(default)]
    dependencies: Vec<crate::engine::input::DependencyInstance>,
    #[serde(default)]
    params: std::collections::BTreeMap<String, ParamValue>,
    origin: TraceOrigin,
}

pub fn validate_task(task: &mut Task) -> Result<(), (String, String)> {
    task.ensure_digest()
        .map_err(|error| ("source_changed".to_string(), error))?;
    let dir = task.request_dir();
    if task.validation_digest.is_some() || dir.join("published/generator.py").is_file() {
        match execution_digest(&dir) {
            Ok(digest) if task.validation_digest.as_deref() == Some(digest.as_str()) => {
                return Ok(());
            }
            Ok(_) if task.validation_digest.is_some() => {
                return Err((
                    "artifact_changed".to_string(),
                    "published generator, schema, or input.json does not match the validated snapshot"
                        .to_string(),
                ));
            }
            Err(error) if task.validation_digest.is_some() => {
                return Err(("artifact_changed".to_string(), error));
            }
            _ => {}
        }
    }
    let mut validated =
        crate::engine::synthesis::validate_generator_artifacts(&dir, &task.request_id)
            .map_err(classify_validation)?;
    let proposal =
        crate::coverage::proposal::Proposal::read(&task.prepared().join("proposal.json"))
            .map_err(|error| ("invalid_artifact".to_string(), error))?;
    let step_trace = proposal
        .trace
        .iter()
        .map(|step| {
            let fields = step
                .mutable_fields
                .iter()
                .filter_map(|field| {
                    validated
                        .input
                        .params
                        .get(&field.field)
                        .cloned()
                        .or_else(|| field.default.clone())
                        .map(|value| (field.field.clone(), value))
                })
                .collect();
            StepInstance {
                name: step.name.clone(),
                step: step.step.clone(),
                action: step.action.clone(),
                condition: step.condition.clone(),
                message: step.message.clone(),
                channel: step.channel.clone(),
                sender: step.sender.clone(),
                receiver: step.receiver.clone(),
                party: step.party.clone(),
                fields,
                omitted_fields: Vec::new(),
                inputs: std::collections::BTreeMap::new(),
                broken_dependencies: Vec::new(),
            }
        })
        .collect::<Vec<_>>();
    let mut step_trace = step_trace;
    for dependency in &proposal.dependencies {
        let Some(dependent) = step_trace
            .iter_mut()
            .find(|step| step.name == dependency.dependent)
        else {
            continue;
        };
        for field in &dependency.fields {
            dependent
                .inputs
                .insert(field.clone(), format!("{}.{field}", dependency.prior));
        }
    }
    let dependencies = proposal
        .dependencies
        .iter()
        .map(|dependency| DependencyInstance {
            prior: dependency.prior.clone(),
            dependent: dependency.dependent.clone(),
            kind: if dependency.kind.is_empty() {
                "order".to_string()
            } else {
                dependency.kind.clone()
            },
            condition: dependency.condition.clone(),
            fields: dependency.fields.clone(),
            origin: "proposal".into(),
        })
        .collect();
    validated.input.trace.steps = step_trace;
    validated.input.trace.dependencies = dependencies;
    let stored = json!({
        "generator": validated.input.generator,
        "step_trace": validated.input.trace.steps,
        "dependencies": validated.input.trace.dependencies,
        "params": params_json(&validated.input.params)?,
        "origin": TraceOrigin {
            request_id: task.request_id.clone(),
            session: proposal.session,
            step_names: proposal.trace.iter().map(|step| step.name.clone()).collect(),
            added_steps: Vec::<String>::new(),
        },
    });
    fs::write(
        dir.join("input.json"),
        serde_json::to_vec_pretty(&stored)
            .map_err(|error| ("infrastructure".to_string(), error.to_string()))?,
    )
    .map_err(|error| ("infrastructure".to_string(), error.to_string()))?;
    let digest = execution_digest(&dir).map_err(|error| ("infrastructure".to_string(), error))?;
    task.validation_digest = Some(digest);
    Ok(())
}

fn ensure_execution_snapshot(task: &Task) -> Result<(), (String, String)> {
    let digest = execution_digest(&task.request_dir())
        .map_err(|error| ("artifact_changed".to_string(), error))?;
    if task.validation_digest.as_deref() != Some(digest.as_str()) {
        return Err((
            "artifact_changed".to_string(),
            "published generator, schema, or input.json does not match the validated snapshot"
                .to_string(),
        ));
    }
    Ok(())
}

fn exit_detail(status: std::process::ExitStatus) -> String {
    use std::os::unix::process::ExitStatusExt;
    if let Some(signal) = status.signal() {
        format!("signal {signal}")
    } else {
        format!("code {}", status.code().unwrap_or(-1))
    }
}

fn classify_validation(message: String) -> (String, String) {
    let kind = if message.contains("does not match") {
        "bad_id"
    } else if message.contains("$ref")
        || message.contains("schema")
        || message.contains("not in the schema")
    {
        "invalid_schema"
    } else if message.contains("params.json")
        || message.contains("not representable")
        || message.contains("integer")
    {
        "invalid_params"
    } else if message.contains("generate") || message.contains("syntax") {
        "invalid_generator"
    } else {
        "invalid_artifact"
    };
    (kind.to_string(), message)
}

fn params_json(
    params: &std::collections::BTreeMap<String, ParamValue>,
) -> Result<Value, (String, String)> {
    let mut object = serde_json::Map::new();
    for (key, value) in params {
        object.insert(key.clone(), param_json(value)?);
    }
    Ok(Value::Object(object))
}

fn param_json(value: &ParamValue) -> Result<Value, (String, String)> {
    Ok(match value {
        ParamValue::Null => Value::Null,
        ParamValue::Int(value) => json!(value),
        ParamValue::Bool(value) => json!(value),
        ParamValue::Str(value) => json!(value),
        ParamValue::List(values) => {
            let mut items = Vec::new();
            for value in values {
                items.push(param_json(value)?);
            }
            Value::Array(items)
        }
        ParamValue::Bytes(_) => {
            return Err((
                "invalid_params".to_string(),
                "byte parameters are outside the production subset".to_string(),
            ));
        }
    })
}

pub fn replay_task(task: &Task) -> Result<ReplayOutcome, (String, String)> {
    task.ensure_digest()
        .map_err(|error| ("source_changed".to_string(), error))?;
    ensure_execution_snapshot(task)?;
    if task
        .stages
        .get("validate")
        .map(|stage| stage.status.as_str())
        != Some("passed")
    {
        return Err((
            "not_validated".to_string(),
            "replay requires a passed validation".to_string(),
        ));
    }
    let input_path = task.request_dir().join("input.json");
    let stored: StoredInput = serde_json::from_slice(
        &fs::read(&input_path)
            .map_err(|error| ("infrastructure".to_string(), error.to_string()))?,
    )
    .map_err(|error| ("infrastructure".to_string(), error.to_string()))?;
    let replay_id = format!(
        "{}",
        fs::read_dir(task.instance_dir.join("replays"))
            .map(|entries| entries.count() + 1)
            .unwrap_or(1)
    );
    let replay_dir = task.instance_dir.join("replays").join(&replay_id);
    let workdir = replay_dir.join("work");
    fs::create_dir_all(&workdir)
        .map_err(|error| ("infrastructure".to_string(), error.to_string()))?;
    let listener = TcpListener::bind(format!("{}:0", task.target.host)).map_err(|error| {
        (
            "sut_start_failed".to_string(),
            format!("reserving a port: {error}"),
        )
    })?;
    let port = listener
        .local_addr()
        .map_err(|error| ("sut_start_failed".to_string(), error.to_string()))?
        .port();
    drop(listener);
    run_cleanup_script(
        task.target.cleanup_script.as_deref(),
        &workdir,
        &task.target.host,
        port,
    )
    .map_err(|error| ("infrastructure".to_string(), error.to_string()))?;
    let placeholders = TargetPlaceholders {
        port,
        root: workdir.clone(),
    };
    let args = task
        .target
        .args
        .iter()
        .map(|arg| placeholders.apply(arg))
        .collect::<Vec<_>>();
    let mut argv = vec![task.target.binary.display().to_string()];
    argv.extend(args);
    let mut supervisor = Supervisor::new(Vec::new());
    let sut_pid = supervisor
        .spawn(&SpawnSpec {
            role: "sut".to_string(),
            argv,
            env: task
                .target
                .extra_env
                .iter()
                .map(|(key, value)| (key.clone(), placeholders.apply(value)))
                .collect(),
            cwd: workdir.clone(),
            stdout_log: replay_dir.join("sut.stdout.log"),
            stderr_log: replay_dir.join("sut.stderr.log"),
            clear_afl: true,
        })
        .map_err(|error| ("sut_start_failed".to_string(), error))?;
    let ready_deadline = Instant::now() + Duration::from_millis(task.target.ready_ms.max(1));
    let mut listening = false;
    while Instant::now() < ready_deadline {
        if listens_on(sut_pid, port) {
            listening = true;
            break;
        }
        if !supervisor.running(sut_pid) {
            supervisor
                .cleanup(Duration::from_millis(task.target.term_grace_ms))
                .map_err(|error| ("cleanup_failed".to_string(), error))?;
            return Err((
                "sut_start_failed".to_string(),
                format!("SUT exited before listening on {port}"),
            ));
        }
        thread::sleep(Duration::from_millis(10));
    }
    if !listening {
        let cleanup = supervisor.cleanup(Duration::from_millis(task.target.term_grace_ms));
        let kind = if cleanup.is_err() {
            "cleanup_failed"
        } else {
            "ready_timeout"
        };
        return Err((
            kind.to_string(),
            format!(
                "SUT did not listen on {port} within {} ms",
                task.target.ready_ms
            ),
        ));
    }
    let request_path = replay_dir.join("exec-request.json");
    let result_path = replay_dir.join("exec-result.json");
    let request = json!({
        "generator": stored.generator,
        "step_trace": stored.step_trace,
        "dependencies": stored.dependencies,
        "params": params_json(&stored.params)?,
        "host": task.target.host,
        "port": port,
        "connect_timeout_s": task.target.connect_ms as f64 / 1000.0,
        "timeout_s": task.target.io_ms as f64 / 1000.0,
        "result": result_path,
    });
    fs::write(
        &request_path,
        serde_json::to_vec_pretty(&request)
            .map_err(|error| ("infrastructure".to_string(), error.to_string()))?,
    )
    .map_err(|error| ("infrastructure".to_string(), error.to_string()))?;
    let standalone =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("python/generator_server/standalone.py");
    let case_deadline = Instant::now() + Duration::from_millis(task.target.case_ms.max(1));
    let python_pid = supervisor
        .spawn(&SpawnSpec {
            role: "python".to_string(),
            argv: vec![
                task.python.display().to_string(),
                standalone.display().to_string(),
                request_path.display().to_string(),
            ],
            env: vec![(
                "PYTHONPATH".to_string(),
                task.pythonpath.display().to_string(),
            )],
            cwd: replay_dir.clone(),
            stdout_log: replay_dir.join("python.stdout.log"),
            stderr_log: replay_dir.join("python.stderr.log"),
            clear_afl: true,
        })
        .map_err(|error| ("infrastructure".to_string(), error))?;
    let mut python_status = None;
    let mut sut_crashed = false;
    while python_status.is_none() {
        if !supervisor.running(sut_pid) && listens_on(sut_pid, port) == false {
            // The process can exit after closing the socket. A zero exit is a normal early SUT exit.
            if let Ok(Some(status)) = supervisor.poll(sut_pid) {
                if !status.success() {
                    sut_crashed = true;
                    supervisor.signal_pid(python_pid, libc::SIGTERM);
                }
            }
        }
        if let Ok(Some(status)) = supervisor.poll(python_pid) {
            python_status = Some(status);
            break;
        }
        if Instant::now() >= case_deadline {
            supervisor.signal_pid(python_pid, libc::SIGKILL);
            supervisor.signal_pid(sut_pid, libc::SIGKILL);
            let _ = supervisor.cleanup(Duration::from_millis(task.target.term_grace_ms));
            return Err((
                "case_timeout".to_string(),
                "python did not finish before the case deadline".to_string(),
            ));
        }
        thread::sleep(Duration::from_millis(10));
    }
    if sut_crashed {
        let _ = supervisor.cleanup(Duration::from_millis(task.target.term_grace_ms));
        return Err((
            "sut_crash".to_string(),
            "SUT exited with an error".to_string(),
        ));
    }
    let status = python_status.unwrap();
    let execution = fs::read(&result_path)
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok());
    if execution
        .as_ref()
        .and_then(|value| value.get("status"))
        .and_then(Value::as_str)
        == Some("error")
    {
        let _ = supervisor.cleanup(Duration::from_millis(task.target.term_grace_ms));
        return Err((
            "python_exception".to_string(),
            execution
                .unwrap()
                .get("traceback")
                .and_then(Value::as_str)
                .unwrap_or("generator raised")
                .to_string(),
        ));
    }
    if !status.success() || execution.is_none() {
        let _ = supervisor.cleanup(Duration::from_millis(task.target.term_grace_ms));
        return Err((
            "missing_result".to_string(),
            "python exited without a valid execution result".to_string(),
        ));
    }
    let execution = execution.unwrap();
    if execution.get("status").and_then(Value::as_str) != Some("ok") {
        let _ = supervisor.cleanup(Duration::from_millis(task.target.term_grace_ms));
        return Err((
            "python_exception".to_string(),
            execution
                .get("traceback")
                .and_then(Value::as_str)
                .unwrap_or("generator raised")
                .to_string(),
        ));
    }
    let exit_deadline = Instant::now() + Duration::from_millis(task.target.sut_exit_ms);
    let mut sut_status = None;
    while Instant::now() < exit_deadline {
        if Supervisor::cancel_requested() {
            let _ = supervisor.cleanup(Duration::from_millis(task.target.term_grace_ms));
            return Err((
                "interrupted".to_string(),
                "evaluation interrupted".to_string(),
            ));
        }
        if let Some(status) = supervisor
            .poll(sut_pid)
            .map_err(|error| ("infrastructure".to_string(), error))?
        {
            sut_status = Some(status);
            break;
        }
        thread::sleep(Duration::from_millis(10));
    }
    let mut note = String::from(
        "Ctx byte counters can miss native sockets and other Python modules; zero is not a failure",
    );
    let mut framework_stopped = false;
    if sut_status.is_none() && supervisor.running(sut_pid) {
        framework_stopped = true;
        supervisor.signal_pid(sut_pid, libc::SIGTERM);
        let kill_at = Instant::now() + Duration::from_millis(task.target.term_grace_ms);
        while Instant::now() < kill_at {
            if let Some(status) = supervisor
                .poll(sut_pid)
                .map_err(|error| ("infrastructure".to_string(), error))?
            {
                sut_status = Some(status);
                break;
            }
            thread::sleep(Duration::from_millis(10));
        }
        if supervisor.running(sut_pid) {
            supervisor.signal_pid(sut_pid, libc::SIGKILL);
            let _ = supervisor.poll(sut_pid);
        }
        note.push_str("; framework cleanup stopped the SUT");
    }
    if let Some(status) = sut_status {
        if !framework_stopped && !status.success() {
            let detail = exit_detail(status);
            let _ = supervisor.cleanup(Duration::from_millis(task.target.term_grace_ms));
            return Err((
                "sut_crash".to_string(),
                format!("SUT exited abnormally before framework cleanup: {detail}"),
            ));
        }
    }
    supervisor
        .cleanup(Duration::from_millis(task.target.term_grace_ms))
        .map_err(|error| ("cleanup_failed".to_string(), error))?;
    for argv in &supervisor.launched {
        let joined = argv.join(" ");
        if joined.contains("forkserver")
            || argv
                .iter()
                .any(|arg| arg.ends_with("/hc") || arg == "hc" || arg.contains("iceoryx"))
        {
            return Err((
                "infrastructure".to_string(),
                "replay launched a forbidden component".to_string(),
            ));
        }
    }
    let _ = stored.origin;
    Ok(ReplayOutcome {
        replay_dir,
        message: note,
    })
}

pub struct ReplayOutcome {
    pub replay_dir: PathBuf,
    pub message: String,
}
