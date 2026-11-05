//! Target execution. Forkserver is the only backend in this tree.
//! A future Nyx-Net backend implements the same case operations.

pub mod forkserver;

pub use forkserver::ForkserverBackend;

use libafl::{executors::ExitKind, Error};

use crate::runtime::report::TargetStop;

/// One case, from process creation through the shared coverage map.
pub trait TargetBackend {
    /// Clear the shared coverage map. The executor restores the work directory.
    fn prepare_case(&mut self) -> Result<(), Error>;

    /// Ask the forkserver for a child and return its pid. The child runs immediately.
    fn start_case(&mut self) -> Result<i32, Error>;

    /// `Ok(None)` while the child is still running.
    fn poll_target(&mut self, timeout: std::time::Duration) -> Result<Option<TargetStop>, Error>;

    /// The child is already dead. The shared map still holds this case's raw counters.
    fn finish_case(&mut self) -> Result<(), Error>;

    /// Stop the child for `reason`. The shared map is left for the observer.
    fn abort_case(&mut self, reason: AbortReason) -> Result<Aborted, Error>;

    /// Pid of the long-lived forkserver process, when there is one.
    fn forkserver_pid(&self) -> i32;
}

/// Child stopped by the fuzzer, plus the signal that was sent.
pub struct Aborted {
    /// Final wait status.
    pub stop: TargetStop,
    /// Signal delivered by the fuzzer.
    pub cleanup_signal: i32,
}

/// Why the executor stopped a live child.
#[derive(Clone, Copy, Debug)]
pub enum AbortReason {
    /// The case deadline fired.
    Deadline,
    /// The generator failed and the target was still alive.
    Generator,
    /// The target did not exit after the generator closed its sockets.
    ExitGrace,
}

const CRASH_SIGNALS: &[i32] = &[
    libc::SIGSEGV,
    libc::SIGBUS,
    libc::SIGILL,
    libc::SIGABRT,
    libc::SIGFPE,
    libc::SIGTRAP,
];

/// Classify the target. Generator results are recorded separately and do not
/// decide this value.
///
/// `target_hang` is set only after the case deadline or the exit grace expired
/// while the target was still running. A normal exit is never a target
/// timeout. An unexpected fatal signal, including a real SIGSEGV, is a crash
/// even when a hang was also in progress or the generator later reports
/// `timeout`, `conn`, or `eof`. A signal this fuzzer sent is not a crash.
pub fn classify(stop: &TargetStop, cleanup: Option<i32>, target_hang: bool) -> ExitKind {
    match stop {
        TargetStop::Signaled(signal) => {
            if is_our_cleanup(*signal, cleanup) {
                if target_hang {
                    ExitKind::Timeout
                } else {
                    ExitKind::Ok
                }
            } else if CRASH_SIGNALS.contains(signal)
                || *signal == libc::SIGKILL
                || *signal == libc::SIGTERM
            {
                ExitKind::Crash
            } else if target_hang {
                ExitKind::Timeout
            } else {
                ExitKind::Ok
            }
        }
        TargetStop::Exited(_) => ExitKind::Ok,
        TargetStop::Running => {
            if target_hang {
                ExitKind::Timeout
            } else {
                ExitKind::Ok
            }
        }
    }
}

fn is_our_cleanup(signal: i32, cleanup: Option<i32>) -> bool {
    let Some(sent) = cleanup else {
        return false;
    };
    if sent == signal {
        return true;
    }
    let ours = sent == libc::SIGTERM || sent == libc::SIGKILL;
    let died = signal == libc::SIGTERM || signal == libc::SIGKILL;
    ours && died
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cleanup_signal_is_not_a_crash() {
        let stop = TargetStop::Signaled(libc::SIGTERM);
        assert_eq!(classify(&stop, Some(libc::SIGTERM), false), ExitKind::Ok);
        assert_eq!(
            classify(&stop, Some(libc::SIGTERM), true),
            ExitKind::Timeout
        );
    }

    #[test]
    fn escalated_sigkill_after_sigterm_is_not_a_crash() {
        let stop = TargetStop::Signaled(libc::SIGKILL);
        assert_eq!(classify(&stop, Some(libc::SIGTERM), false), ExitKind::Ok);
        assert_eq!(
            classify(&stop, Some(libc::SIGTERM), true),
            ExitKind::Timeout
        );
    }

    #[test]
    fn unexpected_sigsegv_outranks_hang_and_generator_errors() {
        let stop = TargetStop::Signaled(libc::SIGSEGV);
        assert_eq!(classify(&stop, None, false), ExitKind::Crash);
        assert_eq!(classify(&stop, None, true), ExitKind::Crash);
        assert_eq!(classify(&stop, Some(libc::SIGTERM), true), ExitKind::Crash);
    }

    #[test]
    fn normal_exit_is_not_a_target_timeout() {
        assert_eq!(classify(&TargetStop::Exited(0), None, false), ExitKind::Ok);
        assert_eq!(classify(&TargetStop::Exited(0), None, true), ExitKind::Ok);
        assert_eq!(classify(&TargetStop::Exited(2), None, true), ExitKind::Ok);
        assert_eq!(
            classify(&TargetStop::Running, None, true),
            ExitKind::Timeout
        );
        assert_eq!(classify(&TargetStop::Running, None, false), ExitKind::Ok);
    }
}
