//! Assemble LibAFL and run one campaign.
//!
//! Statistics printed from the map feedback are occupancy of the shared
//! PCGUARD map. They are not a source-coverage percentage.

use std::{
    collections::HashSet,
    fs,
    path::{Path, PathBuf},
    process::Command,
    sync::Arc,
    time::{Duration, Instant},
};

use crate::{
    backend::{AbortReason, ForkserverBackend, TargetBackend},
    coverage::LlvmCoverageStage,
    capture::PcapToMetadataFeedback,
    config::Campaign,
    executor::{CaseExecutor, CoverageFeedback},
    input::GeneratorInput,
    mutator::GeneratorMutator,
    report::TargetStop,
};
use libafl::{
    corpus::{Corpus, OnDiskCorpus},
    events::SimpleEventManager,
    executors::ExitKind,
    feedbacks::{CombinedFeedback, CrashFeedback, LogicEagerOr},
    fuzzer::{Evaluator, ExecutesInput, Fuzzer, NopInputFilter, StdFuzzer},
    inputs::{BytesInputConverter, Input},
    monitors::SimpleMonitor,
    schedulers::QueueScheduler,
    stages::StdMutationalStage,
    state::{HasCorpus, HasSolutions, StdState},
    Error,
};
use libafl_bolts::{rands::StdRand, tuples::tuple_list};

type State =
    StdState<OnDiskCorpus<GeneratorInput>, GeneratorInput, StdRand, OnDiskCorpus<GeneratorInput>>;
type CampaignFeedback = CombinedFeedback<CoverageFeedback, PcapToMetadataFeedback, LogicEagerOr>;
type CampaignObjective = CombinedFeedback<CrashFeedback, PcapToMetadataFeedback, LogicEagerOr>;
type CampaignFuzzer = StdFuzzer<
    QueueScheduler,
    CampaignFeedback,
    BytesInputConverter,
    NopInputFilter,
    CampaignObjective,
>;
type Manager = SimpleEventManager<GeneratorInput, SimpleMonitor<fn(&str)>, State>;

#[path = "accept/mod.rs"]
mod accept;

pub use accept::{accept_job, accept_scenarios};

#[cfg(feature = "stats")]
#[path = "bench.rs"]
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
        let (executor, coverage) = CaseExecutor::launch(campaign)?;
        let engine = executor.capture_engine();
        let mut feedback: CampaignFeedback =
            CombinedFeedback::new(coverage, PcapToMetadataFeedback::new(Arc::clone(&engine)));
        let mut objective: CampaignObjective =
            CombinedFeedback::new(CrashFeedback::new(), PcapToMetadataFeedback::new(engine));
        let state = StdState::new(
            StdRand::with_seed(0x4d43_4e21),
            OnDiskCorpus::new(&campaign.campaign.corpus)?,
            OnDiskCorpus::new(&campaign.campaign.crashes)?,
            &mut feedback,
            &mut objective,
        )?;
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

/// Load seeds and fuzz for `iterations` generations.
pub fn run(campaign: &Campaign, iterations: Option<u64>) -> Result<(), Error> {
    require_exec(&campaign.target.binary)?;
    let mut session = Session::open(campaign)?;
    session.state.load_initial_inputs(
        &mut session.fuzzer,
        &mut session.executor,
        &mut session.mgr,
        &[campaign.campaign.seeds.clone()],
    )?;
    let mut coverage = LlvmCoverageStage::from_campaign(campaign);
    if campaign.source_coverage.enabled {
        coverage.replay_now(&mut session.executor, &mut session.state)?;
    }
    let iterations = iterations.unwrap_or(campaign.campaign.iterations);
    let mut stages = tuple_list!(
        StdMutationalStage::new(GeneratorMutator::new()),
        coverage
    );
    session.fuzzer.fuzz_loop_for(
        &mut stages,
        &mut session.executor,
        &mut session.state,
        &mut session.mgr,
        iterations,
    )?;
    if campaign.source_coverage.enabled {
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
    fs::create_dir_all(tmp.join("fixture"))?;
    let mode_path = tmp.join("work").join("fftp.conf");
    fs::write(&mode_path, b"a")?;
    let campaign = Campaign {
        target: crate::config::TargetConfig {
            binary: probe.clone(),
            workdir: tmp.join("work"),
            fixture: tmp.join("fixture"),
            config_template: tmp.join("fftp.conf"),
            config_output: "fftp.conf".to_string(),
            args: vec!["@CONFIG@".to_string()],
            cert: "certs/my.crt".to_string(),
            key: "certs/my.pem".to_string(),
            ca: None,
            host: "127.0.0.1".to_string(),
            port: 0,
            extra_env: Vec::new(),
            map_size: 65_536,
            debug_child: false,
        },
        timeouts: crate::config::Timeouts {
            connect_ms: 200,
            io_ms: 200,
            case_ms: 5_000,
            exit_grace_ms: 1_000,
        },
        campaign: crate::config::CampaignPaths {
            seeds: tmp.join("seeds"),
            corpus: tmp.join("corpus"),
            crashes: tmp.join("crashes"),
            python: PathBuf::from("/usr/bin/python3"),
            pythonpath: tmp.clone(),
            core: None,
            iterations: 1,
        },
        capture: crate::config::CaptureConfig::default(),
        source_coverage: crate::config::SourceCoverageConfig::default(),
        base: tmp.clone(),
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
