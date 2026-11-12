use std::{fs, path::PathBuf};

use serde_json::json;

use super::request::GeneratorRequest;
use crate::coverage::proposal::instantiate;
use crate::protocol::{load_model, Binding, PartyRole};

fn fixture() -> (crate::protocol::IndexedModel, Binding, PathBuf) {
    let dir = std::env::temp_dir().join(format!(
        "mcn-synthesis-test-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .subsec_nanos()
    ));
    fs::create_dir_all(&dir).unwrap();
    let body = json!({
        "meta": {"protocol": "demo", "complete": true, "documents": []},
        "parties": [{"name": "client", "description": "c"}, {"name": "server", "description": "s"}],
        "messages": [{"name": "hello", "associated_fields": []}],
        "channels": [{"name": "control", "description": "c", "transport": "TCP",
                      "connection": "connection_oriented",
                      "endpoint_party_names": ["client", "server"],
                      "initiator_party_name": "client", "acceptor_party_name": "server",
                      "endpoint_sources": []}],
        "steps": [{"name": "hello", "action": "send", "message_name": "hello",
                   "channel_name": "control", "sender_name": "client",
                   "receiver_name": "server", "condition": "start"}],
        "step_dependencies": [],
        "unresolved": []
    });
    let path = dir.join("protocol.json");
    fs::write(&path, body.to_string()).unwrap();
    let model = load_model(&path).unwrap();
    let binding = Binding::new(
        [
            ("client".to_string(), PartyRole::Generator),
            ("server".to_string(), PartyRole::Sut),
        ]
        .into_iter()
        .collect(),
    );
    (model, binding, dir)
}

fn write_request(
    dir: &PathBuf,
    model: &crate::protocol::IndexedModel,
    session: &str,
    request: &str,
    origin: &str,
) {
    let mut proposal = instantiate(model, &model.steps).unwrap();
    proposal.origin = origin.to_string();
    proposal.session = session.to_string();
    proposal.goal.reason = "goal".into();
    if origin == "coverage" {
        proposal.goal.handler_ids = vec!["handle_hello".into()];
    }
    for item in &mut proposal.trace {
        item.reason = "target".into();
    }
    fs::write(
        dir.join("proposal.json"),
        serde_json::to_vec_pretty(&proposal).unwrap(),
    )
    .unwrap();
    fs::write(
        dir.join("generator.py"),
        "def generate(ctx, params):\n    return None\n",
    )
    .unwrap();
    fs::write(
        dir.join("params.schema.json"),
        r#"{"type":"object","additionalProperties":false,"properties":{}}"#,
    )
    .unwrap();
    fs::write(dir.join("params.json"), "{}").unwrap();
    let result = json!({
        "request_id": request,
        "status": "ok",
        "added_steps": [],
    });
    fs::write(dir.join("result.json"), result.to_string()).unwrap();
}

fn request(dir: PathBuf, id: &str, autonomous: bool) -> GeneratorRequest {
    GeneratorRequest {
        id: id.to_string(),
        output_dir: dir,
        session: "s".into(),
        autonomous,
    }
}

#[test]
fn autonomous_request_accepts_its_own_proposal_without_a_receipt() {
    let (model, _binding, dir) = fixture();
    write_request(&dir, &model, "s", "r1", "autonomous");
    let request = request(dir.clone(), "r1", true);
    let validated = request.accept().unwrap();
    assert_eq!(validated.origin.step_names, vec!["hello".to_string()]);
    assert_eq!(validated.origin.request_id, "r1");
    assert!(dir.join("published/generator.py").is_file());
    assert!(!dir.join("receipt.json").exists());
}

#[test]
fn dependency_state_keys_become_instance_inputs() {
    let (model, _binding, dir) = fixture();
    write_request(&dir, &model, "s", "r1", "autonomous");
    let path = dir.join("proposal.json");
    let mut proposal: serde_json::Value =
        serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    let mut second = proposal["trace"][0].clone();
    second["name"] = json!("hello_2");
    proposal["trace"].as_array_mut().unwrap().push(second);
    proposal["dependencies"] = json!([{
        "prior": "hello", "dependent": "hello_2", "kind": "data", "fields": ["token"]
    }]);
    fs::write(&path, serde_json::to_vec(&proposal).unwrap()).unwrap();
    let validated = request(dir, "r1", true).accept().unwrap();
    assert_eq!(
        validated.input.trace.steps[1].inputs["token"],
        "hello.token"
    );
    assert_eq!(validated.input.trace.dependencies[0].fields, vec!["token"]);
}

#[test]
fn accepted_input_carries_step_trace_and_mutable_field_defaults() {
    let (model, _binding, dir) = fixture();
    write_request(&dir, &model, "s", "r1", "autonomous");
    let mut proposal: crate::coverage::proposal::Proposal =
        serde_json::from_slice(&fs::read(dir.join("proposal.json")).unwrap()).unwrap();
    proposal.trace[0]
        .mutable_fields
        .push(crate::coverage::proposal::MutableField {
            field: "username".into(),
            kind: "string".into(),
            default: None,
            source: "proposal".into(),
        });
    fs::write(
        dir.join("proposal.json"),
        serde_json::to_vec_pretty(&proposal).unwrap(),
    )
    .unwrap();
    fs::write(
        dir.join("params.schema.json"),
        r#"{"type":"object","additionalProperties":false,"properties":{"username":{"type":"string"}}}"#,
    )
    .unwrap();
    fs::write(dir.join("params.json"), r#"{"username":"ubuntu"}"#).unwrap();
    let request = request(dir.clone(), "r1", true);
    let validated = request.accept().unwrap();
    assert_eq!(validated.input.trace.steps.len(), 1);
    assert_eq!(
        validated.input.trace.steps[0].fields.get("username"),
        Some(&crate::engine::input::ParamValue::Str("ubuntu".into()))
    );
}

#[test]
fn coverage_request_rejects_a_different_request() {
    let (model, _binding, dir) = fixture();
    write_request(&dir, &model, "s", "analysis-1", "coverage");
    let request = request(dir.clone(), "synth-1", false);
    let error = request.accept().unwrap_err();
    assert!(error.contains("request_id"), "{error}");
}

#[test]
fn coverage_proposal_keeps_its_analysis_request_identity() {
    let (model, _binding, dir) = fixture();
    write_request(&dir, &model, "s", "analysis-1", "coverage");
    fs::write(
        dir.join("result.json"),
        json!({"request_id": "analysis-1", "status": "ok", "added_steps": []}).to_string(),
    )
    .unwrap();
    let request = request(dir.clone(), "analysis-1", false);
    let validated = request.accept().unwrap();
    assert_eq!(validated.origin.request_id, "analysis-1");
    assert_eq!(validated.origin.step_names, vec!["hello".to_string()]);
}

fn timeout_stage() -> super::GeneratorSynthesisStage {
    let campaign: crate::app::config::Campaign = toml::from_str(
        r#"
[target]
binary = "b"
workdir = "w"
port = 0
[timeouts]
connect_ms = 1
io_ms = 1
case_ms = 1
exit_grace_ms = 1
[campaign]
seeds = "s"
corpus = "co"
crashes = "cr"
python = "python"
pythonpath = "pp"
"#,
    )
    .unwrap();
    let mut stage = super::GeneratorSynthesisStage::disabled(
        &campaign,
        crate::coverage::proposal::ProposalMailbox::default(),
    );
    stage.initialization_started = true;
    stage
}

fn init_timeout() -> crate::agents::AgentFailure {
    crate::agents::AgentFailure::new(
        "ready.json was not published before the deadline".into(),
        crate::agents::TerminalFact::default(),
    )
}

#[test]
fn init_timeout_count_is_per_stage_and_only_the_third_is_fatal() {
    let mut first = timeout_stage();
    let mut second = timeout_stage();
    for count in 1..=3 {
        let result = first.consume_init_timeout(init_timeout());
        assert_eq!(result.is_err(), count == 3);
        assert_eq!(first.init_timeouts, count);
        assert_eq!(second.init_timeouts, 0);
        assert_eq!(first.paused, count == 3);
        assert!(first.initialization_started);
        assert!(!first.initialized);
    }
    second.consume_init_timeout(init_timeout()).unwrap();
    assert_eq!(second.init_timeouts, 1);
}

#[test]
fn bootstrap_propagates_analysis_failure_while_waiting_for_synthesis() {
    let (_, _, dir) = fixture();
    let mut stage = timeout_stage();
    stage.enabled = true;
    stage.output = dir.clone();
    let error = stage
        .wait_initialized(&mut || {
            Err(libafl::Error::unknown(
                "coverage analysis initialization timed out 3 times",
            ))
        })
        .unwrap_err();
    assert!(error
        .to_string()
        .contains("coverage analysis initialization timed out 3 times"));
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn bootstrap_times_out_on_the_third_notification_without_sending_init_again() {
    let (_, _, dir) = fixture();
    let mut stage = timeout_stage();
    stage.enabled = true;
    stage.output = dir.clone();
    let (commands, requests) = std::sync::mpsc::channel();
    let (outcomes, receiver) = std::sync::mpsc::channel();
    stage.worker = Some(crate::agents::AgentClient::from_channels(
        commands, receiver,
    ));
    for _ in 0..3 {
        outcomes
            .send(super::Outcome::InitTimedOut(init_timeout()))
            .unwrap();
    }
    let error = stage.wait_initialized(&mut || Ok(())).unwrap_err();
    assert!(error
        .to_string()
        .contains("synthesis initialization timed out 3 times"));
    assert_eq!(stage.init_timeouts, 3);
    assert!(requests.try_recv().is_err(), "must not send another init");
    stage.shutdown();
    fs::remove_dir_all(dir).unwrap();
}
