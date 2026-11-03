//! OpenSSL s_server handshake, replay, coverage, and cleanup checks.

use std::fs;

use super::common::{
    accept_scratch, check_identity, fd_count, finish_accept, jaccard, port_is_free,
    processes_matching, replay_edges,
};
use crate::{
    app::{interesting_indexes, require_exec, symmetric_diff, Session},
    config::Campaign,
    input::GeneratorInput,
};
use libafl::{executors::ExitKind, inputs::Input, Error};

/// Valid ClientHello, rejected handshake, replay, and cleanup for `s_server`.
pub(super) fn accept_openssl(campaign: &Campaign) -> Result<(), Error> {
    require_exec(&campaign.target.binary)?;
    let mut failures = Vec::new();
    let scratch = accept_scratch(campaign);
    let _ = fs::remove_dir_all(&scratch.campaign.corpus);
    let _ = fs::remove_dir_all(&scratch.campaign.crashes);
    let _ = fs::remove_dir_all(&scratch.target.workdir);
    let valid = seed_input(&scratch, "valid.json")?;
    let illegal = seed_input(&scratch, "illegal.json")?;
    let truncated = seed_input(&scratch, "truncated.json")?;
    let mut session = Session::open(&scratch)?;

    check_tls(&mut session, &mut failures, "valid", &valid, &["ok"]);
    let valid_edges = interesting_indexes(&session.executor.edge_indexes());
    check_tls(
        &mut session,
        &mut failures,
        "illegal",
        &illegal,
        &["alert", "eof"],
    );
    let illegal_edges = interesting_indexes(&session.executor.edge_indexes());
    check_tls(
        &mut session,
        &mut failures,
        "truncated",
        &truncated,
        &["alert", "eof", "timeout"],
    );
    let truncated_edges = interesting_indexes(&session.executor.edge_indexes());

    if valid_edges.is_empty() || illegal_edges.is_empty() {
        failures.push(format!(
            "coverage: valid has {} shared-map entries beyond the marker and illegal has {}",
            valid_edges.len(),
            illegal_edges.len()
        ));
    } else if symmetric_diff(&valid_edges, &illegal_edges) == 0 {
        failures.push(
            "coverage: valid ClientHello and illegal handshake wrote the same shared-map entries"
                .to_string(),
        );
    } else {
        println!(
            "accept: valid {} entries, illegal {} entries, symmetric difference {}",
            valid_edges.len(),
            illegal_edges.len(),
            symmetric_diff(&valid_edges, &illegal_edges)
        );
    }
    if !truncated_edges.is_empty() && symmetric_diff(&truncated_edges, &valid_edges) == 0 {
        failures.push(
            "coverage: truncated record and valid ClientHello wrote the same shared-map entries"
                .to_string(),
        );
    }
    if let Some(identity) = session.executor.last_identity().cloned() {
        check_identity(&identity, &mut failures);
    } else {
        failures.push("coverage: no forkserver child identity was recorded".to_string());
    }

    let first = interesting_indexes(&replay_edges(&mut session, &valid)?);
    let second = interesting_indexes(&replay_edges(&mut session, &valid)?);
    let score = jaccard(&first, &second);
    println!(
        "accept: replay valid jaccard {score:.3} ({} and {} entries)",
        first.len(),
        second.len()
    );
    if first.is_empty() || score < 0.8 {
        failures.push(format!(
            "replay: jaccard {score:.3} (need >= 0.8 and a non-empty map)"
        ));
    }

    let fd_start = fd_count(std::process::id() as i32);
    let py_start = processes_matching("generator_server");
    let mut rounds = 0u32;
    for round in 1..=100 {
        match session.execute(&valid) {
            Ok(ExitKind::Ok) => {}
            Ok(kind) => {
                failures.push(format!("leak round {round}: exit kind {kind:?}"));
                break;
            }
            Err(error) => {
                failures.push(format!("leak round {round}: {error:?}"));
                break;
            }
        }
        let report = session.executor.last_report();
        let generator = report
            .map(|item| item.generator_status.as_str())
            .unwrap_or("");
        let target = report.map(|item| item.target_status.as_str()).unwrap_or("");
        if generator != "ok" || target != "exited:0" {
            failures.push(format!(
                "leak round {round}: target {target} generator {generator}"
            ));
            break;
        }
        if !port_is_free(session.executor.port()) {
            failures.push(format!(
                "leak round {round}: port {} is still bound",
                session.executor.port()
            ));
            break;
        }
        let targets = processes_matching(&scratch.target.binary.display().to_string());
        if targets.len() != 1 {
            failures.push(format!(
                "leak round {round}: expected only the forkserver, found {} processes matching the target",
                targets.len()
            ));
            break;
        }
        rounds = round;
        if round % 10 == 0 {
            println!("accept: leak rounds {round}/100");
        }
    }
    let fd_end = fd_count(std::process::id() as i32);
    let py_end = processes_matching("generator_server");
    println!(
        "accept: completed {rounds} leak rounds; fd {fd_start} -> {fd_end}; python processes {} -> {}",
        py_start.len(),
        py_end.len()
    );
    if rounds < 100 {
        failures.push(format!("leak: stopped after {rounds} rounds"));
    }
    if fd_end > fd_start.saturating_add(16) {
        failures.push(format!(
            "leak: fuzzer fd count grew from {fd_start} to {fd_end}"
        ));
    }
    if py_end.len() != py_start.len() || py_end.len() > 2 {
        failures.push(format!(
            "leak: generator_server processes {} at start and {} at end",
            py_start.len(),
            py_end.len()
        ));
    }

    finish_accept("openssl", failures)
}

fn seed_input(campaign: &Campaign, name: &str) -> Result<GeneratorInput, Error> {
    GeneratorInput::from_file(campaign.campaign.seeds.join(name))
}

fn check_tls(
    session: &mut Session,
    failures: &mut Vec<String>,
    name: &str,
    input: &GeneratorInput,
    allowed: &[&str],
) {
    match session.execute(input) {
        Ok(ExitKind::Ok) => {
            let report = session.executor.last_report().cloned();
            let target = report
                .as_ref()
                .map(|item| item.target_status.as_str())
                .unwrap_or("");
            let generator = report
                .as_ref()
                .map(|item| item.generator_status.as_str())
                .unwrap_or("");
            let detail = report
                .as_ref()
                .and_then(|item| item.generator_error.clone())
                .unwrap_or_default();
            println!("accept: {name} -> Ok target {target} generator {generator} {detail}");
            if target != "exited:0" {
                failures.push(format!(
                    "{name}: target {target} did not exit after the one connection"
                ));
            }
            if !allowed.contains(&generator) {
                failures.push(format!(
                    "{name}: generator status {generator}, expected one of {allowed:?} ({detail})"
                ));
            }
        }
        Ok(kind) => failures.push(format!(
            "{name}: classified as {kind:?}; a finished s_server child should be Ok"
        )),
        Err(error) => failures.push(format!("{name}: {error:?}")),
    }
}
