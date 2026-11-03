//! Screen output plus the `stats.json` lines the case-study scripts read.

use std::path::PathBuf;

use libafl::{
    monitors::{
        Monitor, MultiMonitor, OnDiskJsonMonitor,
        stats::ClientStatsManager,
    },
};
use libafl_bolts::{ClientId, Error};

fn always_log(_stats: &mut ClientStatsManager) -> bool {
    true
}

/// Prints the fuzzer status and appends the same stats to a JSON-lines file.
pub struct StatsMonitor<F>
where
    F: FnMut(&str),
{
    json: OnDiskJsonMonitor<fn(&mut ClientStatsManager) -> bool>,
    print: MultiMonitor<F>,
}

impl<F> StatsMonitor<F>
where
    F: FnMut(&str),
{
    /// `path` is the JSON-lines file. `print` receives each status line.
    pub fn new(path: impl Into<PathBuf>, print: F) -> Self {
        Self {
            json: OnDiskJsonMonitor::new(path, always_log),
            print: MultiMonitor::new(print),
        }
    }
}

impl<F> Monitor for StatsMonitor<F>
where
    F: FnMut(&str),
{
    fn display(
        &mut self,
        client_stats_manager: &mut ClientStatsManager,
        event_msg: &str,
        sender_id: ClientId,
    ) -> Result<(), Error> {
        self.print
            .display(client_stats_manager, event_msg, sender_id)?;
        self.json
            .display(client_stats_manager, event_msg, sender_id)?;
        Ok(())
    }
}
