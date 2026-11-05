//! One case outcome. Target status and generator status stay separate.

use std::{fs::OpenOptions, io::Write, path::Path};

use libafl::{executors::ExitKind, Error};
use serde::Serialize;

/// How the target process stopped.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum TargetStop {
    /// `exit` status.
    Exited(i32),
    /// Terminated by this signal.
    Signaled(i32),
    /// Still running when the case was classified.
    Running,
}

impl TargetStop {
    /// Decode a `wait` status word.
    pub fn from_raw(status: i32) -> Self {
        if libc::WIFSIGNALED(status) {
            Self::Signaled(libc::WTERMSIG(status))
        } else if libc::WIFEXITED(status) {
            Self::Exited(libc::WEXITSTATUS(status))
        } else {
            Self::Exited(status)
        }
    }

    /// Short stable label stored next to the input.
    pub fn label(&self) -> String {
        match self {
            Self::Exited(code) => format!("exited:{code}"),
            Self::Signaled(signal) => format!("signal:{signal}"),
            Self::Running => "running".to_string(),
        }
    }
}

/// Saved after every case.
#[derive(Clone, Debug, Serialize)]
pub struct CaseReport {
    /// LibAFL exit classification.
    pub exit_kind: String,
    /// Target wait status.
    pub target_status: String,
    /// Generator logical status, such as `ok`, `timeout`, or `alert`.
    pub generator_status: String,
    /// Process reason: `exited`, `signaled`, `timeout`, or `missing`.
    pub generator_exit: String,
    /// Exit code or signal number for the generator process.
    pub generator_code: Option<i32>,
    /// Generator error text, when one was recorded.
    pub generator_error: Option<String>,
    /// Signal the fuzzer itself delivered to the SUT, when it did.
    pub cleanup_signal: Option<i32>,
    /// Non-zero bytes in the shared PCGUARD map, including a possible runtime marker at index 0.
    pub edges: usize,
    /// Coverage source. This build records `pcguard`.
    pub coverage: String,
    /// Wall time for this case.
    pub elapsed_ms: u128,
    /// Setup or execution note. Persistent reports do not include pids.
    pub note: String,
    /// False when this case must not enter coverage, crash, or corpus feedback.
    pub valid: bool,
    /// Consecutive startup timeouts after this case, including this one when it timed out.
    pub startup_timeout_streak: u32,
    /// Consecutive Result/status timeouts after this case.
    pub result_timeout_streak: u32,
}

impl CaseReport {
    /// Build a report from the classified outcome.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        kind: ExitKind,
        stop: &TargetStop,
        generator_status: &str,
        generator_exit: &str,
        generator_code: Option<i32>,
        generator_error: Option<String>,
        cleanup_signal: Option<i32>,
        edges: usize,
        elapsed_ms: u128,
        note: impl Into<String>,
        valid: bool,
        startup_timeout_streak: u32,
        result_timeout_streak: u32,
    ) -> Self {
        Self {
            exit_kind: match kind {
                ExitKind::Ok => "ok",
                ExitKind::Crash => "crash",
                ExitKind::Timeout => "timeout",
                ExitKind::Oom => "oom",
                ExitKind::Diff { .. } => "diff",
            }
            .to_string(),
            target_status: stop.label(),
            generator_status: generator_status.to_string(),
            generator_exit: generator_exit.to_string(),
            generator_code,
            generator_error,
            cleanup_signal,
            edges,
            coverage: "pcguard".to_string(),
            elapsed_ms,
            note: note.into(),
            valid,
            startup_timeout_streak,
            result_timeout_streak,
        }
    }
}

/// Append one JSON object per line.
pub fn append_jsonl(path: &Path, report: &CaseReport) -> Result<(), Error> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let mut file = OpenOptions::new().create(true).append(true).open(path)?;
    let line = serde_json::to_string(report)
        .map_err(|error| Error::serialize(format!("case report: {error}")))?;
    writeln!(file, "{line}")?;
    file.flush()?;
    Ok(())
}

/// Read the whole log. Used by tests that want the last line.
pub fn read_log(path: &Path) -> Result<String, Error> {
    std::fs::read_to_string(path).or_else(|error| {
        if error.kind() == std::io::ErrorKind::NotFound {
            Ok(String::new())
        } else {
            Err(Error::unknown(format!(
                "reading {}: {error}",
                path.display()
            )))
        }
    })
}
