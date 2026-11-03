//! An `Executor` wrapping our modified [`crate::executor::forkserver::ForkserverExecutor`] that
//! can be used for stateful persistent mode fuzzing.
//! It enables resetting the target. This is only useful if the target is ran in AFL persistent mode.

use core::fmt::Debug;
use std::borrow::Cow;
use std::marker::PhantomData;

use libafl::events::{Event, EventFirer, EventWithStats, ExecStats};
use libafl::executors::{Executor, ExitKind, HasObservers};
use libafl::inputs::HasTargetBytes;
use libafl::monitors::stats::{AggregatorOps, UserStats, UserStatsValue};
use libafl::observers::ObserversTuple;
use libafl::state::{HasExecutions, State};
use libafl::HasMetadata;
use libafl::Error;
use libafl_bolts::impl_serdeany;
use libafl_bolts::shmem::ShMemProvider;
use nix::sys::signal::{kill, Signal};
use nix::unistd::Pid;
use serde::{Deserialize, Serialize};

use super::forkserver::ForkserverExecutor;
use crate::input::DeliverInput;

#[derive(Debug)]
pub struct StatefulPersistentExecutor<I, OT, S, SP>
where
    SP: ShMemProvider,
{
    executor: ForkserverExecutor<I, OT, S, SP>,
    /// If the state was reset (i.e., there was a timeout)
    state_reset_occurred: bool,
    /// If the child was reset since the last execution
    child_was_reset: bool,
}

pub trait ResettableForkserver {
    /// Resets the state of the target
    fn reset_target_state(&mut self) -> Result<(), Error>;

    /// Signifies if a reset occurred that was not caused by calling
    /// [`ResettableForkserver::reset_target_state`].
    ///
    /// This is important because we want to control the state of the target,
    /// which is reset after the it's killed.
    ///
    /// Whenever this is called, the `flag` is reset, meaning
    /// that it will only return true once whenever a state reset occurs.
    /// The `flag` is also reset when [`ResettableForkserver::reset_target_state`] is called.
    fn state_reset_occurred(&mut self) -> bool;
}

impl<I, OT, S, SP> StatefulPersistentExecutor<I, OT, S, SP>
where
    OT: ObserversTuple<I, S>,
    SP: ShMemProvider,
{
    /// Create a new [`StatefulPersistentExecutor`]
    pub fn new(executor: ForkserverExecutor<I, OT, S, SP>) -> Self {
        Self {
            executor,
            state_reset_occurred: false,
            child_was_reset: false,
        }
    }

    pub fn into_inner(self) -> ForkserverExecutor<I, OT, S, SP> {
        self.executor
    }
}
impl<I, OT, S, SP> ResettableForkserver for StatefulPersistentExecutor<I, OT, S, SP>
where
    OT: ObserversTuple<I, S>,
    SP: ShMemProvider,
{
    /// Reset the state of the target by killing it.
    /// The forkserver will fork a new process.
    fn reset_target_state(&mut self) -> Result<(), Error> {
        let timed_out = self.executor.forkserver().last_run_timed_out();
        match self.executor.forkserver().child_pid() {
            Some(child_pid) if timed_out => {
                return Err(Error::illegal_state(format!(
                    "Last execution timed out, but there is still a child_pid set (pid={}).",
                    child_pid
                )));
            }
            Some(child_pid) if !timed_out => {
                // usual path, kill the child
                if child_pid.as_raw() > 0 {
                    let process_group = child_pid.as_raw();
                    let result = kill(Pid::from_raw(process_group), Signal::SIGKILL);
                    if let Err(e) = result {
                        log::info!("Error killing child: {e}");
                    }
                    self.executor.forkserver_mut().set_last_run_timed_out(true);
                    self.executor.forkserver_mut().reset_child_pid();
                }
            }
            None => {
                // reset not needed, because child is already killed in case of timeout
                // or, there is no child (such as when calling this before any testcase
                // has been sent)
            }
            _ => unreachable!("All arms are covered."),
        };
        self.child_was_reset = true;
        self.state_reset_occurred = false;
        self.executor.reset_generator()?;
        Ok(())
    }

    fn state_reset_occurred(&mut self) -> bool {
        let result = self.state_reset_occurred;
        self.state_reset_occurred = false;
        result
    }
}

#[derive(Debug, Serialize, Deserialize)]
struct StatefulPersistentExecutorMeta {
    timeouts: u64,
}

impl StatefulPersistentExecutorMeta {
    fn increment_timeouts(&mut self) {
        self.timeouts += 1;
    }

    fn timeouts(&self) -> u64 {
        self.timeouts
    }
}

impl_serdeany!(StatefulPersistentExecutorMeta);

impl<EM, I, Z, OT, S, SP> Executor<EM, I, S, Z> for StatefulPersistentExecutor<I, OT, S, SP>
where
    EM: EventFirer<I, S>,
    OT: ObserversTuple<I, S>,
    S: State + HasExecutions + HasMetadata,
    I: HasTargetBytes + DeliverInput,
    SP: ShMemProvider,
{
    #[inline]
    fn run_target(
        &mut self,
        fuzzer: &mut Z,
        state: &mut S,
        mgr: &mut EM,
        input: &I,
    ) -> Result<ExitKind, Error> {
        let result = self.executor.run_target(fuzzer, state, mgr, input);

        if self.child_was_reset {
            // we communicated to the forkserver that the child was killed via
            // forkserver.last_run_timed_out,
            // but we need to reset it to false for the next run, otherwise the
            // forkserver will wait forever for a stopped child
            self.child_was_reset = false;
        }

        if let Ok(ExitKind::Timeout) = result {
            // the prefix needs to be sent again
            log::debug!("Timeout occurred, resetting state");
            self.state_reset_occurred = true;

            // keep track of timeouts
            if !state.has_metadata::<StatefulPersistentExecutorMeta>() {
                state.add_metadata(StatefulPersistentExecutorMeta { timeouts: 0 })
            }
            let meta = state.metadata_mut::<StatefulPersistentExecutorMeta>()?;

            meta.increment_timeouts();
            let timeouts = meta.timeouts();

            // send timeouts events, but not too often
            if timeouts < 20 || timeouts % 20 == 0 {
                mgr.fire(
                    state,
                    EventWithStats::new(
                        Event::UpdateUserStats {
                            name: Cow::Borrowed("timeouts"),
                            value: UserStats::new(UserStatsValue::Number(timeouts), AggregatorOps::Max),
                            phantom: PhantomData,
                        },
                        ExecStats::new(libafl_bolts::current_time(), *state.executions()),
                    ),
                )?;
            }
        }
        result
    }
}

impl<I, OT, S, SP> HasObservers for StatefulPersistentExecutor<I, OT, S, SP>
where
    OT: ObserversTuple<I, S>,
    SP: ShMemProvider,
{
    type Observers = OT;

    #[inline]
    fn observers(&self) -> libafl_bolts::tuples::RefIndexable<&Self::Observers, Self::Observers> {
        self.executor.observers()
    }

    #[inline]
    fn observers_mut(
        &mut self,
    ) -> libafl_bolts::tuples::RefIndexable<&mut Self::Observers, Self::Observers> {
        self.executor.observers_mut()
    }
}
