//! Case lifecycle policy shared by the executor.
//!
//! Startup timeouts and Result/status timeouts use separate consecutive
//! counters. A success of one kind clears only that counter. Case deadlines
//! and peer-grace kills are not counted here. Nothing in this module restarts
//! a Python server or a SUT forkserver.

use std::{sync::Mutex, time::Duration};

use libafl::{executors::ExitKind, Error};

use crate::runtime::report::TargetStop;

/// Worker exit chosen by the generator itself for an I/O timeout.
pub const EXIT_TIMEOUT: i32 = 75;
/// Worker exit for a connection failure.
pub const EXIT_CONN: i32 = 76;
/// Worker exit for a peer EOF.
pub const EXIT_EOF: i32 = 77;
/// Worker exit for a TLS alert.
pub const EXIT_ALERT: i32 = 78;
/// Worker exit for a script or protocol error.
pub const EXIT_GENERATOR: i32 = 79;
pub const EXIT_DEPENDENCY: i32 = 80;

/// Default wait for both PIDs, in milliseconds.
pub const DEFAULT_STARTUP_TIMEOUT_MS: u64 = 1_000;
/// Default peer-exit grace, in milliseconds.
pub const DEFAULT_PEER_GRACE_MS: u64 = 100;
/// Default wait for a Result or forkserver status, in milliseconds.
pub const DEFAULT_RESULT_TIMEOUT_MS: u64 = 100;
/// Default consecutive recoverable timeouts allowed before the next one ends the job.
pub const DEFAULT_FAILURE_LIMIT: u32 = 5;

/// Durations and the failure limit for one job.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct JobPolicy {
    /// Wait for both PIDs after both start requests are submitted.
    pub startup_timeout: Duration,
    /// Whole-case budget after both PIDs are known. `None` uses the campaign value.
    pub case_timeout: Option<Duration>,
    /// Grace after one root exits, and after a startup deadline.
    pub peer_grace: Duration,
    /// Wait for Result or forkserver status after the root is known to have exited.
    pub result_timeout: Duration,
    /// Recoverable timeouts allowed before the next one of that kind ends the job.
    pub failure_limit: u32,
    /// Classify a SUT killed after peer grace as Timeout.
    pub sut_grace_kill_is_timeout: bool,
}

impl Default for JobPolicy {
    fn default() -> Self {
        Self {
            startup_timeout: Duration::from_millis(DEFAULT_STARTUP_TIMEOUT_MS),
            case_timeout: None,
            peer_grace: Duration::from_millis(DEFAULT_PEER_GRACE_MS),
            result_timeout: Duration::from_millis(DEFAULT_RESULT_TIMEOUT_MS),
            failure_limit: DEFAULT_FAILURE_LIMIT,
            sut_grace_kill_is_timeout: false,
        }
    }
}

impl JobPolicy {
    /// Reject zero and durations that do not fit in a millisecond conversion.
    pub fn from_millis(
        startup_timeout_ms: u64,
        case_timeout_ms: Option<u64>,
        peer_exit_grace_ms: u64,
        result_timeout_ms: u64,
        failure_limit: u32,
        sut_grace_kill_is_timeout: bool,
    ) -> Result<Self, Error> {
        Ok(Self {
            startup_timeout: checked_millis("startup-timeout-ms", startup_timeout_ms)?,
            case_timeout: match case_timeout_ms {
                Some(value) => Some(checked_millis("case-timeout-ms", value)?),
                None => None,
            },
            peer_grace: checked_millis("peer-exit-grace-ms", peer_exit_grace_ms)?,
            result_timeout: checked_millis("result-timeout-ms", result_timeout_ms)?,
            failure_limit,
            sut_grace_kill_is_timeout,
        })
    }

    /// Case budget, using the campaign value when the CLI omitted one.
    pub fn effective_case_timeout(&self, campaign_case_ms: u64) -> Result<Duration, Error> {
        if let Some(timeout) = self.case_timeout {
            return Ok(timeout);
        }
        checked_millis("campaign case_ms", campaign_case_ms)
    }

    /// One line of the values that will actually be used.
    pub fn describe(&self, case_timeout: Duration) -> String {
        format!(
            "startup-timeout-ms {} case-timeout-ms {} peer-exit-grace-ms {} result-timeout-ms {} timeout-failure-limit {} sut-grace-kill-is-timeout {}",
            self.startup_timeout.as_millis(),
            case_timeout.as_millis(),
            self.peer_grace.as_millis(),
            self.result_timeout.as_millis(),
            self.failure_limit,
            self.sut_grace_kill_is_timeout
        )
    }
}

fn checked_millis(name: &str, value: u64) -> Result<Duration, Error> {
    if value == 0 {
        return Err(Error::illegal_argument(format!(
            "{name} must be greater than 0"
        )));
    }
    if value > u64::MAX / 1_000_000 {
        return Err(Error::illegal_argument(format!(
            "{name} cannot be represented as a duration"
        )));
    }
    Ok(Duration::from_millis(value))
}

static POLICY: Mutex<JobPolicy> = Mutex::new(JobPolicy {
    startup_timeout: Duration::from_millis(DEFAULT_STARTUP_TIMEOUT_MS),
    case_timeout: None,
    peer_grace: Duration::from_millis(DEFAULT_PEER_GRACE_MS),
    result_timeout: Duration::from_millis(DEFAULT_RESULT_TIMEOUT_MS),
    failure_limit: DEFAULT_FAILURE_LIMIT,
    sut_grace_kill_is_timeout: false,
});

/// Install the policy parsed from the CLI. Later case loops read it.
pub fn install_job_policy(policy: JobPolicy) {
    *POLICY.lock().unwrap_or_else(|error| error.into_inner()) = policy;
}

/// Policy for this process. The default matches the CLI defaults.
pub fn job_policy() -> JobPolicy {
    POLICY
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .clone()
}

/// Which consecutive counter a timeout belongs to.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FailureKind {
    /// Both PIDs were late, but both eventually became known.
    Startup,
    /// A Result or forkserver status missed its deadline.
    Result,
}

/// Independent startup and Result/status streaks.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TimeoutStreaks {
    /// Consecutive recoverable startup timeouts.
    pub startup: u32,
    /// Consecutive Result/status timeouts.
    pub result: u32,
    /// Exit when a counter becomes greater than this value.
    pub limit: u32,
}

impl TimeoutStreaks {
    /// Counters start at zero.
    pub fn new(limit: u32) -> Self {
        Self {
            startup: 0,
            result: 0,
            limit,
        }
    }

    /// Add one failure. `true` means this failure exceeds the limit and the job must stop.
    pub fn fail(&mut self, kind: FailureKind) -> bool {
        let slot = match kind {
            FailureKind::Startup => &mut self.startup,
            FailureKind::Result => &mut self.result,
        };
        *slot = slot.saturating_add(1);
        *slot > self.limit
    }

    /// Clear only the counter that just succeeded.
    pub fn succeed(&mut self, kind: FailureKind) {
        match kind {
            FailureKind::Startup => self.startup = 0,
            FailureKind::Result => self.result = 0,
        }
    }
}

/// What the startup barrier concluded after the deadline and the peer grace.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StartupEnd {
    /// Both PIDs arrived before the startup deadline.
    OnTime,
    /// The deadline passed, and both PIDs were known by the end of grace.
    LateButKnown,
    /// At least one PID was still missing when grace ended.
    MissingPid {
        /// SUT fork request produced no PID.
        sut: bool,
        /// Python `Started` produced no PID.
        generator: bool,
    },
}

/// Classify the startup barrier. A late-but-known pair is recoverable.
pub fn classify_startup(
    deadline_passed: bool,
    sut_known: bool,
    generator_known: bool,
) -> StartupEnd {
    if sut_known && generator_known && !deadline_passed {
        StartupEnd::OnTime
    } else if sut_known && generator_known {
        StartupEnd::LateButKnown
    } else {
        StartupEnd::MissingPid {
            sut: !sut_known,
            generator: !generator_known,
        }
    }
}

/// Why the fuzzer signalled the SUT, if it did.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SutKillReason {
    /// The SUT exited without a fuzzer signal.
    None,
    /// The whole-case deadline fired.
    CaseDeadline,
    /// The generator exited and the peer grace expired.
    PeerGrace,
}

const CRASH_SIGNALS: &[i32] = &[
    libc::SIGSEGV,
    libc::SIGBUS,
    libc::SIGILL,
    libc::SIGABRT,
    libc::SIGFPE,
    libc::SIGTRAP,
];

/// Classify the SUT. A case-deadline kill is Timeout unless a foreign crash won the race.
pub fn classify_sut(stop: &TargetStop, kill: SutKillReason, grace_is_timeout: bool) -> ExitKind {
    if is_foreign_crash(stop) {
        return ExitKind::Crash;
    }
    match kill {
        SutKillReason::CaseDeadline => ExitKind::Timeout,
        SutKillReason::PeerGrace if grace_is_timeout => ExitKind::Timeout,
        SutKillReason::PeerGrace | SutKillReason::None => {
            let _ = stop;
            ExitKind::Ok
        }
    }
}

fn is_foreign_crash(stop: &TargetStop) -> bool {
    match stop {
        TargetStop::Signaled(signal) => CRASH_SIGNALS.contains(signal),
        _ => false,
    }
}

/// How the generator process ended, separate from its logical status.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProcessReason {
    /// The worker called `_exit`.
    Exited,
    /// An external signal ended the worker.
    Signaled,
    /// The fuzzer killed the worker for a case or peer deadline.
    Timeout,
    /// The Result did not arrive before the result deadline.
    Missing,
}

impl ProcessReason {
    /// Stable label stored in the case report.
    pub fn label(self) -> &'static str {
        match self {
            Self::Exited => "exited",
            Self::Signaled => "signaled",
            Self::Timeout => "timeout",
            Self::Missing => "missing",
        }
    }
}

/// Logical generator status plus the process reason.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GeneratorOutcome {
    /// `ok`, `timeout`, `conn`, `eof`, `alert`, `generator`, `signaled`, or `incomplete`.
    pub logical: String,
    /// Process-level reason.
    pub reason: ProcessReason,
    /// Exit code or signal number, when one was observed.
    pub code: Option<i32>,
}

/// Map a worker wait result. A fuzzer kill is `timeout` even when waitpid reports SIGKILL.
pub fn generator_outcome(
    exit_code: Option<i32>,
    signal: Option<i32>,
    killed_by_fuzzer: bool,
) -> GeneratorOutcome {
    if killed_by_fuzzer {
        return GeneratorOutcome {
            logical: "timeout".to_string(),
            reason: ProcessReason::Timeout,
            code: signal.or(exit_code),
        };
    }
    if let Some(code) = exit_code {
        return GeneratorOutcome {
            logical: logical_exit_status(code).to_string(),
            reason: ProcessReason::Exited,
            code: Some(code),
        };
    }
    if let Some(signal) = signal {
        return GeneratorOutcome {
            logical: "signaled".to_string(),
            reason: ProcessReason::Signaled,
            code: Some(signal),
        };
    }
    GeneratorOutcome {
        logical: "incomplete".to_string(),
        reason: ProcessReason::Missing,
        code: None,
    }
}

fn logical_exit_status(code: i32) -> &'static str {
    match code {
        0 => "ok",
        EXIT_TIMEOUT => "timeout",
        EXIT_CONN => "conn",
        EXIT_EOF => "eof",
        EXIT_ALERT => "alert",
        EXIT_DEPENDENCY => "dependency",
        _ => "generator",
    }
}

/// Identifiers that tie one IPC response to one request.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ResponseId {
    /// Monotonic request id.
    pub request: u64,
    /// Worker nonce.
    pub worker: u64,
    /// Case id.
    pub case: u64,
}

/// What to do with one decoded response.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ResponseDisposition {
    /// Belongs to the open request.
    Accept,
    /// Belongs to a closed request. Keep it out of later cases.
    Drain,
    /// Does not match an open request or a tombstone.
    Reject,
}

/// Accept only the open request. Tombstones and already-closed ids are drained.
pub fn response_disposition(
    open: Option<ResponseId>,
    got: ResponseId,
    closed: bool,
    tombstones: &[ResponseId],
) -> ResponseDisposition {
    if let Some(open) = open {
        if open == got && !closed {
            return ResponseDisposition::Accept;
        }
    }
    if tombstones.iter().any(|item| *item == got) || (closed && open == Some(got)) {
        ResponseDisposition::Drain
    } else {
        ResponseDisposition::Reject
    }
}

/// Parent and start time observed for a reported PID.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PidObservation {
    /// Parent pid.
    pub ppid: i32,
    /// `/proc` starttime.
    pub starttime: u64,
}

/// Whether a reported PID may be signalled.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PidCheck {
    /// `/proc` matches the expected parent.
    Usable(PidObservation),
    /// `/proc` was already gone. Do not signal a bare pid.
    AlreadyExited,
    /// The pid is present but does not match the expected parent.
    Rejected,
}

/// Check a PID before any signal. A missing `/proc` entry is an early exit, not a reuse.
pub fn assess_pid(found: Option<PidObservation>, expected_ppid: i32) -> PidCheck {
    match found {
        None => PidCheck::AlreadyExited,
        Some(observed) if observed.ppid == expected_ppid && expected_ppid > 0 => {
            PidCheck::Usable(observed)
        }
        Some(_) => PidCheck::Rejected,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn streaks_are_independent_and_reset_on_their_own_success() {
        let mut streaks = TimeoutStreaks::new(5);
        for _ in 0..5 {
            assert!(!streaks.fail(FailureKind::Startup));
        }
        assert_eq!(streaks.startup, 5);
        assert_eq!(streaks.result, 0);
        assert!(!streaks.fail(FailureKind::Result));
        assert_eq!(streaks.result, 1);
        streaks.succeed(FailureKind::Startup);
        assert_eq!(streaks.startup, 0);
        assert_eq!(streaks.result, 1);
        for _ in 0..4 {
            assert!(!streaks.fail(FailureKind::Result));
        }
        assert_eq!(streaks.result, 5);
        assert!(streaks.fail(FailureKind::Result));
        streaks.succeed(FailureKind::Result);
        assert_eq!(streaks.result, 0);
        assert!(!streaks.fail(FailureKind::Startup));
        assert_eq!(streaks.startup, 1);
    }

    #[test]
    fn sixth_startup_timeout_exits_without_touching_the_result_counter() {
        let mut streaks = TimeoutStreaks::new(DEFAULT_FAILURE_LIMIT);
        for _ in 0..5 {
            assert!(!streaks.fail(FailureKind::Startup));
        }
        assert!(streaks.fail(FailureKind::Startup));
        assert_eq!(streaks.startup, 6);
        assert_eq!(streaks.result, 0);
    }

    #[test]
    fn startup_resolution_keeps_late_pids_recoverable() {
        assert_eq!(classify_startup(false, true, true), StartupEnd::OnTime);
        assert_eq!(classify_startup(true, true, true), StartupEnd::LateButKnown);
        assert_eq!(
            classify_startup(true, false, true),
            StartupEnd::MissingPid {
                sut: true,
                generator: false
            }
        );
        assert_eq!(
            classify_startup(false, false, false),
            StartupEnd::MissingPid {
                sut: true,
                generator: true
            }
        );
    }

    #[test]
    fn sut_grace_kill_stays_ok_unless_requested() {
        let killed = TargetStop::Signaled(libc::SIGKILL);
        assert_eq!(
            classify_sut(&killed, SutKillReason::PeerGrace, false),
            ExitKind::Ok
        );
        assert_eq!(
            classify_sut(&killed, SutKillReason::PeerGrace, true),
            ExitKind::Timeout
        );
        assert_eq!(
            classify_sut(&killed, SutKillReason::CaseDeadline, false),
            ExitKind::Timeout
        );
        assert_eq!(
            classify_sut(&TargetStop::Exited(0), SutKillReason::None, false),
            ExitKind::Ok
        );
        assert_eq!(
            classify_sut(
                &TargetStop::Signaled(libc::SIGSEGV),
                SutKillReason::CaseDeadline,
                true
            ),
            ExitKind::Crash
        );
    }

    #[test]
    fn generator_io_timeout_stays_an_exit_and_a_fuzzer_kill_is_timeout() {
        let io = generator_outcome(Some(EXIT_TIMEOUT), None, false);
        assert_eq!(io.logical, "timeout");
        assert_eq!(io.reason, ProcessReason::Exited);
        let killed = generator_outcome(None, Some(libc::SIGKILL), true);
        assert_eq!(killed.logical, "timeout");
        assert_eq!(killed.reason, ProcessReason::Timeout);
        assert_eq!(killed.code, Some(libc::SIGKILL));
        let alert = generator_outcome(Some(EXIT_ALERT), None, false);
        assert_eq!(alert.logical, "alert");
        assert_eq!(alert.reason, ProcessReason::Exited);
        let missing = generator_outcome(None, None, false);
        assert_eq!(missing.reason, ProcessReason::Missing);
    }

    #[test]
    fn late_responses_drain_and_unknown_responses_are_rejected() {
        let open = ResponseId {
            request: 4,
            worker: 9,
            case: 2,
        };
        let closed = ResponseId {
            request: 3,
            worker: 9,
            case: 1,
        };
        assert_eq!(
            response_disposition(Some(open), open, false, &[]),
            ResponseDisposition::Accept
        );
        assert_eq!(
            response_disposition(Some(open), open, true, &[]),
            ResponseDisposition::Drain
        );
        assert_eq!(
            response_disposition(Some(open), closed, false, &[closed]),
            ResponseDisposition::Drain
        );
        let foreign = ResponseId {
            request: 8,
            worker: 1,
            case: 1,
        };
        assert_eq!(
            response_disposition(Some(open), foreign, false, &[closed]),
            ResponseDisposition::Reject
        );
    }

    #[test]
    fn pid_check_rejects_a_reused_or_unrelated_pid() {
        assert_eq!(assess_pid(None, 20), PidCheck::AlreadyExited);
        assert_eq!(
            assess_pid(
                Some(PidObservation {
                    ppid: 20,
                    starttime: 5
                }),
                20
            ),
            PidCheck::Usable(PidObservation {
                ppid: 20,
                starttime: 5
            })
        );
        assert_eq!(
            assess_pid(
                Some(PidObservation {
                    ppid: 4,
                    starttime: 5
                }),
                20
            ),
            PidCheck::Rejected
        );
        assert_eq!(
            assess_pid(
                Some(PidObservation {
                    ppid: 0,
                    starttime: 5
                }),
                0
            ),
            PidCheck::Rejected
        );
    }

    #[test]
    fn zero_durations_are_rejected() {
        let error = JobPolicy::from_millis(0, None, 100, 100, 5, false).unwrap_err();
        assert!(
            format!("{error:?}").contains("startup-timeout-ms"),
            "{error:?}"
        );
        let error = JobPolicy::from_millis(1000, Some(0), 100, 100, 5, false).unwrap_err();
        assert!(
            format!("{error:?}").contains("case-timeout-ms"),
            "{error:?}"
        );
        let error = checked_millis("result-timeout-ms", u64::MAX).unwrap_err();
        assert!(format!("{error:?}").contains("represented"), "{error:?}");
    }
}
