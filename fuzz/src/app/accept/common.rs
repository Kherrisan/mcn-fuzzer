//! Helpers shared by the SUT acceptance scenarios.

use std::{collections::HashSet, fs};

use crate::{
    app::config::Campaign, app::Session, engine::executor::TraceIdentity,
    engine::input::GeneratorInput,
};
use libafl::Error;

pub(super) fn accept_scratch(campaign: &Campaign) -> Campaign {
    let mut scratch = campaign.clone();
    scratch.campaign.corpus = scratch.base.join("accept-corpus");
    scratch.campaign.crashes = scratch.base.join("accept-crashes");
    scratch.target.workdir = scratch.base.join("accept-work");
    scratch
}

pub(super) fn finish_accept(label: &str, failures: Vec<String>) -> Result<(), Error> {
    if failures.is_empty() {
        println!("accept: {label} checks passed");
        Ok(())
    } else {
        for failure in &failures {
            eprintln!("accept fail: {failure}");
        }
        Err(Error::unknown(format!(
            "{} {label} acceptance check(s) failed",
            failures.len()
        )))
    }
}

pub(super) fn check_identity(identity: &TraceIdentity, failures: &mut Vec<String>) {
    println!(
        "accept: forkserver child {} parent {} forkserver {} fuzzer {} cmdline_match {}",
        identity.child_pid,
        identity.parent_pid,
        identity.forkserver_pid,
        identity.fuzzer_pid,
        identity.cmdline_matches_target
    );
    if identity.child_pid == identity.forkserver_pid
        || identity.child_pid == identity.fuzzer_pid
        || identity.parent_pid != identity.forkserver_pid
        || !identity.cmdline_matches_target
    {
        failures.push(format!(
            "forkserver child: child {} parent {} forkserver {} fuzzer {} cmdline {}",
            identity.child_pid,
            identity.parent_pid,
            identity.forkserver_pid,
            identity.fuzzer_pid,
            identity.cmdline_matches_target
        ));
    }
}

pub(super) fn replay_edges(
    session: &mut Session,
    input: &GeneratorInput,
) -> Result<Vec<usize>, Error> {
    session.execute(input)?;
    Ok(session.executor.edge_indexes())
}

pub(super) fn jaccard(left: &[usize], right: &[usize]) -> f64 {
    let left: HashSet<_> = left.iter().copied().collect();
    let right: HashSet<_> = right.iter().copied().collect();
    let union = left.union(&right).count();
    if union == 0 {
        1.0
    } else {
        left.intersection(&right).count() as f64 / union as f64
    }
}

pub(super) fn processes_matching(needle: &str) -> Vec<i32> {
    let mut found = Vec::new();
    let Ok(dir) = fs::read_dir("/proc") else {
        return found;
    };
    for entry in dir.flatten() {
        let Ok(pid) = entry.file_name().to_string_lossy().parse::<i32>() else {
            continue;
        };
        let Ok(cmdline) = fs::read(format!("/proc/{pid}/cmdline")) else {
            continue;
        };
        if String::from_utf8_lossy(&cmdline).contains(needle) {
            found.push(pid);
        }
    }
    found
}

pub(super) fn fd_count(pid: i32) -> usize {
    fs::read_dir(format!("/proc/{pid}/fd"))
        .map(|entries| entries.flatten().count())
        .unwrap_or(0)
}

pub(super) fn port_is_free(port: u16) -> bool {
    std::net::TcpListener::bind(("127.0.0.1", port)).is_ok()
}
