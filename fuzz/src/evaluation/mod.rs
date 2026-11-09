//! Offline orchestration for generator synthesis evaluation.
//!
//! This module does not start HC, the resident Python server, a forkserver,
//! a namespace, or capture.

mod agent;
mod config;
mod process;
mod replay;
mod report;
mod task;

use std::{
    fs,
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

use crate::app::config::Campaign;

pub use config::Suite;
pub use task::Task;

#[cfg(test)]
mod tests;

/// Adopt orphaned grandchildren. The evaluation binary calls this once.
pub fn install_subreaper() {
    process::Supervisor::become_subreaper();
}

/// TERM and INT ask in-flight supervisors to stop and reap their children.
pub fn install_signal_handlers() {
    process::Supervisor::install_signal_handlers();
}

/// Spawn long-lived children and block until TERM or INT, then clean them up.
pub fn signal_probe() -> i32 {
    let Ok(dir) = std::env::var("MCN_EVAL_PROBE_DIR") else {
        eprintln!("MCN_EVAL_PROBE_DIR is required");
        return 2;
    };
    let dir = PathBuf::from(dir);
    if let Err(error) = fs::create_dir_all(&dir) {
        eprintln!("{error}");
        return 2;
    }
    let mut supervisor = process::Supervisor::new(Vec::new());
    let spec = |role: &str, argv: Vec<String>, stdout: &str, stderr: &str| process::SpawnSpec {
        role: role.to_string(),
        argv,
        env: Vec::new(),
        cwd: dir.clone(),
        stdout_log: dir.join(stdout),
        stderr_log: dir.join(stderr),
        clear_afl: false,
    };
    let sleep_pid = match supervisor.spawn(&spec(
        "sleep",
        vec!["/bin/sleep".into(), "60".into()],
        "sleep.out",
        "sleep.err",
    )) {
        Ok(pid) => pid,
        Err(error) => {
            let _ = fs::write(dir.join("result.txt"), error);
            return 1;
        }
    };
    let script = format!(
        "python3 -c 'import os,time; os.setsid(); time.sleep(60)' </dev/null >/dev/null 2>&1 &\necho $! > '{}'\nexit 0\n",
        dir.join("orphan.pid").display()
    );
    let shell_pid = match supervisor.spawn(&spec(
        "shell",
        vec!["/bin/sh".into(), "-c".into(), script],
        "shell.out",
        "shell.err",
    )) {
        Ok(pid) => pid,
        Err(error) => {
            let _ = supervisor.cleanup(Duration::from_millis(200));
            let _ = fs::write(dir.join("result.txt"), error);
            return 1;
        }
    };
    let _ = supervisor.wait_for(shell_pid, Duration::from_secs(2));
    let _ = fs::write(dir.join("sleep.pid"), sleep_pid.to_string());
    let _ = fs::write(dir.join("ready"), "1");
    let deadline = Instant::now() + Duration::from_secs(20);
    while Instant::now() < deadline && !process::Supervisor::cancel_requested() {
        std::thread::sleep(Duration::from_millis(20));
    }
    if !process::Supervisor::cancel_requested() {
        let _ = supervisor.cleanup(Duration::from_millis(200));
        let _ = fs::write(dir.join("result.txt"), "timed out waiting for a signal");
        return 2;
    }
    match supervisor.cleanup(Duration::from_millis(300)) {
        Ok(()) => {
            let _ = fs::write(dir.join("result.txt"), "cleaned");
            0
        }
        Err(error) => {
            let _ = fs::write(dir.join("result.txt"), error);
            1
        }
    }
}

pub struct Options {
    pub suite: PathBuf,
    pub profiles: Vec<String>,
    pub cases: Vec<String>,
    pub repeat: u32,
    pub out: Option<PathBuf>,
}

pub fn prepare(options: &Options) -> Result<Vec<PathBuf>, String> {
    let tasks = build(options)?;
    Ok(tasks
        .into_iter()
        .map(|task| task.instance_dir.join("task.json"))
        .collect())
}

pub fn generate(path: &Path) -> Result<Task, String> {
    run_stage(path, "generate", |task| {
        let campaign = Campaign::load(&task.sources.campaign).map_err(|error| error.to_string())?;
        let profile = campaign
            .agents
            .profiles
            .get(&task.profile)
            .ok_or_else(|| format!("agent profile {} is not defined", task.profile))?;
        let provider = agent::provider_of(task)?;
        let deadline = Duration::from_secs(campaign.generator_synthesis.init_timeout_secs.max(1));
        let generation_deadline =
            Duration::from_secs(campaign.generator_synthesis.task_timeout_secs.max(1));
        let init_prompt = fs::read_to_string(task.prepared().join("prompts/init.md"))
            .map_err(|error| error.to_string())?;
        let generation_prompt = fs::read_to_string(task.prepared().join("prompts/generation.md"))
            .map_err(|error| error.to_string())?;
        let env = profile.env.clone();
        let mut session = agent::DirectAgentSession::new(task, provider, deadline, env);
        session
            .initialize(&init_prompt)
            .map_err(|(kind, message)| format!("{kind}: {message}"))?;
        session.deadline = generation_deadline;
        session
            .generate(&generation_prompt)
            .map_err(|(kind, message)| format!("{kind}: {message}"))?;
        Ok(())
    })
}

pub fn validate(path: &Path) -> Result<Task, String> {
    run_stage(path, "validate", |task| {
        replay::validate_task(task).map_err(|(kind, message)| format!("{kind}: {message}"))
    })
}

pub fn replay(path: &Path) -> Result<Task, String> {
    run_stage(path, "replay", |task| {
        let outcome =
            replay::replay_task(task).map_err(|(kind, message)| format!("{kind}: {message}"))?;
        if let Some(stage) = task.stages.get_mut("replay") {
            stage.message = Some(outcome.message);
            stage.artifacts = vec![outcome.replay_dir];
        }
        Ok(())
    })
}

pub fn run(options: &Options) -> Result<bool, String> {
    let mut tasks = build(options)?;
    let out = tasks
        .first()
        .map(|task| {
            task.instance_dir
                .parent()
                .and_then(|path| path.parent())
                .and_then(|path| path.parent())
                .unwrap_or(Path::new("."))
                .to_path_buf()
        })
        .unwrap_or_else(|| PathBuf::from("."));
    for task in &mut tasks {
        if let Err(message) = stage_generate(task) {
            fail_stage(task, "generate", &message);
            mark_not_run(task, &["validate", "replay"]);
            continue;
        }
        if let Err(message) = stage_validate(task) {
            fail_stage(task, "validate", &message);
            mark_not_run(task, &["replay"]);
            continue;
        }
        if let Err(message) = stage_replay(task) {
            fail_stage(task, "replay", &message);
        }
    }
    // The instance dir is out/profile/case/repeat, so the run root is three parents up
    // only when every task shares that layout. Callers pass --out, which build() uses.
    let root = shared_root(&tasks).unwrap_or(out);
    report::write_report(&root, &tasks)?;
    Ok(tasks.iter().all(report::instance_passed))
}

fn build(options: &Options) -> Result<Vec<Task>, String> {
    if options.repeat == 0 {
        return Err("--repeat must be greater than 0".to_string());
    }
    let suite = Suite::load(&options.suite)?;
    let campaign = Campaign::load(&suite.campaign).map_err(|error| error.to_string())?;
    let profiles = if options.profiles.is_empty() {
        if campaign.generator_synthesis.profile.is_empty() {
            return Err(
                "no --profile was given and generator_synthesis.profile is empty".to_string(),
            );
        }
        vec![campaign.generator_synthesis.profile.clone()]
    } else {
        options.profiles.clone()
    };
    let cases = suite.select(&options.cases)?;
    let out = match &options.out {
        Some(path) => path.clone(),
        None => PathBuf::from("test-results/synthesis").join(task::run_id()),
    };
    fs::create_dir_all(&out).map_err(|error| error.to_string())?;
    let mut tasks = Vec::new();
    for profile in &profiles {
        for case in &cases {
            for repeat in 1..=options.repeat {
                let started = Instant::now();
                let mut prepared =
                    task::prepare_instance(&suite, &campaign, profile, &case.name, repeat, &out)?;
                if let Some(stage) = prepared.stages.get_mut("prepare") {
                    stage.duration_ms = Some(started.elapsed().as_millis() as u64);
                }
                prepared.save()?;
                tasks.push(prepared);
            }
        }
    }
    Ok(tasks)
}

fn run_stage(
    path: &Path,
    name: &str,
    body: impl FnOnce(&mut Task) -> Result<(), String>,
) -> Result<Task, String> {
    let mut task = Task::load(path)?;
    let started = Instant::now();
    match body(&mut task) {
        Ok(()) => {
            let artifacts = task
                .stages
                .get(name)
                .map(|stage| stage.artifacts.clone())
                .unwrap_or_default();
            let message = task
                .stages
                .get(name)
                .and_then(|stage| stage.message.clone());
            let mut record =
                task::StageRecord::passed(started.elapsed().as_millis() as u64, artifacts);
            record.message = message;
            task.stages.insert(name.to_string(), record);
        }
        Err(message) => {
            let kind = kind_of(&message);
            task.stages.insert(
                name.to_string(),
                task::StageRecord::failed(
                    started.elapsed().as_millis() as u64,
                    &kind,
                    message.clone(),
                ),
            );
            task.save()?;
            return Err(message);
        }
    }
    task.save()?;
    Ok(task)
}

fn stage_generate(task: &mut Task) -> Result<(), String> {
    reload_stage(task, generate)
}

fn stage_validate(task: &mut Task) -> Result<(), String> {
    reload_stage(task, validate)
}

fn stage_replay(task: &mut Task) -> Result<(), String> {
    reload_stage(task, replay)
}

fn reload_stage(
    task: &mut Task,
    call: impl Fn(&std::path::Path) -> Result<Task, String>,
) -> Result<(), String> {
    task.save()?;
    let path = task.instance_dir.join("task.json");
    match call(&path) {
        Ok(updated) => {
            *task = updated;
            Ok(())
        }
        Err(message) => {
            if let Ok(updated) = Task::load(&path) {
                *task = updated;
            }
            Err(message)
        }
    }
}

fn fail_stage(task: &mut Task, name: &str, message: &str) {
    if let Some(stage) = task.stages.get(name) {
        if stage.status == "failed" {
            let _ = task.save();
            return;
        }
    }
    task.stages.insert(
        name.to_string(),
        task::StageRecord::failed(0, &kind_of(message), message),
    );
    let _ = task.save();
}

fn mark_not_run(task: &mut Task, names: &[&str]) {
    for name in names {
        task.stages
            .insert((*name).to_string(), task::StageRecord::not_run());
    }
    let _ = task.save();
}

fn kind_of(message: &str) -> String {
    let kinds = [
        "missing_ready",
        "missing_result",
        "bad_id",
        "invalid_schema",
        "invalid_params",
        "invalid_generator",
        "invalid_artifact",
        "cli_exit",
        "deadline",
        "auth",
        "preflight",
        "case_timeout",
        "python_exception",
        "sut_start_failed",
        "sut_crash",
        "artifact_changed",
        "interrupted",
        "ready_timeout",
        "cleanup_failed",
        "source_changed",
        "not_validated",
    ];
    // Structured stage errors carry their kind before the first colon. The
    // diagnostic can also contain a phase name, e.g. "interrupted: codex
    // preflight", which must not override the actual failure kind.
    if let Some((kind, _)) = message.split_once(':') {
        if kinds.contains(&kind) {
            return kind.to_string();
        }
    }
    for kind in kinds {
        if message.contains(kind) {
            return kind.to_string();
        }
    }
    "failed".to_string()
}

fn shared_root(tasks: &[Task]) -> Option<PathBuf> {
    let first = tasks.first()?;
    Some(
        first
            .instance_dir
            .parent()?
            .parent()?
            .parent()?
            .to_path_buf(),
    )
}
