//! Single-process event manager.
//!
//! The monitor client id is the protocol state index, so each target state
//! shows up as its own row. That is the only difference from LibAFL's
//! `SimpleEventManager`.

use std::borrow::Cow;
use std::fmt::Debug;
use std::marker::PhantomData;

use libafl::{
    events::{
        AwaitRestartSafe, BrokerEventResult, Event, EventFirer, EventManagerId, EventReceiver,
        EventRestarter, EventWithStats, HasEventManagerId, ProgressReporter, SendExiting,
        std_maybe_report_progress, std_on_restart, std_report_progress,
    },
    monitors::{Monitor, stats::ClientStatsManager},
    state::{HasCurrentStageId, HasExecutions, HasLastReportTime, MaybeHasClientPerfMonitor, Stoppable},
    HasMetadata,
};
use libafl_bolts::{ClientId, Error};

use crate::state::MultipleStates;

/// A simple event manager that attributes stats to the current protocol state.
pub struct LibAFLStarManager<I, MT, S> {
    monitor: MT,
    events: Vec<EventWithStats<I>>,
    client_stats_manager: ClientStatsManager,
    phantom: PhantomData<S>,
    /// When every protocol state shares one corpus, report them as one client.
    single_state: bool,
}

impl<I, MT, S> Debug for LibAFLStarManager<I, MT, S>
where
    MT: Debug,
    I: Debug,
{
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.debug_struct("LibAFLStarManager")
            .field("monitor", &self.monitor)
            .field("events", &self.events)
            .finish_non_exhaustive()
    }
}

impl<I, MT, S> LibAFLStarManager<I, MT, S>
where
    I: Debug,
    MT: Monitor,
    S: Stoppable,
{
    /// One monitor row per protocol state.
    pub fn new(monitor: MT) -> Self {
        Self::build(monitor, false)
    }

    /// One monitor row for the whole fuzzer. Used by the single-corpus modes.
    pub fn single_corpus(monitor: MT) -> Self {
        Self::build(monitor, true)
    }

    fn build(monitor: MT, single_state: bool) -> Self {
        let mut manager = Self {
            monitor,
            events: Vec::new(),
            client_stats_manager: ClientStatsManager::default(),
            phantom: PhantomData,
            single_state,
        };
        let _ = manager.client_stats_manager.client_stats_insert(ClientId(0));
        manager
    }

    fn client_id<M: MultipleStates<I>>(&self, state: &M) -> ClientId {
        let state_idx = if self.single_state {
            0
        } else {
            state.current_state_idx().0
        };
        // Client 0 is the broker in multi-process monitors. Start at 1.
        ClientId(u32::try_from(state_idx + 1).unwrap_or(u32::MAX))
    }

    fn handle_in_broker(
        monitor: &mut MT,
        client_stats_manager: &mut ClientStatsManager,
        client_id: ClientId,
        event: &EventWithStats<I>,
        executions: u64,
        time: core::time::Duration,
    ) -> Result<BrokerEventResult, Error> {
        client_stats_manager.client_stats_insert(client_id)?;
        client_stats_manager.update_client_stats_for(client_id, |client_stat| {
            client_stat.update_executions(executions, time);
        })?;
        match event.event() {
            Event::NewTestcase { corpus_size, .. } => {
                client_stats_manager.update_client_stats_for(client_id, |client_stat| {
                    client_stat.update_corpus_size(*corpus_size as u64);
                })?;
                monitor.display(client_stats_manager, event.event().name(), client_id)?;
                Ok(BrokerEventResult::Handled)
            }
            Event::Heartbeat => {
                monitor.display(client_stats_manager, event.event().name(), client_id)?;
                Ok(BrokerEventResult::Handled)
            }
            Event::UpdateUserStats { name, value, .. } => {
                client_stats_manager.update_client_stats_for(client_id, |client_stat| {
                    client_stat.update_user_stats(Cow::Owned(name.to_string()), value.clone());
                })?;
                monitor.display(client_stats_manager, event.event().name(), client_id)?;
                Ok(BrokerEventResult::Handled)
            }
            Event::Objective { objective_size, .. } => {
                client_stats_manager.update_client_stats_for(client_id, |client_stat| {
                    client_stat.update_objective_size(*objective_size as u64);
                })?;
                monitor.display(client_stats_manager, event.event().name(), client_id)?;
                Ok(BrokerEventResult::Handled)
            }
            Event::Log {
                severity_level,
                message,
                ..
            } => {
                log::log!((*severity_level).into(), "{message}");
                Ok(BrokerEventResult::Handled)
            }
            _ => Ok(BrokerEventResult::Forward),
        }
    }
}

impl<I, MT, S> EventFirer<I, S> for LibAFLStarManager<I, MT, S>
where
    I: Debug,
    MT: Monitor,
    S: Stoppable + MultipleStates<I> + HasExecutions,
{
    fn should_send(&self) -> bool {
        true
    }

    fn fire(&mut self, state: &mut S, event: EventWithStats<I>) -> Result<(), Error> {
        let client_id = self.client_id(state);
        let executions = *state.executions();
        let time = libafl_bolts::current_time();
        match Self::handle_in_broker(
            &mut self.monitor,
            &mut self.client_stats_manager,
            client_id,
            &event,
            executions,
            time,
        )? {
            BrokerEventResult::Forward => self.events.push(event),
            BrokerEventResult::Handled => (),
        }
        Ok(())
    }
}

impl<I, MT, S> SendExiting for LibAFLStarManager<I, MT, S> {
    fn send_exiting(&mut self) -> Result<(), Error> {
        Ok(())
    }

    fn on_shutdown(&mut self) -> Result<(), Error> {
        self.send_exiting()
    }
}

impl<I, MT, S> AwaitRestartSafe for LibAFLStarManager<I, MT, S> {
    fn await_restart_safe(&mut self) {}
}

impl<I, MT, S> EventRestarter<S> for LibAFLStarManager<I, MT, S>
where
    S: HasCurrentStageId,
{
    fn on_restart(&mut self, state: &mut S) -> Result<(), Error> {
        std_on_restart(self, state)
    }
}

impl<I, MT, S> EventReceiver<I, S> for LibAFLStarManager<I, MT, S>
where
    I: Debug,
    MT: Monitor,
    S: Stoppable,
{
    fn try_receive(&mut self, state: &mut S) -> Result<Option<(EventWithStats<I>, bool)>, Error> {
        while let Some(event) = self.events.pop() {
            if matches!(event.event(), Event::Stop) {
                state.request_stop();
            } else {
                return Err(Error::unknown(format!(
                    "Received illegal message that should not have arrived: {event:?}."
                )));
            }
        }
        Ok(None)
    }

    fn on_interesting(
        &mut self,
        _state: &mut S,
        _event: EventWithStats<I>,
    ) -> Result<(), Error> {
        Ok(())
    }
}

impl<I, MT, S> ProgressReporter<S> for LibAFLStarManager<I, MT, S>
where
    I: Debug,
    MT: Monitor,
    S: HasMetadata
        + HasExecutions
        + HasLastReportTime
        + Stoppable
        + MaybeHasClientPerfMonitor
        + MultipleStates<I>
        + HasCurrentStageId,
{
    fn maybe_report_progress(
        &mut self,
        state: &mut S,
        monitor_timeout: core::time::Duration,
    ) -> Result<(), Error> {
        std_maybe_report_progress(self, state, monitor_timeout)
    }

    fn report_progress(&mut self, state: &mut S) -> Result<(), Error> {
        std_report_progress(self, state)
    }
}

impl<I, MT, S> HasEventManagerId for LibAFLStarManager<I, MT, S> {
    fn mgr_id(&self) -> EventManagerId {
        EventManagerId(0)
    }
}
