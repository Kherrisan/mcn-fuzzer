//! The core fuzzer logic! When to switch fuzzing different states.
//!
//! Extending or wrapping [`libafl::fuzzer::StdFuzzer`] is painful, so we write a
//! function that would otherwise be an implementation on the state.
//! A wrapper fuzzer would have to reimplement all the required traits.
//! In the future this perhaps could be done with the Ambassador crate, but even
//! that would be painful since they are external traits.

use std::{
    io::ErrorKind,
    marker::PhantomData,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::Duration,
};

use std::borrow::Cow;

use libafl::{
    corpus::Corpus,
    events::{Event, EventFirer, EventWithStats, ExecStats, ProgressReporter},
    inputs::Input,
    monitors::stats::{AggregatorOps, UserStats, UserStatsValue},
    stages::StagesTuple,
    state::{HasCorpus, HasExecutions, HasLastReportTime},
    HasMetadata,
    Evaluator, Fuzzer, HasFeedback,
};
use libafl_bolts::{current_time, rands::Rand, Error};
use signal_hook::consts::TERM_SIGNALS;

use crate::{
    executor::ResettableForkserver,
    state::{LibAFLStarState, MultipleStates, TargetStateIdx},
    state_scheduler::StateScheduler,
};

/// Runs the fuzzing loop until a terminating signal is received.
///
/// `loops`:  How many seeds are selected until a new state is selected according to the `state_scheduler`.
///
/// Note: loops does not denote the number of executions, but the number of seeds. Depending on the stages used, a chosen seed
/// can result in multiple or many executions.
pub fn fuzz_loop_with_signal_handling<Z, E, EM, ST, SS, I, C, R, SC>(
    fuzzer: &mut Z,
    stages: &mut ST,
    executor: &mut E,
    state: &mut LibAFLStarState<I, C, R, SC>,
    manager: &mut EM,
    state_scheduler: &mut SS,
    loops: usize,
) -> Result<(), Error>
where
    I: Input,
    R: Rand,
    C: Corpus<I> + serde::Serialize + for<'de> serde::Deserialize<'de>,
    R: Rand + serde::Serialize + for<'de> serde::Deserialize<'de>,
    SC: Corpus<I> + serde::Serialize + for<'de> serde::Deserialize<'de>,
    Z: Fuzzer<E, EM, I, LibAFLStarState<I, C, R, SC>, ST>
        + Evaluator<E, EM, I, LibAFLStarState<I, C, R, SC>>
        + HasFeedback,
    E: ResettableForkserver,
    EM: ProgressReporter<LibAFLStarState<I, C, R, SC>> + EventFirer<I, LibAFLStarState<I, C, R, SC>>,
    ST: StagesTuple<E, EM, LibAFLStarState<I, C, R, SC>, Z>,
    SS: StateScheduler,
{
    // best overall coverage
    let mut best_edge_coverage: usize = 0;

    // setup signal handling:
    let quitting = Arc::new(AtomicBool::new(false));
    for sig in TERM_SIGNALS {
        signal_hook::flag::register(*sig, Arc::clone(&quitting))?;
    }

    'outer: loop {
        // 1. choose a target state
        let new_state_idx =
            state_scheduler.choose_next_state(fuzzer, stages, executor, state, manager)?;
        match change_target_state(fuzzer, executor, state, manager, new_state_idx) {
            // Can be thrown if a blocking (system) call is interrupted by a signal.
            Err(Error::Unknown(error, _)) if &error == "Unix error: EINTR" => {
                log::warn!("Received EINTR error when handling a signal. We will quit.");
                break;
            }
            // These are socket failures. This is not a fatal error, simply continue, i.e., restart the outer loop
            // and choose a new target state, thereby killing the target.
            Err(Error::OsError(error, _, _))
                if [
                    ErrorKind::ConnectionRefused,
                    ErrorKind::ConnectionAborted,
                    ErrorKind::ConnectionReset,
                    ErrorKind::BrokenPipe,
                    ErrorKind::NotConnected,
                ]
                .iter()
                .any(|i| *i == error.kind()) =>
            {
                log::warn!("Recoverable connection error when changing state.");
                continue;
            }
            Err(Error::OsError(error, _, _)) if error.kind() == ErrorKind::TimedOut => {
                // the forkserver is misbehaving
                return Err(Error::shutting_down());
            }
            Err(e) => {
                log::warn!("error when changing state");
                return Err(e);
            }
            // all good!
            Ok(_) => {
                log::debug!("Changed target state to {:?}", new_state_idx);
            }
        };

        // 2. The target is now in the correct state! Fuzz the state for a while
        for _ in 0..loops {
            log::debug!("Before fuzz_one");
            match fuzzer.fuzz_one(stages, executor, state, manager) {
                // Can be thrown if a blocking (system) call is interrupted by a signal.
                Err(Error::Unknown(error, _)) if &error == "Unix error: EINTR" => {
                    log::debug!("Received EINTR error when handling a signal. We will quit.");
                    break;
                }
                // These are socket failures. This is not a fatal error, simply break out of the for-loop.
                // and choose a new target state, thereby killing the target.
                Err(Error::OsError(error, _, _))
                    if [
                        ErrorKind::ConnectionRefused,
                        ErrorKind::ConnectionAborted,
                        ErrorKind::ConnectionReset,
                        ErrorKind::BrokenPipe,
                        ErrorKind::NotConnected,
                    ]
                    .iter()
                    .any(|i| *i == error.kind()) =>
                {
                    log::debug!("Recoverable connection error during fuzzing loop, stopping fuzzing this state early.");
                    break;
                }
                Err(Error::OsError(error, _, _)) if error.kind() == ErrorKind::TimedOut => {
                    // the forkserver is misbehaving
                    log::debug!("Forkserver timed out, stopping fuzzing this state early.");
                    return Err(Error::shutting_down());
                }
                Err(e) => {
                    log::warn!("error when sending test cases");
                    return Err(e);
                }
                Ok(_) => {
                    log::debug!("Fuzzed target state {:?}", new_state_idx);
                }
            }

            if quitting.load(Ordering::Relaxed) {
                log::debug!("Received quitting signal, stopping fuzzing.");
                break 'outer;
            }

            manager.maybe_report_progress(state, Duration::from_secs(15))?;

            if executor.state_reset_occurred() {
                match send_prefix(fuzzer, executor, state, manager) {
                    Err(Error::Unknown(error, _)) if &error == "Unix error: EINTR" => {
                        log::warn!("Received EINTR error when handling a signal. We will quit.");
                        break;
                    }
                    Err(Error::OsError(error, _, _))
                        if [
                            ErrorKind::ConnectionRefused,
                            ErrorKind::ConnectionAborted,
                            ErrorKind::ConnectionReset,
                            ErrorKind::BrokenPipe,
                            ErrorKind::NotConnected,
                        ]
                        .iter()
                        .any(|i| *i == error.kind()) =>
                    {
                        log::debug!("Recoverable connection error during fuzzing loop, stopping fuzzing this state early.");
                        break;
                    }
                    Err(Error::OsError(error, _, _)) if error.kind() == ErrorKind::TimedOut => {
                        // the forkserver is misbehaving
                        log::debug!("Forkserver timed out, stopping fuzzing this state early.");
                        return Err(Error::shutting_down());
                    }
                    Err(e) => {
                        log::warn!("error when sending prefix");
                        return Err(e);
                    }
                    Ok(_) => {
                        log::debug!("Reset state occurred, sent prefix to target state {:?}", new_state_idx);
                    }
                };
            }
        }

        *state.fuzz_cycles() += 1;

        // update the exec stats before moving to a different state
        manager.fire(
            state,
            EventWithStats::new(
                Event::Heartbeat,
                ExecStats::new(current_time(), *state.executions()),
            ),
        )?;

        // report the overall edge coverage
        let (covered_edges, total_edges) = state.calculate_total_coverage()?;
        if covered_edges > best_edge_coverage {
            best_edge_coverage = covered_edges;
            manager.fire(
                state,
                EventWithStats::new(
                    Event::UpdateUserStats {
                        name: Cow::Borrowed("overall_cov"),
                        value: UserStats::new(
                            UserStatsValue::Ratio(covered_edges as u64, total_edges as u64),
                            AggregatorOps::Max,
                        ),
                        phantom: PhantomData,
                    },
                    ExecStats::new(current_time(), *state.executions()),
                ),
            )?;
        }

        // have we received a terminating signal?
        if quitting.load(Ordering::Relaxed) {
            break 'outer;
        }
    }

    // update executions stats for all states to _ensure_ they're accurate at the end
    state.for_each(|state| {
        manager.fire(
            state,
            EventWithStats::new(
                Event::Heartbeat,
                ExecStats::new(current_time(), *state.executions()),
            ),
        )
    })?;
    Ok(())
}

/// Change the state we are fuzzing.
///
/// This is the core mechanic of the libaflstar fuzzer
/// where we choose which _state_ of our target we want to
/// focus on fuzzing next.
pub fn change_target_state<Z, E, EM, I, S>(
    fuzzer: &mut Z,
    executor: &mut E,
    state: &mut S,
    manager: &mut EM,
    new_state_id: TargetStateIdx,
) -> Result<(), Error>
where
    I: Input,
    S: MultipleStates<I> + HasMetadata + HasExecutions + HasLastReportTime + HasCorpus<I>,
    Z: Evaluator<E, EM, I, S>,
    E: ResettableForkserver,
    EM: ProgressReporter<S>,
{
    state.switch_state(new_state_id)?;
    executor.reset_target_state()?;
    send_prefix(fuzzer, executor, state, manager)?;
    Ok(())
}

/// Executes the testcases of the prefix of the currently selected target state.
fn send_prefix<Z, E, EM, I, S>(
    fuzzer: &mut Z,
    executor: &mut E,
    state: &mut S,
    manager: &mut EM,
) -> Result<(), Error>
where
    I: Input,
    S: MultipleStates<I> + HasMetadata + HasExecutions + HasLastReportTime,
    Z: Evaluator<E, EM, I, S>,
    EM: ProgressReporter<S>,
{
    for i in 0..state.prefix().prefix.len() {
        let input = state.prefix().prefix[i]
            .input()
            .clone()
            .expect("Prefix testcases should always have input");
        fuzzer.evaluate_input(state, executor, manager, &input)?;
    }
    Ok(())
}
