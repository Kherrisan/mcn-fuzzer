//! Assemble LibAFL and run one campaign.
//!
//! Statistics printed from the map feedback are occupancy of the shared
//! PCGUARD map. They are not a source-coverage percentage.

pub mod config;

use std::{
    collections::HashSet,
    fs,
    path::{Path, PathBuf},
    process::Command,
    sync::Arc,
    time::{Duration, Instant},
};

use crate::{
    app::config::Campaign,
    coverage::capture::PcapToMetadataFeedback,
    coverage::LlvmCoverageStage,
    engine::executor::{CaseExecutor, CoverageFeedback},
    engine::input::GeneratorInput,
    engine::mutation_stage::GeneratorMutationStage,
    engine::stagnation::BitmapProgress,
    engine::synthesis::GeneratorSynthesisStage,
    runtime::backend::{AbortReason, ForkserverBackend, TargetBackend},
    runtime::report::TargetStop,
};
use libafl::{
    corpus::{Corpus, OnDiskCorpus},
    events::{ProgressReporter, SimpleEventManager},
    executors::ExitKind,
    feedbacks::{CombinedFeedback, CrashFeedback, LogicEagerOr, NewHashFeedback},
    fuzzer::{Evaluator, ExecutesInput, Fuzzer, NopInputFilter, StdFuzzer},
    inputs::{BytesInputConverter, Input},
    observers::AsanBacktraceObserver,
    monitors::SimpleMonitor,
    schedulers::QueueScheduler,
    stages::Stage,
    state::{HasCorpus, HasSolutions, StdState},
    Error, HasMetadata,
};
use libafl_bolts::{rands::StdRand, tuples::tuple_list};

type State =
    StdState<OnDiskCorpus<GeneratorInput>, GeneratorInput, StdRand, OnDiskCorpus<GeneratorInput>>;
type CampaignFeedback = CombinedFeedback<CoverageFeedback, PcapToMetadataFeedback, LogicEagerOr>;
type AsanObjective = NewHashFeedback<AsanBacktraceObserver>;
type CampaignObjective = CombinedFeedback<
    CombinedFeedback<AsanObjective, CrashFeedback, LogicEagerOr>,
    PcapToMetadataFeedback,
    LogicEagerOr,
>;
type CampaignFuzzer = StdFuzzer<
    QueueScheduler,
    CampaignFeedback,
    BytesInputConverter,
    NopInputFilter,
    CampaignObjective,
>;
type Manager = SimpleEventManager<GeneratorInput, SimpleMonitor<fn(&str)>, State>;

mod accept;

pub use accept::{accept_job, accept_scenarios};

#[cfg(feature = "stats")]
mod bench;

#[cfg(feature = "stats")]
pub use bench::{bench_capture, execute_bench_job as bench_job, finalize_bench};

struct Session {
    executor: CaseExecutor,
    fuzzer: CampaignFuzzer,
    state: State,
    mgr: Manager,
}

impl Session {
    fn open(campaign: &Campaign) -> Result<Self, Error> {
        if let Some(root) = &campaign.output_root {
            fs::create_dir_all(root).map_err(|error| {
                Error::unknown(format!(
                    "creating campaign root {}: {error}",
                    root.display()
                ))
            })?;
        }
        fs::create_dir_all(&campaign.campaign.corpus).map_err(|error| {
            Error::unknown(format!(
                "creating corpus {}: {error}",
                campaign.campaign.corpus.display()
            ))
        })?;
        fs::create_dir_all(&campaign.campaign.crashes).map_err(|error| {
            Error::unknown(format!(
                "creating crashes {}: {error}",
                campaign.campaign.crashes.display()
            ))
        })?;
        if campaign.coverage.enabled {
            fs::create_dir_all(&campaign.coverage.output).map_err(|error| {
                Error::unknown(format!(
                    "creating coverage output {}: {error}",
                    campaign.coverage.output.display()
                ))
            })?;
        }
        if campaign.generator_synthesis.enabled {
            fs::create_dir_all(&campaign.generator_synthesis.output).map_err(|error| {
                Error::unknown(format!(
                    "creating synthesis output {}: {error}",
                    campaign.generator_synthesis.output.display()
                ))
            })?;
        }
        let (executor, coverage) = CaseExecutor::launch(campaign)?;
        let engine = executor.capture_engine();
        let mut feedback: CampaignFeedback =
            CombinedFeedback::new(coverage, PcapToMetadataFeedback::new(Arc::clone(&engine)));
        let asan = executor.asan_observer().clone();
        let asan_objective = NewHashFeedback::new(&asan);
        let asan_objective: CombinedFeedback<AsanObjective, CrashFeedback, LogicEagerOr> =
            CombinedFeedback::new(asan_objective, CrashFeedback::new());
        let mut objective: CampaignObjective =
            CombinedFeedback::new(asan_objective, PcapToMetadataFeedback::new(engine));
        let mut state = StdState::new(
            StdRand::with_seed(0x4d43_4e21),
            OnDiskCorpus::new(&campaign.campaign.corpus)?,
            OnDiskCorpus::new(&campaign.campaign.crashes)?,
            &mut feedback,
            &mut objective,
        )?;
        state.add_metadata(BitmapProgress::new(
            campaign.campaign.bitmap_stagnation_limit,
        )?);
        let fuzzer = StdFuzzer::new(QueueScheduler::new(), feedback, objective);
        let mgr = SimpleEventManager::new(SimpleMonitor::new(print_campaign_line as fn(&str)));
        Ok(Self {
            executor,
            fuzzer,
            state,
            mgr,
        })
    }

    fn execute(&mut self, input: &GeneratorInput) -> Result<ExitKind, Error> {
        self.fuzzer
            .execute_input(&mut self.state, &mut self.executor, &mut self.mgr, input)
    }

    fn evaluate(&mut self, input: &GeneratorInput) -> Result<(), Error> {
        self.fuzzer
            .evaluate_input(&mut self.state, &mut self.executor, &mut self.mgr, input)?;
        Ok(())
    }
}

fn print_campaign_line(line: &str) {
    let shown = line.replace(
        "coverage_map_occupancy:",
        "coverage_map_occupancy (shared-map entries, source pcguard, not source coverage):",
    );
    println!("{shown}");
}

/// Load seeds and fuzz until the campaign timeout or iteration limit.
pub fn run(campaign: &Campaign, iterations: Option<u64>) -> Result<(), Error> {
    let started = Instant::now();
    let deadline = campaign
        .campaign
        .timeout_secs
        .map(|seconds| {
            started
                .checked_add(Duration::from_secs(seconds))
                .ok_or_else(|| Error::illegal_argument("campaign.timeout_secs is too large"))
        })
        .transpose()?;
    require_exec(&campaign.target.binary)?;
    let mut session = Session::open(campaign)?;
    let mailbox = crate::coverage::proposal::ProposalMailbox::default();
    let mut synthesis = GeneratorSynthesisStage::from_campaign(campaign, mailbox.clone())?;
    let mut analysis =
        crate::coverage::schedule::CoverageAnalysisStage::from_campaign(campaign, mailbox)?;
    if campaign.generator_synthesis.enabled {
        synthesis.initialize()?;
    }
    if campaign.coverage.analysis_enabled {
        analysis.initialize()?;
    }
    let mut mutation = GeneratorMutationStage::with_queue(
        campaign.generator_synthesis.output.join("candidates.json"),
    );
    if campaign.generator_synthesis.enabled {
        mutation.load_queue(&mut session.state)?;
    }
    let seed_files = std::fs::read_dir(&campaign.campaign.seeds)
        .map(|entries| {
            entries
                .filter_map(|entry| entry.ok())
                .any(|entry| entry.path().extension().and_then(|ext| ext.to_str()) == Some("json"))
        })
        .unwrap_or(false);
    if seed_files {
        session.state.load_initial_inputs(
            &mut session.fuzzer,
            &mut session.executor,
            &mut session.mgr,
            &[campaign.campaign.seeds.clone()],
        )?;
    }
    if campaign.generator_synthesis.enabled
        && session.state.corpus().count() == 0
        && !mutation.has_queue(&session.state)
    {
        let mut bootstrap_rand = libafl_bolts::rands::StdRand::with_seed(0x5052_4f54);
        let ready = synthesis.bootstrap_first(&mut bootstrap_rand, || analysis.poll_agent())?;
        mutation.enqueue(&mut session.state, ready.request, ready.input, ready.origin)?;
    }
    // A seedless campaign cannot ask the scheduler for a corpus input yet.
    // Run the same mutation stage directly while it still has synthesis input.
    // The baseline is evaluated like any other input, so it may itself enable
    // the ordinary fuzzer loop when feedback keeps it.
    if session.state.corpus().count() == 0 {
        while mutation.has_queue(&session.state) {
            if session.state.corpus().count() > 0 {
                break;
            }
            if deadline.is_some_and(|deadline| Instant::now() >= deadline) {
                break;
            }
            mutation.perform(
                &mut session.fuzzer,
                &mut session.executor,
                &mut session.state,
                &mut session.mgr,
            )?;
        }
        if session.state.corpus().count() == 0 {
            return Err(Error::unknown(
                "seedless synthesis input produced no corpus entry after mutation exploration"
                    .to_string(),
            ));
        }
    }
    let mut coverage = LlvmCoverageStage::from_campaign(campaign);
    if campaign.coverage.enabled {
        coverage.replay_now(&mut session.executor, &mut session.state)?;
    }
    let iteration_limit =
        iterations.or_else(|| deadline.is_none().then_some(campaign.campaign.iterations));
    let mut stages = tuple_list!(synthesis, mutation, coverage, analysis);
    let loop_result = if let Some(iterations) = iteration_limit {
        session
            .fuzzer
            .fuzz_loop_for(
                &mut stages,
                &mut session.executor,
                &mut session.state,
                &mut session.mgr,
                iterations,
            )
            .map(|_| ())
    } else {
        let deadline = deadline.expect("timed campaign has a deadline");
        let mut result = Ok(());
        while Instant::now() < deadline {
            if let Err(error) = session
                .mgr
                .maybe_report_progress(&mut session.state, Duration::from_secs(15))
            {
                result = Err(error);
                break;
            }
            if let Err(error) = session.fuzzer.fuzz_one(
                &mut stages,
                &mut session.executor,
                &mut session.state,
                &mut session.mgr,
            ) {
                result = Err(error);
                break;
            }
        }
        result.and_then(|()| session.mgr.report_progress(&mut session.state))
    };
    // Stop agent workers before their MCP service, then release the shared core.
    drop(stages);
    crate::coverage::mcp::shutdown();
    crate::agents::shutdown_private_core();
    loop_result?;
    if campaign.coverage.enabled {
        LlvmCoverageStage::from_campaign(campaign)
            .replay_now(&mut session.executor, &mut session.state)?;
    }
    let occupied = interesting_indexes(&session.executor.edge_indexes()).len();
    println!(
        "last case coverage_map_occupancy entries {occupied}/{}; corpus {}; crashes {}. \
         Campaign coverage is the coverage_map_occupancy ratio printed above. \
         Counts exclude the runtime marker at index 0 and are not source-line coverage. source pcguard.",
        session.executor.map_len(),
        session.state.corpus().count(),
        session.state.solutions().count()
    );
    Ok(())
}

/// Run one saved generator input through the same executor path.
pub fn replay(campaign: &Campaign, input_path: &Path) -> Result<(), Error> {
    require_exec(&campaign.target.binary)?;
    let input = GeneratorInput::from_file(input_path)?;
    let mut session = Session::open(campaign)?;
    let kind = session.execute(&input)?;
    print_report(&session, kind);
    Ok(())
}

/// Compile the PCGUARD probe and check that different branches write the shared map.
/// A lone runtime marker at index 0 is not coverage. No perf permission is required.
pub fn doctor() -> Result<(), Error> {
    let probe = compile_probe()?;
    require_exec(&probe)?;
    let tmp = std::env::temp_dir().join(format!("mcn-doctor-{}", std::process::id()));
    let _ = fs::remove_dir_all(&tmp);
    fs::create_dir_all(tmp.join("work"))?;
    let mode_path = tmp.join("work").join("fftp.conf");
    fs::write(&mode_path, b"a")?;
    let campaign = Campaign {
        target: crate::app::config::TargetConfig {
            binary: probe.clone(),
            source_dir: None,
            workdir: tmp.join("work"),
            cleanup_script: None,
            args: vec!["fftp.conf".to_string()],
            configure_options: Vec::new(),
            cli_args: Vec::new(),
            host: "127.0.0.1".to_string(),
            port: 0,
            extra_env: Vec::new(),
            map_size: 65_536,
            debug_child: false,
        },
        timeouts: crate::app::config::Timeouts {
            connect_ms: 200,
            io_ms: 200,
            case_ms: 5_000,
            exit_grace_ms: 1_000,
        },
        campaign: crate::app::config::CampaignPaths {
            seeds: tmp.join("seeds"),
            corpus: tmp.join("corpus"),
            crashes: tmp.join("crashes"),
            python: PathBuf::from("/usr/bin/python3"),
            pythonpath: tmp.clone(),
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
        base: tmp.clone(),
        output_root: None,
    };
    let mut backend = ForkserverBackend::spawn(&campaign, 0)?;
    let allocated = campaign.target.map_size;
    let handshake = backend.map_len();
    println!(
        "doctor: pcguard compiler afl-clang-fast, handshake map {handshake}/{allocated}, source pcguard"
    );
    if handshake == 0 || handshake > allocated {
        return Err(Error::unknown(format!(
            "handshake map size {handshake} does not fit the {allocated} byte allocation"
        )));
    }

    let (child, stop_a, map_a) = run_probe_mode(&mut backend, &mode_path, b'a', true)?;
    let (_again, stop_a2, map_a2) = run_probe_mode(&mut backend, &mode_path, b'a', false)?;
    let (_child_b, stop_b, map_b) = run_probe_mode(&mut backend, &mode_path, b'b', false)?;
    let (_child_m, stop_m, map_m) = run_probe_mode(&mut backend, &mode_path, b'm', false)?;
    let (_child_t, stop_t, map_t) = run_probe_mode(&mut backend, &mode_path, b't', false)?;

    for (name, stop) in [
        ("a", &stop_a),
        ("a2", &stop_a2),
        ("b", &stop_b),
        ("m", &stop_m),
        ("t", &stop_t),
    ] {
        if !matches!(stop, TargetStop::Exited(0)) {
            return Err(Error::unknown(format!(
                "coverage probe mode {name} did not exit 0 ({})",
                stop.label()
            )));
        }
    }

    let edges_a = interesting_indexes(&nonzero_indexes(&map_a));
    let edges_b = interesting_indexes(&nonzero_indexes(&map_b));
    let edges_m = interesting_indexes(&nonzero_indexes(&map_m));
    let edges_t = interesting_indexes(&nonzero_indexes(&map_t));
    println!(
        "doctor: child {child} forkserver {} fuzzer {} marker {} \
         branch A {} entries, B {} entries, main {} entries, thread {} entries, map {}",
        backend.forkserver_pid(),
        std::process::id(),
        map_a.first().copied().unwrap_or(0),
        edges_a.len(),
        edges_b.len(),
        edges_m.len(),
        edges_t.len(),
        handshake
    );
    if edges_a.is_empty() || edges_b.is_empty() {
        return Err(Error::unknown(
            "PCGUARD self-check saw no shared-map entries beyond the runtime marker. \
             map[0] alone is not coverage."
                .to_string(),
        ));
    }
    if symmetric_diff(&edges_a, &edges_b) == 0 {
        return Err(Error::unknown(
            "PCGUARD branches A and B wrote the same shared-map entries".to_string(),
        ));
    }
    if map_a != map_a2 {
        return Err(Error::unknown(
            "PCGUARD counters were not cleared between two runs of branch A".to_string(),
        ));
    }
    if edges_t.is_empty() || symmetric_diff(&edges_t, &edges_m) == 0 {
        return Err(Error::unknown(
            "PCGUARD did not record the worker thread separately from the main thread".to_string(),
        ));
    }
    println!(
        "doctor: shared map recorded different branches, a stable reset, and the worker thread"
    );
    Ok(())
}

fn run_probe_mode(
    backend: &mut ForkserverBackend,
    mode_path: &Path,
    mode: u8,
    check_identity: bool,
) -> Result<(i32, TargetStop, Vec<u8>), Error> {
    fs::write(mode_path, [mode])?;
    backend.prepare_case()?;
    let child = backend.start_case()?;
    if check_identity {
        // The probe can exit before this read. A missing /proc entry or an
        // empty zombie cmdline is not a coverage failure.
        note_probe_identity(child, backend.forkserver_pid())?;
    }
    let deadline = Instant::now() + Duration::from_secs(5);
    let mut stop = None;
    while Instant::now() < deadline {
        if let Some(status) = backend.poll_target(Duration::from_millis(50))? {
            stop = Some(status);
            break;
        }
    }
    let stop = if let Some(stop) = stop {
        backend.finish_case()?;
        stop
    } else {
        backend.abort_case(AbortReason::Deadline)?.stop
    };
    Ok((child, stop, backend.map().to_vec()))
}

/// Forkserver-returned pid must name a distinct child. `/proc` is optional.
fn require_forkserver_pid(child: i32, forkserver: i32) -> Result<(), Error> {
    if child <= 0 || (forkserver > 0 && child == forkserver) || child == std::process::id() as i32 {
        return Err(Error::unknown(format!(
            "forkserver returned probe pid {child}, which is not a distinct child (forkserver {forkserver}, fuzzer {})",
            std::process::id()
        )));
    }
    Ok(())
}

/// Read `/proc` only while the child is still there. A reaped process or a
/// zombie with an empty cmdline does not fail doctor.
fn note_probe_identity(child: i32, forkserver: i32) -> Result<(), Error> {
    require_forkserver_pid(child, forkserver)?;
    let status = match fs::read_to_string(format!("/proc/{child}/status")) {
        Ok(text) => text,
        Err(error) => {
            println!(
                "doctor: identity snapshot skipped for child {child} ({error}); using forkserver pid"
            );
            return Ok(());
        }
    };
    let proc_field = |key: &str| {
        status.lines().find_map(|line| {
            let (name, value) = line.split_once(':')?;
            if name == key {
                Some(value.trim().to_string())
            } else {
                None
            }
        })
    };
    let state = proc_field("State");
    let parent = proc_field("PPid").and_then(|value| value.parse::<i32>().ok());
    let cmdline = fs::read(format!("/proc/{child}/cmdline")).unwrap_or_default();
    let cmdline_text = String::from_utf8_lossy(&cmdline)
        .trim_matches('\0')
        .to_string();
    let live = state.as_deref() != Some("Z") && !cmdline_text.is_empty();
    if !live {
        println!(
            "doctor: identity snapshot incomplete for child {child} (state {state:?}); using forkserver pid"
        );
        return Ok(());
    }
    let parent_ok = forkserver > 0 && parent == Some(forkserver);
    let cmdline_ok = cmdline_text.contains("coverage_probe");
    println!(
        "doctor: identity snapshot child {child} parent {parent:?} parent_ok {parent_ok} cmdline_ok {cmdline_ok}"
    );
    Ok(())
}

fn nonzero_indexes(map: &[u8]) -> Vec<usize> {
    map.iter()
        .enumerate()
        .filter(|(_, byte)| **byte != 0)
        .map(|(index, _)| index)
        .collect()
}

fn interesting_indexes(indexes: &[usize]) -> Vec<usize> {
    indexes
        .iter()
        .copied()
        .filter(|index| *index != 0)
        .collect()
}

fn symmetric_diff(left: &[usize], right: &[usize]) -> usize {
    let left: HashSet<_> = left.iter().copied().collect();
    let right: HashSet<_> = right.iter().copied().collect();
    left.symmetric_difference(&right).count()
}

fn print_report(session: &Session, kind: ExitKind) {
    if let Some(report) = session.executor.last_report() {
        println!(
            "replay: {kind:?} target {} generator {} edges {} coverage {} note {}",
            report.target_status,
            report.generator_status,
            report.edges,
            report.coverage,
            report.note
        );
    }
}

fn require_exec(path: &Path) -> Result<(), Error> {
    let file = fs::read(path).map_err(|error| {
        Error::unknown(format!(
            "reading {}: {error}. Build the campaign target first.",
            path.display()
        ))
    })?;
    if file.len() < 18 || file.get(0..4) != Some(b"\x7fELF") {
        return Err(Error::illegal_argument(format!(
            "{} is not an ELF binary",
            path.display()
        )));
    }
    Ok(())
}

fn compile_probe() -> Result<PathBuf, Error> {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let out_dir = root.join("targets/lightftp/build");
    fs::create_dir_all(&out_dir)?;
    let out = out_dir.join("coverage_probe");
    let afl = root.join(".deps/AFLplusplus");
    let cc = afl.join("afl-clang-fast");
    if !cc.is_file() {
        return Err(Error::unknown(format!(
            "afl-clang-fast is missing at {}",
            cc.display()
        )));
    }
    let output = Command::new(&cc)
        .env("AFL_PATH", &afl)
        .env("AFL_LLVM_INSTRUMENT", "PCGUARD")
        .env("AFL_LLVM_THREADSAFE_INST", "1")
        .env_remove("AFL_LLVM_ONLY_FSRV")
        .env_remove("AFL_GCC_ONLY_FSRV")
        .arg("-std=c11")
        .arg("-O2")
        .arg("-fno-lto")
        .arg("-pthread")
        .arg(root.join("harness/coverage_probe.c"))
        .arg("-o")
        .arg(&out)
        .output()
        .map_err(|error| {
            Error::unknown(format!("afl-clang-fast for the coverage probe: {error}"))
        })?;
    if !output.status.success() {
        return Err(Error::unknown(format!(
            "afl-clang-fast failed to build harness/coverage_probe.c: {}",
            String::from_utf8_lossy(&output.stderr)
        )));
    }
    require_exec(&out)?;
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::{note_probe_identity, require_forkserver_pid};

    #[test]
    fn reaped_or_zombie_snapshot_does_not_fail_doctor() {
        let mut child = std::process::Command::new("true").spawn().unwrap();
        let pid = child.id() as i32;
        let forkserver = pid.wrapping_add(1);
        std::thread::sleep(std::time::Duration::from_millis(50));
        note_probe_identity(pid, forkserver).unwrap();
        let _ = child.wait();
        note_probe_identity(pid, forkserver).unwrap();
        assert!(require_forkserver_pid(0, forkserver).is_err());
        assert!(require_forkserver_pid(std::process::id() as i32, forkserver).is_err());
        assert!(require_forkserver_pid(pid, pid).is_err());
    }
}
