//! One case: restore the work directory, run the backend, and drive Python.
//!
//! Execution count increases once here. LibAFL calls `pre_exec` before this
//! method and `post_exec` after it. `HitcountsMapObserver` classifies the raw
//! PCGUARD counters once, in `post_exec`. This method only clears the map
//! before the child starts.

use std::{
    fs,
    os::fd::{AsRawFd, OwnedFd},
    path::{Path, PathBuf},
    process::Command,
    sync::Arc,
    time::{Duration, Instant},
};

use libafl::{
    executors::{Executor, ExitKind, HasObservers},
    feedbacks::MaxMapFeedback,
    observers::{
        AsanBacktraceObserver, CanTrack, ExplicitTracking, HitcountsMapObserver, StdMapObserver,
    },
    state::HasExecutions,
    Error, HasMetadata,
};
use libafl_bolts::tuples::{tuple_list, RefIndexable};

#[cfg(feature = "stats")]
use crate::coverage::capture::PhaseSnapshot;
use crate::{
    app::config::Campaign,
    coverage::capture::{CaptureEngine, PcapObserver},
    engine::input::GeneratorInput,
    engine::stagnation::BitmapProgress,
    runtime::backend::{ForkserverBackend, TargetBackend},
    runtime::generator::{GeneratorClient, GeneratorUpdate},
    runtime::lifecycle::{
        assess_pid, classify_startup, classify_sut, generator_outcome, FailureKind,
        GeneratorOutcome, PidCheck, PidObservation, ProcessReason, StartupEnd, SutKillReason,
        TimeoutStreaks,
    },
    runtime::report::{append_jsonl, CaseReport, TargetStop},
};

/// Hitcount observer over the shared PCGUARD map, with novelty indexes.
pub type CoverageObserver =
    ExplicitTracking<HitcountsMapObserver<StdMapObserver<'static, u8, false>>, false, true>;

/// Coverage feedback. The ratio it reports is shared-map occupancy, not source coverage.
pub type CoverageFeedback =
    MaxMapFeedback<CoverageObserver, HitcountsMapObserver<StdMapObserver<'static, u8, false>>>;

/// Forkserver child checked after it is created.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TraceIdentity {
    /// Forked target pid.
    pub child_pid: i32,
    /// Parent pid read from `/proc`.
    pub parent_pid: i32,
    /// Long-lived forkserver pid.
    pub forkserver_pid: i32,
    /// This fuzzer process.
    pub fuzzer_pid: i32,
    /// `/proc/pid/cmdline` contains the target binary path.
    pub cmdline_matches_target: bool,
}

/// Coordinates one forkserver backend and the Python generator.
pub struct CaseExecutor {
    observers: (CoverageObserver, (PcapObserver, (AsanBacktraceObserver, ()))),
    backend: ForkserverBackend,
    capture: Arc<CaptureEngine>,

    generator: GeneratorClient,
    host: String,
    port: u16,
    connect_timeout: Duration,
    io_timeout: Duration,
    case_timeout: Duration,
    startup_timeout: Duration,
    exit_grace: Duration,
    result_timeout: Duration,
    sut_grace_kill_is_timeout: bool,
    streaks: TimeoutStreaks,
    cleanup_script: Option<PathBuf>,
    workdir: PathBuf,
    binary: PathBuf,
    report_path: PathBuf,
    asan_log_dir: PathBuf,
    last_report: Option<CaseReport>,
    last_identity: Option<TraceIdentity>,
}

impl CaseExecutor {
    /// Spawn the forkserver and the Python server.
    pub fn launch(campaign: &Campaign) -> Result<(Self, CoverageFeedback), Error> {
        let capture = Arc::new(CaptureEngine::new(campaign.capture.clone()));
        capture.set_worker(crate::runtime::worker_id()?);
        let port = pick_port(campaign.target.port)?;
        let base = &campaign.base;
        run_cleanup_script(
            campaign.target.cleanup_script.as_deref(),
            &campaign.target.workdir,
            &campaign.target.host,
            port,
        )?;
        let mut backend = ForkserverBackend::spawn(campaign, port)?;
        let map_len = backend.map_len();
        let map_ptr = backend.map_ptr();
        let observer = unsafe {
            HitcountsMapObserver::new(StdMapObserver::from_mut_ptr(
                "coverage_map_occupancy",
                map_ptr,
                map_len,
            ))
            .track_novelties()
        };
        let feedback = MaxMapFeedback::new(&observer);
        let pcap = PcapObserver::new(Arc::clone(&capture));
        let log_root = campaign.output_root.as_deref().unwrap_or(base);
        let asan_log_dir = crate::runtime::backend::asan_log_dir(campaign);
        fs::create_dir_all(&asan_log_dir).map_err(|error| {
            Error::unknown(format!(
                "creating ASAN log dir {}: {error}",
                asan_log_dir.display()
            ))
        })?;
        let asan = AsanBacktraceObserver::new("asan");
        let log_path = log_root.join("generator.log");
        let generator = GeneratorClient::spawn(
            &campaign.campaign.python,
            &campaign.campaign.pythonpath,
            &log_path,
        )?;
        if campaign.capture.enabled {
            capture.preflight()?;
        }
        let policy = crate::runtime::lifecycle::job_policy();
        let case_timeout = policy.effective_case_timeout(campaign.timeouts.case_ms)?;
        println!("lifecycle: {}", policy.describe(case_timeout));
        let executor = Self {
            observers: tuple_list!(observer, pcap, asan),
            backend,
            capture,
            generator,
            host: campaign.target.host.clone(),
            port,
            connect_timeout: Duration::from_millis(campaign.timeouts.connect_ms),
            io_timeout: Duration::from_millis(campaign.timeouts.io_ms),
            case_timeout,
            startup_timeout: policy.startup_timeout,
            exit_grace: policy.peer_grace,
            result_timeout: policy.result_timeout,
            sut_grace_kill_is_timeout: policy.sut_grace_kill_is_timeout,
            streaks: TimeoutStreaks::new(policy.failure_limit),
            cleanup_script: campaign.target.cleanup_script.clone(),
            workdir: campaign.target.workdir.clone(),
            binary: campaign.target.binary.clone(),
            report_path: log_root.join("cases.jsonl"),
            asan_log_dir,
            last_report: None,
            last_identity: None,
        };
        Ok((executor, feedback))
    }

    /// Borrow the observer so `MaxMapFeedback::new` can take its name and handle.
    pub fn observer(&self) -> &CoverageObserver {
        &self.observers.0
    }

    /// ASAN stack observer. Parsed logs update the objective hash.
    pub fn asan_observer(&self) -> &AsanBacktraceObserver {
        &self.observers.1.1.0
    }

    /// Shared capture engine used by the feedbacks.
    pub fn capture_engine(&self) -> Arc<CaptureEngine> {
        Arc::clone(&self.capture)
    }

    /// Phase timings for the latest case, including a sidecar write that already finished.
    #[cfg(feature = "stats")]
    pub fn phase_snapshot(&self) -> PhaseSnapshot {
        self.capture.phase_snapshot()
    }

    /// Startup preflight cost. Zero when capture is off.
    #[cfg(feature = "stats")]
    pub fn preflight_us(&self) -> u128 {
        self.capture.preflight_us()
    }

    /// Last case report, if `run_target` finished one.
    pub fn last_report(&self) -> Option<&CaseReport> {
        self.last_report.as_ref()
    }

    /// Child recorded for the latest case.
    pub fn last_identity(&self) -> Option<&TraceIdentity> {
        self.last_identity.as_ref()
    }

    /// Handshake length of the shared map.
    pub fn map_len(&self) -> usize {
        self.backend.map_len()
    }

    /// Non-zero entries in the shared PCGUARD map.
    pub fn edge_indexes(&self) -> Vec<usize> {
        self.map_slice()
            .iter()
            .enumerate()
            .filter(|(_, byte)| **byte != 0)
            .map(|(index, _)| index)
            .collect()
    }

    /// Control port chosen for this campaign.
    pub fn port(&self) -> u16 {
        self.port
    }

    /// Forkserver process, not the per-case child.
    pub fn forkserver_pid(&self) -> i32 {
        self.backend.forkserver_pid()
    }

    /// Resident Python server. Coverage cleanup must not signal it.
    pub fn generator_server_pid(&self) -> i32 {
        self.generator.server_pid() as i32
    }

    /// Prefix of the PCGUARD map. Coverage replay must leave it unchanged.
    pub fn map_prefix(&self) -> Vec<u8> {
        let map = self.backend.map();
        map[..map.len().min(64)].to_vec()
    }

    /// Path of the JSONL case log.
    pub fn report_path(&self) -> &Path {
        &self.report_path
    }

    /// Replace the whole-case and exit-grace budgets. Used by the timeout check.
    pub fn set_case_budget(&mut self, case_ms: u64, exit_grace_ms: u64) {
        self.case_timeout = Duration::from_millis(case_ms);
        self.exit_grace = Duration::from_millis(exit_grace_ms);
    }

    /// Current whole-case budget in milliseconds.
    pub fn case_budget_ms(&self) -> u64 {
        self.case_timeout.as_millis() as u64
    }

    /// Current exit grace in milliseconds.
    pub fn exit_grace_ms(&self) -> u64 {
        self.exit_grace.as_millis() as u64
    }

    fn map_slice(&self) -> &[u8] {
        self.backend.map()
    }

    fn run_cleanup(&self) -> Result<(), Error> {
        run_cleanup_script(
            self.cleanup_script.as_deref(),
            &self.workdir,
            &self.host,
            self.port,
        )
    }

    fn record(
        &mut self,
        kind: ExitKind,
        stop: &TargetStop,
        outcome: &GeneratorOutcome,
        generator_error: Option<String>,
        cleanup_signal: Option<i32>,
        started: Instant,
        note: String,
        valid: bool,
    ) -> Result<(), Error> {
        let edges = if valid {
            self.map_slice().iter().filter(|byte| **byte != 0).count()
        } else {
            0
        };
        let report = CaseReport::new(
            kind,
            stop,
            &outcome.logical,
            outcome.reason.label(),
            outcome.code,
            generator_error,
            cleanup_signal,
            edges,
            started.elapsed().as_millis(),
            note,
            valid,
            self.streaks.startup,
            self.streaks.result,
        );
        append_jsonl(&self.report_path, &report)?;
        self.last_report = Some(report);
        Ok(())
    }

    /// Reap adopted orphans and end capture. Does not restart a server or forkserver.
    fn release_case(&mut self, sut_live: bool) -> Result<(), Error> {
        if sut_live {
            let _ = self.backend.kill_child(false);
        }
        if std::process::id() == 1 {
            let mut residents = std::collections::HashSet::new();
            residents.insert(std::process::id() as i32);
            let forkserver = self.backend.forkserver_pid();
            if forkserver > 0 {
                residents.insert(forkserver);
            }
            let server = self.generator.server_pid() as i32;
            if server > 0 {
                residents.insert(server);
            }
            crate::runtime::cleanup_namespace(&residents)?;
        }
        self.capture.end_case()?;
        Ok(())
    }

    fn fail_open_case(&mut self, sut_live: bool, error: Error) -> Error {
        match self.release_case(sut_live) {
            Ok(()) => error,
            Err(cleanup) => cleanup,
        }
    }

    fn invalidate_map(&mut self) {
        let _ = self.backend.prepare_case();
    }
}

impl CaseExecutor {
    /// Feed any ASan log for the just-finished child into the observer.
    ///
    /// ASAN writes `<prefix>.<pid>` after the child exits. Parsing only when the
    /// file exists keeps ordinary non-ASAN cases from producing a spurious zero
    /// stack hash.
    fn observe_asan_log(&mut self) {
        let Some(pid) = self.last_identity.as_ref().map(|identity| identity.child_pid) else {
            return;
        };
        let path = self.asan_log_dir.join(format!("asanlog.{pid}"));
        let Ok(text) = fs::read_to_string(&path) else {
            return;
        };
        if !text.contains("AddressSanitizer") {
            return;
        }
        self.observers.1.1.0.parse_asan_output(&text);
    }
}

impl<EM, S, Z> Executor<EM, GeneratorInput, S, Z> for CaseExecutor
where
    S: HasExecutions + HasMetadata,
{
    fn run_target(
        &mut self,
        _fuzzer: &mut Z,
        state: &mut S,
        _mgr: &mut EM,
        input: &GeneratorInput,
    ) -> Result<ExitKind, Error> {
        let kind = self.drive_case(state, input)?;
        self.observe_asan_log();
        let valid = self.last_report.as_ref().is_some_and(|report| report.valid);
        state
            .metadata_or_insert_with(BitmapProgress::default)
            .observe(self.map_slice(), valid);
        Ok(kind)
    }
}

impl CaseExecutor {
    fn drive_case<S>(&mut self, state: &mut S, input: &GeneratorInput) -> Result<ExitKind, Error>
    where
        S: HasExecutions,
    {
        if let Some(fatal) = self.capture.fatal() {
            return Err(Error::unknown(format!(
                "capture is fatal before the case: {fatal}"
            )));
        }
        if crate::runtime::shutdown_requested() {
            return Err(Error::unknown(
                "PID 1 received a termination signal before the case".to_string(),
            ));
        }
        if self.backend.status_owed() && !self.backend.discard_status(self.result_timeout)? {
            self.backend.retire();
            return Err(Error::unknown(
                "forkserver status was not drained; the forkserver was not restarted".to_string(),
            ));
        }
        self.generator.drain_tombstones()?;
        *state.executions_mut() += 1;
        let started = Instant::now();
        self.last_report = None;
        #[cfg(feature = "stats")]
        let setup_at = Instant::now();
        self.run_cleanup()?;
        self.backend.prepare_case()?;
        #[cfg(feature = "stats")]
        self.capture
            .set_target_setup(setup_at.elapsed().as_micros());
        #[cfg(feature = "stats")]
        let ready_at = Instant::now();
        let case_id = self.capture.allocate_case();
        self.capture.begin_case(case_id)?;
        if let Some(fatal) = self.capture.fatal() {
            return Err(self.fail_open_case(false, Error::unknown(fatal)));
        }
        #[cfg(feature = "stats")]
        self.capture.set_capture_ready(if self.capture.enabled() {
            ready_at.elapsed().as_micros()
        } else {
            0
        });
        let mut handoff = self.capture.handoff();
        let worker_id = crate::runtime::worker_id()?.nonce;
        if let Err(error) = self.backend.begin_start() {
            handoff.disarm();
            return Err(self.fail_open_case(false, error));
        }
        if let Err(error) = self.generator.submit(
            input,
            &self.host,
            self.port,
            self.io_timeout,
            self.connect_timeout,
            worker_id,
            case_id,
        ) {
            self.backend.retire();
            handoff.disarm();
            return Err(self.fail_open_case(false, error));
        }

        let startup_deadline = Instant::now() + self.startup_timeout;
        let mut grace_deadline = None;
        let mut sut: Option<Watched> = None;
        let mut generator: Option<Watched> = None;
        let mut early_code = None;
        let mut early_signal = None;
        let mut startup_warned = false;
        loop {
            if sut.is_none() {
                match self.backend.poll_start(Duration::from_millis(1)) {
                    Ok(Some(pid)) => {
                        sut = Some(watch_pid(pid, self.backend.forkserver_pid())?);
                        self.last_identity = Some(read_identity(
                            pid,
                            self.backend.forkserver_pid(),
                            &self.binary,
                        ));
                    }
                    Ok(None) => {}
                    Err(error) => {
                        self.generator.retire();
                        handoff.disarm();
                        return Err(self.fail_open_case(false, error));
                    }
                }
            }
            match self.generator.poll_update() {
                Ok(Some(GeneratorUpdate::Started { pid })) => {
                    if generator.is_some() {
                        self.generator.retire();
                        self.backend.retire();
                        handoff.disarm();
                        return Err(self.fail_open_case(
                            sut.as_ref().is_some_and(|item| !item.exited),
                            Error::unknown(
                                "duplicate generator Started; servers were not restarted"
                                    .to_string(),
                            ),
                        ));
                    }
                    generator = Some(watch_pid(pid, self.generator.server_pid() as i32)?);
                }
                Ok(Some(GeneratorUpdate::Result { code, signal })) => {
                    early_code = code;
                    early_signal = signal;
                    if let Some(watched) = generator.as_mut() {
                        watched.exited = true;
                    }
                }
                Ok(None) => {}
                Err(error) => {
                    self.backend.retire();
                    handoff.disarm();
                    return Err(
                        self.fail_open_case(sut.as_ref().is_some_and(|item| !item.exited), error)
                    );
                }
            }
            if let Some(watched) = sut.as_mut() {
                refresh_exit(watched);
            }
            if let Some(watched) = generator.as_mut() {
                refresh_exit(watched);
            }
            let now = Instant::now();
            let both = sut.is_some() && generator.is_some();
            if both && now <= startup_deadline && grace_deadline.is_none() {
                break;
            }
            if now >= startup_deadline && grace_deadline.is_none() {
                if !startup_warned {
                    log::warn!(
                        "startup timeout after {} ms; waiting {} ms of peer grace",
                        self.startup_timeout.as_millis(),
                        self.exit_grace.as_millis()
                    );
                    startup_warned = true;
                }
                grace_deadline = Some(now + self.exit_grace);
            }
            if grace_deadline.is_some_and(|end| both || now >= end)
                || crate::runtime::shutdown_requested()
            {
                break;
            }
        }

        let deadline_passed = Instant::now() > startup_deadline;
        let resolution = classify_startup(deadline_passed, sut.is_some(), generator.is_some());
        if !matches!(resolution, StartupEnd::OnTime) {
            signal_pair(sut.as_mut(), generator.as_mut())?;
            let _ = self.backend.kill_child(false);
            self.collect_pair_status(&mut sut, &mut generator, &mut early_code, &mut early_signal)?;
        }
        match resolution {
            StartupEnd::OnTime => self.streaks.succeed(FailureKind::Startup),
            StartupEnd::LateButKnown => {
                let stop_job = self.streaks.fail(FailureKind::Startup);
                self.invalidate_map();
                let outcome = generator_outcome(early_code, early_signal, true);
                self.finish_recorded(
                    &mut handoff,
                    ExitKind::Timeout,
                    &TargetStop::Running,
                    &outcome,
                    Some("startup timeout".to_string()),
                    self.backend.cleanup_signal(),
                    started,
                    "startup_timeout".to_string(),
                    false,
                )?;
                if stop_job {
                    return Err(Error::unknown(format!(
                        "startup timeout streak {} exceeded limit {}; the job stops without restarting servers",
                        self.streaks.startup, self.streaks.limit
                    )));
                }
                return Ok(ExitKind::Timeout);
            }
            StartupEnd::MissingPid {
                sut: missing_sut,
                generator: missing_generator,
            } => {
                let _ = self.streaks.fail(FailureKind::Startup);
                if missing_sut {
                    self.backend.retire();
                }
                if missing_generator {
                    self.generator.retire();
                }
                self.invalidate_map();
                handoff.disarm();
                let error = Error::unknown(format!(
                    "startup pid missing after grace (sut {missing_sut}, generator {missing_generator}); servers were not restarted"
                ));
                return Err(self.fail_open_case(false, error));
            }
        }

        #[cfg(feature = "stats")]
        let interaction_at = Instant::now();
        let case_deadline = Instant::now() + self.case_timeout;
        let mut peer_deadline = None;
        let mut case_timed_out = false;
        let mut sut_kill = SutKillReason::None;
        let mut generator_killed = early_code.is_some() || early_signal.is_some();
        let mut sut_stop = None;
        let mut gen_code = early_code;
        let mut gen_signal = early_signal;
        loop {
            if let Some(watched) = sut.as_mut() {
                refresh_exit(watched);
            }
            if let Some(watched) = generator.as_mut() {
                refresh_exit(watched);
            }
            if sut_stop.is_none() {
                if let Some(status) = self.backend.poll_target(Duration::from_millis(1))? {
                    sut_stop = Some(status);
                    if let Some(watched) = sut.as_mut() {
                        watched.exited = true;
                    }
                }
            }
            if gen_code.is_none() && gen_signal.is_none() {
                match self.generator.poll_update()? {
                    Some(GeneratorUpdate::Result { code, signal }) => {
                        gen_code = code;
                        gen_signal = signal;
                        if let Some(watched) = generator.as_mut() {
                            watched.exited = true;
                        }
                    }
                    Some(GeneratorUpdate::Started { .. }) => {
                        self.generator.retire();
                        self.backend.retire();
                        handoff.disarm();
                        return Err(self.fail_open_case(
                            false,
                            Error::unknown(
                                "duplicate generator Started during the case; servers were not restarted"
                                    .to_string(),
                            ),
                        ));
                    }
                    None => {}
                }
            }
            let sut_dead = sut.as_ref().is_some_and(|item| item.exited) || sut_stop.is_some();
            let gen_dead = generator.as_ref().is_some_and(|item| item.exited)
                || gen_code.is_some()
                || gen_signal.is_some();
            if sut_dead && gen_dead {
                break;
            }
            if (sut_dead ^ gen_dead) && peer_deadline.is_none() && !case_timed_out {
                peer_deadline = Some(Instant::now() + self.exit_grace);
            }
            if peer_deadline.is_some_and(|end| Instant::now() >= end) {
                if !sut_dead {
                    signal_one(sut.as_mut())?;
                    let _ = self.backend.kill_child(false);
                    sut_kill = SutKillReason::PeerGrace;
                }
                if !gen_dead {
                    signal_one(generator.as_mut())?;
                    generator_killed = true;
                }
                peer_deadline = None;
            }
            if Instant::now() >= case_deadline && !case_timed_out {
                case_timed_out = true;
                if !sut_dead {
                    signal_one(sut.as_mut())?;
                    let _ = self.backend.kill_child(true);
                    sut_kill = SutKillReason::CaseDeadline;
                }
                if !gen_dead {
                    signal_one(generator.as_mut())?;
                    generator_killed = true;
                }
            }
            if crate::runtime::shutdown_requested() {
                signal_pair(sut.as_mut(), generator.as_mut())?;
                let _ = self.backend.kill_child(true);
                break;
            }
            if case_timed_out
                && sut.as_ref().is_some_and(|item| item.exited)
                && generator.as_ref().is_some_and(|item| item.exited)
            {
                break;
            }
        }

        let result_deadline = Instant::now() + self.result_timeout;
        while Instant::now() < result_deadline
            && (sut_stop.is_none() || (gen_code.is_none() && gen_signal.is_none()))
        {
            if sut_stop.is_none() {
                if let Some(status) = self.backend.poll_target(Duration::from_millis(5))? {
                    sut_stop = Some(status);
                }
            }
            if gen_code.is_none() && gen_signal.is_none() {
                if let Some(GeneratorUpdate::Result { code, signal }) =
                    self.generator.poll_update()?
                {
                    gen_code = code;
                    gen_signal = signal;
                }
            }
        }
        let mut result_late = false;
        if sut_stop.is_none() {
            log::warn!(
                "forkserver status missed the {} ms result deadline",
                self.result_timeout.as_millis()
            );
            self.backend.mark_status_owed();
            result_late = true;
        }
        if gen_code.is_none() && gen_signal.is_none() {
            log::warn!(
                "generator Result missed the {} ms result deadline",
                self.result_timeout.as_millis()
            );
            self.generator.tombstone_inflight();
            result_late = true;
        }
        if result_late {
            let stop_job = self.streaks.fail(FailureKind::Result);
            let stop = sut_stop.clone().unwrap_or(TargetStop::Running);
            let outcome = generator_outcome(gen_code, gen_signal, generator_killed);
            self.invalidate_map();
            self.finish_recorded(
                &mut handoff,
                ExitKind::Timeout,
                &stop,
                &outcome,
                Some("result timeout".to_string()),
                self.backend.cleanup_signal(),
                started,
                "result_timeout".to_string(),
                false,
            )?;
            #[cfg(feature = "stats")]
            self.capture
                .set_interaction(interaction_at.elapsed().as_micros());
            if stop_job {
                return Err(Error::unknown(format!(
                    "result timeout streak {} exceeded limit {}; the job stops without restarting servers",
                    self.streaks.result, self.streaks.limit
                )));
            }
            return Ok(ExitKind::Timeout);
        }
        self.streaks.succeed(FailureKind::Result);
        let stop = sut_stop.unwrap_or(TargetStop::Running);
        let mut outcome = generator_outcome(gen_code, gen_signal, generator_killed);
        let mut generator_error = None;
        let mut note = String::new();
        if case_timed_out {
            sut_kill = SutKillReason::CaseDeadline;
            outcome = generator_outcome(gen_code.or(Some(libc::SIGKILL)), gen_signal, true);
            generator_error = Some("case deadline".to_string());
            note = "case deadline".to_string();
        } else if matches!(sut_kill, SutKillReason::PeerGrace) {
            note = "peer grace".to_string();
        } else if generator_killed {
            generator_error = Some("peer grace".to_string());
            note = "peer grace".to_string();
        } else if outcome.reason == ProcessReason::Exited && outcome.logical == "timeout" {
            generator_error = Some("io timeout".to_string());
        }
        let kind = classify_sut(&stop, sut_kill, self.sut_grace_kill_is_timeout);
        if let Err(error) = self.backend.finish_case() {
            if self.backend.child_pid().is_some() {
                handoff.disarm();
                return Err(self.fail_open_case(true, error));
            }
        }
        self.finish_recorded(
            &mut handoff,
            kind,
            &stop,
            &outcome,
            generator_error,
            self.backend.cleanup_signal(),
            started,
            note,
            true,
        )?;
        #[cfg(feature = "stats")]
        self.capture
            .set_interaction(interaction_at.elapsed().as_micros());
        Ok(kind)
    }

    fn finish_recorded(
        &mut self,
        handoff: &mut crate::coverage::capture::PostExecHandoff,
        kind: ExitKind,
        stop: &TargetStop,
        outcome: &GeneratorOutcome,
        generator_error: Option<String>,
        cleanup_signal: Option<i32>,
        started: Instant,
        note: String,
        valid: bool,
    ) -> Result<(), Error> {
        if !valid {
            self.invalidate_map();
        }
        if let Err(error) = self.release_case(false) {
            handoff.disarm();
            return Err(error);
        }
        if let Err(error) = self.record(
            kind,
            stop,
            outcome,
            generator_error,
            cleanup_signal,
            started,
            note,
            valid,
        ) {
            handoff.disarm();
            return Err(error);
        }
        handoff.disarm();
        Ok(())
    }

    fn collect_pair_status(
        &mut self,
        sut: &mut Option<Watched>,
        generator: &mut Option<Watched>,
        code: &mut Option<i32>,
        signal: &mut Option<i32>,
    ) -> Result<(), Error> {
        let deadline = Instant::now() + self.result_timeout;
        while Instant::now() < deadline {
            if let Some(watched) = sut.as_mut() {
                refresh_exit(watched);
            }
            if let Some(watched) = generator.as_mut() {
                refresh_exit(watched);
            }
            let _ = self.backend.poll_target(Duration::from_millis(5))?;
            if code.is_none() && signal.is_none() {
                if let Some(GeneratorUpdate::Result {
                    code: next_code,
                    signal: next_signal,
                }) = self.generator.poll_update()?
                {
                    *code = next_code;
                    *signal = next_signal;
                }
            }
            if self.backend.child_pid().is_none() && (code.is_some() || signal.is_some()) {
                break;
            }
        }
        if self.backend.child_pid().is_some() {
            self.backend.mark_status_owed();
        }
        if code.is_none() && signal.is_none() {
            self.generator.tombstone_inflight();
        }
        Ok(())
    }
}

struct Watched {
    pid: i32,
    pidfd: Option<OwnedFd>,
    starttime: u64,
    exited: bool,
}

fn watch_pid(pid: i32, expected_ppid: i32) -> Result<Watched, Error> {
    let found = crate::runtime::process_birth(pid)
        .map(|(ppid, starttime)| PidObservation { ppid, starttime });
    match assess_pid(found, expected_ppid) {
        PidCheck::Rejected => Err(Error::unknown(format!(
            "pid {pid} failed the parent check and was not signalled"
        ))),
        PidCheck::AlreadyExited => Ok(Watched {
            pid,
            pidfd: None,
            starttime: 0,
            exited: true,
        }),
        PidCheck::Usable(observed) => match crate::runtime::open_pidfd(pid) {
            Ok(pidfd) => {
                let exited = crate::runtime::pidfd_has_exited(pidfd.as_raw_fd()).unwrap_or(false);
                Ok(Watched {
                    pid,
                    pidfd: Some(pidfd),
                    starttime: observed.starttime,
                    exited,
                })
            }
            Err(_) if crate::runtime::process_birth(pid).is_none() => Ok(Watched {
                pid,
                pidfd: None,
                starttime: observed.starttime,
                exited: true,
            }),
            Err(error) => Err(error),
        },
    }
}

fn refresh_exit(watched: &mut Watched) {
    if watched.exited {
        return;
    }
    let Some(pidfd) = watched.pidfd.as_ref() else {
        watched.exited = true;
        return;
    };
    if crate::runtime::pidfd_has_exited(pidfd.as_raw_fd()).unwrap_or(false) {
        watched.exited = true;
    }
}

fn signal_one(watched: Option<&mut Watched>) -> Result<(), Error> {
    let Some(watched) = watched else {
        return Ok(());
    };
    if watched.exited || watched.pidfd.is_none() {
        watched.exited = true;
        return Ok(());
    }
    if let Some((_, starttime)) = crate::runtime::process_birth(watched.pid) {
        if starttime != watched.starttime {
            return Err(Error::unknown(format!(
                "pid {} was reused and was not signalled",
                watched.pid
            )));
        }
    }
    let fd = watched.pidfd.as_ref().expect("pidfd").as_raw_fd();
    match crate::runtime::signal_pidfd(fd, libc::SIGKILL)? {
        crate::runtime::SignalDelivery::Delivered => Ok(()),
        crate::runtime::SignalDelivery::AlreadyGone => {
            watched.exited = true;
            Ok(())
        }
    }
}

fn signal_pair(sut: Option<&mut Watched>, generator: Option<&mut Watched>) -> Result<(), Error> {
    signal_one(sut)?;
    signal_one(generator)
}

impl HasObservers for CaseExecutor {
    type Observers = (CoverageObserver, (PcapObserver, (AsanBacktraceObserver, ())));

    fn observers(&self) -> RefIndexable<&Self::Observers, Self::Observers> {
        RefIndexable::from(&self.observers)
    }

    fn observers_mut(&mut self) -> RefIndexable<&mut Self::Observers, Self::Observers> {
        RefIndexable::from(&mut self.observers)
    }
}

fn read_identity(child: i32, forkserver: i32, binary: &Path) -> TraceIdentity {
    let parent_pid = proc_ppid(child).unwrap_or(0);
    let cmdline = fs::read(format!("/proc/{child}/cmdline")).unwrap_or_default();
    let text = String::from_utf8_lossy(&cmdline);
    let needle = binary.display().to_string();
    TraceIdentity {
        child_pid: child,
        parent_pid,
        forkserver_pid: forkserver,
        fuzzer_pid: std::process::id() as i32,
        cmdline_matches_target: text.contains(&needle),
    }
}

fn proc_ppid(pid: i32) -> Option<i32> {
    let status = fs::read_to_string(format!("/proc/{pid}/status")).ok()?;
    status.lines().find_map(|line| {
        let (key, value) = line.split_once(':')?;
        if key == "PPid" {
            value.trim().parse().ok()
        } else {
            None
        }
    })
}

pub(crate) fn pick_port(requested: u16) -> Result<u16, Error> {
    if requested != 0 {
        return Ok(requested);
    }
    let listener = std::net::TcpListener::bind("127.0.0.1:0")
        .map_err(|error| Error::unknown(format!("picking a free port: {error}")))?;
    let port = listener
        .local_addr()
        .map_err(|error| Error::unknown(format!("reading the chosen port: {error}")))?
        .port();
    Ok(port)
}

pub(crate) fn run_cleanup_script(
    script: Option<&Path>,
    workdir: &Path,
    host: &str,
    port: u16,
) -> Result<(), Error> {
    fs::create_dir_all(workdir)?;
    let Some(script) = script else {
        return Ok(());
    };
    if !script.is_file() {
        return Err(Error::illegal_argument(format!(
            "cleanup script {} is missing",
            script.display()
        )));
    }
    let status = Command::new("/bin/sh")
        .arg(script)
        .current_dir(workdir)
        .env("MCN_FUZZ_WORKDIR", workdir)
        .env("MCN_FUZZ_HOST", host)
        .env("MCN_FUZZ_PORT", port.to_string())
        .status()
        .map_err(|error| {
            Error::unknown(format!(
                "starting cleanup script {}: {error}",
                script.display()
            ))
        })?;
    if !status.success() {
        return Err(Error::unknown(format!(
            "cleanup script {} exited with {status}",
            script.display()
        )));
    }
    Ok(())
}
