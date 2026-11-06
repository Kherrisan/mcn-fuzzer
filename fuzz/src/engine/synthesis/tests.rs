use std::{fs, path::PathBuf, sync::Arc};

use serde_json::json;

use super::request::GeneratorRequest;
use crate::coverage::proposal::{proposal_id, Proposal, ProposalGoal, ProposalStep};
use crate::protocol::{load_model, Binding, PartyRole, TraceId};

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
        "meta": {"schema_version": "7.0.0", "protocol": "demo", "complete": true, "documents": []},
        "parties": [{"name": "client", "description": "c"}, {"name": "server", "description": "s"}],
        "messages": [{"name": "hello", "associated_fields": []}],
        "channels": [{"name": "control", "description": "c", "transport": "TCP",
                      "connection": "connection_oriented",
                      "endpoint_party_names": ["client", "server"],
                      "initiator_party_name": "client", "acceptor_party_name": "server",
                      "endpoint_sources": []}],
        "steps": [{"id": "step-001", "name": "hello", "message_name": "hello",
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
    proposal_id_value: Option<&str>,
    trace_id: Option<&str>,
) {
    let step = &model.steps[0];
    let trace_id = trace_id
        .map(str::to_string)
        .unwrap_or_else(|| TraceId::from_steps(std::slice::from_ref(step)).0);
    let mut proposal = Proposal {
        version: 1,
        proposal_id: String::new(),
        origin: origin.to_string(),
        session: session.to_string(),
        request_id: request.to_string(),
        build: String::new(),
        snapshot: String::new(),
        protocol_id: model.protocol_id.clone(),
        graph_revision: 0,
        trace_id: trace_id.clone(),
        goal: ProposalGoal {
            handler_ids: vec![],
            reason: "goal".into(),
        },
        trace: vec![ProposalStep {
            step_id: step.id().0,
            handler_ids: vec![],
            reason: "target".into(),
            constraints: vec![],
        }],
        context: json!({}),
    };
    proposal.proposal_id = proposal_id_value
        .map(str::to_string)
        .unwrap_or_else(|| proposal_id(&proposal).unwrap());
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
        "protocol_id": model.protocol_id,
        "proposal_id": proposal.proposal_id,
        "trace_id": trace_id,
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
    proposal_id: Option<String>,
    trace_id: Option<String>,
    autonomous: bool,
) -> GeneratorRequest {
    GeneratorRequest {
        id: id.to_string(),
        output_dir: dir,
        protocol_id: model.protocol_id.clone(),
        session: "s".into(),
        build: String::new(),
        snapshot: String::new(),
        proposal_id,
        trace_id,
        model: Arc::new(model.clone()),
        binding: binding.clone(),
        autonomous,
    }
}

#[test]
fn autonomous_request_accepts_its_own_proposal_without_a_receipt() {
    let (model, binding, dir) = fixture();
    write_request(&dir, &model, "s", "r1", "autonomous", None, None);
    let request = request(&model, &binding, dir.clone(), "r1", None, None, true);
    let validated = request.accept().unwrap();
    assert_eq!(validated.origin.step_ids, vec![model.steps[0].id().0]);
    assert!(dir.join("published/generator.py").is_file());
    assert!(!dir.join("receipt.json").exists());
}

#[test]
fn coverage_request_rejects_a_different_proposal() {
    let (model, binding, dir) = fixture();
    write_request(&dir, &model, "s", "r1", "coverage", Some("deadbeef"), None);
    let request = request(
        &model,
        &binding,
        dir.clone(),
        "r1",
        Some("cafebabe".into()),
        None,
        false,
    );
    let error = request.accept().unwrap_err();
    assert!(error.contains("proposal_id"), "{error}");
}

#[test]
fn coverage_proposal_keeps_its_analysis_request_identity() {
    let (model, binding, dir) = fixture();
    write_request(&dir, &model, "s", "analysis-1", "coverage", None, None);
    let mut proposal =
        crate::coverage::proposal::Proposal::read(&dir.join("proposal.json")).unwrap();
    proposal.goal.handler_ids = vec!["h1".into()];
    proposal.trace[0].handler_ids = vec!["h1".into()];
    proposal.proposal_id = proposal_id(&proposal).unwrap();
    fs::write(
        dir.join("proposal.json"),
        serde_json::to_vec_pretty(&proposal).unwrap(),
    )
    .unwrap();
    let result = json!({
        "request_id": "synth-1",
        "protocol_id": model.protocol_id,
        "proposal_id": proposal.proposal_id,
        "trace_id": proposal.trace_id,
        "status": "ok",
        "added_steps": [],
    });
    fs::write(dir.join("result.json"), result.to_string()).unwrap();
    let request = request(
        &model,
        &binding,
        dir.clone(),
        "synth-1",
        Some(proposal.proposal_id.clone()),
        Some(proposal.trace_id.clone()),
        false,
    );
    let validated = request.accept().unwrap();
    assert_eq!(validated.origin.trace_id, proposal.trace_id);
}
