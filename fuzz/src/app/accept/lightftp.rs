//! LightFTP protocol, recovery, leak, coverage, and classification checks.

use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
};

use super::common::{
    accept_scratch, check_identity, fd_count, finish_accept, jaccard, port_is_free,
    processes_matching, replay_edges,
};
use crate::{
    app::config::Campaign,
    app::{interesting_indexes, require_exec, symmetric_diff, Session},
    engine::input::{GeneratorInput, ParamValue, StepInstance},
    engine::mutator::GeneratorMutator,
};
use libafl::{
    corpus::Corpus, executors::ExitKind, fuzzer::Fuzzer, inputs::Input, stages::StdMutationalStage,
    state::HasCorpus, Error,
};
use libafl_bolts::tuples::tuple_list;

/// Protocol, recovery, leak, coverage, replay, and classification checks.
pub(super) fn accept_lightftp_main(campaign: &Campaign) -> Result<(), Error> {
    require_exec(&campaign.target.binary)?;
    let mut failures = Vec::new();
    let scratch = accept_scratch(campaign);
    let _ = fs::remove_dir_all(&scratch.campaign.corpus);
    let _ = fs::remove_dir_all(&scratch.campaign.crashes);
    let _ = fs::remove_dir_all(&scratch.target.workdir);
    {
        let mut session = Session::open(&scratch)?;
        let list = mode_input(&scratch, "list", "ubuntu", "ubuntu");
        let retr = mode_input(&scratch, "retr", "ubuntu", "ubuntu");
        let quit = mode_input(&scratch, "quit", "ubuntu", "ubuntu");
        let bad = mode_input(&scratch, "bad_login", "ubuntu", "wrong-password");
        let disconnect = mode_input(&scratch, "disconnect", "ubuntu", "ubuntu");
        let send_only = mode_input(&scratch, "send_only", "ubuntu", "ubuntu");
        let globals = mode_input(&scratch, "globals", "ubuntu", "ubuntu");
        let stor = mode_input(&scratch, "stor", "ubuntu", "ubuntu");
        let absent = mode_input(&scratch, "check_absent", "ubuntu", "ubuntu");

        check_protocol(&mut session, &mut failures, "quit", &quit, true);
        let quit_edges = interesting_indexes(&session.executor.edge_indexes());
        check_protocol(&mut session, &mut failures, "list", &list, true);
        let list_edges = interesting_indexes(&session.executor.edge_indexes());
        check_protocol(&mut session, &mut failures, "retr", &retr, true);
        let retr_edges = interesting_indexes(&session.executor.edge_indexes());
        check_protocol(&mut session, &mut failures, "bad_login", &bad, true);
        check_protocol(
            &mut session,
            &mut failures,
            "disconnect",
            &disconnect,
            false,
        );
        check_protocol(&mut session, &mut failures, "send_only", &send_only, false);
        let io_timeout = mode_input(&scratch, "io_timeout", "ubuntu", "ubuntu");
        check_generator_timeout(&mut session, &mut failures, "io-timeout", &io_timeout);

        if quit_edges.is_empty() || list_edges.is_empty() {
            failures.push(format!(
                "coverage: quit has {} shared-map entries beyond the marker and list has {}",
                quit_edges.len(),
                list_edges.len()
            ));
        } else if symmetric_diff(&quit_edges, &list_edges) == 0 {
            failures.push(
                "coverage: quit and list produced the same shared-map entries beyond the marker"
                    .to_string(),
            );
        } else {
            println!(
                "accept: quit {} entries, list {} entries, symmetric difference {}",
                quit_edges.len(),
                list_edges.len(),
                symmetric_diff(&quit_edges, &list_edges)
            );
        }
        if !retr_edges.is_empty() && symmetric_diff(&retr_edges, &list_edges) == 0 {
            failures.push(
                "coverage: retr and list produced the same shared-map entries beyond the marker"
                    .to_string(),
            );
        }
        if let Some(identity) = session.executor.last_identity().cloned() {
            check_identity(&identity, &mut failures);
        } else {
            failures.push("coverage: no forkserver child identity was recorded".to_string());
        }

        session.evaluate(&quit)?;
        session.evaluate(&list)?;
        let corpus = session.state.corpus().count();
        if corpus < 2 {
            failures.push(format!(
                "corpus: expected quit and list to both be kept, corpus count is {corpus}"
            ));
        } else {
            println!("accept: corpus kept {corpus} inputs");
        }
        let saved = first_corpus_file(&scratch.campaign.corpus);
        match saved {
            Some(path) => {
                let saved_input = GeneratorInput::from_file(&path)?;
                let first = interesting_indexes(&replay_edges(&mut session, &saved_input)?);
                let second = interesting_indexes(&replay_edges(&mut session, &saved_input)?);
                let score = jaccard(&first, &second);
                println!(
                    "accept: replay {} jaccard {score:.3} ({} and {} entries)",
                    path.display(),
                    first.len(),
                    second.len()
                );
                if first.is_empty() || score < 0.8 {
                    failures.push(format!(
                        "replay: jaccard {score:.3} for {} (need >= 0.8 and a non-empty map)",
                        path.display()
                    ));
                }
            }
            None => failures.push("replay: corpus directory has no saved input".to_string()),
        }

        check_protocol(&mut session, &mut failures, "globals-a", &globals, true);
        check_protocol(&mut session, &mut failures, "stor", &stor, true);
        check_protocol(
            &mut session,
            &mut failures,
            "absent-after-stor",
            &absent,
            true,
        );
        check_protocol(
            &mut session,
            &mut failures,
            "globals-after-stor",
            &globals,
            true,
        );
        check_protocol(
            &mut session,
            &mut failures,
            "retr-after-restore",
            &retr,
            true,
        );

        let fd_start = fd_count(std::process::id() as i32);
        let py_start = processes_matching("generator_server");
        let mut rounds = 0u32;
        for round in 1..=100 {
            match session.execute(&list) {
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

        let case_ms = session.executor.case_budget_ms();
        let grace_ms = session.executor.exit_grace_ms();
        session.executor.set_case_budget(1_500, 4_000);
        let hang = mode_input(&scratch, "hang", "ubuntu", "ubuntu");
        match session.execute(&hang) {
            Ok(ExitKind::Timeout) => {
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
                let cleanup_signal = report.as_ref().and_then(|item| item.cleanup_signal);
                println!("accept: python stall -> Timeout target {target} generator {generator} {detail}");
                if !target.starts_with("signal:") {
                    failures.push(format!(
                        "python stall: target status {target}; expected the blocked SUT to be terminated after the generator deadline"
                    ));
                }
                if generator != "timeout" || detail != "case deadline" {
                    failures.push(format!(
                        "python stall: generator status {generator} with detail {detail:?}, expected case deadline"
                    ));
                }
                if cleanup_signal.is_none() {
                    failures.push(
                        "python stall: timed-out SUT has no recorded cleanup signal".to_string(),
                    );
                }
            }
            Ok(kind) => failures.push(format!(
                "python stall: expected a bounded timeout, got {kind:?}"
            )),
            Err(error) => failures.push(format!("python stall: {error:?}")),
        }
        session.executor.set_case_budget(case_ms, grace_ms);

        let mut stages = tuple_list!(StdMutationalStage::new(GeneratorMutator::new()));
        if session.state.corpus().count() == 0 {
            failures.push("fuzz: corpus is empty, so the mutational stage was not run".to_string());
        } else if let Err(error) = session.fuzzer.fuzz_loop_for(
            &mut stages,
            &mut session.executor,
            &mut session.state,
            &mut session.mgr,
            3,
        ) {
            failures.push(format!("fuzz: 3 mutational iterations failed: {error:?}"));
        } else {
            println!(
                "accept: 3 mutational iterations finished; corpus {}; shared-map entries {}",
                session.state.corpus().count(),
                interesting_indexes(&session.executor.edge_indexes()).len()
            );
        }
    }

    finish_accept("main", failures)
}

pub(super) fn accept_lightftp_hang(campaign: &Campaign) -> Result<(), Error> {
    require_exec(&campaign.target.binary)?;
    let mut failures = Vec::new();
    let mut stuck = accept_scratch(campaign);
    stuck
        .target
        .extra_env
        .push(("MCN_TEST_HANG".to_string(), "client".to_string()));
    stuck.target.workdir = stuck.base.join("accept-hang-work");
    stuck.campaign.corpus = stuck.base.join("accept-hang-corpus");
    stuck.campaign.crashes = stuck.base.join("accept-hang-solutions");
    stuck.timeouts.case_ms = 400;
    stuck.timeouts.exit_grace_ms = 300;
    let _ = fs::remove_dir_all(&stuck.campaign.corpus);
    let _ = fs::remove_dir_all(&stuck.campaign.crashes);
    let _ = fs::remove_dir_all(&stuck.target.workdir);
    match Session::open(&stuck) {
        Ok(mut session) => {
            let input = mode_input(&stuck, "quit", "ubuntu", "ubuntu");
            match session.execute(&input) {
                Ok(ExitKind::Timeout) => {
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
                    println!(
                        "accept: target hang -> Timeout target {target} generator {generator} {detail}"
                    );
                    if target.starts_with("exited:") {
                        failures.push(format!(
                            "hang: target {target} exited normally and must not be a target timeout"
                        ));
                    }
                    if report
                        .as_ref()
                        .is_some_and(|item| item.exit_kind == "crash")
                    {
                        failures.push("hang: target hang was stored as a crash".to_string());
                    }
                }
                Ok(kind) => failures.push(format!(
                    "hang: expected Timeout after the case deadline and exit grace, got {kind:?}"
                )),
                Err(error) => failures.push(format!("hang: {error:?}")),
            }
        }
        Err(error) => failures.push(format!("hang: failed to start the session: {error:?}")),
    }
    finish_accept("hang", failures)
}

pub(super) fn accept_lightftp_crash(campaign: &Campaign) -> Result<(), Error> {
    require_exec(&campaign.target.binary)?;
    let mut failures = Vec::new();
    let mut crash = accept_scratch(campaign);
    crash
        .target
        .extra_env
        .push(("MCN_TEST_CRASH".to_string(), "client".to_string()));
    crash.target.workdir = crash.base.join("accept-crash-work");
    crash.campaign.corpus = crash.base.join("accept-crash-corpus");
    crash.campaign.crashes = crash.base.join("accept-crash-solutions");
    let _ = fs::remove_dir_all(&crash.campaign.corpus);
    let _ = fs::remove_dir_all(&crash.campaign.crashes);
    let _ = fs::remove_dir_all(&crash.target.workdir);
    match Session::open(&crash) {
        Ok(mut session) => {
            let input = mode_input(&crash, "list", "ubuntu", "ubuntu");
            match session.execute(&input) {
                Ok(ExitKind::Crash) => {
                    let status = session
                        .executor
                        .last_report()
                        .map(|item| item.generator_status.clone())
                        .unwrap_or_default();
                    println!(
                        "accept: injected client SIGSEGV classified as crash (generator {status})"
                    );
                    if status == "ok" {
                        failures.push(
                            "crash: generator reported ok, so the client thread may not have been reached"
                                .to_string(),
                        );
                    }
                }
                Ok(kind) => failures.push(format!(
                    "crash: MCN_TEST_CRASH=client was classified as {kind:?}, not Crash"
                )),
                Err(error) => failures.push(format!("crash: {error:?}")),
            }
        }
        Err(error) => failures.push(format!("crash: failed to start the session: {error:?}")),
    }
    finish_accept("crash", failures)
}

fn check_protocol(
    session: &mut Session,
    failures: &mut Vec<String>,
    name: &str,
    input: &GeneratorInput,
    require_clean_exit: bool,
) {
    match session.execute(input) {
        Ok(kind) => {
            let report = session.executor.last_report().cloned();
            let generator = report
                .as_ref()
                .map(|item| item.generator_status.as_str())
                .unwrap_or("");
            let target = report
                .as_ref()
                .map(|item| item.target_status.as_str())
                .unwrap_or("");
            println!("accept: {name} -> {kind:?} target {target} generator {generator}");
            if generator != "ok" {
                let detail = report
                    .as_ref()
                    .and_then(|item| item.generator_error.clone())
                    .unwrap_or_default();
                failures.push(format!("{name}: generator status {generator} {detail}"));
            }
            if require_clean_exit && target != "exited:0" {
                failures.push(format!("{name}: target status {target}, expected exited:0"));
            }
            if matches!(kind, ExitKind::Crash) {
                failures.push(format!("{name}: classified as a target crash"));
            }
        }
        Err(error) => failures.push(format!("{name}: {error:?}")),
    }
}

fn check_generator_timeout(
    session: &mut Session,
    failures: &mut Vec<String>,
    name: &str,
    input: &GeneratorInput,
) {
    match session.execute(input) {
        Ok(ExitKind::Ok) => {
            let report = session.executor.last_report().cloned();
            let generator = report
                .as_ref()
                .map(|item| item.generator_status.as_str())
                .unwrap_or("");
            let target = report
                .as_ref()
                .map(|item| item.target_status.as_str())
                .unwrap_or("");
            let detail = report
                .as_ref()
                .and_then(|item| item.generator_error.clone())
                .unwrap_or_default();
            println!("accept: {name} -> Ok target {target} generator {generator} {detail}");
            if generator != "timeout" || !detail.contains("recv") {
                failures.push(format!(
                    "{name}: expected a generator recv timeout and a normal target exit, got {generator} target {target} {detail}"
                ));
            }
            if target != "exited:0" {
                failures.push(format!(
                    "{name}: target {target} was not a normal exit, so the I/O timeout was not kept separate from a target hang"
                ));
            }
        }
        Ok(kind) => failures.push(format!(
            "{name}: I/O timeout was classified as {kind:?}; a normal target exit must not be a target timeout"
        )),
        Err(error) => failures.push(format!("{name}: {error:?}")),
    }
}

fn mode_input(campaign: &Campaign, mode: &str, user: &str, password: &str) -> GeneratorInput {
    let mut fields = BTreeMap::new();
    fields.insert("user".to_string(), ParamValue::Str(user.to_string()));
    fields.insert(
        "password".to_string(),
        ParamValue::Str(password.to_string()),
    );
    fields.insert("mode".to_string(), ParamValue::Str(mode.to_string()));
    fields.insert("file".to_string(), ParamValue::Str("hello.txt".to_string()));
    fields.insert("pause_ms".to_string(), ParamValue::Int(0));
    let mut params = BTreeMap::new();
    params.insert("session_1".to_string(), fields);
    GeneratorInput::with_trace(
        campaign.base.join("generators").join("ftp_session.py"),
        vec![StepInstance {
            name: "session_1".to_string(),
            step: "ftp_session".to_string(),
            action: "send".to_string(),
            condition: String::new(),
            message: None,
            channel: "control".to_string(),
            sender: None,
            receiver: None,
            party: None,
            omitted_fields: Vec::new(),
            inputs: BTreeMap::new(),
            broken_dependencies: Vec::new(),
        }],
        params,
    )
}

fn first_corpus_file(dir: &Path) -> Option<PathBuf> {
    let mut files = fs::read_dir(dir).ok()?.flatten().collect::<Vec<_>>();
    files.sort_by_key(|entry| entry.file_name());
    files.into_iter().find_map(|entry| {
        let path = entry.path();
        if path.extension().and_then(|ext| ext.to_str()) == Some("json")
            || fs::read_to_string(&path)
                .ok()
                .is_some_and(|text| text.contains("\"generator\""))
        {
            Some(path)
        } else {
            None
        }
    })
}
