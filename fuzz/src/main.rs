//! `mcn-fuzz run`, `replay`, `doctor`, and `accept`.
//! `accept` re-execs one hidden `accept-job` per scenario.
//! `bench-capture` exists only with `--features stats`.

use std::{path::PathBuf, process::ExitCode};

use clap::{Parser, Subcommand};
use libafl::Error;

use mcn_fuzz::{app, app::config::Campaign, runtime::lifecycle::JobPolicy};

#[derive(Parser)]
#[command(
    name = "mcn-fuzz",
    about = "Forkserver fuzzer with PCGUARD and a Python generator"
)]
struct Cli {
    /// Account that owns the fuzzer after namespaces are created. User name or uid. Not 0.
    #[arg(long, global = true)]
    run_as: Option<String>,
    /// Wait for both the SUT and generator PIDs, in milliseconds.
    #[arg(long, global = true, default_value_t = 1000)]
    startup_timeout_ms: u64,
    /// Whole-case budget after both PIDs are known. Defaults to the campaign case_ms.
    #[arg(long, global = true)]
    case_timeout_ms: Option<u64>,
    /// Grace after one root exits, and after a startup deadline, in milliseconds.
    #[arg(long, global = true, default_value_t = 100)]
    peer_exit_grace_ms: u64,
    /// Wait for a Result or forkserver status after the root has exited, in milliseconds.
    #[arg(long, global = true, default_value_t = 100)]
    result_timeout_ms: u64,
    /// Consecutive recoverable timeouts allowed before the next one of that kind ends the job.
    #[arg(long, global = true, default_value_t = 5)]
    timeout_failure_limit: u32,
    /// Classify a SUT killed after peer grace as a timeout.
    #[arg(long, global = true, action = clap::ArgAction::SetTrue)]
    sut_grace_kill_is_timeout: bool,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Load the campaign seeds and fuzz.
    Run {
        #[arg(long)]
        campaign: PathBuf,
        #[arg(long)]
        iterations: Option<u64>,
        /// Save the full llvm-cov JSON for source-coverage snapshots.
        /// Disabled by default; summary-only data is still used for coverage deltas.
        #[arg(long)]
        source_coverage_json: bool,
    },
    /// Execute one saved generator input.
    Replay {
        #[arg(long)]
        campaign: PathBuf,
        #[arg(long)]
        input: PathBuf,
    },
    /// Check the PCGUARD shared map, handshake, and branch feedback.
    Doctor,
    /// Run acceptance checks. Each scenario is a separate supervised job.
    Accept {
        #[arg(long)]
        campaign: PathBuf,
    },
    /// One supervised acceptance scenario. Invoked by `accept`.
    #[command(hide = true)]
    AcceptJob {
        #[arg(long)]
        campaign: PathBuf,
        #[arg(long)]
        scenario: String,
    },
    /// Paired capture-off / capture-on throughput. Runs as the current user.
    #[cfg(feature = "stats")]
    BenchCapture {
        #[arg(long)]
        campaign: PathBuf,
        #[arg(long)]
        output: PathBuf,
    },
    /// One supervised bench side. Invoked by `bench-capture`.
    #[cfg(feature = "stats")]
    BenchJob {
        #[arg(long)]
        campaign: PathBuf,
        #[arg(long)]
        output: PathBuf,
        #[arg(long)]
        trial: u32,
        #[arg(long)]
        condition: String,
    },
    /// Write the bench report from per-job records, as the target user.
    #[cfg(feature = "stats")]
    BenchFinalize {
        #[arg(long)]
        campaign: PathBuf,
        #[arg(long)]
        output: PathBuf,
    },
}

fn main() -> ExitCode {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();
    let cli = Cli::parse();
    let policy = match JobPolicy::from_millis(
        cli.startup_timeout_ms,
        cli.case_timeout_ms,
        cli.peer_exit_grace_ms,
        cli.result_timeout_ms,
        cli.timeout_failure_limit,
        cli.sut_grace_kill_is_timeout,
    ) {
        Ok(policy) => policy,
        Err(error) => {
            eprintln!("mcn-fuzz: {error}");
            return ExitCode::from(1);
        }
    };
    mcn_fuzz::runtime::lifecycle::install_job_policy(policy);
    if !orchestrator(&cli.command) {
        let capture = capture_requested(&cli.command);
        let identity = match mcn_fuzz::runtime::resolve_identity(cli.run_as.as_deref()) {
            Ok(identity) => identity,
            Err(error) => {
                eprintln!("mcn-fuzz: {error}");
                return ExitCode::from(1);
            }
        };
        if let Err(error) = ensure_command_outputs(&cli.command, &identity) {
            eprintln!("mcn-fuzz: {error}");
            return ExitCode::from(1);
        }
        if let Command::Run { campaign, .. } = &cli.command {
            if let Ok(loaded) = load(campaign) {
                if loaded.protocol_generation.enabled || loaded.coverage.analysis_enabled {
                    let output = if loaded.protocol_generation.enabled {
                        loaded.protocol_generation.output.clone()
                    } else {
                        loaded.coverage.output.clone()
                    };
                    mcn_fuzz::agents::install_generation_request(
                        mcn_fuzz::agents::GenerationRequest {
                            hc: loaded.agents.hc_executable.clone(),
                            output,
                        },
                    );
                }
            }
        }
        let job = accept_supervise_job(&cli.command);
        match mcn_fuzz::runtime::supervise(job, capture, &identity) {
            Ok(mcn_fuzz::runtime::Role::Parent(code)) => {
                return ExitCode::from(code as u8);
            }
            Ok(mcn_fuzz::runtime::Role::Child(worker)) => {
                mcn_fuzz::runtime::install_worker(worker);
            }
            Err(error) => {
                if let Command::Run { campaign, .. } = &cli.command {
                    if let Ok(loaded) = load(campaign) {
                        mcn_fuzz::agents::discard_recorded_runtime(
                            &loaded.protocol_generation.output,
                        );
                    }
                }
                eprintln!("mcn-fuzz: {error}");
                return ExitCode::from(1);
            }
        }
    }
    let result = match &cli.command {
        Command::Run {
            campaign,
            iterations,
            source_coverage_json,
        } => load(campaign).and_then(|mut campaign| {
            campaign.coverage.export_json = *source_coverage_json;
            app::run(&campaign, *iterations)
        }),
        Command::Replay { campaign, input } => {
            load(&campaign).and_then(|campaign| app::replay(&campaign, &input))
        }
        Command::Doctor => app::doctor(),
        Command::Accept { campaign } => orchestrate_accept(campaign, &cli),
        Command::AcceptJob { campaign, scenario } => {
            load(campaign).and_then(|campaign| app::accept_job(&campaign, scenario))
        }
        #[cfg(feature = "stats")]
        Command::BenchCapture { campaign, output } => orchestrate_bench(campaign, output, &cli),
        #[cfg(feature = "stats")]
        Command::BenchJob {
            campaign,
            output,
            trial,
            condition,
        } => {
            load(campaign).and_then(|campaign| app::bench_job(&campaign, output, *trial, condition))
        }
        #[cfg(feature = "stats")]
        Command::BenchFinalize { campaign, output } => {
            load(campaign).and_then(|campaign| app::finalize_bench(&campaign, output))
        }
    };
    let code = if let Err(error) = result {
        eprintln!("mcn-fuzz: {error}");
        1
    } else {
        0
    };
    if !orchestrator(&cli.command) {
        if let Command::Run { campaign, .. } = &cli.command {
            if let Ok(loaded) = load(campaign) {
                mcn_fuzz::agents::discard_recorded_runtime(&loaded.protocol_generation.output);
            }
        }
        mcn_fuzz::runtime::report_child_exit(code);
    }
    ExitCode::from(code as u8)
}

fn capture_requested(command: &Command) -> bool {
    match command {
        Command::Run { campaign, .. }
        | Command::Replay { campaign, .. }
        | Command::AcceptJob { campaign, .. } => Campaign::load(campaign)
            .map(|campaign| campaign.capture.enabled)
            .unwrap_or(false),
        Command::Accept { .. } => false,
        #[cfg(feature = "stats")]
        Command::BenchJob { condition, .. } => condition == "on",
        Command::Doctor => false,
        #[cfg(feature = "stats")]
        Command::BenchCapture { .. } | Command::BenchFinalize { .. } => false,
    }
}

fn ensure_command_outputs(
    command: &Command,
    identity: &mcn_fuzz::runtime::TargetIdentity,
) -> Result<(), Error> {
    let paths = match command {
        Command::Run { campaign, .. }
        | Command::Replay { campaign, .. }
        | Command::AcceptJob { campaign, .. } => {
            let loaded = Campaign::load(campaign)?;
            vec![
                loaded.base.clone(),
                loaded.campaign.corpus.clone(),
                loaded.target.workdir.clone(),
            ]
        }
        #[cfg(feature = "stats")]
        Command::BenchJob { output, .. } | Command::BenchFinalize { output, .. } => {
            vec![output.clone()]
        }
        _ => return Ok(()),
    };
    for path in paths {
        mcn_fuzz::runtime::ensure_output_accessible(&path, identity)?;
    }
    Ok(())
}

#[cfg(feature = "stats")]
fn orchestrate_bench(
    campaign: &std::path::Path,
    output: &std::path::Path,
    cli: &Cli,
) -> Result<(), Error> {
    use std::process::Command;
    let run_as = cli.run_as.as_deref();
    let identity = mcn_fuzz::runtime::resolve_identity(run_as)?;
    mcn_fuzz::runtime::ensure_output_accessible(output, &identity)?;
    let exe = std::env::current_exe()
        .map_err(|error| Error::unknown(format!("current executable: {error}")))?;
    for trial in 0..5u32 {
        let order = if trial % 2 == 0 {
            ["off", "on"]
        } else {
            ["on", "off"]
        };
        for condition in order {
            let mut child = Command::new(&exe);
            if let Some(spec) = run_as {
                child.arg("--run-as").arg(spec);
            }
            push_policy(&mut child, cli);
            let status = child
                .arg("bench-job")
                .arg("--campaign")
                .arg(campaign)
                .arg("--output")
                .arg(output)
                .arg("--trial")
                .arg(trial.to_string())
                .arg("--condition")
                .arg(condition)
                .status()
                .map_err(|error| Error::unknown(format!("starting bench job: {error}")))?;
            if !status.success() {
                eprintln!(
                    "mcn-fuzz: bench job {trial} {condition} exited {status}; the record is kept"
                );
            }
        }
    }
    let mut report = Command::new(&exe);
    if let Some(spec) = run_as {
        report.arg("--run-as").arg(spec);
    }
    push_policy(&mut report, cli);
    let status = report
        .arg("bench-finalize")
        .arg("--campaign")
        .arg(campaign)
        .arg("--output")
        .arg(output)
        .status()
        .map_err(|error| Error::unknown(format!("starting bench report: {error}")))?;
    if !status.success() {
        return Err(Error::unknown(
            "bench report retained failed jobs instead of treating them as zero cost".to_string(),
        ));
    }
    Ok(())
}

fn orchestrate_accept(campaign: &std::path::Path, cli: &Cli) -> Result<(), Error> {
    use std::process::Command;
    let run_as = cli.run_as.as_deref();
    let loaded = load(campaign)?;
    let identity = mcn_fuzz::runtime::resolve_identity(run_as)?;
    for path in [
        loaded.base.as_path(),
        loaded.campaign.corpus.as_path(),
        loaded.target.workdir.as_path(),
    ] {
        mcn_fuzz::runtime::ensure_output_accessible(path, &identity)?;
    }
    let exe = std::env::current_exe()
        .map_err(|error| Error::unknown(format!("current executable: {error}")))?;
    let mut failures = Vec::new();
    for scenario in app::accept_scenarios(&loaded) {
        let mut child = Command::new(&exe);
        if let Some(spec) = run_as {
            child.arg("--run-as").arg(spec);
        }
        push_policy(&mut child, cli);
        match child
            .arg("accept-job")
            .arg("--campaign")
            .arg(campaign)
            .arg("--scenario")
            .arg(scenario)
            .status()
        {
            Ok(status) if status.success() => {}
            Ok(status) => {
                let line = format!("accept job {scenario} exited {status}");
                eprintln!("mcn-fuzz: {line}");
                failures.push(line);
            }
            Err(error) => {
                let line = format!("accept job {scenario} failed to start: {error}");
                eprintln!("mcn-fuzz: {line}");
                failures.push(line);
            }
        }
    }
    if failures.is_empty() {
        println!("accept: all jobs passed");
        Ok(())
    } else {
        Err(Error::unknown(format!(
            "{} accept job(s) failed: {}",
            failures.len(),
            failures.join("; ")
        )))
    }
}

fn push_policy(command: &mut std::process::Command, cli: &Cli) {
    command
        .arg("--startup-timeout-ms")
        .arg(cli.startup_timeout_ms.to_string())
        .arg("--peer-exit-grace-ms")
        .arg(cli.peer_exit_grace_ms.to_string())
        .arg("--result-timeout-ms")
        .arg(cli.result_timeout_ms.to_string())
        .arg("--timeout-failure-limit")
        .arg(cli.timeout_failure_limit.to_string());
    if let Some(value) = cli.case_timeout_ms {
        command.arg("--case-timeout-ms").arg(value.to_string());
    }
    if cli.sut_grace_kill_is_timeout {
        command.arg("--sut-grace-kill-is-timeout");
    }
}

fn accept_supervise_job(command: &Command) -> u64 {
    match command {
        Command::AcceptJob { scenario, .. } => match scenario.as_str() {
            "main" | "openssl" => 1,
            "hang" => 2,
            "crash" => 3,
            _ => 0,
        },
        _ => 0,
    }
}

fn orchestrator(command: &Command) -> bool {
    match command {
        Command::Accept { .. } => true,
        #[cfg(feature = "stats")]
        Command::BenchCapture { .. } => true,
        _ => false,
    }
}

fn load(path: &std::path::Path) -> Result<Campaign, Error> {
    let path = fs_canonicalize(path)?;
    Campaign::load(&path)
}

fn fs_canonicalize(path: &std::path::Path) -> Result<PathBuf, Error> {
    std::fs::canonicalize(path).map_err(|error| {
        Error::unknown(format!(
            "campaign file {} is not available: {error}",
            path.display()
        ))
    })
}
