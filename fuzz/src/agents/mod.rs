//! Coding-agent profiles and the campaign-private HC client.
//!
//! This module does not interpret protocol models. Callers pass argv, a prompt,
//! and a workspace. Credentials stay in the process environment and are not
//! copied into task logs.

pub(crate) mod artifacts;
mod hc;
mod profile;
mod session;
mod worker;
pub(crate) use artifacts::wait_file;
pub(crate) use session::{redact_text, AgentFailure, TerminalFact};
pub use session::{AgentSession, SessionPhase};
pub(crate) use worker::{
    AgentClient, AgentCommand, AgentInit, AgentOutcome, AgentTask, SessionInit, WorkerConfig,
};

pub(crate) const INIT_TIMEOUT_ATTEMPTS: u32 = 3;

/// Build a readable HC terminal name scoped to the directory containing the
/// campaign configuration.
pub(crate) fn campaign_terminal_name(kind: &str, campaign_base: &Path) -> String {
    let raw_id = campaign_base
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("campaign");
    let campaign_id: String = raw_id
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() || matches!(character, '-' | '_') {
                character
            } else {
                '-'
            }
        })
        .collect();
    let campaign_id = campaign_id.trim_matches('-');
    let campaign_id = if campaign_id.is_empty() {
        "campaign"
    } else {
        campaign_id
    };
    format!("mcn-fuzzer-{kind}-{campaign_id}")
}

pub use hc::{HcClient, HcError, MockCore, TerminalInfo};
pub use profile::{command_line, CommandLine};

use std::{
    fs,
    os::unix::{fs::PermissionsExt, process::CommandExt},
    path::{Path, PathBuf},
    process::Command,
    sync::Mutex,
    time::{Duration, Instant},
};

use crate::runtime::{drop_privileges, prepare_owned_dir, user_home, TargetIdentity};

#[derive(Clone, Debug)]
pub struct GenerationRequest {
    pub hc: PathBuf,
    pub output: PathBuf,
}

static REQUEST: Mutex<Option<GenerationRequest>> = Mutex::new(None);
static BOOT: Mutex<Option<BootRecord>> = Mutex::new(None);

/// Called on the supervisor side before `unshare`, for `run` only.
pub fn install_generation_request(request: GenerationRequest) {
    *REQUEST.lock().unwrap_or_else(|error| error.into_inner()) = Some(request);
}

pub fn current_boot() -> Option<BootRecord> {
    BOOT.lock()
        .unwrap_or_else(|error| error.into_inner())
        .clone()
}

/// Test hook. Production boot still goes through `boot_if_requested`.
#[cfg(test)]
pub fn install_boot_for_test(record: BootRecord) {
    *BOOT.lock().unwrap_or_else(|error| error.into_inner()) = Some(record);
}

/// Test hook.
#[cfg(test)]
pub fn clear_boot_for_test() {
    *BOOT.lock().unwrap_or_else(|error| error.into_inner()) = None;
}

pub fn generation_requested() -> bool {
    REQUEST
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .is_some()
}

/// Start the private core while PID 1 still has the host network.
pub fn boot_if_requested(identity: &TargetIdentity) -> Result<(), libafl::Error> {
    let request = REQUEST
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .clone();
    let Some(request) = request else {
        return Ok(());
    };
    let nonce = format!(
        "{}{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.subsec_nanos())
            .unwrap_or(1)
    );
    let session = format!("s{nonce}");
    let runtime = PathBuf::from(format!("/tmp/mcn-hc-{nonce}"));
    let socket = runtime.join("herdr-core").join(format!("{session}.sock"));
    let plan = BootPlan {
        executable: request.hc,
        session: session.clone(),
        runtime_dir: runtime,
        state_dir: request.output.join("hc-state"),
        config_dir: request.output.join("hc-config"),
        socket,
    };
    prepare_owned_dir(&request.output, identity)?;
    let _ = fs::write(
        request.output.join("private-hc-runtime"),
        plan.runtime_dir.display().to_string(),
    );
    let record = bootstrap(&plan, identity)?;
    *BOOT.lock().unwrap_or_else(|error| error.into_inner()) = Some(record);
    Ok(())
}

/// How PID 1 starts one private core before it enters the SUT network namespace.
#[derive(Clone, Debug)]
pub struct BootPlan {
    pub executable: PathBuf,
    pub session: String,
    pub runtime_dir: PathBuf,
    pub state_dir: PathBuf,
    pub config_dir: PathBuf,
    pub socket: PathBuf,
}

/// Remembered core identity. Shutdown signals only these processes.
#[derive(Clone, Debug, Default)]
pub struct BootRecord {
    pub socket: PathBuf,
    pub session: String,
}

/// Start a private core with a short-lived, already-dropped-privilege `hc` process.
///
/// A later connection failure does not start a replacement core.
pub fn bootstrap(plan: &BootPlan, identity: &TargetIdentity) -> Result<BootRecord, libafl::Error> {
    if let Err(error) = prepare_boot_dirs(plan, identity) {
        discard_boot_dirs(plan);
        return Err(error);
    }
    let home = user_home(identity.uid)?;
    let mut child = Command::new(&plan.executable);
    child
        .arg("ls")
        .env_clear()
        .env("HOME", &home)
        .env("USER", &identity.name)
        .env("LOGNAME", &identity.name)
        .env(
            "PATH",
            std::env::var("PATH").unwrap_or_else(|_| "/usr/bin:/bin".to_string()),
        )
        .env("XDG_RUNTIME_DIR", &plan.runtime_dir)
        .env("XDG_STATE_HOME", &plan.state_dir)
        .env("XDG_CONFIG_HOME", &plan.config_dir)
        .env("HC_SESSION", &plan.session)
        .env("HC_SOCKET", &plan.socket)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::piped());
    unsafe {
        child.pre_exec({
            let identity = identity.clone();
            move || {
                drop_privileges(&identity).map_err(|error| {
                    std::io::Error::new(std::io::ErrorKind::PermissionDenied, error.to_string())
                })?;
                Ok(())
            }
        });
    }
    let mut child = match child.spawn() {
        Ok(child) => child,
        Err(error) => {
            discard_boot_dirs(plan);
            return Err(libafl::Error::unknown(format!(
                "starting private HC core via {} failed: {error}. protocol generation needs the hc executable",
                plan.executable.display()
            )));
        }
    };
    let deadline = Instant::now() + Duration::from_secs(30);
    loop {
        if let Some(status) = child.try_wait()? {
            if !status.success() {
                discard_boot_dirs(plan);
                return Err(libafl::Error::unknown(format!(
                    "private HC bootstrap exited {status}"
                )));
            }
            break;
        }
        if plan.socket.exists() {
            let _ = child.kill();
            let _ = child.wait();
            break;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            discard_boot_dirs(plan);
            return Err(libafl::Error::unknown(
                "private HC core did not open its socket before the bootstrap deadline".to_string(),
            ));
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    if !plan.socket.exists() {
        discard_boot_dirs(plan);
        return Err(libafl::Error::unknown(format!(
            "private HC socket {} is missing after bootstrap; a replacement core is not started",
            plan.socket.display()
        )));
    }
    Ok(BootRecord {
        socket: plan.socket.clone(),
        session: plan.session.clone(),
    })
}

fn prepare_boot_dirs(plan: &BootPlan, identity: &TargetIdentity) -> Result<(), libafl::Error> {
    for path in [
        plan.runtime_dir.clone(),
        plan.state_dir.clone(),
        plan.config_dir.clone(),
        plan.socket.parent().unwrap_or(Path::new(".")).to_path_buf(),
    ] {
        fs::create_dir_all(&path)?;
        std::os::unix::fs::chown(&path, Some(identity.uid), Some(identity.gid)).map_err(
            |error| libafl::Error::unknown(format!("chown {} failed: {error}", path.display())),
        )?;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}

fn discard_boot_dirs(plan: &BootPlan) {
    let _ = fs::remove_dir_all(&plan.runtime_dir);
    let _ = fs::remove_dir_all(&plan.state_dir);
    let _ = fs::remove_dir_all(&plan.config_dir);
}

/// Remove a runtime directory recorded for this campaign. Startup can be killed
/// before the worker reaches its own shutdown, so the supervisor calls this too.
pub fn discard_recorded_runtime(output: &Path) {
    let marker = output.join("private-hc-runtime");
    let Ok(text) = fs::read_to_string(&marker) else {
        return;
    };
    let runtime = PathBuf::from(text.trim());
    if runtime.starts_with("/tmp/")
        && runtime
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name.starts_with("mcn-hc-"))
    {
        let _ = fs::remove_dir_all(&runtime);
    }
    let _ = fs::remove_file(&marker);
}

/// Stop this campaign's private core. Individual workers only close their terminal.
pub fn shutdown_private_core() {
    let boot = current_boot();
    let Some(boot) = boot else {
        return;
    };
    if let Ok(mut client) = HcClient::connect(&boot.socket, Duration::from_secs(2)) {
        shutdown_session(&mut client, &boot.session);
    }
    if let Some(runtime) = boot.socket.parent().and_then(|path| path.parent()) {
        let name = runtime
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("");
        if runtime != std::env::temp_dir() && name.starts_with("mcn-hc-") {
            let _ = fs::remove_dir_all(runtime);
        }
    }
}

/// Stop this campaign's terminal and private core. Other sessions are not signaled.
pub fn shutdown_session(client: &mut HcClient, session: &str) {
    let _ = client.call("server.stop", serde_json::json!({}));
    let self_pid = std::process::id() as i32;
    for pid in pids_with_session(session) {
        if pid == self_pid {
            continue;
        }
        unsafe {
            libc::kill(pid, libc::SIGTERM);
        }
    }
}

fn pids_with_session(session: &str) -> Vec<i32> {
    let needle = format!("HC_SESSION={session}");
    let mut found = Vec::new();
    let Ok(entries) = fs::read_dir("/proc") else {
        return found;
    };
    for entry in entries.flatten() {
        let Ok(pid) = entry.file_name().to_string_lossy().parse::<i32>() else {
            continue;
        };
        let Ok(bytes) = fs::read(format!("/proc/{pid}/environ")) else {
            continue;
        };
        let text = String::from_utf8_lossy(&bytes);
        if text.split('\0').any(|item| item == needle) {
            found.push(pid);
        }
    }
    found
}
