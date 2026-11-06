//! Coverage-analysis requests, acceptance, and the persistent event log.
//!
//! The worker only prepares artifacts. The fuzzer thread accepts a result.

use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

use serde_json::{json, Value};

use crate::protocol::{DepEdge, IndexedModel, ProtocolExplorationGraph, SelectionReport};

/// Shared event log. The fuzzer thread is the only appender.
pub type SharedLog = std::sync::Arc<std::sync::Mutex<AnalysisLog>>;

pub fn shared_log(
    path: std::path::PathBuf,
    session: impl Into<String>,
    build: impl Into<String>,
) -> SharedLog {
    std::sync::Arc::new(std::sync::Mutex::new(AnalysisLog::open(
        path, session, build,
    )))
}

/// Write every selector event into the persistent JSONL log.
pub fn record_selection(log: &mut AnalysisLog, report: &SelectionReport) -> Result<(), String> {
    for event in &report.events {
        let kind = event
            .get("kind")
            .and_then(Value::as_str)
            .unwrap_or("selection");
        log.append(kind, event.clone())?;
        log::info!("exploration {kind} revision={}", report.revision);
    }
    Ok(())
}

const INIT_PROMPT: &str = include_str!("../../prompts/coverage-analysis-init.md");
const TASK_PROMPT: &str = include_str!("../../prompts/coverage-analysis-task.md");

/// Append-only analysis event log. One writer, no multi-process append.
pub struct AnalysisLog {
    path: PathBuf,
    seq: u64,
    session: String,
    build: String,
}

impl AnalysisLog {
    pub fn open(path: PathBuf, session: impl Into<String>, build: impl Into<String>) -> Self {
        Self {
            path,
            seq: 0,
            session: session.into(),
            build: build.into(),
        }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn append(&mut self, kind: &str, body: Value) -> Result<u64, String> {
        self.seq += 1;
        let event = json!({
            "time": now_millis(),
            "kind": kind,
            "seq": self.seq,
            "session": self.session,
            "build": self.build,
            "request": body.get("request").cloned().unwrap_or(Value::Null),
            "snapshot": body.get("snapshot").cloned().unwrap_or(Value::Null),
            "input_protocol": body.get("input_protocol").cloned().unwrap_or(Value::Null),
            "output_protocol": body.get("output_protocol").cloned().unwrap_or(Value::Null),
            "model_version": body.get("model_version").cloned().unwrap_or(Value::Null),
            "selection": body.get("selection").cloned().unwrap_or(Value::Null),
            "attempt": body.get("attempt").cloned().unwrap_or(Value::Null),
            "trace": body.get("trace").cloned().unwrap_or(Value::Null),
            "body": body,
        });
        if let Some(parent) = self.path.parent() {
            fs::create_dir_all(parent)
                .map_err(|error| format!("creating analysis log dir: {error}"))?;
        }
        let mut file = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
            .map_err(|error| format!("opening analysis log {}: {error}", self.path.display()))?;
        writeln!(file, "{event}").map_err(|error| format!("writing analysis log: {error}"))?;
        log::info!(
            "coverage analysis {kind} seq={} session={} build={}",
            self.seq,
            self.session,
            self.build
        );
        Ok(self.seq)
    }
}

fn now_millis() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis())
        .unwrap_or(0)
}

/// One accepted analysis update. Rejected updates leave the graph unchanged.
#[derive(Debug)]
pub struct AcceptedAnalysis {
    pub added: Vec<DepEdge>,
    pub priority_events: Vec<Value>,
}

pub fn accept_analysis(
    graph: &mut ProtocolExplorationGraph,
    model: &IndexedModel,
    request: &str,
    session: &str,
    build: &str,
    snapshot: &str,
    input_protocol: &str,
    result: &Value,
    dependencies: &[DepEdge],
    deltas: &[(String, f64, String)],
    log: &mut AnalysisLog,
) -> Result<AcceptedAnalysis, String> {
    let status = result.get("status").and_then(Value::as_str).unwrap_or("");
    if result.get("session").and_then(Value::as_str) != Some(session)
        || result.get("request").and_then(Value::as_str) != Some(request)
        || result.get("build").and_then(Value::as_str) != Some(build)
        || result.get("snapshot").and_then(Value::as_str) != Some(snapshot)
        || result.get("input_protocol").and_then(Value::as_str) != Some(input_protocol)
    {
        let reason = "result identity does not match the frozen request";
        let _ = log.append(
            "model_rejected",
            json!({"request": request, "snapshot": snapshot, "reason": reason, "status": "rejected"}),
        );
        return Err(reason.to_string());
    }
    if status != "ok" {
        let reason = format!("analysis status is {status}");
        let _ = log.append(
            "model_rejected",
            json!({"request": request, "reason": reason, "status": "rejected"}),
        );
        return Err(reason);
    }
    if graph.applied.contains(request) {
        let _ = log.append(
            "model_ignored",
            json!({"request": request, "reason": "duplicate result", "status": "ignored"}),
        );
        return Err("duplicate result".to_string());
    }
    for (step_id, delta, _) in deltas {
        if graph.score_of(step_id).is_none() {
            let reason = format!("unknown step id {step_id}");
            let _ = log.append(
                "model_rejected",
                json!({"request": request, "reason": reason, "status": "rejected"}),
            );
            return Err(reason);
        }
        if !delta.is_finite() {
            let reason = format!("non-finite delta for {step_id}");
            let _ = log.append(
                "model_rejected",
                json!({"request": request, "reason": reason, "status": "rejected"}),
            );
            return Err(reason);
        }
    }
    let added = match graph.try_add_dependencies(dependencies) {
        Ok(added) => added,
        Err(error) => {
            let _ = log.append(
                "model_rejected",
                json!({"request": request, "reason": error, "status": "rejected"}),
            );
            return Err(error);
        }
    };
    let priority_events = match graph.apply_deltas(request, deltas) {
        Ok(events) => events,
        Err(error) => {
            let _ = log.append(
                "model_rejected",
                json!({"request": request, "reason": error, "status": "rejected"}),
            );
            return Err(error);
        }
    };
    let _ = log.append(
        "model_accepted",
        json!({
            "request": request,
            "snapshot": snapshot,
            "input_protocol": input_protocol,
            "output_protocol": model.protocol_id,
            "status": "accepted",
            "dependencies": added,
            "priority": priority_events,
        }),
    );
    let _ = log.append(
        "priority_snapshot",
        json!({
            "request": request,
            "model_version": graph.priority_version,
            "steps": graph.eligible_scores(model, &crate::protocol::Binding::default()),
        }),
    );
    Ok(AcceptedAnalysis {
        added,
        priority_events,
    })
}

/// Render the initialization prompt. The caller publishes it through HC.
pub fn initialization_prompt(
    session: &str,
    workspace: &str,
    source_dir: &str,
    model: &str,
    sut_parties: &[String],
) -> String {
    INIT_PROMPT
        .replace("{{ session_id }}", session)
        .replace("{{ campaign.workspace }}", workspace)
        .replace("{{ source_dir }}", source_dir)
        .replace("{{ model }}", model)
        .replace("{{ sut_parties }}", &json!(sut_parties).to_string())
        .replace(
            "{{ execution.summary }}",
            "Coverage analysis reads the SUT source directory and protocol model.",
        )
}

/// Render one analysis task prompt.
pub struct TaskPaths<'a> {
    pub session: &'a str,
    pub request: &'a str,
    pub output: &'a str,
    pub snapshot: &'a str,
    pub snapshot_dir: &'a str,
    pub build: &'a str,
    pub callgraph_json: &'a str,
    pub cfg_json: &'a str,
    pub manifest: &'a str,
    pub binary: &'a str,
    pub model: &'a str,
    pub source_dir: &'a str,
}

pub fn task_prompt(paths: TaskPaths<'_>) -> String {
    TASK_PROMPT
        .replace("{{ session_id }}", paths.session)
        .replace("{{ request_id }}", paths.request)
        .replace("{{ output_dir }}", paths.output)
        .replace("{{ snapshot }}", paths.snapshot)
        .replace("{{ snapshot_dir }}", paths.snapshot_dir)
        .replace("{{ build }}", paths.build)
        .replace("{{ callgraph_json }}", paths.callgraph_json)
        .replace("{{ cfg_json }}", paths.cfg_json)
        .replace("{{ manifest }}", paths.manifest)
        .replace("{{ binary }}", paths.binary)
        .replace("{{ model }}", paths.model)
        .replace("{{ source_dir }}", paths.source_dir)
}

/// Log the tool's handler and region artifacts. Missing rows are a hard error.
pub fn record_measurements(
    log: &mut AnalysisLog,
    request: &str,
    snapshot: &str,
    handlers: &[Value],
    regions: &[Value],
) -> Result<(), String> {
    log_handlers(log, request, handlers)?;
    log_regions(log, request, snapshot, regions)?;
    Ok(())
}

/// Record every handler mapping, including misses, as linked events.
pub fn log_handlers(log: &mut AnalysisLog, request: &str, rows: &[Value]) -> Result<(), String> {
    log.append(
        "handlers_begin",
        json!({"request": request, "count": rows.len()}),
    )?;
    for row in rows {
        log.append("handler_mapping", json!({"request": request, "row": row}))?;
    }
    log.append(
        "handlers_complete",
        json!({"request": request, "count": rows.len()}),
    )?;
    Ok(())
}

/// Record uncovered regions, including an explicit zero result.
pub fn log_regions(
    log: &mut AnalysisLog,
    request: &str,
    snapshot: &str,
    regions: &[Value],
) -> Result<(), String> {
    if regions.is_empty() {
        log.append(
            "uncovered_regions",
            json!({"request": request, "snapshot": snapshot, "count": 0, "zero": true}),
        )?;
        return Ok(());
    }
    log.append(
        "uncovered_regions_begin",
        json!({"request": request, "snapshot": snapshot, "count": regions.len()}),
    )?;
    for region in regions {
        log.append(
            "uncovered_region",
            json!({"request": request, "snapshot": snapshot, "region": region}),
        )?;
        if let Some(unknown) = region.get("unknown").and_then(Value::as_array) {
            for boundary in unknown {
                log.append(
                    "unknown_boundary",
                    json!({
                        "request": request,
                        "snapshot": snapshot,
                        "handler": region.get("handler").cloned().unwrap_or(Value::Null),
                        "boundary": boundary,
                    }),
                )?;
            }
        }
    }
    log.append(
        "uncovered_regions_complete",
        json!({"request": request, "snapshot": snapshot, "count": regions.len()}),
    )?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::agents::{HcClient, MockCore};
    use crate::protocol::{load_model, Binding, PartyRole};
    use serde_json::json;
    use std::collections::BTreeMap;
    use std::fs;
    use std::sync::atomic::{AtomicU64, Ordering};

    static TEMP: AtomicU64 = AtomicU64::new(0);

    fn model() -> IndexedModel {
        let value = json!({
            "meta": {"schema_version": "7.0.0", "protocol": "p", "complete": false, "documents": [{"doc_id": "d", "rfc": "RFC 1", "input_sha256": "a", "document_sha256": "b", "path": "doc.json"}]},
            "parties": [{"name": "client", "description": "c"}, {"name": "server", "description": "s"}],
            "messages": [{"name": "m", "associated_fields": []}],
            "channels": [{"name": "c", "description": "c", "transport": "TCP", "connection": "connection_oriented", "endpoint_party_names": ["client", "server"], "initiator_party_name": "client", "acceptor_party_name": "server", "endpoint_sources": []}],
            "steps": [
                {"id": "1", "name": "a", "message_name": "m", "channel_name": "c", "sender_name": "client", "receiver_name": "server", "condition": "go"},
                {"id": "2", "name": "b", "message_name": "m", "channel_name": "c", "sender_name": "server", "receiver_name": "client", "condition": "go"}
            ],
            "step_dependencies": [],
            "unresolved": []
        });
        let path = std::env::temp_dir().join(format!(
            "mcn-analysis-{}-{}.json",
            std::process::id(),
            TEMP.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::write(&path, serde_json::to_vec(&value).unwrap()).unwrap();
        load_model(&path).unwrap()
    }

    #[test]
    fn rejected_cycle_does_not_change_scores_and_log_failure_is_visible() {
        let model = model();
        let mut graph = ProtocolExplorationGraph::from_model(&model);
        let dir = std::env::temp_dir().join(format!("mcn-analysis-log-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let mut log = AnalysisLog::open(dir.join("events.jsonl"), "s1", "build");
        let error = accept_analysis(
            &mut graph,
            &model,
            "r1",
            "s1",
            "build",
            "snap",
            &model.protocol_id,
            &json!({"status": "ok", "session": "s1", "request": "r1", "build": "build", "snapshot": "snap", "input_protocol": model.protocol_id}),
            &[DepEdge { prior: "a".into(), dependent: "b".into(), condition: "ab".into(), field_relations: vec![] },
              DepEdge { prior: "b".into(), dependent: "a".into(), condition: "ba".into(), field_relations: vec![] }],
            &[(model.steps[0].id().0, 3.0, "nope".into())],
            &mut log,
        )
        .unwrap_err();
        assert!(error.contains("cycle"));
        assert_eq!(graph.score_of(&model.steps[0].id().0), Some(0.0));
        let text = fs::read_to_string(log.path()).unwrap();
        assert!(text.contains("model_rejected"));
        assert!(text.contains("cycle"));
        log_regions(&mut log, "r1", "snap", &[]).unwrap();
        let text = fs::read_to_string(log.path()).unwrap();
        assert!(text.contains("\"zero\":true"));
        let blocked = dir.join("not-a-directory");
        fs::write(&blocked, b"x").unwrap();
        let mut bad = AnalysisLog::open(blocked.join("events.jsonl"), "s1", "build");
        let error = bad.append("priority_snapshot", json!({})).unwrap_err();
        assert!(error.contains("analysis log"));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn closing_one_terminal_leaves_the_other_queryable() {
        let path = std::env::temp_dir().join(format!(
            "mcn-hc-two-{}-{}.sock",
            std::process::id(),
            TEMP.fetch_add(1, Ordering::Relaxed)
        ));
        let core = MockCore::spawn(path.clone()).unwrap();
        let mut client = HcClient::connect(&path, std::time::Duration::from_secs(2)).unwrap();
        client
            .run_terminal(
                "generator",
                &["/bin/true".into()],
                &[],
                &std::env::temp_dir(),
            )
            .unwrap();
        client
            .run_terminal(
                "coverage-analysis",
                &["/bin/true".into()],
                &[],
                &std::env::temp_dir(),
            )
            .unwrap();
        client.kill("generator").unwrap();
        let info = client.get("coverage-analysis").unwrap();
        assert_eq!(info.state, "live");
        drop(core);
        let _ = Binding::new({
            let mut roles = BTreeMap::new();
            roles.insert("client".into(), PartyRole::Generator);
            roles
        });
    }

    #[test]
    fn selector_events_are_appended_to_the_jsonl_log() {
        let model = model();
        let graph = ProtocolExplorationGraph::from_model(&model);
        let mut rand = libafl_bolts::rands::StdRand::with_seed(1);
        let mut roles = BTreeMap::new();
        roles.insert("client".into(), PartyRole::Generator);
        roles.insert("server".into(), PartyRole::Sut);
        let report = graph.select(
            &model,
            &Binding::new(roles),
            &mut rand,
            &crate::protocol::SelectionContext::default(),
            4,
            1,
            2,
        );
        let dir = std::env::temp_dir().join(format!("mcn-selection-log-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let mut log = AnalysisLog::open(dir.join("events.jsonl"), "s1", "build");
        record_selection(&mut log, &report).unwrap();
        let text = fs::read_to_string(log.path()).unwrap();
        assert!(text.contains("priority_snapshot"));
        assert!(text.contains("\"steps\""));
        assert!(
            text.contains("components") || text.contains("trace") || text.contains("trace_skipped")
        );
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn initialization_prompt_requires_ready_json() {
        let source = "/only-for-coverage-analysis/sut-src";
        let prompt = initialization_prompt(
            "s-test",
            "/work",
            source,
            "/model.json",
            &["server-PI".into(), "server-DTP".into()],
        );
        assert!(prompt.contains("WRITE ready.json"));
        assert!(prompt.contains("SESSION=s-test"));
        assert!(prompt.contains(&format!("SOURCE_DIR={source}")));
        assert!(prompt.contains(r#"SUT_PARTIES=["server-PI","server-DTP"]"#));
        assert!(!prompt.contains("{{ sut_parties }}"));
        let task = task_prompt(TaskPaths {
            session: "s-test",
            request: "r1",
            output: "/out",
            snapshot: "3",
            snapshot_dir: "/snap",
            build: "build",
            callgraph_json: "/graphs/callgraph.json",
            cfg_json: "/graphs/cfg.json",
            manifest: "/graphs/manifest.json",
            binary: "/graphs/fftp",
            model: "/model.json",
            source_dir: source,
        });
        assert!(task.contains("REQUEST=r1"));
        assert!(!task.contains("coverage_analysis.measure"));
        assert!(task.contains("in-process"));
        assert!(
            task.contains("--callgraph-json /graphs/callgraph.json")
                || task.contains("CALLGRAPH_JSON=/graphs/callgraph.json")
        );
        assert!(task.contains("CFG_JSON=/graphs/cfg.json"));
        assert!(task.contains("MANIFEST=/graphs/manifest.json"));
        assert!(task.contains(&format!("SOURCE_DIR={source}")));
        assert!(task.contains("step_dependencies"));
        assert!(task.contains("priority-delta.json"));
        assert!(prompt.contains("handlers.json"));
        assert!(prompt.contains("MODEL=/model.json"));
        assert!(task.contains("handlers.json"));
        assert!(task.contains("dispatch"));
        assert!(task.contains("do not open regions.json"));
        assert!(task.contains("region_summary"));
        assert!(task.contains("INPUT_FILE.steps"));
        assert!(task.contains("unknown boundaries"));
        assert!(task.contains("Do not search or list SOURCE_DIR"));
    }

    #[test]
    fn measurements_log_every_mapping_region_and_unknown_boundary() {
        let dir = std::env::temp_dir().join(format!(
            "mcn-measure-log-{}-{}",
            std::process::id(),
            TEMP.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = fs::remove_dir_all(&dir);
        let mut log = AnalysisLog::open(dir.join("events.jsonl"), "s1", "build");
        let handlers = vec![json!({
            "message": "USER",
            "step_ids": ["1"],
            "step_names": ["login"],
            "handlers": [{"symbol": "ftpUSER", "location": "ftpserv.c:10"}],
            "unmatched": false
        })];
        let regions = vec![json!({
            "handler": "USER",
            "regions": [{
                "branch_directions": 1,
                "functions": ["ftpUSER"],
                "function_count": 1,
                "locations": ["ftpserv.c:1"]
            }],
            "branch_directions": 1,
            "function_count": 1,
            "unknown": [{
                "function": "ftpUSER",
                "location": "ftpserv.c:2",
                "reason": "indirect"
            }],
            "zero": false
        })];
        record_measurements(&mut log, "r1", "000002", &handlers, &regions).unwrap();
        let text = fs::read_to_string(log.path()).unwrap();
        assert!(text.contains("handlers_begin"));
        assert!(text.contains("handler_mapping"));
        assert!(text.contains("ftpUSER"));
        assert!(text.contains("uncovered_region"));
        assert!(text.contains("unknown_boundary"));
        assert!(text.contains("indirect"));
        assert!(text.contains("handlers_complete"));
        assert!(text.contains("uncovered_regions_complete"));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn analysis_prompts_include_source_dir_and_generator_prompts_do_not() {
        let dir = std::env::temp_dir().join(format!(
            "mcn-source-prompt-{}-{}",
            std::process::id(),
            TEMP.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join("sut-src")).unwrap();
        fs::write(
            dir.join("sut-src").join("ftpserv.c"),
            b"int ftpUSER(void){return 0;}",
        )
        .unwrap();
        let model = json!({
            "meta": {"schema_version": "7.0.0", "protocol": "p", "complete": false, "documents": [{"doc_id": "d", "rfc": "RFC 1", "input_sha256": "a", "document_sha256": "b", "path": "doc.json"}]},
            "parties": [{"name": "client", "description": "c"}, {"name": "server", "description": "s"}],
            "messages": [{"name": "m", "associated_fields": []}],
            "channels": [{"name": "c", "description": "c", "transport": "TCP", "connection": "connection_oriented", "endpoint_party_names": ["client", "server"], "initiator_party_name": "client", "acceptor_party_name": "server", "endpoint_sources": []}],
            "steps": [
                {"id": "1", "name": "a", "message_name": "m", "channel_name": "c", "sender_name": "client", "receiver_name": "server", "condition": "go"}
            ],
            "step_dependencies": [],
            "unresolved": []
        });
        fs::write(
            dir.join("protocol.json"),
            serde_json::to_vec(&model).unwrap(),
        )
        .unwrap();
        let source = dir.join("sut-src");
        let toml = format!(
            r#"
[target]
binary = "/bin/true"
source_dir = "{source}"
workdir = "{dir}"
fixture = "{dir}"
config_template = "{dir}/protocol.json"
port = 0
[timeouts]
connect_ms = 1
io_ms = 1
case_ms = 1
exit_grace_ms = 1
[campaign]
seeds = "{dir}"
corpus = "{dir}/corpus"
crashes = "{dir}/crashes"
python = "/usr/bin/python3"
pythonpath = "{dir}"
iterations = 1
[protocol_generation]
enabled = false
model = "{dir}/protocol.json"
[parties]
sut = ["server"]
generator = ["client"]
"#,
            dir = dir.display(),
            source = source.display(),
        );
        fs::write(dir.join("campaign.toml"), toml).unwrap();
        let campaign = crate::app::config::Campaign::load(&dir.join("campaign.toml")).unwrap();
        let resolved = campaign
            .target
            .source_dir
            .as_ref()
            .expect("source")
            .display()
            .to_string();
        let init = initialization_prompt(
            "sess",
            "/work",
            &resolved,
            "/model.json",
            &["server".into()],
        );
        let task = task_prompt(TaskPaths {
            session: "sess",
            request: "r",
            output: "/out",
            snapshot: "1",
            snapshot_dir: "/snap",
            build: "build",
            callgraph_json: "/cg.json",
            cfg_json: "/cfg.json",
            manifest: "/manifest.json",
            binary: "/fftp",
            model: "/protocol.json",
            source_dir: &resolved,
        });
        assert!(init.contains(&format!("SOURCE_DIR={resolved}")));
        assert!(task.contains(&format!("SOURCE_DIR={resolved}")));
        assert!(task.contains("dispatch"));
        let context =
            crate::engine::synthesis::SynthesisPromptContext::from_campaign(&campaign).unwrap();
        let generator_init = context
            .initialization_prompt("sess", false, &dir.join("init"))
            .unwrap();
        let trace = crate::protocol::StepTrace::new(context.model().steps.clone());
        let generator = context
            .generation_prompt(&dir.join("request"), "req", "", &trace)
            .unwrap();
        for prompt in [&generator_init, &generator] {
            assert!(!prompt.contains(&resolved), "{prompt}");
            assert!(!prompt.contains("SOURCE_DIR="), "{prompt}");
            assert!(prompt.contains("Do not read or search the SUT source code"));
        }
        let _ = fs::remove_dir_all(&dir);
    }
}
