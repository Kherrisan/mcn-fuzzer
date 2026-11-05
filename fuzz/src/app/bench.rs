//! Paired capture throughput benchmark.
//!
//! This module is compiled only with `--features stats`. The default build
//! leaves it out, so phase timings and this command are absent from the
//! normal fuzzer binary.

use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    process::Command,
    time::Instant,
};

use libafl::{
    corpus::Corpus,
    inputs::Input,
    state::{HasCorpus, HasSolutions},
    Error,
};
use serde::Serialize;

#[cfg(test)]
use super::interesting_indexes;
use super::{require_exec, Session};
use crate::{app::config::Campaign, engine::input::GeneratorInput};

/// Five paired capture-off/on trials. The process stays the current user.
/// Capture-on uses the campaign helper with no extra capabilities and no root wrapper.
pub fn bench_capture(campaign: &Campaign, output: &Path) -> Result<(), Error> {
    require_exec(&campaign.target.binary)?;
    let output = absolute_dir(output)?;
    let seeds = sorted_seed_files(&campaign.campaign.seeds)?;
    if seeds.is_empty() {
        return Err(Error::illegal_argument(format!(
            "no seeds in {}",
            campaign.campaign.seeds.display()
        )));
    }
    let loaded: Vec<GeneratorInput> = seeds
        .iter()
        .map(|path| GeneratorInput::from_file(path))
        .collect::<Result<_, _>>()?;
    let mut runs = Vec::new();
    let mut capture_on_failed = None;
    for trial in 0..5u32 {
        let order = if trial % 2 == 0 {
            ["off", "on"]
        } else {
            ["on", "off"]
        };
        for condition in order {
            println!("bench-capture trial {trial} {condition}");
            let run = run_bench_condition(campaign, &output, trial, condition, &seeds, &loaded)?;
            if condition == "on" {
                if let Some(error) = &run.error {
                    capture_on_failed = Some(error.clone());
                }
            }
            runs.push(run);
        }
    }
    write_bench_report(campaign, &output, &seeds, &runs)?;
    if let Some(error) = capture_on_failed {
        return Err(Error::unknown(format!(
            "capture-on preflight failed before any measured case: {error}"
        )));
    }
    Ok(())
}

#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
struct BenchCase {
    index: u32,
    seed: String,
    evaluate_us: u128,
    saved: bool,
    exit_kind: String,
    target_status: String,
    generator_status: String,
    generator_error: Option<String>,
    phases: crate::coverage::capture::PhaseSnapshot,
}

#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
struct BenchRun {
    trial: u32,
    condition: String,
    startup_us: u128,
    /// Measured preflight after a successful open. `None` when `Session::open` failed;
    /// that attempt's cost stays inside `startup_us`.
    preflight_us: Option<u128>,
    warmup_us: u128,
    measured_wall_us: u128,
    error: Option<String>,
    cases: Vec<BenchCase>,
}

fn absolute_dir(path: &Path) -> Result<PathBuf, Error> {
    fs::create_dir_all(path)?;
    fs::canonicalize(path)
        .map_err(|error| Error::os_error(error, format!("canonicalizing {}", path.display())))
}

fn prepare_bench_campaign(
    campaign: &Campaign,
    output: &Path,
    trial: u32,
    condition: &str,
) -> Result<Campaign, Error> {
    let mut campaign = campaign.clone();
    campaign.capture.enabled = condition == "on";
    let scratch_path = output.join(format!("trial-{trial}-{condition}"));
    if scratch_path.exists() {
        fs::remove_dir_all(&scratch_path)?;
    }
    let scratch = absolute_dir(&scratch_path)?;
    let work = absolute_dir(&scratch.join("work"))?;
    campaign.base = scratch.clone();
    campaign.campaign.corpus = scratch.join("corpus");
    campaign.campaign.crashes = scratch.join("crashes");
    campaign.target.workdir = work;
    // The forkserver chdirs into workdir before exec. A relative binary or
    // `@CONFIG@` would be resolved from that directory and miss the real files.
    campaign.target.binary = absolute_path(&campaign.target.binary)?;
    campaign.target.fixture = absolute_path(&campaign.target.fixture)?;
    campaign.target.config_template = absolute_path(&campaign.target.config_template)?;
    // Do not follow the venv symlink. `python` points at the system interpreter;
    // executing that path drops the venv and cannot import cbor2.
    campaign.campaign.python = absolute_symlink(&campaign.campaign.python)?;
    campaign.campaign.pythonpath = absolute_path(&campaign.campaign.pythonpath)?;
    Ok(campaign)
}

fn absolute_symlink(path: &Path) -> Result<PathBuf, Error> {
    let name = path.file_name().ok_or_else(|| {
        Error::illegal_argument(format!("path {} has no file name", path.display()))
    })?;
    let parent = match path.parent() {
        Some(parent) if !parent.as_os_str().is_empty() => parent,
        _ => Path::new("."),
    };
    if !path.is_file() {
        return Err(Error::illegal_argument(format!(
            "path {} is missing",
            path.display()
        )));
    }
    Ok(absolute_path(parent)?.join(name))
}

fn absolute_path(path: &Path) -> Result<PathBuf, Error> {
    fs::canonicalize(path)
        .map_err(|error| Error::os_error(error, format!("canonicalizing {}", path.display())))
}

fn run_bench_condition(
    campaign: &Campaign,
    output: &Path,
    trial: u32,
    condition: &str,
    seed_paths: &[PathBuf],
    seeds: &[GeneratorInput],
) -> Result<BenchRun, Error> {
    let campaign = prepare_bench_campaign(campaign, output, trial, condition)?;
    let startup_at = Instant::now();
    let mut session = match Session::open(&campaign) {
        Ok(session) => session,
        Err(error) => {
            return Ok(BenchRun {
                trial,
                condition: condition.to_string(),
                startup_us: startup_at.elapsed().as_micros(),
                preflight_us: None,
                warmup_us: 0,
                measured_wall_us: 0,
                error: Some(format!("{error:?}")),
                cases: Vec::new(),
            });
        }
    };
    let startup_us = startup_at.elapsed().as_micros();
    let preflight_us = Some(session.executor.preflight_us());
    let warmup_at = Instant::now();
    for input in seeds {
        session.evaluate(input)?;
    }
    let warmup_us = warmup_at.elapsed().as_micros();
    let measured_at = Instant::now();
    let mut cases = Vec::new();
    for index in 0..60u32 {
        let seed_index = index as usize % seeds.len();
        let before = session.state.corpus().count() + session.state.solutions().count();
        let case_at = Instant::now();
        session.evaluate(&seeds[seed_index])?;
        let saved = session.state.corpus().count() + session.state.solutions().count() > before;
        let report = session.executor.last_report().cloned();
        cases.push(BenchCase {
            index,
            seed: seed_paths[seed_index]
                .file_name()
                .map(|name| name.to_string_lossy().into_owned())
                .unwrap_or_default(),
            evaluate_us: case_at.elapsed().as_micros(),
            saved,
            exit_kind: report
                .as_ref()
                .map(|item| item.exit_kind.clone())
                .unwrap_or_default(),
            target_status: report
                .as_ref()
                .map(|item| item.target_status.clone())
                .unwrap_or_default(),
            generator_status: report
                .as_ref()
                .map(|item| item.generator_status.clone())
                .unwrap_or_default(),
            generator_error: report.and_then(|item| item.generator_error),
            phases: session.executor.phase_snapshot(),
        });
    }
    Ok(BenchRun {
        trial,
        condition: condition.to_string(),
        startup_us,
        preflight_us,
        warmup_us,
        measured_wall_us: measured_at.elapsed().as_micros(),
        error: None,
        cases,
    })
}

/// One supervised trial side. The caller must already be the target user.
pub fn execute_bench_job(
    campaign: &Campaign,
    output: &Path,
    trial: u32,
    condition: &str,
) -> Result<(), Error> {
    let seed_paths = sorted_seed_files(&campaign.campaign.seeds)?;
    let seeds = seed_paths
        .iter()
        .map(|path| GeneratorInput::from_file(path))
        .collect::<Result<Vec<_>, _>>()?;
    let run = run_bench_condition(campaign, output, trial, condition, &seed_paths, &seeds)?;
    fs::create_dir_all(output)?;
    let path = output.join(format!("job-{trial}-{condition}.json"));
    let encoded = serde_json::to_vec_pretty(&run)
        .map_err(|error| Error::serialize(format!("bench job json: {error}")))?;
    fs::write(&path, encoded)?;
    if let Some(error) = run.error {
        return Err(Error::unknown(error));
    }
    Ok(())
}

/// Write the paired report from per-job records. Missing jobs stay failures.
pub fn finalize_bench(campaign: &Campaign, output: &Path) -> Result<(), Error> {
    let seed_paths = sorted_seed_files(&campaign.campaign.seeds)?;
    let mut runs = Vec::new();
    for trial in 0..5u32 {
        let order = if trial % 2 == 0 {
            ["off", "on"]
        } else {
            ["on", "off"]
        };
        for condition in order {
            let path = output.join(format!("job-{trial}-{condition}.json"));
            let run = if path.is_file() {
                let bytes = fs::read(&path)?;
                serde_json::from_slice(&bytes)
                    .map_err(|error| Error::serialize(format!("{}: {error}", path.display())))?
            } else {
                BenchRun {
                    trial,
                    condition: condition.to_string(),
                    startup_us: 0,
                    preflight_us: None,
                    warmup_us: 0,
                    measured_wall_us: 0,
                    error: Some(format!(
                        "job {trial} {condition} produced no record; not a measured sample"
                    )),
                    cases: Vec::new(),
                }
            };
            runs.push(run);
        }
    }
    write_bench_report(campaign, output, &seed_paths, &runs)?;
    if runs.iter().any(|run| run.error.is_some()) {
        return Err(Error::unknown(
            "one or more bench jobs failed; the report keeps those failures".to_string(),
        ));
    }
    Ok(())
}

fn sorted_seed_files(dir: &Path) -> Result<Vec<PathBuf>, Error> {
    let mut paths = Vec::new();
    for entry in fs::read_dir(dir)? {
        let path = entry?.path();
        if path.extension().and_then(|ext| ext.to_str()) == Some("json") {
            paths.push(path);
        }
    }
    paths.sort();
    Ok(paths)
}

fn write_bench_report(
    campaign: &Campaign,
    output: &Path,
    seeds: &[PathBuf],
    runs: &[BenchRun],
) -> Result<(), Error> {
    let version = crate::coverage::capture::linked_pcap_version();
    let uid = unsafe { libc::geteuid() };
    let startup_identity =
        format!("euid {uid}; capture opens lo inside the namespace before setuid");
    let kernel = command_text("uname", &[std::ffi::OsStr::new("-r")]);
    let seed_hash = command_text(
        "sha256sum",
        &seeds
            .iter()
            .map(|path| path.as_os_str())
            .collect::<Vec<_>>(),
    );
    let profile = if cfg!(debug_assertions) {
        "debug"
    } else {
        "release"
    };
    let mut jsonl = String::new();
    jsonl.push_str(&serde_json::json!({
        "kind": "environment",
        "uid": uid,
        "kernel": kernel.trim(),
        "build_profile": profile,
        "capture_backend": version,
        "proc_status": startup_identity.trim(),
        "file_limit": campaign.capture.file_limit,
        "compression_level": crate::coverage::capture::COMPRESSION_LEVEL,
        "timeouts": {
            "connect_ms": campaign.timeouts.connect_ms,
            "io_ms": campaign.timeouts.io_ms,
            "case_ms": campaign.timeouts.case_ms,
            "exit_grace_ms": campaign.timeouts.exit_grace_ms,
        },
        "seed_hash": seed_hash.trim(),
        "binary": campaign.target.binary,
        "note": "mcn-fuzz and the helper were started as this uid. No CAP_SYS_ADMIN, CAP_SYS_PTRACE, or sudo wrapper was added.",
    }).to_string());
    jsonl.push('\n');
    for run in runs {
        let stats = success_stats(&run.cases, run.measured_wall_us);
        jsonl.push_str(
            &serde_json::json!({
                "kind": "condition",
                "trial": run.trial,
                "condition": run.condition,
                "startup_us": run.startup_us,
                "preflight_us": run.preflight_us,
                "warmup_us": run.warmup_us,
                "measured_wall_us": run.measured_wall_us,
                "cases": stats.measured,
                "status_ok": stats.ok,
                "expected": stats.expected,
                "anomalous": stats.anomalous,
                "outcomes": outcome_counts(&run.cases),
                "wall_cases_per_s": stats.wall_cases_per_s,
                "ok_per_wall": stats.ok_per_wall,
                "error": run.error,
            })
            .to_string(),
        );
        jsonl.push('\n');
        for case in &run.cases {
            jsonl.push_str(
                &serde_json::json!({
                    "kind": "case",
                    "trial": run.trial,
                    "condition": run.condition,
                    "index": case.index,
                    "seed": case.seed,
                    "evaluate_us": case.evaluate_us,
                    "saved": case.saved,
                    "expected": is_expected_outcome(case),
                    "exit_kind": case.exit_kind,
                    "target_status": case.target_status,
                    "generator_status": case.generator_status,
                    "generator_error": case.generator_error,
                    "target_setup_us": case.phases.target_setup_us,
                    "capture_ready_us": case.phases.capture_ready_us,
                    "interaction_us": case.phases.interaction_us,
                    "tail_drain_us": case.phases.tail_drain_us,
                    "stop_reap_us": case.phases.stop_reap_us,
                    "pcap_read_us": case.phases.pcap_read_us,
                    "zlib_us": case.phases.zlib_us,
                    "persist_us": case.phases.persist_us,
                    "uncompressed_len": case.phases.uncompressed_len,
                    "compressed_len": case.phases.compressed_len,
                    "packets_captured": case.phases.packets_captured,
                    "packets_received": case.phases.packets_received,
                    "packets_dropped": case.phases.packets_dropped,
                    "capture_status": case.phases.status,
                })
                .to_string(),
            );
            jsonl.push('\n');
        }
    }
    for pair in pair_metrics(runs) {
        jsonl.push_str(
            &serde_json::to_string(&pair)
                .map_err(|error| Error::serialize(format!("pair metric: {error}")))?,
        );
        jsonl.push('\n');
    }
    for summary in capture_summaries(runs) {
        jsonl.push_str(
            &serde_json::to_string(&summary)
                .map_err(|error| Error::serialize(format!("capture summary: {error}")))?,
        );
        jsonl.push('\n');
    }
    fs::write(output.join("results.jsonl"), jsonl)?;
    fs::write(
        output.join("report.md"),
        bench_markdown(
            campaign,
            uid,
            profile,
            &startup_identity,
            &version,
            &kernel,
            &seed_hash,
            runs,
        ),
    )?;
    Ok(())
}

fn bench_markdown(
    campaign: &Campaign,
    uid: u32,
    profile: &str,
    caps: &str,
    version: &str,
    kernel: &str,
    seed_hash: &str,
    runs: &[BenchRun],
) -> String {
    let mut text = String::new();
    text.push_str("# PCAP capture throughput\n\n");
    text.push_str(&format!("- uid: {uid}\n"));
    text.push_str(&format!("- kernel: {}\n", kernel.trim()));
    text.push_str(&format!("- build profile: {profile}\n"));
    text.push_str(&format!("- target: {}\n", campaign.target.binary.display()));
    text.push_str(&format!(
        "- capture backend: {}\n",
        version.trim().replace('\n', " ")
    ));
    text.push_str(&format!("- startup identity: {}\n", caps.trim()));
    let policy = crate::runtime::lifecycle::job_policy();
    let case_timeout = policy
        .effective_case_timeout(campaign.timeouts.case_ms)
        .unwrap_or(std::time::Duration::from_millis(campaign.timeouts.case_ms));
    text.push_str(&format!("- lifecycle: {}\n", policy.describe(case_timeout)));
    text.push_str(&format!("- file limit: {}\n", campaign.capture.file_limit));
    text.push_str(&format!(
        "- zlib level: {}\n",
        crate::coverage::capture::COMPRESSION_LEVEL
    ));
    text.push_str(&format!(
        "- timeouts ms: connect {} io {} case {} exit {}\n",
        campaign.timeouts.connect_ms,
        campaign.timeouts.io_ms,
        campaign.timeouts.case_ms,
        campaign.timeouts.exit_grace_ms
    ));
    text.push_str(&format!("- seed sha256:\n```\n{}\n```\n", seed_hash.trim()));
    text.push_str("\nThe process is started with sudo, then the supervisor and PID 1 drop to this uid before outputs and cases. Capture uses the linked libpcap on lo.\n\n");
    text.push_str("Wall-clock cases/s uses every measured case. Case mean/p50/p95 and paired latency increase use expected outcomes only: the seed's expected generator status and `target_status=exited:0`. Anomalous statuses are excluded. `ok/wall` separately counts `generator_status=ok`.\n\n");
    append_condition_table(&mut text, runs);
    append_preflight_failures(&mut text, runs);
    append_pair_summary(&mut text, runs);
    append_capture_summaries(&mut text, runs);
    text.push_str("\n## Phase share\n\n");
    text.push_str("Steady-state rows below use every measured case. Warmup and preflight are excluded. These rows are phase cost, not throughput.\n\n");
    for condition in ["off", "on"] {
        let cases: Vec<&BenchCase> = runs
            .iter()
            .filter(|run| run.condition == condition)
            .flat_map(|run| run.cases.iter())
            .collect();
        if cases.is_empty() {
            text.push_str(&format!("### {condition}\n\nNo measured cases.\n\n"));
            continue;
        }
        text.push_str(&format!("### {condition}\n\n"));
        text.push_str("| phase | total s | mean ms | p50 ms | p95 ms | share |\n| --- | --- | --- | --- | --- | --- |\n");
        let total_eval: u128 = cases.iter().map(|case| case.evaluate_us).sum();
        for (name, values) in phase_columns(&cases) {
            let total: u128 = values.iter().sum();
            let share = if total_eval == 0 {
                0.0
            } else {
                total as f64 / total_eval as f64
            };
            text.push_str(&format!(
                "| {name} | {:.3} | {} | {} | {} | {:.1}% |\n",
                total as f64 / 1_000_000.0,
                ms_stat(&values, Stat::Mean),
                ms_stat(&values, Stat::P50),
                ms_stat(&values, Stat::P95),
                share * 100.0
            ));
        }
        let saved = cases.iter().filter(|case| case.saved).count();
        text.push_str(&format!(
            "\nSaved testcases during measured cases: {saved}.\n\n"
        ));
    }
    text.push_str("OpenSSL latency includes its fixed seed cycle. Compare capture-on with the paired capture-off run only.\n");
    text
}

/// Generator status this seed is supposed to produce.
///
/// OpenSSL `illegal.json` expects a TLS alert and `truncated.json` expects a
/// receive timeout. Every other seed, including LightFTP, expects `ok`.
fn expected_generator_status(seed: &str) -> &'static str {
    match seed {
        "illegal.json" => "alert",
        "truncated.json" => "timeout",
        _ => "ok",
    }
}

fn is_status_ok(case: &BenchCase) -> bool {
    case.generator_status == "ok"
}

fn is_expected_outcome(case: &BenchCase) -> bool {
    case.target_status == "exited:0"
        && case.generator_status == expected_generator_status(&case.seed)
}

fn outcome_counts(cases: &[BenchCase]) -> String {
    let mut counts: BTreeMap<String, usize> = BTreeMap::new();
    for case in cases {
        *counts.entry(case.generator_status.clone()).or_insert(0) += 1;
    }
    if counts.is_empty() {
        return "-".to_string();
    }
    counts
        .iter()
        .map(|(status, count)| format!("{status}×{count}"))
        .collect::<Vec<_>>()
        .join(", ")
}

struct SuccessStats {
    measured: usize,
    /// Cases whose generator status is `ok`.
    ok: usize,
    /// Expected generator status and `target_status=exited:0`.
    expected: usize,
    /// Cases that are not the expected outcome. LightFTP `cancelled` is anomalous.
    anomalous: usize,
    /// Measured cases divided by `measured_wall_us`. Every outcome stays in the denominator.
    wall_cases_per_s: Option<f64>,
    /// `generator_status=ok` cases divided by the same wall.
    ok_per_wall: Option<f64>,
    /// Mean/p50/p95 of expected outcomes, not of `generator_status=ok` alone.
    mean_us: Option<u128>,
    p50_us: Option<u128>,
    p95_us: Option<u128>,
}

fn per_wall(count: usize, measured: usize, wall_us: u128) -> Option<f64> {
    if measured == 0 || wall_us == 0 {
        None
    } else {
        Some(count as f64 / (wall_us as f64 / 1_000_000.0))
    }
}

fn success_stats(cases: &[BenchCase], wall_us: u128) -> SuccessStats {
    let measured = cases.len();
    let ok = cases.iter().filter(|case| is_status_ok(case)).count();
    let latencies: Vec<u128> = cases
        .iter()
        .filter(|case| is_expected_outcome(case))
        .map(|case| case.evaluate_us)
        .collect();
    let expected = latencies.len();
    let anomalous = measured - expected;
    let (mean_us, p50_us, p95_us) = if expected == 0 {
        (None, None, None)
    } else {
        let sum: u128 = latencies.iter().sum();
        (
            Some(sum / expected as u128),
            Some(percentile(&latencies, 0.50)),
            Some(percentile(&latencies, 0.95)),
        )
    };
    SuccessStats {
        measured,
        ok,
        expected,
        anomalous,
        wall_cases_per_s: per_wall(measured, measured, wall_us),
        ok_per_wall: per_wall(ok, measured, wall_us),
        mean_us,
        p50_us,
        p95_us,
    }
}

fn fmt_rate(rate: Option<f64>) -> String {
    match rate {
        Some(value) => format!("{value:.3}"),
        None => "-".to_string(),
    }
}

fn fmt_ms(value: Option<u128>) -> String {
    match value {
        Some(micros) => format!("{:.1}", micros as f64 / 1000.0),
        None => "-".to_string(),
    }
}

fn fmt_optional_ms(value: Option<u128>) -> String {
    match value {
        Some(micros) => format!("{:.1}", micros as f64 / 1000.0),
        None => "N/A".to_string(),
    }
}

fn fmt_pct(value: Option<f64>) -> String {
    match value {
        Some(value) if value.is_finite() => format!("{:.1}%", value * 100.0),
        _ => "-".to_string(),
    }
}

fn append_condition_table(text: &mut String, runs: &[BenchRun]) {
    text.push_str("## Conditions\n\n");
    text.push_str("Wall-clock cases/s is measured cases / `measured_wall_us` and includes every outcome. An expected outcome has the seed's generator status and `target_status=exited:0`. `illegal.json` expects alert and `truncated.json` expects timeout. Other seeds expect ok, so a LightFTP `cancelled` case is anomalous. A target crash is anomalous even when the generator status matches. Case mean/p50/p95 use expected outcomes only. ok/wall counts `generator_status=ok` only.\n\n");
    text.push_str("startup ms is the whole `Session::open`. When open fails, that figure includes the failed capture preflight and preflight ms is N/A, because the failed open does not return a separate preflight duration. When open succeeds, preflight ms is the measured preflight and is included in startup ms.\n\n");
    text.push_str("| trial | condition | measured | expected | anomalous | outcomes | wall s | wall cases/s | ok/wall | expected mean ms | expected p50 ms | expected p95 ms | startup ms | preflight ms | warmup ms | open |\n");
    text.push_str("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n");
    for run in runs {
        let stats = success_stats(&run.cases, run.measured_wall_us);
        let open = if run.error.is_some() {
            "preflight failed"
        } else {
            ""
        };
        text.push_str(&format!(
            "| {} | {} | {} | {} | {} | {} | {:.3} | {} | {} | {} | {} | {} | {:.1} | {} | {:.1} | {open} |\n",
            run.trial,
            run.condition,
            stats.measured,
            stats.expected,
            stats.anomalous,
            outcome_counts(&run.cases),
            run.measured_wall_us as f64 / 1_000_000.0,
            fmt_rate(stats.wall_cases_per_s),
            fmt_rate(stats.ok_per_wall),
            fmt_ms(stats.mean_us),
            fmt_ms(stats.p50_us),
            fmt_ms(stats.p95_us),
            run.startup_us as f64 / 1000.0,
            fmt_optional_ms(run.preflight_us),
            run.warmup_us as f64 / 1000.0,
        ));
    }
    text.push_str("\nExpected generator status by seed:\n\n");
    text.push_str(
        "- `illegal.json`: alert. A TLS alert is the expected SUT response, not an anomaly.\n",
    );
    text.push_str("- `truncated.json`: timeout. `recv timed out` is the expected SUT response, not an anomaly.\n");
    text.push_str("- every other seed, including all LightFTP seeds: ok. `cancelled` and any other generator status are anomalies.\n");
    text.push_str("- every seed also requires `target_status=exited:0`. A crash is not an expected outcome.\n\n");
}

fn append_preflight_failures(text: &mut String, runs: &[BenchRun]) {
    let failed: Vec<&BenchRun> = runs
        .iter()
        .filter(|run| run.condition == "on" && run.error.is_some())
        .collect();
    if failed.is_empty() {
        return;
    }
    text.push_str("## Preflight failure\n\n");
    text.push_str("Capture-on startup preflight failed before the first case. No capture-on workload was executed. The recorded error is below.\n\n");
    for run in failed {
        text.push_str(&format!(
            "Trial {}:\n\n```\n{}\n```\n\n",
            run.trial,
            run.error.as_deref().unwrap_or("")
        ));
    }
}

#[derive(Serialize)]
struct PairMetric {
    kind: &'static str,
    trial: u32,
    off_measured: usize,
    on_measured: usize,
    off_ok: usize,
    on_ok: usize,
    off_anomalous: usize,
    on_anomalous: usize,
    off_wall_cases_per_s: Option<f64>,
    on_wall_cases_per_s: Option<f64>,
    wall_throughput_loss: Option<f64>,
    off_ok_per_wall: Option<f64>,
    on_ok_per_wall: Option<f64>,
    ok_per_wall_loss: Option<f64>,
    off_mean_us: Option<u128>,
    on_mean_us: Option<u128>,
    latency_increase: Option<f64>,
    note: String,
}

fn pair_metrics(runs: &[BenchRun]) -> Vec<PairMetric> {
    let mut trials: Vec<u32> = runs.iter().map(|run| run.trial).collect();
    trials.sort_unstable();
    trials.dedup();
    trials
        .into_iter()
        .map(|trial| {
            let off = runs
                .iter()
                .find(|run| run.trial == trial && run.condition == "off");
            let on = runs
                .iter()
                .find(|run| run.trial == trial && run.condition == "on");
            let off_wall = off.map(|run| run.measured_wall_us).unwrap_or(0);
            let on_wall = on.map(|run| run.measured_wall_us).unwrap_or(0);
            let off_stats = success_stats(off.map(|run| run.cases.as_slice()).unwrap_or(&[]), off_wall);
            let on_stats = success_stats(on.map(|run| run.cases.as_slice()).unwrap_or(&[]), on_wall);
            let on_samples = on.is_some_and(|run| run.error.is_none() && !run.cases.is_empty());
            let mut notes = Vec::new();
            let (wall_throughput_loss, ok_per_wall_loss, latency_increase) = if !on_samples {
                notes.push("not computable: no valid capture-on sample".to_string());
                (None, None, None)
            } else {
                let wall_loss = relative_loss(off_stats.wall_cases_per_s, on_stats.wall_cases_per_s);
                let ok_loss = relative_loss(off_stats.ok_per_wall, on_stats.ok_per_wall);
                let increase = match (off_stats.mean_us, on_stats.mean_us) {
                    (Some(off_mean), Some(on_mean)) if off_mean > 0 => {
                        Some((on_mean as f64 - off_mean as f64) / off_mean as f64)
                    }
                    _ => None,
                };
                if wall_loss.is_none() {
                    notes.push("wall-clock throughput loss is not computable: measured wall is zero or a side has no measured cases".to_string());
                }
                if ok_loss.is_none() {
                    notes.push("ok/wall loss is not computable: capture-off has no generator_status ok case or measured wall is zero".to_string());
                }
                if increase.is_none() {
                    notes.push("latency increase is not computable: a side has no expected outcome".to_string());
                }
                (wall_loss, ok_loss, increase)
            };
            PairMetric {
                kind: "pair",
                trial,
                off_measured: off_stats.measured,
                on_measured: if on_samples { on_stats.measured } else { 0 },
                off_ok: off_stats.ok,
                on_ok: if on_samples { on_stats.ok } else { 0 },
                off_anomalous: off_stats.anomalous,
                on_anomalous: if on_samples { on_stats.anomalous } else { 0 },
                off_wall_cases_per_s: off_stats.wall_cases_per_s,
                on_wall_cases_per_s: if on_samples { on_stats.wall_cases_per_s } else { None },
                wall_throughput_loss,
                off_ok_per_wall: off_stats.ok_per_wall,
                on_ok_per_wall: if on_samples { on_stats.ok_per_wall } else { None },
                ok_per_wall_loss,
                off_mean_us: off_stats.mean_us,
                on_mean_us: if on_samples { on_stats.mean_us } else { None },
                latency_increase,
                note: notes.join("; "),
            }
        })
        .collect()
}

fn relative_loss(off_rate: Option<f64>, on_rate: Option<f64>) -> Option<f64> {
    match (off_rate, on_rate) {
        (Some(off_rate), Some(on_rate)) if off_rate > 0.0 => Some((off_rate - on_rate) / off_rate),
        _ => None,
    }
}

fn mean_if_complete(values: &[Option<f64>]) -> Option<f64> {
    if values.is_empty() || values.iter().any(Option::is_none) {
        None
    } else {
        Some(values.iter().flatten().sum::<f64>() / values.len() as f64)
    }
}

fn append_pair_summary(text: &mut String, runs: &[BenchRun]) {
    text.push_str("## Paired capture-on versus capture-off\n\n");
    text.push_str("Wall-clock throughput loss is `(off wall cases/s - on wall cases/s) / off wall cases/s`, where wall cases/s is measured cases / `measured_wall_us`. ok/wall counts `generator_status=ok` over that same wall. Latency increase is `(on expected mean - off expected mean) / off expected mean` and uses expected outcomes only, including OpenSSL alert and timeout when the target exited 0. Anomalous statuses are excluded.\n\n");
    text.push_str("| trial | off measured | off ok | off anomalous | on measured | on ok | on anomalous | off wall cases/s | on wall cases/s | wall throughput loss | off ok/wall | on ok/wall | ok/wall loss | off expected mean ms | on expected mean ms | latency increase | note |\n");
    text.push_str("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n");
    let pairs = pair_metrics(runs);
    for pair in &pairs {
        let wall_loss = pair
            .wall_throughput_loss
            .map(|value| fmt_pct(Some(value)))
            .unwrap_or_else(|| "not computable".to_string());
        let ok_loss = pair
            .ok_per_wall_loss
            .map(|value| fmt_pct(Some(value)))
            .unwrap_or_else(|| "not computable".to_string());
        let latency = pair
            .latency_increase
            .map(|value| fmt_pct(Some(value)))
            .unwrap_or_else(|| "not computable".to_string());
        text.push_str(&format!(
            "| {} | {} | {} | {} | {} | {} | {} | {} | {} | {wall_loss} | {} | {} | {ok_loss} | {} | {} | {latency} | {} |\n",
            pair.trial,
            pair.off_measured,
            pair.off_ok,
            pair.off_anomalous,
            pair.on_measured,
            pair.on_ok,
            pair.on_anomalous,
            fmt_rate(pair.off_wall_cases_per_s),
            fmt_rate(pair.on_wall_cases_per_s),
            fmt_rate(pair.off_ok_per_wall),
            fmt_rate(pair.on_ok_per_wall),
            fmt_ms(pair.off_mean_us),
            fmt_ms(pair.on_mean_us),
            pair.note,
        ));
    }
    text.push('\n');
    let wall_mean = mean_if_complete(
        &pairs
            .iter()
            .map(|pair| pair.wall_throughput_loss)
            .collect::<Vec<_>>(),
    );
    let ok_mean = mean_if_complete(
        &pairs
            .iter()
            .map(|pair| pair.ok_per_wall_loss)
            .collect::<Vec<_>>(),
    );
    let latency_mean = mean_if_complete(
        &pairs
            .iter()
            .map(|pair| pair.latency_increase)
            .collect::<Vec<_>>(),
    );
    text.push_str(&format!(
        "Five-pair mean wall-clock throughput loss: {}. Five-pair mean ok/wall loss: {}. Five-pair mean latency increase: {}.\n\n",
        wall_mean.map(|value| fmt_pct(Some(value))).unwrap_or_else(|| "not computable: no valid capture-on sample or measured wall is zero".to_string()),
        ok_mean.map(|value| fmt_pct(Some(value))).unwrap_or_else(|| "not computable: no valid capture-on sample or capture-off has no generator_status ok case".to_string()),
        latency_mean.map(|value| fmt_pct(Some(value))).unwrap_or_else(|| "not computable: no valid capture-on sample or a side has no expected outcome".to_string()),
    ));
}

#[derive(Serialize)]
struct CaptureSummary {
    kind: &'static str,
    scope: &'static str,
    condition: String,
    seed: String,
    cases: usize,
    expected: usize,
    anomalous: usize,
    statuses: String,
    packets_captured: u64,
    packets_received: u64,
    packets_dropped: u64,
    uncompressed: u64,
    compressed: u64,
    ratio: Option<f64>,
}

fn capture_summaries(runs: &[BenchRun]) -> Vec<CaptureSummary> {
    let mut out = Vec::new();
    for condition in ["off", "on"] {
        let cases: Vec<&BenchCase> = runs
            .iter()
            .filter(|run| run.condition == condition)
            .flat_map(|run| run.cases.iter())
            .collect();
        if cases.is_empty() {
            continue;
        }
        out.push(summarize_capture("case", condition, "", &cases));
        let mut seeds: Vec<String> = cases.iter().map(|case| case.seed.clone()).collect();
        seeds.sort();
        seeds.dedup();
        for seed in seeds {
            let selected: Vec<&BenchCase> = cases
                .iter()
                .copied()
                .filter(|case| case.seed == seed)
                .collect();
            out.push(summarize_capture("seed", condition, &seed, &selected));
        }
    }
    out
}

fn summarize_capture(
    scope: &'static str,
    condition: &str,
    seed: &str,
    cases: &[&BenchCase],
) -> CaptureSummary {
    let mut statuses: BTreeMap<String, usize> = BTreeMap::new();
    for case in cases {
        let key = format!("{}/{}", case.generator_status, case.target_status);
        *statuses.entry(key).or_insert(0) += 1;
    }
    let status_text = statuses
        .iter()
        .map(|(key, count)| format!("{key}×{count}"))
        .collect::<Vec<_>>()
        .join(", ");
    let uncompressed = cases.iter().map(|case| case.phases.uncompressed_len).sum();
    let compressed = cases.iter().map(|case| case.phases.compressed_len).sum();
    CaptureSummary {
        kind: "capture_summary",
        scope,
        condition: condition.to_string(),
        seed: seed.to_string(),
        cases: cases.len(),
        expected: cases
            .iter()
            .filter(|case| is_expected_outcome(case))
            .count(),
        anomalous: cases
            .iter()
            .filter(|case| !is_expected_outcome(case))
            .count(),
        statuses: status_text,
        packets_captured: cases.iter().map(|case| case.phases.packets_captured).sum(),
        packets_received: cases.iter().map(|case| case.phases.packets_received).sum(),
        packets_dropped: cases.iter().map(|case| case.phases.packets_dropped).sum(),
        uncompressed,
        compressed,
        ratio: if uncompressed == 0 {
            None
        } else {
            Some(compressed as f64 / uncompressed as f64)
        },
    }
}

fn append_capture_summaries(text: &mut String, runs: &[BenchRun]) {
    text.push_str("## Packets, drops, and size\n\n");
    let on_cases = runs
        .iter()
        .filter(|run| run.condition == "on")
        .map(|run| run.cases.len())
        .sum::<usize>();
    if on_cases == 0 {
        text.push_str("Capture-on packet, drop, size, and ratio summaries per seed and per case are not computable: no valid capture-on sample.\n\n");
    }
    let summaries = capture_summaries(runs);
    if summaries.is_empty() {
        return;
    }
    text.push_str("| scope | condition | seed | cases | expected | anomalous | generator/target | captured | received | dropped | uncompressed | compressed | ratio |\n");
    text.push_str(
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n",
    );
    for summary in &summaries {
        let ratio = match summary.ratio {
            Some(value) => format!("{value:.3}"),
            None => "not computable: uncompressed size is 0".to_string(),
        };
        let seed = if summary.seed.is_empty() {
            "-"
        } else {
            summary.seed.as_str()
        };
        text.push_str(&format!(
            "| {} | {} | {seed} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {ratio} |\n",
            summary.scope,
            summary.condition,
            summary.cases,
            summary.expected,
            summary.anomalous,
            summary.statuses.replace('|', "/"),
            summary.packets_captured,
            summary.packets_received,
            summary.packets_dropped,
            summary.uncompressed,
            summary.compressed,
        ));
    }
    text.push('\n');
}

enum Stat {
    Mean,
    P50,
    P95,
}

fn ms_stat(values: &[u128], stat: Stat) -> String {
    if values.is_empty() {
        return "-".to_string();
    }
    let value = match stat {
        Stat::Mean => values.iter().sum::<u128>() / values.len() as u128,
        Stat::P50 => percentile(values, 0.50),
        Stat::P95 => percentile(values, 0.95),
    };
    format!("{:.1}", value as f64 / 1000.0)
}

fn percentile(values: &[u128], fraction: f64) -> u128 {
    let mut sorted = values.to_vec();
    sorted.sort_unstable();
    let index = ((sorted.len() - 1) as f64 * fraction).round() as usize;
    sorted[index.min(sorted.len() - 1)]
}

fn phase_columns(cases: &[&BenchCase]) -> Vec<(&'static str, Vec<u128>)> {
    let column = |pick: fn(&BenchCase) -> u128| cases.iter().map(|case| pick(case)).collect();
    vec![
        ("evaluate", column(|case| case.evaluate_us)),
        ("target setup", column(|case| case.phases.target_setup_us)),
        ("capture ready", column(|case| case.phases.capture_ready_us)),
        ("interaction", column(|case| case.phases.interaction_us)),
        ("tail and drain", column(|case| case.phases.tail_drain_us)),
        ("stop to reap", column(|case| case.phases.stop_reap_us)),
        ("pcap read", column(|case| case.phases.pcap_read_us)),
        ("zlib", column(|case| case.phases.zlib_us)),
        ("persist", column(|case| case.phases.persist_us)),
    ]
}

fn command_text(program: &str, args: &[&std::ffi::OsStr]) -> String {
    match Command::new(program).args(args).output() {
        Ok(output) => format!(
            "{}{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        ),
        Err(error) => format!("{program} failed: {error}"),
    }
}

#[cfg(test)]
mod tests {
    use super::{
        absolute_dir, append_capture_summaries, append_condition_table, append_pair_summary,
        append_preflight_failures, prepare_bench_campaign, BenchCase, BenchRun,
    };
    use crate::app::config::Campaign;
    use crate::engine::input::GeneratorInput;
    use libafl::inputs::Input;
    use std::path::{Path, PathBuf};

    fn phases() -> crate::coverage::capture::PhaseSnapshot {
        crate::coverage::capture::PhaseSnapshot {
            target_setup_us: 0,
            capture_ready_us: 0,
            interaction_us: 0,
            tail_drain_us: 0,
            stop_reap_us: 0,
            pcap_read_us: 0,
            zlib_us: 0,
            persist_us: 0,
            uncompressed_len: 0,
            compressed_len: 0,
            packets_captured: 0,
            packets_received: 0,
            packets_dropped: 0,
            status: "disabled".to_string(),
            note: String::new(),
            pids: Vec::new(),
        }
    }

    fn sample_case(seed: &str, status: &str, evaluate_us: u128) -> BenchCase {
        let mut phase = phases();
        phase.uncompressed_len = 1000;
        phase.compressed_len = 400;
        phase.packets_captured = 3;
        phase.packets_received = 4;
        phase.packets_dropped = 1;
        BenchCase {
            index: 0,
            seed: seed.to_string(),
            evaluate_us,
            saved: false,
            exit_kind: "ok".to_string(),
            target_status: "exited:0".to_string(),
            generator_status: status.to_string(),
            generator_error: if status == "ok" {
                None
            } else {
                Some("generator cancelled after the target exited".to_string())
            },
            phases: phase,
        }
    }

    fn sample_run(
        trial: u32,
        condition: &str,
        error: Option<String>,
        wall_us: u128,
        cases: Vec<BenchCase>,
    ) -> BenchRun {
        BenchRun {
            trial,
            condition: condition.to_string(),
            startup_us: 0,
            preflight_us: None,
            warmup_us: 0,
            measured_wall_us: wall_us,
            error,
            cases,
        }
    }

    #[test]
    fn preflight_report_quotes_the_actual_error_and_skips_missing_pairs() {
        let runs = vec![
            sample_run(0, "off", None, 1_000_000, vec![sample_case("list.json", "ok", 1_000_000)]),
            sample_run(
                0,
                "on",
                Some("capture preflight failed before any case as uid 1005: missing executable /tmp/no-such-ptcpdump".to_string()),
                0,
                Vec::new(),
            ),
        ];
        let mut text = String::new();
        append_preflight_failures(&mut text, &runs);
        append_pair_summary(&mut text, &runs);
        append_capture_summaries(&mut text, &runs);
        append_condition_table(&mut text, &runs);
        assert!(text.contains("## Preflight failure"), "{text}");
        assert!(
            text.contains("startup ms is the whole `Session::open`"),
            "{text}"
        );
        assert!(
            text.contains("includes the failed capture preflight"),
            "{text}"
        );
        assert!(text.contains("N/A"), "{text}");
        assert!(
            text.contains("missing executable /tmp/no-such-ptcpdump"),
            "{text}"
        );
        assert!(!text.contains("## Permission failure"), "{text}");
        assert!(!text.contains("operation not permitted"), "{text}");
        assert!(
            text.contains("not computable: no valid capture-on sample"),
            "{text}"
        );
        assert!(
            text.contains("per seed and per case are not computable"),
            "{text}"
        );
    }

    #[test]
    fn paired_summary_uses_successful_interactions_only() {
        let runs = vec![
            sample_run(
                0,
                "off",
                None,
                10_000_000,
                vec![
                    sample_case("list.json", "ok", 1_000_000),
                    sample_case("list.json", "cancelled", 9_000_000),
                ],
            ),
            sample_run(
                0,
                "on",
                None,
                4_000_000,
                vec![
                    sample_case("list.json", "ok", 2_000_000),
                    sample_case("retr.json", "ok", 2_000_000),
                ],
            ),
        ];
        let mut text = String::new();
        append_pair_summary(&mut text, &runs);
        append_capture_summaries(&mut text, &runs);
        let pairs = super::pair_metrics(&runs);
        let encoded = serde_json::to_string(&pairs).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&encoded).unwrap();
        assert_eq!(parsed[0]["kind"], "pair");
        assert_eq!(parsed[0]["off_anomalous"], 1);
        assert_eq!(parsed[0]["on_anomalous"], 0);
        assert!((parsed[0]["off_wall_cases_per_s"].as_f64().unwrap() - 0.2).abs() < 1e-9);
        assert!((parsed[0]["off_ok_per_wall"].as_f64().unwrap() - 0.1).abs() < 1e-9);
        assert!((parsed[0]["wall_throughput_loss"].as_f64().unwrap() + 1.5).abs() < 1e-9);
        assert!((parsed[0]["ok_per_wall_loss"].as_f64().unwrap() + 4.0).abs() < 1e-9);
        let summaries = serde_json::to_string(&super::capture_summaries(&runs)).unwrap();
        assert!(
            summaries.contains("\"kind\":\"capture_summary\""),
            "{summaries}"
        );
        assert!(text.contains("0.200"), "{text}");
        assert!(text.contains("0.100"), "{text}");
        assert!(text.contains("-150.0%"), "{text}");
        assert!(text.contains("-400.0%"), "{text}");
        assert!(text.contains("100.0%"), "{text}");
        assert!(text.contains("| 0 | 2 | 1 | 1 | 2 | 2 | 0 |"), "{text}");
        assert!(text.contains("| seed | off | list.json |"), "{text}");
        assert!(text.contains("0.400"), "{text}");
    }

    #[test]
    fn openssl_alert_and_timeout_are_expected_outcomes() {
        let runs = vec![sample_run(
            0,
            "off",
            None,
            3_000_000,
            vec![
                sample_case("illegal.json", "alert", 70_000),
                sample_case("truncated.json", "timeout", 1_000_000),
                sample_case("valid.json", "ok", 140_000),
                sample_case("quit.json", "cancelled", 600_000),
            ],
        )];
        let stats = super::success_stats(&runs[0].cases, runs[0].measured_wall_us);
        assert_eq!(stats.measured, 4);
        assert_eq!(stats.expected, 3);
        assert_eq!(stats.anomalous, 1);
        assert_eq!(stats.ok, 1);
        assert_eq!(stats.mean_us, Some((70_000 + 1_000_000 + 140_000) / 3));
        assert_eq!(stats.p50_us, Some(140_000));
        assert_eq!(stats.p95_us, Some(1_000_000));
        assert!((stats.wall_cases_per_s.unwrap() - 4.0 / 3.0).abs() < 1e-9);
        assert!((stats.ok_per_wall.unwrap() - 1.0 / 3.0).abs() < 1e-9);
        let mut crashed = sample_case("illegal.json", "alert", 50_000);
        crashed.target_status = "signaled:11".to_string();
        let with_crash = super::success_stats(
            &[sample_case("illegal.json", "alert", 70_000), crashed],
            1_000_000,
        );
        assert_eq!(with_crash.expected, 1);
        assert_eq!(with_crash.anomalous, 1);
        assert_eq!(with_crash.mean_us, Some(70_000));
        let mut text = String::new();
        super::append_condition_table(&mut text, &runs);
        super::append_capture_summaries(&mut text, &runs);
        assert!(text.contains("| 0 | off | 4 | 3 | 1 |"), "{text}");
        assert!(text.contains("alert×1"), "{text}");
        assert!(text.contains("timeout×1"), "{text}");
        assert!(text.contains("`illegal.json`: alert"), "{text}");
        assert!(text.contains("`truncated.json`: timeout"), "{text}");
        assert!(text.contains("cancelled"), "{text}");
        assert!(!text.contains("| error |"), "{text}");
        let summary = super::capture_summaries(&runs);
        let illegal = summary
            .iter()
            .find(|item| item.seed == "illegal.json")
            .unwrap();
        let truncated = summary
            .iter()
            .find(|item| item.seed == "truncated.json")
            .unwrap();
        let quit = summary
            .iter()
            .find(|item| item.seed == "quit.json")
            .unwrap();
        assert_eq!(illegal.expected, 1);
        assert_eq!(illegal.anomalous, 0);
        assert_eq!(truncated.expected, 1);
        assert_eq!(truncated.anomalous, 0);
        assert_eq!(quit.anomalous, 1);
    }

    #[test]
    fn relative_bench_scratch_becomes_absolute() {
        let relative = PathBuf::from(format!("target/abs-only-{}", std::process::id()));
        let absolute = absolute_dir(&relative).unwrap();
        assert!(absolute.is_absolute(), "{}", absolute.display());
        let _ = std::fs::remove_dir_all(absolute);
    }

    #[test]
    fn lightftp_bench_paths_serve_list_and_retr() {
        let campaign_path = Path::new("targets/lightftp/campaign.toml");
        let binary = Path::new("targets/lightftp/build/fftp");
        if !campaign_path.is_file() || !binary.is_file() {
            return;
        }
        let campaign = Campaign::load(campaign_path).unwrap();
        let relative = PathBuf::from(format!("target/bench-abs-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&relative);
        let laid = prepare_bench_campaign(&campaign, &relative, 0, "off").unwrap();
        assert!(
            laid.target.workdir.is_absolute(),
            "{}",
            laid.target.workdir.display()
        );
        assert!(laid.base.is_absolute(), "{}", laid.base.display());
        let mut session = super::Session::open(&laid).unwrap();
        let conf = std::fs::read_to_string(laid.target.workdir.join("fftp.conf")).unwrap();
        let roots: Vec<&str> = conf
            .lines()
            .filter(|line| line.starts_with("root="))
            .map(|line| line.trim_start_matches("root="))
            .collect();
        assert!(!roots.is_empty(), "{conf}");
        for root in &roots {
            assert!(Path::new(root).is_absolute(), "{root}");
            assert_eq!(Path::new(root), laid.target.workdir.as_path());
        }
        let pid = session.executor.forkserver_pid();
        let cmdline = std::fs::read(format!("/proc/{pid}/cmdline")).unwrap();
        let args: Vec<String> = cmdline
            .split(|byte| *byte == 0)
            .filter(|part| !part.is_empty())
            .map(|part| String::from_utf8_lossy(part).into_owned())
            .collect();
        assert!(
            args.iter()
                .any(|arg| arg.ends_with("fftp.conf") && Path::new(arg).is_absolute()),
            "{args:?}"
        );
        for seed_name in ["list.json", "retr.json"] {
            let input =
                GeneratorInput::from_file(Path::new("targets/lightftp/seeds").join(seed_name))
                    .unwrap();
            session.evaluate(&input).unwrap();
            let report = session.executor.last_report().cloned().unwrap();
            assert_eq!(report.generator_status, "ok", "{seed_name} {report:?}");
            assert_eq!(report.target_status, "exited:0", "{seed_name} {report:?}");
            assert!(report.generator_error.is_none(), "{seed_name} {report:?}");
            let edges = super::interesting_indexes(&session.executor.edge_indexes());
            assert!(edges.len() > 40, "{seed_name} edges {}", edges.len());
        }
        drop(session);
        let _ = std::fs::remove_dir_all(&relative);
    }
}
