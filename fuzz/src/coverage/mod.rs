//! Optional LLVM source-coverage replay.
//!
//! The stage replays saved generator inputs through a forkserver-only coverage
//! binary. It does not update the PCGUARD bitmap, corpus, objectives, or the
//! main execution counter. Raw profiles stay mapped for the whole batch and
//! are replaced only after that batch's forkserver has exited.

pub mod capture;
pub mod command;
pub mod events;
pub mod mcp;
pub mod mutations;
pub mod proposal;
pub mod schedule;
mod sut_output;

use std::{
    collections::HashSet,
    fs::{self, File},
    io::Write,
    os::fd::{AsRawFd, OwnedFd},
    os::unix::process::ExitStatusExt,
    path::{Path, PathBuf},
    process::{Command, ExitStatus},
    thread::sleep,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use flate2::{write::ZlibEncoder, Compression};
use libafl::{
    corpus::Corpus,
    executors::ExitKind,
    stages::{Restartable, Stage},
    state::{HasCorpus, HasExecutions, HasSolutions},
    Error, HasMetadata,
};
use libafl_bolts::impl_serdeany;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

use sut_output::SutLog;

use crate::{
    app::config::{Campaign, SourceCoverageConfig},
    engine::executor::{self, CaseExecutor},
    engine::input::GeneratorInput,
    engine::stagnation::BitmapProgress,
    runtime::backend::{ForkserverBackend, TargetBackend},
    runtime::generator::{GeneratorClient, GeneratorUpdate},
    runtime::lifecycle::{
        assess_pid, classify_startup, classify_sut, job_policy, FailureKind, PidCheck,
        PidObservation, StartupEnd, SutKillReason, TimeoutStreaks,
    },
    runtime::report::TargetStop,
};

const BUILD_FLAGS: &str = "\
-O2 -g -fno-lto -fprofile-instr-generate -fcoverage-mapping \
-fprofile-update=atomic -mllvm -runtime-counter-relocation AFL_LLVM_ONLY_FSRV=1";

/// Where a saved input lives.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct CaseLocation {
    /// `corpus:<id>` or `solution:<id>`.
    pub id: String,
    /// LibAFL id observed in this process.
    pub corpus_id: usize,
    /// On-disk file name when LibAFL recorded one.
    pub filename: Option<String>,
}

/// Covered counts from one `llvm-cov export` summary.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct CoverageSummary {
    /// Covered functions.
    pub functions: u64,
    /// Covered lines.
    pub lines: u64,
    /// Covered regions.
    pub regions: u64,
    /// Covered branches.
    pub branches: u64,
    /// Covered function instantiations.
    pub instantiations: u64,
    /// Total lines from `llvm-cov`. Missing checkpoint fields stay zero.
    #[serde(default)]
    pub lines_total: u64,
    /// Total branches from `llvm-cov`. Missing checkpoint fields stay zero.
    #[serde(default)]
    pub branches_total: u64,
}

/// One published batch. The stage keeps `progress`; the coverage command also
/// writes a CSV row from the cumulative summary and the failure, if any.
#[derive(Clone, Debug)]
pub struct BatchReport {
    /// Checkpoint after this batch. Unchanged when nothing was published.
    pub progress: SourceCoverageProgress,
    /// Cumulative covered and total counts.
    pub summary: CoverageSummary,
    /// Summary before this batch.
    pub previous: CoverageSummary,
    /// Inputs whose profiles were kept in this batch.
    pub succeeded: u64,
    /// Replay stopped after a published prefix, or after no successful input.
    pub failure: Option<String>,
}

impl CoverageSummary {
    /// True when any covered count grew. Execution count alone is not growth.
    pub fn grew_from(self, previous: Self) -> bool {
        self.functions > previous.functions
            || self.lines > previous.lines
            || self.regions > previous.regions
            || self.branches > previous.branches
            || self.instantiations > previous.instantiations
    }
}

/// What a finished batch is allowed to commit.
#[derive(Clone, Debug)]
pub struct BatchDecision {
    /// Cases replayed in this batch.
    pub succeeded: Vec<String>,
    /// Coverage counts increased.
    pub summary_grew: bool,
    /// Index, export, and snapshot publish all finished.
    pub publish_ok: bool,
}

/// Replacing a raw profile is refused while its forkserver is still alive.
pub fn restore_raw_allowed(forkserver_alive: bool) -> Result<(), Error> {
    if forkserver_alive {
        return Err(Error::illegal_state(
            "refusing to replace a raw profile while the coverage forkserver is alive".to_string(),
        ));
    }
    Ok(())
}

/// Child environment for the coverage forkserver. It does not reuse the main map id.
pub fn coverage_child_env(
    main_shm: &str,
    coverage_shm: &str,
    profile_file: &str,
) -> Result<Vec<(String, String)>, Error> {
    if coverage_shm.is_empty() || coverage_shm == main_shm {
        return Err(Error::illegal_argument(
            "coverage compatibility map must be a distinct __AFL_SHM_ID".to_string(),
        ));
    }
    Ok(vec![
        ("__AFL_SHM_ID".to_string(), coverage_shm.to_string()),
        ("LLVM_PROFILE_FILE".to_string(), profile_file.to_string()),
        ("AFL_LLVM_ONLY_FSRV".to_string(), "1".to_string()),
    ])
}

/// Progress restored from a checkpoint and stored in the LibAFL state.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct SourceCoverageProgress {
    /// Summary of the last published export.
    pub last_summary: CoverageSummary,
    /// Latest snapshot number. Zero means nothing has been published.
    pub snapshot: u64,
    /// Build fingerprint this progress belongs to.
    pub fingerprint: String,
    /// Replay executions. This is not the main fuzz execution counter.
    pub replay_executions: u64,
}

impl SourceCoverageProgress {
    fn new(fingerprint: &str) -> Self {
        Self {
            fingerprint: fingerprint.to_string(),
            ..Self::default()
        }
    }
}

impl_serdeany!(SourceCoverageProgress);

/// Replay stage. Disabled campaigns return immediately.
#[derive(Clone, Debug)]
pub struct LlvmCoverageStage {
    campaign: Campaign,
}

impl LlvmCoverageStage {
    /// Stage bound to one campaign.
    pub fn from_campaign(campaign: &Campaign) -> Self {
        Self {
            campaign: campaign.clone(),
        }
    }

    /// Replay any inputs that are not in the published set.
    pub fn replay_now<S>(&mut self, executor: &mut CaseExecutor, state: &mut S) -> Result<(), Error>
    where
        S: HasCorpus<GeneratorInput> + HasSolutions<GeneratorInput> + HasMetadata + HasExecutions,
    {
        if !self.campaign.coverage.enabled {
            return Ok(());
        }
        let executions = *state.executions();
        let map = executor.map_prefix();
        let result = run_batch(&self.campaign, executor, state);
        if *state.executions() != executions || executor.map_prefix() != map {
            log::error!("source coverage replay changed the main execution counter or PCGUARD map");
        }
        keep_fuzzing(result)
    }
}

impl<S> Restartable<S> for LlvmCoverageStage {
    fn should_restart(&mut self, _state: &mut S) -> Result<bool, Error> {
        Ok(true)
    }

    fn clear_progress(&mut self, _state: &mut S) -> Result<(), Error> {
        Ok(())
    }
}

impl<E, EM, S, Z> Stage<E, EM, S, Z> for LlvmCoverageStage
where
    S: HasCorpus<GeneratorInput> + HasSolutions<GeneratorInput> + HasMetadata + HasExecutions,
    E: CoverageHost,
{
    fn perform(
        &mut self,
        _fuzzer: &mut Z,
        executor: &mut E,
        state: &mut S,
        _manager: &mut EM,
    ) -> Result<(), Error> {
        if !state
            .metadata_or_insert_with(BitmapProgress::default)
            .source_coverage_due()
        {
            return Ok(());
        }
        if !self.campaign.coverage.enabled {
            state
                .metadata_or_insert_with(BitmapProgress::default)
                .finish_source_coverage();
            return Ok(());
        }
        let executions = *state.executions();
        let map = executor.coverage_map_prefix();
        let result = run_batch_hosted(&self.campaign, executor, state);
        if *state.executions() != executions || executor.coverage_map_prefix() != map {
            log::error!("source coverage replay changed the main execution counter or PCGUARD map");
        }
        state
            .metadata_or_insert_with(BitmapProgress::default)
            .finish_source_coverage();
        keep_fuzzing(result)
    }
}

/// A coverage failure is logged and does not stop the main fuzz loop.
fn keep_fuzzing(result: Result<(), Error>) -> Result<(), Error> {
    if let Err(error) = result {
        log::warn!("source coverage replay failed, fuzz continues: {error:?}");
    }
    Ok(())
}

/// What the stage needs from the main executor without running a case on it.
pub trait CoverageHost {
    /// Main forkserver pid.
    fn main_forkserver_pid(&self) -> i32;
    /// Main Python server pid.
    fn main_generator_pid(&self) -> i32;
    /// Bytes that must stay unchanged.
    fn coverage_map_prefix(&self) -> Vec<u8>;
}

impl CoverageHost for CaseExecutor {
    fn main_forkserver_pid(&self) -> i32 {
        self.forkserver_pid()
    }

    fn main_generator_pid(&self) -> i32 {
        self.generator_server_pid()
    }

    fn coverage_map_prefix(&self) -> Vec<u8> {
        self.map_prefix()
    }
}

fn run_batch_hosted<E, S>(campaign: &Campaign, executor: &mut E, state: &mut S) -> Result<(), Error>
where
    E: CoverageHost,
    S: HasCorpus<GeneratorInput> + HasSolutions<GeneratorInput> + HasMetadata,
{
    run_batch_with(
        campaign,
        executor.main_forkserver_pid(),
        executor.main_generator_pid(),
        state,
    )
}

fn run_batch<S>(
    campaign: &Campaign,
    executor: &mut CaseExecutor,
    state: &mut S,
) -> Result<(), Error>
where
    S: HasCorpus<GeneratorInput> + HasSolutions<GeneratorInput> + HasMetadata,
{
    run_batch_with(
        campaign,
        executor.forkserver_pid(),
        executor.generator_server_pid(),
        state,
    )
}

fn run_batch_with<S>(
    campaign: &Campaign,
    main_forkserver: i32,
    main_generator: i32,
    state: &mut S,
) -> Result<(), Error>
where
    S: HasCorpus<GeneratorInput> + HasSolutions<GeneratorInput> + HasMetadata,
{
    let config = &campaign.coverage;
    let fingerprint = build_fingerprint(&config.binary, &config.llvm_profdata, &config.llvm_cov)?;
    let root = config.output.clone();
    let _lock = OutputLock::acquire(&config.output)?;
    match resume_or_refuse(&root)? {
        Resume::Refuse { reason } => {
            log::warn!("{reason}");
            return Ok(());
        }
        Resume::Ready => {}
    }
    let mut progress = state
        .metadata_map()
        .get::<SourceCoverageProgress>()
        .cloned()
        .unwrap_or_else(|| SourceCoverageProgress::new(&fingerprint));
    if progress.fingerprint.is_empty() {
        progress.fingerprint = fingerprint.clone();
    }
    if !progress.fingerprint.is_empty() && progress.fingerprint != fingerprint {
        log::warn!(
            "source coverage fingerprint changed from {} to {fingerprint}; resetting coverage progress",
            progress.fingerprint
        );
        progress = SourceCoverageProgress::new(&fingerprint);
    }
    if let Some(saved) = load_checkpoint(&root) {
        if saved.fingerprint == fingerprint && saved.snapshot >= progress.snapshot {
            progress = saved;
        }
    }
    let pending = pending_cases(state)?;
    if pending.is_empty() {
        return Ok(());
    }
    let timings = BatchTimings::default();
    let result = replay_pending(
        campaign,
        config,
        &root,
        &fingerprint,
        Some(main_forkserver),
        Some(main_generator),
        &pending,
        &progress,
        timings,
    );
    match result {
        Ok(report) => {
            state.add_metadata(report.progress);
            Ok(())
        }
        Err(error) => Err(error),
    }
}

#[derive(Default)]
struct BatchTimings {
    replay: Duration,
    index: Duration,
    export: Duration,
    compress: Duration,
    persist: Duration,
}

struct PendingCase {
    id: String,
    location: CaseLocation,
    input: GeneratorInput,
}

fn pending_cases<S>(state: &S) -> Result<Vec<PendingCase>, Error>
where
    S: HasCorpus<GeneratorInput> + HasSolutions<GeneratorInput>,
{
    let mut cases = Vec::new();
    collect_corpus(state.corpus(), "corpus", &mut cases)?;
    collect_corpus(state.solutions(), "solution", &mut cases)?;
    Ok(cases)
}

fn collect_corpus<C>(corpus: &C, kind: &str, out: &mut Vec<PendingCase>) -> Result<(), Error>
where
    C: Corpus<GeneratorInput>,
{
    for nth in 0..corpus.count_all() {
        let id = corpus.nth_from_all(nth);
        let mut borrowed = corpus.get_from_all(id)?.borrow_mut();
        let input = borrowed.load_input(corpus)?.clone();
        let filename = borrowed.filename().clone();
        drop(borrowed);
        let case_id = format!("{kind}:{}", id.0);
        out.push(PendingCase {
            location: CaseLocation {
                id: case_id.clone(),
                corpus_id: id.0,
                filename,
            },
            id: case_id,
            input,
        });
    }
    Ok(())
}

fn replay_pending(
    campaign: &Campaign,
    config: &SourceCoverageConfig,
    root: &Path,
    fingerprint: &str,
    main_forkserver: Option<i32>,
    main_generator: Option<i32>,
    pending: &[PendingCase],
    progress: &SourceCoverageProgress,
    mut timings: BatchTimings,
) -> Result<BatchReport, Error> {
    let main_forkserver = main_forkserver.unwrap_or(0);
    let main_generator = main_generator.unwrap_or(0);
    let work_raw = root.join("work-raw");
    let rollback_raw = root.join("rollback-raw");
    prepare_work_raw(root, &work_raw)?;
    let profile = work_raw.join("%m%c.profraw");
    let started = Instant::now();
    let batch = format!(
        "{}-{}-{}",
        fingerprint,
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|value| value.as_nanos())
            .unwrap_or(0)
    );
    let log = SutLog::open(root, batch)?;
    let mut session = CoverageSession::start(campaign, config, &profile, log)?;
    let mut succeeded = Vec::new();
    let mut failures = Vec::new();
    let mut failed = false;
    for case in pending {
        if let Err(error) = copy_dir(&work_raw, &rollback_raw) {
            session.close(main_forkserver, main_generator);
            let _ = session.log.batch_end(&format!("failed: {error:?}"));
            return Err(error);
        }
        match session.replay(case, main_forkserver, main_generator) {
            Ok(outcome) if profile_usable(&work_raw) => {
                succeeded.push(RecordedCase {
                    id: case.id.clone(),
                    location: case.location.clone(),
                    exit_kind: outcome.exit_kind,
                });
                if outcome.stop_batch {
                    failures.push(format!(
                        "{} ended the coverage batch on a timeout streak; servers were not restarted",
                        &case.id
                    ));
                    break;
                }
            }
            Ok(outcome) => {
                failures.push(format!(
                    "{} profile missing after {}",
                    &case.id, outcome.exit_kind
                ));
                failed = true;
                break;
            }
            Err(error) => {
                failures.push(format!("{}: {error:?}", &case.id));
                failed = true;
                break;
            }
        }
    }
    timings.replay = started.elapsed();
    session.close(main_forkserver, main_generator);
    let reaped = session.forkserver_reaped;
    let batch_outcome = if !reaped {
        if failed {
            format!("cleanup_incomplete; failures: {}", failures.join("; "))
        } else {
            "cleanup_incomplete".to_string()
        }
    } else if failed {
        format!("failed: {}", failures.join("; "))
    } else {
        "replayed".to_string()
    };
    session.log.batch_end(&batch_outcome)?;
    if !reaped {
        return Err(Error::illegal_state(
            "coverage forkserver was still alive after the batch; the raw profile was not replaced or indexed"
                .to_string(),
        ));
    }
    wait_profile_quiescent(&work_raw);
    if failed {
        restore_raw_allowed(false)?;
        copy_dir(&rollback_raw, &work_raw)?;
    }
    if succeeded.is_empty() {
        if failed {
            return Err(Error::unknown(failures.join("; ")));
        }
        return Ok(unchanged_report(progress));
    }
    let indexed = root.join("coverage.profdata.tmp");
    let index_at = Instant::now();
    merge_raw(&config.llvm_profdata, &work_raw, &indexed).map_err(|error| {
        Error::unknown(format!(
            "{error}; snapshot was not published after {} ms",
            index_at.elapsed().as_millis()
        ))
    })?;
    timings.index = index_at.elapsed();
    let export_at = Instant::now();
    // Analysis consumes detailed counters in-process from the frozen snapshot.
    // Export once here while replay owns the executor, never in an async worker.
    let detailed = config.export_json || config.analysis_enabled;
    let summary_output = export_cov(&config.llvm_cov, &config.binary, &indexed, !detailed)
        .map_err(|error| {
            Error::unknown(format!(
                "{error}; snapshot was not published after {} ms",
                export_at.elapsed().as_millis()
            ))
        })?;
    timings.export = export_at.elapsed();
    let summary = parse_summary(&summary_output)?;
    let snapshot_json = detailed.then_some(summary_output.as_str());
    let grew = summary.grew_from(progress.last_summary);
    let decision = BatchDecision {
        succeeded: succeeded.iter().map(|case| case.id.clone()).collect(),
        summary_grew: grew,
        publish_ok: false,
    };
    let snapshot = progress.snapshot.saturating_add(1);
    let replay_executions = progress
        .replay_executions
        .saturating_add(succeeded.len() as u64);
    let published = publish_snapshot(
        root,
        fingerprint,
        snapshot,
        &indexed,
        snapshot_json,
        &summary,
        &progress.last_summary,
        &decision,
        &succeeded,
        &failures,
        &work_raw,
        &config.binary,
        &config.llvm_profdata,
        &config.llvm_cov,
        replay_executions,
        &mut timings,
        &session.log,
    );
    let publish_ok = match published {
        Ok(()) => true,
        Err(error) => {
            log::warn!("source coverage snapshot was not published: {error:?}");
            false
        }
    };
    log::info!(
        "source coverage batch replay {} ms, index {} ms, export {} ms, compress {} ms, persist {} ms",
        timings.replay.as_millis(),
        timings.index.as_millis(),
        timings.export.as_millis(),
        timings.compress.as_millis(),
        timings.persist.as_millis()
    );
    if !publish_ok {
        return Err(Error::unknown(
            "source coverage snapshot was not published".to_string(),
        ));
    }
    let mut updated = progress.clone();
    updated.last_summary = summary;
    updated.snapshot = snapshot;
    updated.replay_executions = replay_executions;
    updated.fingerprint = fingerprint.to_string();
    let failure = if failed {
        let message = failures.join("; ");
        log::warn!(
            "source coverage batch ended early after publishing the successful prefix: {message}"
        );
        Some(message)
    } else {
        None
    };
    Ok(BatchReport {
        progress: updated,
        summary,
        previous: progress.last_summary,
        succeeded: succeeded.len() as u64,
        failure,
    })
}

fn unchanged_report(progress: &SourceCoverageProgress) -> BatchReport {
    BatchReport {
        progress: progress.clone(),
        summary: progress.last_summary,
        previous: progress.last_summary,
        succeeded: 0,
        failure: None,
    }
}

struct RecordedCase {
    id: String,
    location: CaseLocation,
    exit_kind: String,
}

struct ReplayOutcome {
    exit_kind: String,
    stop_batch: bool,
}

struct CoverageSession {
    backend: ForkserverBackend,
    generator: GeneratorClient,
    campaign: Campaign,
    port: u16,
    closed: bool,
    /// True only after the coverage forkserver pid has left `/proc`.
    forkserver_reaped: bool,
    streaks: TimeoutStreaks,
    next_case: u64,
    grace_is_timeout: bool,
    log: SutLog,
}

impl CoverageSession {
    fn start(
        campaign: &Campaign,
        config: &SourceCoverageConfig,
        profile: &Path,
        mut log: SutLog,
    ) -> Result<Self, Error> {
        log.batch_start()?;
        let parts = (|| -> Result<(ForkserverBackend, GeneratorClient, Campaign, u16), Error> {
            let mut owned = campaign.clone();
            owned.target.binary = config.binary.clone();
            owned.capture.enabled = false;
            let port = executor::pick_port(0)?;
            let work = std::env::temp_dir().join(format!(
                "mcn-cov-{}-{}",
                std::process::id(),
                profile
                    .parent()
                    .and_then(|path| path.file_name())
                    .map(|name| name.to_string_lossy().into_owned())
                    .unwrap_or_else(|| "raw".to_string())
            ));
            let _ = fs::remove_dir_all(&work);
            owned.target.workdir = work;
            executor::run_cleanup_script(
                owned.target.cleanup_script.as_deref(),
                &owned.target.workdir,
                &owned.target.host,
                port,
            )?;
            let (stdout, stderr) = log.observer_files()?;
            let backend = ForkserverBackend::spawn_coverage_with_output(
                &owned,
                port,
                &profile.display().to_string(),
                Some(stdout),
                Some(stderr),
            )?;
            let log_path = campaign
                .output_root
                .as_deref()
                .unwrap_or(&campaign.base)
                .join("coverage-generator.log");
            let generator = GeneratorClient::spawn_tagged(
                &campaign.campaign.python,
                &campaign.campaign.pythonpath,
                &log_path,
                "-cov",
            )?;
            Ok((backend, generator, owned, port))
        })();
        let (backend, generator, owned, port) = match parts {
            Ok(parts) => parts,
            Err(error) => {
                let _ = log.batch_end(&format!("start_failed: {error:?}"));
                return Err(error);
            }
        };
        let policy = job_policy();
        Ok(Self {
            backend,
            generator,
            campaign: owned,
            port,
            closed: false,
            forkserver_reaped: false,
            streaks: TimeoutStreaks::new(policy.failure_limit),
            next_case: 1,
            grace_is_timeout: policy.sut_grace_kill_is_timeout,
            log,
        })
    }

    /// One coverage case with SUT output markers around the existing replay.
    fn replay(
        &mut self,
        case: &PendingCase,
        main_forkserver: i32,
        main_generator: i32,
    ) -> Result<ReplayOutcome, Error> {
        let case_no = self
            .log
            .case_start(&&case.id, case.location.filename.as_deref())?;
        let result = self.replay_inner(&case.input, main_forkserver, main_generator);
        let mut outcome = match &result {
            Ok(outcome) => outcome.exit_kind.clone(),
            Err(error) => format!("error: {error:?}"),
        };
        if result.is_err() {
            match self.reap_case(main_forkserver, main_generator) {
                Ok(()) => {}
                Err(error) => outcome.push_str(&format!("; cleanup failed: {error:?}")),
            }
        }
        if let Err(error) = self.log.case_end(case_no, &&case.id, &outcome) {
            return Err(Error::unknown(format!(
                "SUT log case_end marker failed after {outcome}: {error:?}"
            )));
        }
        result
    }

    /// Existing replay body; callers wrap it with SUT output markers.
    fn replay_inner(
        &mut self,
        input: &GeneratorInput,
        main_forkserver: i32,
        main_generator: i32,
    ) -> Result<ReplayOutcome, Error> {
        let policy = job_policy();
        if self.backend.status_owed() && !self.backend.discard_status(policy.result_timeout)? {
            self.backend.retire();
            return Err(Error::unknown(
                "coverage forkserver status was not drained; it was not restarted".to_string(),
            ));
        }
        self.generator.drain_tombstones()?;
        executor::run_cleanup_script(
            self.campaign.target.cleanup_script.as_deref(),
            &self.campaign.target.workdir,
            &self.campaign.target.host,
            self.port,
        )?;
        if let Err(error) = self.backend.begin_start() {
            return Err(error);
        }
        let worker = crate::runtime::worker_id()?.nonce;
        let case_id = self.next_case;
        self.next_case = self.next_case.saturating_add(1);
        if let Err(error) = self.generator.submit(
            input,
            &self.campaign.target.host,
            self.port,
            Duration::from_millis(self.campaign.timeouts.io_ms),
            Duration::from_millis(self.campaign.timeouts.connect_ms),
            worker,
            case_id,
        ) {
            self.backend.retire();
            return Err(error);
        }

        let startup_deadline = Instant::now() + policy.startup_timeout;
        let mut grace_deadline = None;
        let mut sut: Option<Watched> = None;
        let mut generator: Option<Watched> = None;
        let mut early_code = None;
        let mut early_signal = None;
        loop {
            if sut.is_none() {
                match self.backend.poll_start(Duration::from_millis(1))? {
                    Some(pid) => {
                        sut = Some(watch_cov_pid(pid, self.backend.forkserver_pid())?);
                    }
                    None => {}
                }
            }
            match self.generator.poll_update()? {
                Some(GeneratorUpdate::Started { pid }) => {
                    if generator.is_some() {
                        self.generator.retire();
                        self.backend.retire();
                        return Err(Error::unknown(
                            "duplicate coverage generator Started; servers were not restarted"
                                .to_string(),
                        ));
                    }
                    generator = Some(watch_cov_pid(pid, self.generator.server_pid() as i32)?);
                }
                Some(GeneratorUpdate::Result { code, signal }) => {
                    early_code = code;
                    early_signal = signal;
                    if let Some(watched) = generator.as_mut() {
                        watched.exited = true;
                    }
                }
                None => {}
            }
            if let Some(watched) = sut.as_mut() {
                refresh_cov(watched);
            }
            if let Some(watched) = generator.as_mut() {
                refresh_cov(watched);
            }
            let now = Instant::now();
            let both = sut.is_some() && generator.is_some();
            if both && now <= startup_deadline && grace_deadline.is_none() {
                break;
            }
            if now >= startup_deadline && grace_deadline.is_none() {
                grace_deadline = Some(now + policy.peer_grace);
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
            signal_cov(sut.as_mut())?;
            signal_cov(generator.as_mut())?;
            let _ = self.backend.kill_child(false);
        }
        match resolution {
            StartupEnd::OnTime => self.streaks.succeed(FailureKind::Startup),
            StartupEnd::LateButKnown => {
                let stop_batch = self.streaks.fail(FailureKind::Startup);
                let stop = self.collect_pair(
                    &mut sut,
                    &mut generator,
                    &mut early_code,
                    &mut early_signal,
                    policy.result_timeout,
                )?;
                self.reap_case(main_forkserver, main_generator)?;
                let observed = stop.is_some() || sut.as_ref().is_some_and(|item| item.exited);
                if !observed {
                    return Err(Error::unknown(
                        "coverage startup timed out before the child exit was observed".to_string(),
                    ));
                }
                return Ok(ReplayOutcome {
                    exit_kind: exit_label(
                        &stop.unwrap_or(TargetStop::Running),
                        SutKillReason::None,
                        self.grace_is_timeout,
                        "startup_timeout",
                    ),
                    stop_batch,
                });
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
                self.reap_case(main_forkserver, main_generator)?;
                return Err(Error::unknown(format!(
                    "coverage startup pid missing after grace (sut {missing_sut}, generator {missing_generator}); servers were not restarted"
                )));
            }
        }

        let case_timeout = policy.effective_case_timeout(self.campaign.timeouts.case_ms)?;
        let case_deadline = Instant::now() + case_timeout;
        let mut peer_deadline = None;
        let mut case_timed_out = false;
        let mut sut_kill = SutKillReason::None;
        let mut sut_stop = None;
        let mut gen_code = early_code;
        let mut gen_signal = early_signal;
        loop {
            if let Some(watched) = sut.as_mut() {
                refresh_cov(watched);
            }
            if let Some(watched) = generator.as_mut() {
                refresh_cov(watched);
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
                        return Err(Error::unknown(
                            "duplicate coverage generator Started during the case; servers were not restarted"
                                .to_string(),
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
                peer_deadline = Some(Instant::now() + policy.peer_grace);
            }
            if peer_deadline.is_some_and(|end| Instant::now() >= end) {
                if !sut_dead {
                    signal_cov(sut.as_mut())?;
                    let _ = self.backend.kill_child(false);
                    sut_kill = SutKillReason::PeerGrace;
                }
                if !gen_dead {
                    signal_cov(generator.as_mut())?;
                }
                peer_deadline = None;
            }
            if Instant::now() >= case_deadline && !case_timed_out {
                case_timed_out = true;
                if !sut_dead {
                    signal_cov(sut.as_mut())?;
                    let _ = self.backend.kill_child(true);
                    sut_kill = SutKillReason::CaseDeadline;
                }
                if !gen_dead {
                    signal_cov(generator.as_mut())?;
                }
            }
            if crate::runtime::shutdown_requested() {
                signal_cov(sut.as_mut())?;
                signal_cov(generator.as_mut())?;
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

        let result_deadline = Instant::now() + policy.result_timeout;
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
        let mut stop_batch = false;
        if sut_stop.is_none() {
            self.backend.mark_status_owed();
            stop_batch = self.streaks.fail(FailureKind::Result);
        } else {
            self.streaks.succeed(FailureKind::Result);
        }
        if gen_code.is_none() && gen_signal.is_none() {
            self.generator.tombstone_inflight();
            if sut_stop.is_some() {
                stop_batch = self.streaks.fail(FailureKind::Result) || stop_batch;
            }
        }
        let observed = sut_stop.is_some() || sut.as_ref().is_some_and(|item| item.exited);
        self.reap_case(main_forkserver, main_generator)?;
        if !observed {
            return Err(Error::unknown(
                "coverage case ended before the child exit was observed".to_string(),
            ));
        }
        let note = if case_timed_out {
            "case_timeout"
        } else if matches!(sut_kill, SutKillReason::PeerGrace) {
            "peer_grace"
        } else if stop_batch {
            "result_timeout"
        } else {
            "exited"
        };
        if case_timed_out {
            sut_kill = SutKillReason::CaseDeadline;
        }
        Ok(ReplayOutcome {
            exit_kind: exit_label(
                &sut_stop.unwrap_or(TargetStop::Running),
                sut_kill,
                self.grace_is_timeout,
                note,
            ),
            stop_batch,
        })
    }

    fn collect_pair(
        &mut self,
        sut: &mut Option<Watched>,
        generator: &mut Option<Watched>,
        code: &mut Option<i32>,
        signal: &mut Option<i32>,
        timeout: Duration,
    ) -> Result<Option<TargetStop>, Error> {
        let deadline = Instant::now() + timeout;
        let mut stop = None;
        while Instant::now() < deadline {
            if let Some(watched) = sut.as_mut() {
                refresh_cov(watched);
            }
            if let Some(watched) = generator.as_mut() {
                refresh_cov(watched);
            }
            if stop.is_none() {
                stop = self.backend.poll_target(Duration::from_millis(5))?;
            }
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
            if stop.is_some() && (code.is_some() || signal.is_some()) {
                break;
            }
        }
        if stop.is_none() && self.backend.child_pid().is_some() {
            self.backend.mark_status_owed();
        }
        if code.is_none() && signal.is_none() {
            self.generator.tombstone_inflight();
        }
        Ok(stop)
    }

    /// Reap this round's SUT, generator worker, and leftover descendants.
    /// Both main fuzz residents and this batch's servers stay up.
    fn reap_case(&mut self, main_forkserver: i32, main_generator: i32) -> Result<(), Error> {
        if std::process::id() != 1 {
            return Ok(());
        }
        let mut residents = HashSet::new();
        residents.insert(std::process::id() as i32);
        for pid in [
            main_forkserver,
            main_generator,
            self.backend.forkserver_pid(),
            self.generator.server_pid() as i32,
        ] {
            if pid > 0 {
                residents.insert(pid);
            }
        }
        crate::runtime::cleanup_namespace(&residents)
    }

    fn close(&mut self, main_forkserver: i32, main_generator: i32) {
        if self.closed {
            return;
        }
        self.closed = true;
        let forkserver = self.backend.forkserver_pid();
        let child = self.backend.child_pid().unwrap_or(0);
        let python = self.generator.server_pid() as i32;
        trace_residents(
            "before",
            main_forkserver,
            main_generator,
            forkserver,
            python,
        );
        self.generator.retire();
        self.backend.retire();
        let forkserver_gone = wait_dead(forkserver);
        let _ = wait_dead(child);
        let _ = wait_dead(python);
        self.forkserver_reaped = forkserver_gone;
        // Host-namespace tests must not sweep the process table. Cleanup runs
        // only for the fuzzer that is PID 1 in its own PID namespace.
        if std::process::id() == 1 {
            let mut residents = HashSet::new();
            residents.insert(std::process::id() as i32);
            if main_forkserver > 0 {
                residents.insert(main_forkserver);
            }
            if main_generator > 0 {
                residents.insert(main_generator);
            }
            let _ = crate::runtime::cleanup_namespace(&residents);
        }
        trace_residents("after", main_forkserver, main_generator, forkserver, python);
    }
}

/// Test-only snapshot. Set `MCN_COV_PID_TRACE` to a file path. This writes
/// process state and does not signal anyone.
fn trace_residents(
    phase: &str,
    main_forkserver: i32,
    main_python: i32,
    coverage_forkserver: i32,
    coverage_python: i32,
) {
    let Ok(path) = std::env::var("MCN_COV_PID_TRACE") else {
        return;
    };
    if path.is_empty() {
        return;
    }
    let mut line = format!(
        "phase={phase} self={} {}",
        std::process::id(),
        pid_snapshot("self", std::process::id() as i32)
    );
    line.push(' ');
    line.push_str(&pid_snapshot("main_forkserver", main_forkserver));
    line.push(' ');
    line.push_str(&pid_snapshot("main_python", main_python));
    line.push(' ');
    line.push_str(&pid_snapshot("coverage_forkserver", coverage_forkserver));
    line.push(' ');
    line.push_str(&pid_snapshot("coverage_python", coverage_python));
    line.push('\n');
    if let Some(parent) = Path::new(&path).parent() {
        let _ = fs::create_dir_all(parent);
    }
    if let Ok(mut file) = fs::OpenOptions::new().create(true).append(true).open(&path) {
        let _ = file.write_all(line.as_bytes());
    }
}

fn pid_snapshot(label: &str, pid: i32) -> String {
    if pid <= 0 {
        return format!("{label} pid={pid} alive=0 state=absent ppid= nspid=");
    }
    let status = fs::read_to_string(format!("/proc/{pid}/status")).unwrap_or_default();
    if status.is_empty() {
        return format!("{label} pid={pid} alive=0 state=absent ppid= nspid=");
    }
    let state = status_field(&status, "State:");
    let ppid = status_field(&status, "PPid:");
    let nspid = status_field(&status, "NSpid:");
    let alive = usize::from(!state.starts_with('Z'));
    format!("{label} pid={pid} alive={alive} state={state} ppid={ppid} nspid={nspid}")
}

fn status_field(status: &str, prefix: &str) -> String {
    status
        .lines()
        .find_map(|line| line.strip_prefix(prefix))
        .map(|value| value.split_whitespace().collect::<Vec<_>>().join(","))
        .unwrap_or_default()
}

impl Drop for CoverageSession {
    fn drop(&mut self) {
        if !self.closed {
            let forkserver = self.backend.forkserver_pid();
            let child = self.backend.child_pid().unwrap_or(0);
            let python = self.generator.server_pid() as i32;
            self.generator.retire();
            self.backend.retire();
            let _ = wait_dead(forkserver);
            let _ = wait_dead(child);
            let _ = wait_dead(python);
        }
    }
}

struct Watched {
    pid: i32,
    pidfd: Option<OwnedFd>,
    starttime: u64,
    exited: bool,
}

fn watch_cov_pid(pid: i32, expected_ppid: i32) -> Result<Watched, Error> {
    let found = crate::runtime::process_birth(pid)
        .map(|(ppid, starttime)| PidObservation { ppid, starttime });
    match assess_pid(found, expected_ppid) {
        PidCheck::Rejected => Err(Error::unknown(format!(
            "coverage pid {pid} failed the parent check and was not signalled"
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

fn refresh_cov(watched: &mut Watched) {
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

fn signal_cov(watched: Option<&mut Watched>) -> Result<(), Error> {
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
                "coverage pid {} was reused and was not signalled",
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

fn exit_label(
    stop: &TargetStop,
    kill: SutKillReason,
    grace_is_timeout: bool,
    note: &str,
) -> String {
    let kind = match classify_sut(stop, kill, grace_is_timeout) {
        ExitKind::Ok => "ok",
        ExitKind::Crash => "crash",
        ExitKind::Oom => "oom",
        ExitKind::Timeout => "timeout",
        ExitKind::Diff { .. } => "diff",
    };
    format!("{kind}/{} {note}", stop.label())
}

/// SIGKILL was already sent. Reap our zombie so a live `/proc` entry cannot
/// keep the raw profile file looking mapped.
fn wait_dead(pid: i32) -> bool {
    if pid <= 0 {
        return true;
    }
    let deadline = Instant::now() + Duration::from_secs(2);
    while Instant::now() < deadline {
        let rc = unsafe { libc::waitpid(pid, std::ptr::null_mut(), libc::WNOHANG) };
        if rc == pid || !proc_exists(pid) {
            return true;
        }
        if rc < 0 {
            let err = std::io::Error::last_os_error();
            if err.raw_os_error() == Some(libc::ECHILD) && !proc_exists(pid) {
                return true;
            }
        }
        sleep(Duration::from_millis(10));
    }
    let _ = unsafe { libc::waitpid(pid, std::ptr::null_mut(), libc::WNOHANG) };
    !proc_exists(pid)
}

fn proc_exists(pid: i32) -> bool {
    Path::new(&format!("/proc/{pid}")).exists()
}

/// The raw file must stop changing before it is indexed or replaced.
fn wait_profile_quiescent(work_raw: &Path) {
    let mut previous = profile_token(work_raw);
    for _ in 0..10 {
        sleep(Duration::from_millis(20));
        let next = profile_token(work_raw);
        if next == previous {
            return;
        }
        previous = next;
    }
}

fn profile_token(work_raw: &Path) -> Vec<(String, u64, u128)> {
    let mut rows = Vec::new();
    let Ok(entries) = fs::read_dir(work_raw) else {
        return rows;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|ext| ext.to_str()) != Some("profraw") {
            continue;
        }
        let meta = entry.metadata().ok();
        let len = meta.as_ref().map(|item| item.len()).unwrap_or(0);
        let modified = meta
            .and_then(|item| item.modified().ok())
            .and_then(|time| time.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|time| time.as_millis())
            .unwrap_or(0);
        rows.push((path.display().to_string(), len, modified));
    }
    rows.sort();
    rows
}

enum Resume {
    Ready,
    Refuse { reason: String },
}

fn resume_or_refuse(root: &Path) -> Result<Resume, Error> {
    let profdata = root.join("current.profdata");
    let checkpoint = latest_checkpoint(root);
    if profdata.is_file() && checkpoint.is_none() {
        return Ok(Resume::Refuse {
            reason: format!(
                "source coverage at {} has profdata without a raw checkpoint; use a new output directory",
                root.display()
            ),
        });
    }
    Ok(Resume::Ready)
}

fn latest_checkpoint(root: &Path) -> Option<PathBuf> {
    let snapshots = root.join("snapshots");
    let mut names = fs::read_dir(&snapshots).ok()?.flatten().collect::<Vec<_>>();
    names.sort_by_key(|entry| entry.file_name());
    names.into_iter().rev().find_map(|entry| {
        let raw = entry.path().join("checkpoint").join("raw");
        raw.is_dir().then_some(entry.path())
    })
}

fn load_checkpoint(root: &Path) -> Option<SourceCoverageProgress> {
    let path = latest_checkpoint(root)?
        .join("checkpoint")
        .join("progress.json");
    let text = fs::read_to_string(path).ok()?;
    serde_json::from_str(&text).ok()
}

fn prepare_work_raw(root: &Path, work_raw: &Path) -> Result<(), Error> {
    if work_raw.exists() {
        fs::remove_dir_all(work_raw)?;
    }
    fs::create_dir_all(work_raw)?;
    if let Some(previous) = latest_checkpoint(root) {
        copy_dir(&previous.join("checkpoint").join("raw"), work_raw)?;
    }
    Ok(())
}

fn profile_usable(work_raw: &Path) -> bool {
    fs::read_dir(work_raw)
        .ok()
        .into_iter()
        .flatten()
        .flatten()
        .any(|entry| {
            entry.path().extension().and_then(|ext| ext.to_str()) == Some("profraw")
                && entry.metadata().map(|meta| meta.len() > 0).unwrap_or(false)
        })
}

fn merge_raw(tool: &str, work_raw: &Path, output: &Path) -> Result<(), Error> {
    let mut raws = Vec::new();
    for entry in fs::read_dir(work_raw)? {
        let path = entry?.path();
        if path.extension().and_then(|ext| ext.to_str()) == Some("profraw") {
            raws.push(path);
        }
    }
    if raws.is_empty() {
        return Err(Error::unknown(
            "coverage batch produced no raw profile".to_string(),
        ));
    }
    if let Some(parent) = output.parent() {
        fs::create_dir_all(parent)?;
    }
    let started = Instant::now();
    let output_status = Command::new(tool)
        .arg("merge")
        .arg("-sparse")
        .args(&raws)
        .arg("-o")
        .arg(output)
        .output()
        .map_err(|error| Error::unknown(format!("starting {tool}: {error}")))?;
    if !output_status.status.success() {
        return Err(Error::unknown(tool_failure(
            tool,
            output_status.status,
            &output_status.stderr,
            started.elapsed(),
        )));
    }
    Ok(())
}

fn tool_failure(tool: &str, status: ExitStatus, stderr: &[u8], elapsed: Duration) -> String {
    let how = match (status.code(), status.signal()) {
        (Some(code), _) => format!("exit {code}"),
        (None, Some(signal)) => format!("signal {signal}"),
        (None, None) => format!("status {status}"),
    };
    format!(
        "{tool} failed with {how} after {} ms: {}",
        elapsed.as_millis(),
        String::from_utf8_lossy(stderr).trim()
    )
}

fn export_cov(
    tool: &str,
    binary: &Path,
    profile: &Path,
    summary_only: bool,
) -> Result<String, Error> {
    let mut command = Command::new(tool);
    command
        .arg("export")
        .arg(binary)
        .arg(format!("-instr-profile={}", profile.display()));
    if summary_only {
        command.arg("--summary-only");
    }
    let started = Instant::now();
    let output = command
        .output()
        .map_err(|error| Error::unknown(format!("starting {tool}: {error}")))?;
    if !output.status.success() {
        return Err(Error::unknown(tool_failure(
            tool,
            output.status,
            &output.stderr,
            started.elapsed(),
        )));
    }
    String::from_utf8(output.stdout)
        .map_err(|error| Error::serialize(format!("llvm-cov json: {error}")))
}

fn parse_summary(json: &str) -> Result<CoverageSummary, Error> {
    let value: Value = serde_json::from_str(json)
        .map_err(|error| Error::serialize(format!("coverage json: {error}")))?;
    let totals = value
        .pointer("/data/0/totals")
        .cloned()
        .unwrap_or(Value::Null);
    Ok(CoverageSummary {
        functions: metric(&totals, "functions", "covered"),
        lines: metric(&totals, "lines", "covered"),
        regions: metric(&totals, "regions", "covered"),
        branches: metric(&totals, "branches", "covered"),
        instantiations: metric(&totals, "instantiations", "covered"),
        lines_total: metric(&totals, "lines", "count"),
        branches_total: metric(&totals, "branches", "count"),
    })
}

fn metric(totals: &Value, name: &str, field: &str) -> u64 {
    totals
        .get(name)
        .and_then(|item| item.get(field))
        .and_then(Value::as_u64)
        .unwrap_or(0)
}

#[allow(clippy::too_many_arguments)]
fn publish_snapshot(
    root: &Path,
    fingerprint: &str,
    snapshot: u64,
    profdata: &Path,
    json: Option<&str>,
    summary: &CoverageSummary,
    previous: &CoverageSummary,
    decision: &BatchDecision,
    succeeded: &[RecordedCase],
    failures: &[String],
    work_raw: &Path,
    binary: &Path,
    profdata_tool: &str,
    cov_tool: &str,
    replay_executions: u64,
    timings: &mut BatchTimings,
    sut_log: &SutLog,
) -> Result<(), Error> {
    let incoming = root
        .join("snapshots")
        .join(format!(".incoming-{snapshot:06}"));
    if incoming.exists() {
        fs::remove_dir_all(&incoming)?;
    }
    fs::create_dir_all(incoming.join("checkpoint").join("raw"))?;
    let profdata_bytes = fs::read(profdata)?;
    let compress_at = Instant::now();
    let mut encoder = ZlibEncoder::new(Vec::new(), Compression::new(1));
    encoder
        .write_all(&profdata_bytes)
        .map_err(|error| Error::unknown(format!("zlib coverage profile: {error}")))?;
    let compressed = encoder
        .finish()
        .map_err(|error| Error::unknown(format!("zlib coverage profile: {error}")))?;
    timings.compress = compress_at.elapsed();
    let persist_at = Instant::now();
    fs::write(incoming.join("coverage.profdata.zlib"), &compressed)?;
    if let Some(json) = json {
        fs::write(incoming.join("coverage.json"), json)?;
    }
    let sut_copy = sut_log.copy_to(&incoming)?;
    copy_dir(work_raw, &incoming.join("checkpoint").join("raw"))?;
    timings.persist = persist_at.elapsed();
    let metadata = serde_json::json!({
        "case_ids": succeeded.iter().map(|case| &case.id).collect::<Vec<_>>(),
        "locations": succeeded.iter().map(|case| &case.location).collect::<Vec<_>>(),
        "coverage_delta": {
            "functions": summary.functions.saturating_sub(previous.functions),
            "lines": summary.lines.saturating_sub(previous.lines),
            "regions": summary.regions.saturating_sub(previous.regions),
            "branches": summary.branches.saturating_sub(previous.branches),
            "instantiations": summary.instantiations.saturating_sub(previous.instantiations),
            "grew": decision.summary_grew,
        },
        "summary": summary,
        "json_exported": json.is_some(),
        "failures": failures,
        "binary": binary.display().to_string(),
        "binary_sha256": file_sha256(binary).unwrap_or_default(),
        "llvm_profdata": tool_version(profdata_tool),
        "llvm_cov": tool_version(cov_tool),
        "fingerprint": fingerprint,
        "timings_ms": {
            "replay": timings.replay.as_millis(),
            "index": timings.index.as_millis(),
            "export": timings.export.as_millis(),
            "compress": timings.compress.as_millis(),
            "persist": timings.persist.as_millis(),
        },
        "exit_kinds": succeeded.iter().map(|case| &case.exit_kind).collect::<Vec<_>>(),
        "sut_output": {
            "stdout": {
                "file": sut_output::STDOUT_LOG,
                "bytes": sut_copy.stdout_bytes,
            },
            "stderr": {
                "file": sut_output::STDERR_LOG,
                "bytes": sut_copy.stderr_bytes,
            },
        },
    });
    fs::write(
        incoming.join("metadata.json"),
        serde_json::to_vec_pretty(&metadata)
            .map_err(|error| Error::serialize(format!("coverage metadata: {error}")))?,
    )?;
    let mut checkpoint_progress = SourceCoverageProgress::new(fingerprint);
    checkpoint_progress.last_summary = *summary;
    checkpoint_progress.snapshot = snapshot;
    checkpoint_progress.replay_executions = replay_executions;
    fs::write(
        incoming.join("checkpoint").join("progress.json"),
        serde_json::to_vec_pretty(&checkpoint_progress)
            .map_err(|error| Error::serialize(format!("coverage checkpoint: {error}")))?,
    )?;
    let final_dir = root.join("snapshots").join(format!("{snapshot:06}"));
    fs::rename(&incoming, &final_dir)?;
    let latest_json = root.join("latest.json");
    if let Some(json) = json {
        atomic_write(&latest_json, json.as_bytes())?;
    } else if latest_json.exists() {
        // Do not leave the previous full export looking like it belongs to
        // this newer snapshot, which intentionally has no JSON report.
        fs::remove_file(latest_json)?;
    }
    atomic_write(&root.join("current.profdata"), &profdata_bytes)?;
    if snapshot > 1 {
        let previous = root
            .join("snapshots")
            .join(format!("{:06}", snapshot - 1))
            .join("checkpoint")
            .join("raw");
        let _ = fs::remove_dir_all(previous);
    }
    Ok(())
}

fn atomic_write(path: &Path, bytes: &[u8]) -> Result<(), Error> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp = path.with_extension("tmp-write");
    fs::write(&tmp, bytes)?;
    fs::rename(tmp, path)?;
    Ok(())
}

fn copy_dir(from: &Path, to: &Path) -> Result<(), Error> {
    if to.exists() {
        fs::remove_dir_all(to)?;
    }
    fs::create_dir_all(to)?;
    if !from.exists() {
        return Ok(());
    }
    for entry in fs::read_dir(from)? {
        let entry = entry?;
        let dest = to.join(entry.file_name());
        if entry.file_type()?.is_dir() {
            copy_dir(&entry.path(), &dest)?;
        } else {
            fs::copy(entry.path(), dest)?;
        }
    }
    Ok(())
}

pub(crate) fn build_fingerprint(binary: &Path, profdata: &str, cov: &str) -> Result<String, Error> {
    let mut material = file_sha256(binary)?;
    material.push_str(&tool_version(profdata));
    material.push_str(&tool_version(cov));
    material.push_str(BUILD_FLAGS);
    Ok(sha256_bytes(material.as_bytes()))
}

fn file_sha256(path: &Path) -> Result<String, Error> {
    let bytes = fs::read(path).map_err(|error| {
        Error::unknown(format!(
            "reading {} for coverage fingerprint: {error}",
            path.display()
        ))
    })?;
    Ok(sha256_bytes(&bytes))
}

fn sha256_bytes(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn tool_version(tool: &str) -> String {
    Command::new(tool)
        .arg("--version")
        .output()
        .ok()
        .and_then(|output| String::from_utf8(output.stdout).ok())
        .unwrap_or_else(|| format!("{tool} missing"))
}

struct OutputLock {
    file: File,
}

impl OutputLock {
    fn acquire(dir: &Path) -> Result<Self, Error> {
        fs::create_dir_all(dir)?;
        let file = File::create(dir.join(".lock"))?;
        let rc = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
        if rc != 0 {
            return Err(Error::unknown(format!(
                "source coverage output {} is locked",
                dir.display()
            )));
        }
        Ok(Self { file })
    }
}

impl Drop for OutputLock {
    fn drop(&mut self) {
        unsafe {
            libc::flock(self.file.as_raw_fd(), libc::LOCK_UN);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use flate2::read::ZlibDecoder;
    use libafl::corpus::CorpusId;
    use std::{collections::BTreeMap, io::Read};

    fn test_sut_log(dir: &Path) -> SutLog {
        let mut log = SutLog::open(dir, "test-batch").unwrap();
        log.batch_start().unwrap();
        log
    }

    #[test]
    fn summary_growth_ignores_equal_counts() {
        let base = CoverageSummary {
            functions: 1,
            lines: 2,
            regions: 3,
            branches: 4,
            instantiations: 5,
            ..CoverageSummary::default()
        };
        assert!(!base.grew_from(base));
        assert!(CoverageSummary { lines: 3, ..base }.grew_from(base));
    }

    #[test]
    fn raw_rollback_is_refused_while_the_forkserver_is_alive() {
        assert!(restore_raw_allowed(true).is_err());
        assert!(restore_raw_allowed(false).is_ok());
    }

    #[test]
    fn coverage_shm_env_does_not_reuse_the_main_map() {
        let env = coverage_child_env("main-id", "cov-id", "/tmp/raw/%m%c.profraw").unwrap();
        assert!(env
            .iter()
            .any(|(key, value)| key == "__AFL_SHM_ID" && value == "cov-id"));
        assert!(coverage_child_env("same", "same", "x").is_err());
    }

    #[test]
    fn export_summary_reads_covered_counts() {
        let json = r#"{"data":[{"totals":{"functions":{"covered":2},"lines":{"covered":5},"regions":{"covered":7},"branches":{"covered":1},"instantiations":{"covered":3}}}]}"#;
        let summary = parse_summary(json).unwrap();
        assert_eq!(summary.functions, 2);
        assert_eq!(summary.lines, 5);
        assert_eq!(summary.instantiations, 3);
        assert_eq!(summary.lines_total, 0);
        assert_eq!(summary.branches_total, 0);
        let with_totals = r#"{"data":[{"totals":{"lines":{"covered":1,"count":4},"branches":{"covered":2,"count":0}}}]}"#;
        let totals = parse_summary(with_totals).unwrap();
        assert_eq!(totals.lines, 1);
        assert_eq!(totals.lines_total, 4);
        assert_eq!(totals.branches, 2);
        assert_eq!(totals.branches_total, 0);
        assert!(!totals.grew_from(CoverageSummary {
            lines: 1,
            branches: 2,
            lines_total: 9,
            ..CoverageSummary::default()
        }));
    }

    #[test]
    #[serial_test::serial]
    fn cli_batches_match_one_stage_replay_of_the_same_probe_inputs() {
        let dir = std::env::temp_dir().join(format!("mcn-cov-cli-probe-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join("work")).unwrap();
        fs::write(dir.join("work").join("mode"), b"a").unwrap();
        let binary = compile_coverage_probe(&dir);
        let script = dir.join("gen.py");
        fs::write(
            &script,
            b"from generator_server import step_handler\n@step_handler('probe')\ndef handle_probe(ctx, params):\n    return None\n",
        )
        .unwrap();
        let corpus = dir.join("corpus");
        fs::create_dir_all(&corpus).unwrap();
        for (name, n) in [("a.json", 1), ("b.json", 2)] {
            fs::write(
                corpus.join(name),
                format!(
                    "{{\"generator\":\"{}\",\"params\":{{\"n\":{n}}},\"step_trace\":[{{\"name\":\"probe-1\",\"step\":\"probe\",\"action\":\"send\",\"channel\":\"control\"}}]}}\n",
                    script.display()
                ),
            )
            .unwrap();
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
        let _env = EnvGuard::set("__AFL_SHM_ID", "unit-cli-shm");
        let mut campaign = probe_campaign(&dir.join("work"), &binary);
        campaign.coverage.binary = binary;
        campaign.campaign.python =
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(".venv/bin/python");
        campaign.campaign.pythonpath = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("python");
        let cli_out = dir.join("cli-out");
        super::command::run(&campaign, &cli_out, Some(corpus.as_path()), 1)
            .unwrap_or_else(|error| panic!("cli coverage failed: {error}"));
        let report =
            super::command::replay_directory_once(&campaign, &dir.join("stage-root"), &corpus)
                .unwrap_or_else(|error| panic!("stage batch failed: {error}"));
        assert!(report.failure.is_none(), "{:?}", report.failure);
        assert_eq!(report.succeeded, 2);
        let csv = fs::read_to_string(cli_out.join("cov_over_time.csv")).unwrap();
        let last = csv.lines().last().unwrap();
        let cols: Vec<_> = last.split(',').collect();
        assert_eq!(cols[7], "2", "{csv}");
        assert_eq!(cols[2], report.summary.lines.to_string(), "{csv}");
        assert_eq!(cols[4], report.summary.branches.to_string(), "{csv}");
        assert_eq!(cols[5], report.summary.lines_total.to_string(), "{csv}");
        assert_eq!(cols[6], report.summary.branches_total.to_string(), "{csv}");
        assert!(report.summary.lines_total > 0, "{:?}", report.summary);
        assert_eq!(csv.lines().count(), 3, "{csv}");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn published_snapshot_zlib_matches_profdata_and_records_batch_case_ids() {
        let dir = std::env::temp_dir().join(format!("mcn-cov-snap-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join("work-raw")).unwrap();
        fs::write(dir.join("work-raw").join("a.profraw"), b"raw-bytes").unwrap();
        fs::write(dir.join("coverage.profdata.tmp"), b"profdata-bytes").unwrap();
        let decision = BatchDecision {
            succeeded: vec!["corpus:abc".to_string()],
            summary_grew: true,
            publish_ok: false,
        };
        let recorded = RecordedCase {
            id: "corpus:abc".to_string(),
            location: CaseLocation {
                id: "corpus:1".to_string(),
                corpus_id: CorpusId(1).0,
                filename: Some("one".to_string()),
            },
            exit_kind: "ok".to_string(),
        };
        publish_snapshot(
            &dir,
            "finger",
            1,
            &dir.join("coverage.profdata.tmp"),
            Some("{\"data\":[]}"),
            &CoverageSummary {
                lines: 4,
                functions: 2,
                ..CoverageSummary::default()
            },
            &CoverageSummary {
                lines: 1,
                functions: 2,
                ..CoverageSummary::default()
            },
            &decision,
            &[recorded],
            &[],
            &dir.join("work-raw"),
            Path::new("/tmp/missing-binary"),
            "llvm-profdata-18",
            "llvm-cov-18",
            3,
            &mut BatchTimings {
                replay: Duration::from_millis(3),
                ..BatchTimings::default()
            },
            &test_sut_log(&dir),
        )
        .unwrap();
        let compressed = fs::read(dir.join("snapshots/000001/coverage.profdata.zlib")).unwrap();
        let mut decoder = ZlibDecoder::new(compressed.as_slice());
        let mut roundtrip = Vec::new();
        decoder.read_to_end(&mut roundtrip).unwrap();
        assert_eq!(roundtrip, b"profdata-bytes");
        let metadata: Value = serde_json::from_str(
            &fs::read_to_string(dir.join("snapshots/000001/metadata.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(metadata["case_ids"][0], "corpus:abc");
        assert_eq!(metadata["coverage_delta"]["lines"], 3);
        assert_eq!(metadata["coverage_delta"]["functions"], 0);
        assert_eq!(metadata["sut_output"]["stdout"]["file"], "sut.stdout.log");
        assert_eq!(metadata["sut_output"]["stderr"]["file"], "sut.stderr.log");
        assert!(metadata["sut_output"]["stdout"]["bytes"].as_u64().unwrap() > 0);
        assert!(dir.join("snapshots/000001/sut.stdout.log").is_file());
        assert!(dir.join("snapshots/000001/sut.stderr.log").is_file());
        assert!(dir.join("current.profdata").is_file());
        assert!(dir.join("latest.json").is_file());
        let progress: SourceCoverageProgress = serde_json::from_str(
            &fs::read_to_string(dir.join("snapshots/000001/checkpoint/progress.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(progress.replay_executions, 3);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn profdata_without_a_raw_checkpoint_is_refused() {
        let dir = std::env::temp_dir().join(format!("mcn-cov-refuse-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("current.profdata"), b"old").unwrap();
        match resume_or_refuse(&dir).unwrap() {
            Resume::Refuse { reason } => assert!(reason.contains("new output directory")),
            Resume::Ready => panic!("old profdata without raw was accepted"),
        }
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn sha256_matches_known_digests() {
        assert_eq!(
            sha256_bytes(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(
            sha256_bytes(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn coverage_failure_keeps_fuzzing_and_does_not_advance_the_checkpoint() {
        assert!(keep_fuzzing(Err(Error::unknown("index failed".to_string()))).is_ok());
        assert!(keep_fuzzing(Ok(())).is_ok());

        let dir = std::env::temp_dir().join(format!("mcn-cov-fail-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join("raw")).unwrap();
        fs::write(dir.join("raw").join("seed.profraw"), b"raw-bytes").unwrap();
        fs::write(dir.join("coverage.profdata.tmp"), b"profdata-bytes").unwrap();
        let decision = BatchDecision {
            succeeded: vec!["corpus:old".to_string()],
            summary_grew: true,
            publish_ok: false,
        };
        let recorded = RecordedCase {
            id: "corpus:old".to_string(),
            location: CaseLocation {
                id: "corpus:1".to_string(),
                corpus_id: 1,
                filename: None,
            },
            exit_kind: "ok".to_string(),
        };
        publish_snapshot(
            &dir,
            "finger",
            1,
            &dir.join("coverage.profdata.tmp"),
            Some("{\"data\":[]}"),
            &CoverageSummary {
                lines: 1,
                ..CoverageSummary::default()
            },
            &CoverageSummary::default(),
            &decision,
            &[recorded],
            &[],
            &dir.join("raw"),
            Path::new("/tmp/missing-binary"),
            "llvm-profdata-18",
            "llvm-cov-18",
            1,
            &mut BatchTimings::default(),
            &test_sut_log(&dir),
        )
        .unwrap();
        let empty = dir.join("empty-raw");
        fs::create_dir_all(&empty).unwrap();
        assert!(merge_raw("llvm-profdata-18", &empty, &dir.join("should-not-matter")).is_err());
        assert!(export_cov(
            "llvm-cov-18",
            Path::new("/tmp/mcn-no-such-coverage-binary"),
            &dir.join("coverage.profdata.tmp"),
            false,
        )
        .is_err());
        let blocked = dir.join("blocked-root");
        fs::write(&blocked, b"not-a-directory").unwrap();
        assert!(publish_snapshot(
            &blocked,
            "finger",
            2,
            &dir.join("coverage.profdata.tmp"),
            Some("{}"),
            &CoverageSummary::default(),
            &CoverageSummary::default(),
            &decision,
            &[],
            &["disk write failed".to_string()],
            &dir.join("raw"),
            Path::new("/tmp/missing-binary"),
            "llvm-profdata-18",
            "llvm-cov-18",
            2,
            &mut BatchTimings::default(),
            &test_sut_log(&dir),
        )
        .is_err());
        let after = load_checkpoint(&dir).unwrap();
        assert_eq!(after.snapshot, 1);
        assert_eq!(
            fs::read(dir.join("current.profdata")).unwrap(),
            b"profdata-bytes"
        );
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn same_corpus_id_in_corpus_and_solutions_stays_distinct() {
        use libafl::corpus::{InMemoryCorpus, Testcase};
        let dir = std::env::temp_dir().join(format!("mcn-cov-both-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let script = dir.join("gen.py");
        fs::write(&script, b"def generate(ctx, params):\n    return b''\n").unwrap();
        let input = GeneratorInput::new(script, BTreeMap::new());
        let mut corpus = InMemoryCorpus::new();
        let mut solutions = InMemoryCorpus::new();
        corpus.add(Testcase::new(input.clone())).unwrap();
        solutions.add(Testcase::new(input)).unwrap();
        let mut cases = Vec::new();
        collect_corpus(&corpus, "corpus", &mut cases).unwrap();
        collect_corpus(&solutions, "solution", &mut cases).unwrap();
        assert_eq!(cases.len(), 2);
        assert_eq!(cases[0].location.corpus_id, cases[1].location.corpus_id);
        assert_eq!(cases[0].location.corpus_id, 0);
        assert_ne!(cases[0].id, cases[1].id);
        assert!(cases[0].id.starts_with("corpus:"));
        assert!(cases[1].id.starts_with("solution:"));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn missing_coverage_binary_fails_before_a_forkserver_starts() {
        let mut campaign = probe_campaign(
            &std::env::temp_dir().join(format!("mcn-cov-missing-{}", std::process::id())),
            Path::new("/tmp/mcn-no-such-coverage-binary"),
        );
        campaign.target.workdir = std::env::temp_dir();
        match ForkserverBackend::spawn_coverage(&campaign, 0, "/tmp/%m%c.profraw") {
            Err(error) => assert!(format!("{error:?}").contains("missing"), "{error:?}"),
            Ok(_) => panic!("missing coverage binary started a forkserver"),
        }
    }

    #[test]
    #[serial_test::serial]
    fn coverage_probe_reports_status_accumulates_profile_and_rolls_back_after_reap() {
        let dir = std::env::temp_dir().join(format!("mcn-cov-probe-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join("raw")).unwrap();
        fs::create_dir_all(dir.join("work")).unwrap();
        let binary = compile_coverage_probe(&dir);
        let mode = dir.join("work").join("mode");
        fs::write(&mode, b"a").unwrap();
        let _env = EnvGuard::set("__AFL_SHM_ID", "unit-main-shm");
        let campaign = probe_campaign(&dir.join("work"), &binary);
        let profile = dir.join("raw").join("%m%c.profraw");
        let mut backend =
            ForkserverBackend::spawn_coverage(&campaign, 0, &profile.display().to_string())
                .unwrap_or_else(|error| panic!("coverage probe handshake failed: {error:?}"));
        let forkserver = backend.forkserver_pid();
        assert!(forkserver > 0, "forkserver pid was not observed");
        let main_shm = std::env::var("__AFL_SHM_ID").unwrap();
        let child_env = fs::read(format!("/proc/{forkserver}/environ")).unwrap_or_default();
        let child_env = String::from_utf8_lossy(&child_env);
        assert!(child_env.contains("AFL_LLVM_ONLY_FSRV=1"));
        assert!(!child_env.contains(&format!("__AFL_SHM_ID={main_shm}")));

        let (pid_a, stop_a) = drive_probe(&mut backend, &mode, b"a", false);
        assert_eq!(stop_a, TargetStop::Exited(0), "A status {stop_a:?}");
        let (pid_b, stop_b) = drive_probe(&mut backend, &mode, b"b", false);
        assert_eq!(stop_b, TargetStop::Exited(0), "B status {stop_b:?}");
        let (pid_kill, stop_kill) = drive_probe(&mut backend, &mode, b"h", true);
        assert_eq!(
            stop_kill,
            TargetStop::Signaled(libc::SIGKILL),
            "SIGKILL status {stop_kill:?}"
        );
        assert_ne!(pid_a, pid_b);
        assert_ne!(pid_b, pid_kill);
        assert_eq!(backend.forkserver_pid(), forkserver);
        assert!(
            profile_usable(&dir.join("raw")),
            "raw profile missing after SIGKILL"
        );
        let rollback = dir.join("rollback");
        copy_dir(&dir.join("raw"), &rollback).unwrap();
        assert!(restore_raw_allowed(proc_exists(forkserver)).is_err());
        let live_bytes = fs::read(first_profraw(&dir.join("raw"))).unwrap();
        assert!(!live_bytes.is_empty());

        backend.retire();
        assert!(wait_dead(forkserver), "coverage forkserver was not reaped");
        assert!(restore_raw_allowed(false).is_ok());
        fs::write(first_profraw(&dir.join("raw")), b"clobber").unwrap();
        copy_dir(&rollback, &dir.join("raw")).unwrap();
        assert_eq!(
            fs::read(first_profraw(&dir.join("raw"))).unwrap(),
            live_bytes
        );

        let counts = profile_counts(&binary, &dir.join("raw"));
        assert_eq!(counts.func_a, 2, "profile counts {counts:?}");
        assert_eq!(counts.func_b, 1, "profile counts {counts:?}");
        assert_eq!(counts.main, 3, "profile counts {counts:?}");
        let mut restarted =
            ForkserverBackend::spawn_coverage(&campaign, 0, &profile.display().to_string())
                .unwrap();
        let restarted_pid = restarted.forkserver_pid();
        restarted.retire();
        assert!(
            wait_dead(restarted_pid),
            "restarted coverage forkserver was not reaped"
        );
        let again = profile_counts(&binary, &dir.join("raw"));
        assert_eq!(
            again.func_a, counts.func_a,
            "restart doubled func_a: {again:?}"
        );
        assert_eq!(
            again.func_b, counts.func_b,
            "restart doubled func_b: {again:?}"
        );
        assert_eq!(again.main, counts.main, "restart doubled main: {again:?}");

        let mut backend =
            ForkserverBackend::spawn_coverage(&campaign, 0, &profile.display().to_string())
                .unwrap();
        let (_pid, stop_nonzero) = drive_probe(&mut backend, &mode, b"z", false);
        assert_eq!(stop_nonzero, TargetStop::Exited(9), "{stop_nonzero:?}");
        let (_pid, stop_crash) = drive_probe(&mut backend, &mode, b"c", false);
        assert_eq!(
            stop_crash,
            TargetStop::Signaled(libc::SIGSEGV),
            "{stop_crash:?}"
        );
        assert!(
            profile_usable(&dir.join("raw")),
            "raw profile missing after crash"
        );
        let forkserver = backend.forkserver_pid();
        unsafe {
            libc::kill(forkserver, libc::SIGKILL);
        }
        assert!(wait_dead(forkserver));
        let eof = match backend.begin_start() {
            Err(error) => format!("{error:?}"),
            Ok(()) => {
                let mut message = String::new();
                for _ in 0..20 {
                    match backend.poll_start(Duration::from_millis(50)) {
                        Err(error) => {
                            message = format!("{error:?}");
                            break;
                        }
                        Ok(Some(pid)) => {
                            message = format!("unexpected child {pid}");
                            break;
                        }
                        Ok(None) => {}
                    }
                }
                message
            }
        };
        assert!(
            eof.contains("pipe")
                || eof.contains("forkserver")
                || eof.contains("status")
                || eof.contains("Could not"),
            "control EOF was not reported: {eof}"
        );
        drop(backend);

        assert_parent_drop_reaps_forkserver(&campaign, &profile);
        let abrupt = abrupt_parent_exit_forkserver(&campaign, &profile);
        eprintln!("coverage probe abrupt parent _exit forkserver survived={abrupt}");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn pid_trace_records_this_process_and_does_not_signal_it() {
        let dir = std::env::temp_dir().join(format!("mcn-cov-pid-trace-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("trace.txt");
        let _guard = EnvGuard::set("MCN_COV_PID_TRACE", &path.display().to_string());
        let self_pid = std::process::id() as i32;
        trace_residents("unit", self_pid, 0, 0, 0);
        let text = fs::read_to_string(&path).unwrap();
        assert!(text.contains("phase=unit"), "{text}");
        assert!(
            text.contains(&format!("self pid={self_pid} alive=1")),
            "{text}"
        );
        assert!(
            text.contains(&format!("main_forkserver pid={self_pid} alive=1")),
            "{text}"
        );
        assert!(text.contains("coverage_forkserver pid=0 alive=0"), "{text}");
        assert!(Path::new(&format!("/proc/{self_pid}")).exists());
        let _ = fs::remove_dir_all(&dir);
    }
}

#[cfg(test)]
struct EnvGuard {
    key: &'static str,
    prev: Option<String>,
}

#[cfg(test)]
impl EnvGuard {
    fn set(key: &'static str, value: &str) -> Self {
        let prev = std::env::var(key).ok();
        std::env::set_var(key, value);
        Self { key, prev }
    }
}

#[cfg(test)]
impl Drop for EnvGuard {
    fn drop(&mut self) {
        match &self.prev {
            Some(value) => std::env::set_var(self.key, value),
            None => std::env::remove_var(self.key),
        }
    }
}

#[cfg(test)]
fn probe_campaign(workdir: &Path, binary: &Path) -> Campaign {
    use crate::app::config::{Campaign, CampaignPaths, TargetConfig, Timeouts};
    Campaign {
        target: TargetConfig {
            binary: binary.to_path_buf(),
            source_dir: None,
            workdir: workdir.to_path_buf(),
            cleanup_script: None,
            args: vec![workdir.join("mode").display().to_string()],
            host: "127.0.0.1".to_string(),
            port: 0,
            extra_env: Vec::new(),
            map_size: 65_536,
            debug_child: false,
        },
        timeouts: Timeouts::default(),
        campaign: CampaignPaths {
            seeds: workdir.to_path_buf(),
            corpus: workdir.to_path_buf(),
            crashes: workdir.to_path_buf(),
            python: PathBuf::from("/usr/bin/python3"),
            pythonpath: workdir.to_path_buf(),
            core: None,
            iterations: 1,
            timeout_secs: None,
            bitmap_stagnation_limit: crate::engine::stagnation::DEFAULT_BITMAP_STAGNATION_LIMIT,
        },
        capture: crate::app::config::CaptureConfig::default(),
        coverage: crate::app::config::CoverageConfig::default(),
        _legacy_source_coverage: (),
        _legacy_generator_synthesis: (),
        generator_synthesis: crate::app::config::GeneratorSynthesisConfig::default(),
        agents: crate::app::config::AgentCatalog::default(),
        parties: None,
        base: workdir.to_path_buf(),
        output_root: None,
    }
}

#[cfg(test)]
fn compile_coverage_probe(dir: &Path) -> PathBuf {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let afl = root.join(".deps/AFLplusplus");
    let cc = afl.join("afl-clang-fast");
    let source = dir.join("probe.c");
    let binary = dir.join("probe");
    fs::write(
        &source,
        r#"
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
__attribute__((noinline)) int func_a(void) { volatile int x = 1; return x; }
__attribute__((noinline)) int func_b(void) { volatile int y = 2; return y; }
int main(int argc, char **argv) {
    FILE *file = fopen(argc > 1 ? argv[1] : "", "r");
    int mode = file ? fgetc(file) : 'a';
    if (file) fclose(file);
    if (mode != 'z') {
        write(1, "probe-out\n", 10);
        write(2, "probe-err\n", 10);
    }
    if (mode == 'l') {
        static unsigned char big[300000];
        memset(big, 0xff, sizeof big);
        write(1, big, sizeof big);
        write(2, "ERR-LAST\n", 9);
        return 0;
    }
    if (mode == 'b') { func_b(); return 0; }
    if (mode == 'z') return 9;
    if (mode == 'c') { func_a(); raise(SIGSEGV); }
    if (mode == 'h') { func_a(); for (;;) pause(); }
    func_a();
    return 0;
}
"#,
    )
    .unwrap();
    let output = std::process::Command::new(&cc)
        .env("AFL_LLVM_ONLY_FSRV", "1")
        .env("AFL_PATH", &afl)
        .env_remove("AFL_LLVM_INSTRUMENT")
        .env_remove("AFL_GCC_ONLY_FSRV")
        .args([
            "-O2",
            "-g",
            "-fno-lto",
            "-fprofile-instr-generate",
            "-fcoverage-mapping",
            "-fprofile-update=atomic",
            "-mllvm",
            "-runtime-counter-relocation",
        ])
        .arg(&source)
        .arg("-o")
        .arg(&binary)
        .arg("-fprofile-instr-generate")
        .output()
        .unwrap_or_else(|error| panic!("starting afl-clang-fast: {error}"));
    assert!(
        output.status.success(),
        "coverage probe build failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    binary
}

#[cfg(test)]
fn spawn_probe_with_log(campaign: &Campaign, log: &SutLog, profile: &Path) -> ForkserverBackend {
    let (stdout, stderr) = log.observer_files().unwrap();
    ForkserverBackend::spawn_coverage_with_output(
        campaign,
        0,
        &profile.display().to_string(),
        Some(stdout),
        Some(stderr),
    )
    .unwrap_or_else(|error| panic!("coverage probe handshake failed: {error:?}"))
}

#[cfg(test)]
fn marker_positions(bytes: &[u8], event: &str) -> Vec<usize> {
    let text = String::from_utf8_lossy(bytes);
    text.match_indices(&format!("\"event\":\"{event}\""))
        .map(|(index, _)| index)
        .collect()
}

#[test]
#[serial_test::serial]
fn replay_captures_split_output_across_batches_and_freezes_snapshots() {
    let dir = std::env::temp_dir().join(format!("mcn-cov-output-{}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(dir.join("raw")).unwrap();
    fs::create_dir_all(dir.join("work")).unwrap();
    let binary = compile_coverage_probe(&dir);
    let mode = dir.join("work").join("mode");
    fs::write(&mode, b"a").unwrap();
    let _env = EnvGuard::set("__AFL_SHM_ID", "unit-output-shm");
    let campaign = probe_campaign(&dir.join("work"), &binary);
    let profile = dir.join("raw").join("%m%c.profraw");

    let mut first = SutLog::open(&dir, "batch-one").unwrap();
    first.batch_start().unwrap();
    let mut backend = spawn_probe_with_log(&campaign, &first, &profile);
    for (value, case_id) in [
        (b"a".as_slice(), "corpus:a"),
        (b"z".as_slice(), "corpus:z"),
        (b"c".as_slice(), "corpus:c"),
    ] {
        let case = first.case_start(case_id, Some("seed.json")).unwrap();
        let (_pid, stop) = drive_probe(&mut backend, &mode, value, false);
        first.case_end(case, case_id, &format!("{stop:?}")).unwrap();
    }
    backend.retire();
    assert!(wait_dead(backend.forkserver_pid()));
    first.batch_end("replayed").unwrap();
    let snapshot = dir.join("snapshot-one");
    let copied = first.copy_to(&snapshot).unwrap();
    assert!(copied.stdout_bytes > 0);
    assert!(copied.stderr_bytes > 0);
    let frozen_stdout = fs::read(snapshot.join(sut_output::STDOUT_LOG)).unwrap();
    let frozen_stderr = fs::read(snapshot.join(sut_output::STDERR_LOG)).unwrap();

    let mut second = SutLog::open(&dir, "batch-two").unwrap();
    second.batch_start().unwrap();
    let mut backend = spawn_probe_with_log(&campaign, &second, &profile);
    let case = second.case_start("corpus:large", None).unwrap();
    let (_pid, stop) = drive_probe(&mut backend, &mode, b"l", false);
    second
        .case_end(case, "corpus:large", &format!("{stop:?}"))
        .unwrap();
    backend.retire();
    assert!(wait_dead(backend.forkserver_pid()));
    second.batch_end("replayed").unwrap();
    drop(second);

    let live_stdout = fs::read(dir.join(sut_output::STDOUT_LOG)).unwrap();
    let live_stderr = fs::read(dir.join(sut_output::STDERR_LOG)).unwrap();
    assert_eq!(
        fs::read(snapshot.join(sut_output::STDOUT_LOG)).unwrap(),
        frozen_stdout,
        "a later batch changed an old snapshot"
    );
    assert_eq!(
        fs::read(snapshot.join(sut_output::STDERR_LOG)).unwrap(),
        frozen_stderr,
        "a later batch changed an old snapshot"
    );
    assert!(live_stdout.len() >= 300_000, "large stdout was truncated");
    assert!(
        live_stdout
            .windows(3)
            .any(|window| window == [0xff, 0xff, 0xff]),
        "non-UTF-8 stdout bytes were altered"
    );
    assert!(live_stderr.windows(9).any(|window| window == b"ERR-LAST\n"));
    let stdout_text = String::from_utf8_lossy(&live_stdout);
    let stderr_text = String::from_utf8_lossy(&live_stderr);
    assert!(stdout_text.contains("probe-out"));
    assert!(stderr_text.contains("probe-err"));
    assert!(
        !stdout_text.contains("probe-err"),
        "stderr leaked into stdout"
    );
    assert!(
        !stderr_text.contains("probe-out"),
        "stdout leaked into stderr"
    );
    assert_eq!(marker_positions(&live_stdout, "case_start").len(), 4);
    assert_eq!(marker_positions(&live_stdout, "case_end").len(), 4);
    assert_eq!(marker_positions(&live_stdout, "batch_start").len(), 2);
    assert_eq!(marker_positions(&live_stdout, "batch_end").len(), 2);
    assert!(frozen_stdout
        .windows(9)
        .any(|window| window == b"batch_end"));
    let text = String::from_utf8_lossy(&live_stdout);
    for case_id in ["corpus:a", "corpus:z", "corpus:c"] {
        let start = text.find(&format!("\"case_id\":\"{case_id}\"")).unwrap();
        let end = text[start..].find("\"event\":\"case_end\"").unwrap() + start;
        if case_id == "corpus:z" {
            assert!(
                !text[start..end].contains("probe-out"),
                "empty-output case carried another case's output"
            );
        }
    }

    let _ = fs::remove_dir_all(&dir);
}

#[test]
#[serial_test::serial]
fn coverage_start_failure_keeps_output_markers_and_no_snapshot() {
    let dir = std::env::temp_dir().join(format!("mcn-cov-start-fail-{}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(dir.join("work")).unwrap();
    fs::write(dir.join("work").join("mode"), b"a").unwrap();
    let mut campaign = probe_campaign(
        &dir.join("work"),
        Path::new("/tmp/mcn-no-such-coverage-binary"),
    );
    campaign.coverage.binary = PathBuf::from("/tmp/mcn-no-such-coverage-binary");
    let mut log = SutLog::open(&dir, "start-failure").unwrap();
    log.batch_start().unwrap();
    let profile = dir.join("raw").join("%m%c.profraw");
    let error = match CoverageSession::start(&campaign, &campaign.coverage, &profile, log) {
        Ok(_) => panic!("missing coverage binary must fail start"),
        Err(error) => error,
    };
    assert!(format!("{error:?}").contains("missing"), "{error:?}");
    let text = fs::read_to_string(dir.join(sut_output::STDOUT_LOG)).unwrap();
    assert!(text.contains("\"event\":\"batch_start\""));
    assert!(text.contains("\"event\":\"batch_end\""));
    assert!(text.contains("start_failed"));
    assert!(!dir.join("snapshots").exists());
    let _ = fs::remove_dir_all(&dir);
}

#[cfg(test)]
fn drive_probe(
    backend: &mut ForkserverBackend,
    mode: &Path,
    value: &[u8],
    kill: bool,
) -> (i32, TargetStop) {
    use crate::runtime::backend::TargetBackend;
    fs::write(mode, value).unwrap();
    backend
        .begin_start()
        .unwrap_or_else(|error| panic!("begin {value:?}: {error:?}"));
    let deadline = Instant::now() + Duration::from_secs(3);
    let mut pid = None;
    while Instant::now() < deadline {
        match backend.poll_start(Duration::from_millis(20)) {
            Ok(Some(next)) => {
                pid = Some(next);
                break;
            }
            Ok(None) => {}
            Err(error) => panic!("poll_start {value:?}: {error:?}"),
        }
    }
    let pid = pid.unwrap_or_else(|| panic!("no child pid for {value:?}"));
    if kill {
        sleep(Duration::from_millis(100));
        backend.kill_child(true).unwrap();
    }
    let deadline = Instant::now() + Duration::from_secs(3);
    while Instant::now() < deadline {
        match backend.poll_target(Duration::from_millis(20)) {
            Ok(Some(stop)) => return (pid, stop),
            Ok(None) => {}
            Err(error) => panic!("poll_target {value:?}: {error:?}"),
        }
    }
    panic!("no wait status for child {pid} mode {value:?}");
}

#[derive(Debug)]
#[cfg(test)]
struct ProfileCounts {
    func_a: u64,
    func_b: u64,
    main: u64,
}

#[cfg(test)]
fn profile_counts(binary: &Path, raw_dir: &Path) -> ProfileCounts {
    let indexed = raw_dir.join("counts.profdata");
    merge_raw("llvm-profdata-18", raw_dir, &indexed).unwrap_or_else(|error| {
        panic!("llvm-profdata merge failed: {error:?}");
    });
    let json = export_cov("llvm-cov-18", binary, &indexed, false).unwrap_or_else(|error| {
        panic!("llvm-cov export failed: {error:?}");
    });
    let value: Value = serde_json::from_str(&json).unwrap();
    ProfileCounts {
        func_a: function_count(&value, "func_a"),
        func_b: function_count(&value, "func_b"),
        main: function_count(&value, "main"),
    }
}

#[cfg(test)]
fn function_count(value: &Value, name: &str) -> u64 {
    let mut found = 0;
    fn walk(value: &Value, name: &str, found: &mut u64) {
        match value {
            Value::Object(map) => {
                if map.get("name").and_then(Value::as_str) == Some(name) {
                    if let Some(count) = map.get("count").and_then(Value::as_u64) {
                        *found = count;
                    }
                }
                for child in map.values() {
                    walk(child, name, found);
                }
            }
            Value::Array(items) => {
                for child in items {
                    walk(child, name, found);
                }
            }
            _ => {}
        }
    }
    walk(value, name, &mut found);
    found
}

#[cfg(test)]
fn first_profraw(dir: &Path) -> PathBuf {
    fs::read_dir(dir)
        .unwrap()
        .flatten()
        .map(|entry| entry.path())
        .find(|path| path.extension().and_then(|ext| ext.to_str()) == Some("profraw"))
        .unwrap_or_else(|| panic!("no profraw in {}", dir.display()))
}

#[cfg(test)]
fn assert_parent_drop_reaps_forkserver(campaign: &Campaign, profile: &Path) {
    let pidfile = profile.parent().unwrap().join("drop.pid");
    let child = unsafe { libc::fork() };
    assert!(child >= 0);
    if child == 0 {
        match ForkserverBackend::spawn_coverage(campaign, 0, &profile.display().to_string()) {
            Ok(backend) => {
                let _ = fs::write(&pidfile, backend.forkserver_pid().to_string());
                drop(backend);
                std::process::exit(0);
            }
            Err(error) => {
                eprintln!("drop-path spawn failed: {error:?}");
                std::process::exit(2);
            }
        }
    }
    let status = wait_child(child);
    assert!(status == 0, "drop child status {status}");
    let forkserver: i32 = fs::read_to_string(&pidfile)
        .unwrap()
        .trim()
        .parse()
        .unwrap();
    assert!(
        wait_dead(forkserver),
        "Drop did not reap coverage forkserver {forkserver}"
    );
}

/// Returns true when the forkserver is still alive after the parent `_exit`s
/// without running destructors. The caller must reap it.
#[cfg(test)]
fn abrupt_parent_exit_forkserver(campaign: &Campaign, profile: &Path) -> bool {
    use crate::runtime::backend::TargetBackend;
    let pidfile = profile.parent().unwrap().join("abrupt.pid");
    let child = unsafe { libc::fork() };
    assert!(child >= 0);
    if child == 0 {
        match ForkserverBackend::spawn_coverage(campaign, 0, &profile.display().to_string()) {
            Ok(backend) => {
                let _ = fs::write(&pidfile, backend.forkserver_pid().to_string());
                std::mem::forget(backend);
                unsafe { libc::_exit(0) };
            }
            Err(error) => {
                eprintln!("abrupt spawn failed: {error:?}");
                unsafe { libc::_exit(2) };
            }
        }
    }
    let status = wait_child(child);
    assert!(status == 0, "abrupt child status {status}");
    let forkserver: i32 = fs::read_to_string(&pidfile)
        .unwrap()
        .trim()
        .parse()
        .unwrap();
    let deadline = Instant::now() + Duration::from_millis(300);
    while Instant::now() < deadline && proc_exists(forkserver) {
        sleep(Duration::from_millis(20));
    }
    let survived = proc_exists(forkserver);
    if survived {
        unsafe {
            libc::kill(forkserver, libc::SIGKILL);
        }
        assert!(
            wait_dead(forkserver),
            "failed to reap orphan forkserver {forkserver}"
        );
    }
    survived
}

#[cfg(test)]
fn wait_child(pid: i32) -> i32 {
    let deadline = Instant::now() + Duration::from_secs(10);
    while Instant::now() < deadline {
        let mut status = 0;
        let rc = unsafe { libc::waitpid(pid, &mut status, libc::WNOHANG) };
        if rc == pid {
            if libc::WIFEXITED(status) {
                return libc::WEXITSTATUS(status);
            }
            return 256 + libc::WTERMSIG(status);
        }
        sleep(Duration::from_millis(10));
    }
    panic!("child {pid} did not exit");
}
