use std::{fs, path::PathBuf, sync::Arc};

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
        "meta": {"schema_version": "8.0.0", "protocol": "demo", "complete": true, "documents": []},
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
    proposal.request_id = request.to_string();
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

fn request(
    model: &crate::protocol::IndexedModel,
    binding: &Binding,
    dir: PathBuf,
    id: &str,
    autonomous: bool,
) -> GeneratorRequest {
    GeneratorRequest {
        id: id.to_string(),
        output_dir: dir,
        session: "s".into(),
        build: String::new(),
        snapshot: String::new(),
        model: Arc::new(model.clone()),
        binding: binding.clone(),
        autonomous,
    }
}

#[test]
fn autonomous_request_accepts_its_own_proposal_without_a_receipt() {
    let (model, binding, dir) = fixture();
    write_request(&dir, &model, "s", "r1", "autonomous");
    let request = request(&model, &binding, dir.clone(), "r1", true);
    let validated = request.accept().unwrap();
    assert_eq!(validated.origin.step_names, vec!["hello".to_string()]);
    assert_eq!(validated.origin.request_id, "r1");
    assert!(dir.join("published/generator.py").is_file());
    assert!(!dir.join("receipt.json").exists());
}

#[test]
fn coverage_request_rejects_a_different_request() {
    let (model, binding, dir) = fixture();
    write_request(&dir, &model, "s", "analysis-1", "coverage");
    let request = request(&model, &binding, dir.clone(), "synth-1", false);
    let error = request.accept().unwrap_err();
    assert!(error.contains("request_id"), "{error}");
}

#[test]
fn coverage_proposal_keeps_its_analysis_request_identity() {
    let (model, binding, dir) = fixture();
    write_request(&dir, &model, "s", "analysis-1", "coverage");
    fs::write(
        dir.join("result.json"),
        json!({"request_id": "analysis-1", "status": "ok", "added_steps": []}).to_string(),
    )
    .unwrap();
    let request = request(&model, &binding, dir.clone(), "analysis-1", false);
    let validated = request.accept().unwrap();
    assert_eq!(validated.origin.request_id, "analysis-1");
    assert_eq!(validated.origin.step_names, vec!["hello".to_string()]);
}
