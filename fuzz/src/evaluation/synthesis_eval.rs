//! Evaluate generator synthesis without HC, forkserver, or the Python server.

use std::path::PathBuf;

use clap::{Args, Parser, Subcommand};
use mcn_fuzz::evaluation::{self, Options};

fn main() {
    evaluation::install_subreaper();
    evaluation::install_signal_handlers();
    if std::env::var_os("MCN_EVAL_SIGNAL_PROBE").is_some() {
        std::process::exit(evaluation::signal_probe());
    }
    let cli = Cli::parse();
    let is_run = matches!(cli.command, Command::Run(_));
    let result = match cli.command {
        Command::Prepare(common) => evaluation::prepare(&common.into_options()).map(|_| true),
        Command::Generate { task } => evaluation::generate(&task).map(|_| true),
        Command::Validate { task } => evaluation::validate(&task).map(|_| true),
        Command::Replay { task } => evaluation::replay(&task).map(|_| true),
        Command::Run(common) => evaluation::run(&common.into_options()),
    };
    match result {
        Ok(all_passed) => {
            if is_run && !all_passed {
                std::process::exit(1);
            }
        }
        Err(error) => {
            eprintln!("mcn-synthesis-eval: {error}");
            std::process::exit(1);
        }
    }
}

#[derive(Parser)]
#[command(name = "mcn-synthesis-eval")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    Prepare(Common),
    Generate {
        #[arg(long)]
        task: PathBuf,
    },
    Validate {
        #[arg(long)]
        task: PathBuf,
    },
    Replay {
        #[arg(long)]
        task: PathBuf,
    },
    Run(Common),
}

#[derive(Args, Clone)]
struct Common {
    #[arg(long)]
    suite: PathBuf,
    #[arg(long)]
    profile: Vec<String>,
    #[arg(long)]
    case: Vec<String>,
    #[arg(long, default_value_t = 1)]
    repeat: u32,
    #[arg(long)]
    out: Option<PathBuf>,
}

impl Common {
    fn into_options(self) -> Options {
        Options {
            suite: self.suite,
            profiles: self.profile,
            cases: self.case,
            repeat: self.repeat,
            out: self.out,
        }
    }
}
