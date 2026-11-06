//! Protocol interaction models, stable identifiers, trace selection, and receipts.
//!
//! The selector does not talk to an agent. A campaign snapshot is loaded once
//! and reused for the whole run.

mod explore;
mod id;
mod model;
mod receipt;
mod select;

pub use explore::{
    DepEdge, ExplorationCheckpoint, PriorityTraceSelector, ProtocolExplorationGraph,
    SelectionReport,
};
pub use id::{protocol_id, StepId, TraceId};
pub use model::{load_model, Binding, IndexedModel, ModelError, PartyRole, StepRecord};
pub use receipt::write_receipt;
pub(crate) use select::covers_sut_and_generator;
pub use select::{RandomTraceSelector, SelectionContext, StepTrace, TraceSelector};

#[cfg(test)]
mod tests {
    use super::*;
    use libafl_bolts::rands::{Rand, StdRand};
    use serde_json::json;

    fn tiny() -> IndexedModel {
        let value = json!({
            "meta": {"schema_version": "7.0.0", "protocol": "demo", "complete": false, "documents": [{"doc_id": "d", "rfc": "RFC 1", "input_sha256": "aa", "document_sha256": "bb", "path": "doc.json"}]},
            "parties": [{"name": "client", "description": "c"}, {"name": "server", "description": "s"}, {"name": "other", "description": "o"}],
            "messages": [{"name": "ping", "associated_fields": []}, {"name": "pong", "associated_fields": [{"name": "code"}]}],
            "channels": [{"name": "control", "description": "c", "transport": "TCP", "connection": "connection_oriented", "endpoint_party_names": ["client", "server"], "initiator_party_name": "client", "acceptor_party_name": "server", "endpoint_sources": []}],
            "steps": [
                {"id": "step-0002", "name": "second", "message_name": "pong", "channel_name": "control", "sender_name": "server", "receiver_name": "client", "condition": "after"},
                {"id": "step-0001", "name": "first", "message_name": "ping", "channel_name": "control", "sender_name": "client", "receiver_name": "server", "condition": "start"}
            ],
            "step_dependencies": [{"prior_step": "first", "dependent_step": "first", "condition": "self", "field_relations": []}],
            "unresolved": [{"description": "open", "object_names": ["ping"]}]
        });
        let path = temp_json("mcn-proto");
        std::fs::write(&path, serde_json::to_vec(&value).unwrap()).unwrap();
        load_model(&path).unwrap()
    }

    fn temp_json(prefix: &str) -> std::path::PathBuf {
        use std::sync::atomic::{AtomicU64, Ordering};
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let n = NEXT.fetch_add(1, Ordering::Relaxed);
        std::env::temp_dir().join(format!("{prefix}-{}-{n}.json", std::process::id()))
    }

    #[test]
    fn step_id_ignores_source_id_and_key_order() {
        let model = tiny();
        let step = &model.steps[1];
        // Keep the prefixes of the existing full identifiers.
        assert_eq!(step.id().0, "5534b33b");
        assert_eq!(StepTrace::new(vec![step.clone()]).id.0, "fc9a94ce");
        let mut changed = step.clone();
        changed.message_name = "pong".to_string();
        assert_ne!(step.id(), changed.id());
        let reordered = serde_json::json!({
            "parties": model.snapshot()["parties"],
            "messages": model.snapshot()["messages"],
            "meta": model.snapshot()["meta"],
            "channels": model.snapshot()["channels"],
            "steps": model.snapshot()["steps"],
            "unresolved": model.snapshot()["unresolved"],
            "step_dependencies": model.snapshot()["step_dependencies"]
        });
        assert_eq!(protocol_id(model.snapshot()), protocol_id(&reordered));
        let mut repeated = model.steps[0].clone();
        let id = repeated.id();
        let once = TraceId::from_steps(std::slice::from_ref(&repeated));
        let twice = TraceId::from_steps(&[repeated.clone(), repeated.clone()]);
        assert_ne!(once, twice);
        repeated.condition = "changed".to_string();
        let changed_id = repeated.id();
        assert_ne!(id, changed_id);
        let reordered_trace = TraceId::from_steps(&[repeated.clone(), model.steps[0].clone()]);
        let original_trace = TraceId::from_steps(&[model.steps[0].clone(), repeated]);
        assert_ne!(reordered_trace, original_trace);
        let mut blocked = std::collections::BTreeMap::new();
        blocked.insert("client".to_string(), PartyRole::Generator);
        blocked.insert("server".to_string(), PartyRole::Unavailable);
        assert!(model.eligible(&Binding::new(blocked)).is_empty());
        let dir = std::env::temp_dir().join(format!("mcn-stepid-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let body = |source: &str, keys_reversed: bool| {
            let step = if keys_reversed {
                format!(
                    r#"{{"condition":"start","receiver_name":"server","sender_name":"client","channel_name":"control","message_name":"ping","name":"first","id":"{source}"}}"#
                )
            } else {
                format!(
                    r#"{{"id":"{source}","name":"first","message_name":"ping","channel_name":"control","sender_name":"client","receiver_name":"server","condition":"start"}}"#
                )
            };
            format!(
                r#"{{"meta":{{"schema_version":"7.0.0","protocol":"demo","complete":false,"documents":[{{"doc_id":"d","rfc":"RFC 1","input_sha256":"aa","document_sha256":"bb","path":"doc.json"}}]}},"parties":[{{"name":"client","description":"c"}},{{"name":"server","description":"s"}}],"messages":[{{"name":"ping","associated_fields":[]}}],"channels":[{{"name":"control","description":"c","transport":"TCP","connection":"connection_oriented","endpoint_party_names":["client","server"],"initiator_party_name":"client","acceptor_party_name":"server","endpoint_sources":[]}}],"steps":[{step}],"step_dependencies":[],"unresolved":[]}}"#
            )
        };
        let left_path = dir.join("a.json");
        let right_path = dir.join("b.json");
        let renumbered_path = dir.join("c.json");
        std::fs::write(&left_path, body("step-0001", false)).unwrap();
        std::fs::write(&right_path, body("step-0001", true)).unwrap();
        std::fs::write(&renumbered_path, body("step-9999", true)).unwrap();
        let left = load_model(&left_path).unwrap();
        let right = load_model(&right_path).unwrap();
        let renumbered = load_model(&renumbered_path).unwrap();
        assert_eq!(left.protocol_id, right.protocol_id);
        assert_ne!(left.protocol_id, renumbered.protocol_id);
        assert_eq!(left.steps[0].id(), right.steps[0].id());
        assert_eq!(left.steps[0].id(), renumbered.steps[0].id());
        assert_ne!(left.steps[0].source_id, renumbered.steps[0].source_id);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn ftp_model_filters_roles_and_rejects_dangling_names() {
        let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../rsc/runs/ftp-gpt-6-sol-high-20261104T031756Z/protocol.json");
        let model = load_model(&path).unwrap_or_else(|error| panic!("{}", error.message()));
        assert!(!model.complete);
        assert_eq!(model.protocol_id.len(), 8);
        let mut roles = std::collections::BTreeMap::new();
        roles.insert("server-PI".to_string(), PartyRole::Sut);
        roles.insert("user-PI".to_string(), PartyRole::Generator);
        let binding = Binding::new(roles);
        let eligible = model.eligible(&binding);
        assert!(!eligible.is_empty());
        assert!(eligible
            .iter()
            .all(|step| step.sender_name == "server-PI" || step.sender_name == "user-PI"));
        let mut rand = StdRand::with_seed(7);
        let first = RandomTraceSelector.select(
            &model,
            &binding,
            &mut rand,
            &SelectionContext::default(),
            8,
            1,
            4,
        );
        let mut rand = StdRand::with_seed(7);
        let second = RandomTraceSelector.select(
            &model,
            &binding,
            &mut rand,
            &SelectionContext::default(),
            8,
            1,
            4,
        );
        assert_eq!(first.unwrap().id, second.unwrap().id);
        let broken = std::env::temp_dir().join(format!("mcn-broken-{}.json", std::process::id()));
        std::fs::write(&broken, br#"{"meta":{"schema_version":"7.0.0","protocol":"x","complete":false,"documents":[{"doc_id":"d","rfc":"RFC 1","input_sha256":"a","document_sha256":"b","path":"p"}]},"parties":[{"name":"a","description":"a"}],"messages":[{"name":"m","associated_fields":[]}],"channels":[],"steps":[{"id":"1","name":"s","message_name":"missing","channel_name":"c","sender_name":"a","receiver_name":"a","condition":"c"}],"step_dependencies":[],"unresolved":[]}"#).unwrap();
        assert!(load_model(&broken).is_err());
        let duplicate = std::env::temp_dir().join(format!("mcn-dup-{}.json", std::process::id()));
        std::fs::write(&duplicate, br#"{"meta":{"schema_version":"7.0.0","protocol":"x","complete":false,"documents":[{"doc_id":"d","rfc":"RFC 1","input_sha256":"a","document_sha256":"b","path":"p"}]},"parties":[{"name":"a","description":"a"},{"name":"a","description":"again"}],"messages":[{"name":"m","associated_fields":[]}],"channels":[],"steps":[],"step_dependencies":[],"unresolved":[]}"#).unwrap();
        let error = load_model(&duplicate).expect_err("duplicate party");
        assert!(
            error.message().contains("duplicate party"),
            "{}",
            error.message()
        );
        let dangling_dep =
            std::env::temp_dir().join(format!("mcn-dep-{}.json", std::process::id()));
        std::fs::write(&dangling_dep, br#"{"meta":{"schema_version":"7.0.0","protocol":"x","complete":false,"documents":[{"doc_id":"d","rfc":"RFC 1","input_sha256":"a","document_sha256":"b","path":"p"}]},"parties":[{"name":"a","description":"a"}],"messages":[{"name":"m","associated_fields":[]}],"channels":[{"name":"c","description":"c","transport":"TCP","connection":"connection_oriented","endpoint_party_names":["a"],"initiator_party_name":null,"acceptor_party_name":null,"endpoint_sources":[]}],"steps":[{"id":"1","name":"s","message_name":"m","channel_name":"c","sender_name":"a","receiver_name":"a","condition":"c"}],"step_dependencies":[{"prior_step":"missing","dependent_step":"s","condition":"c","field_relations":[]}],"unresolved":[]}"#).unwrap();
        let error = load_model(&dangling_dep).expect_err("dangling dependency");
        assert!(
            error.message().contains("missing step"),
            "{}",
            error.message()
        );
        assert!(!model.complete);
    }

    #[test]
    fn receipt_keeps_trace_order_and_sorted_names() {
        let model = tiny();
        let mut roles = std::collections::BTreeMap::new();
        roles.insert("client".to_string(), PartyRole::Generator);
        roles.insert("server".to_string(), PartyRole::Sut);
        let binding = Binding::new(roles);
        let mut rand = StdRand::with_seed(3);
        let trace = RandomTraceSelector
            .select(
                &model,
                &binding,
                &mut rand,
                &SelectionContext::default(),
                4,
                2,
                2,
            )
            .unwrap();
        let path = std::env::temp_dir().join(format!("mcn-receipt-{}.json", std::process::id()));
        write_receipt(&path, &model, &binding, &trace).unwrap();
        let value: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(value["kind"], "target-trace");
        assert!(value.get("model_snapshot").is_none());
        assert_eq!(
            value["model_path"],
            model.model_path().display().to_string()
        );
        assert!(std::path::Path::new(value["model_path"].as_str().unwrap()).is_file());
        assert_eq!(value["protocol_id"], model.protocol_id);
        assert_eq!(value["protocol_id"].as_str().unwrap().len(), 8);
        assert_eq!(value["trace_id"], trace.id.0);
        assert_eq!(value["trace_id"].as_str().unwrap().len(), 8);
        for step in value["steps"].as_array().unwrap() {
            assert_eq!(step["step_id"].as_str().unwrap().len(), 8);
        }
        let names: Vec<&str> = value["messages"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|item| item["name"].as_str())
            .collect();
        let mut sorted = names.clone();
        sorted.sort();
        assert_eq!(names, sorted);
        let _ = rand.next();
    }

    fn role_model(body: serde_json::Value) -> IndexedModel {
        let path = temp_json("mcn-roles");
        std::fs::write(&path, serde_json::to_vec(&body).unwrap()).unwrap();
        load_model(&path).unwrap()
    }

    fn both_binding() -> Binding {
        let mut roles = std::collections::BTreeMap::new();
        roles.insert("sut-a".to_string(), PartyRole::Sut);
        roles.insert("sut-b".to_string(), PartyRole::Sut);
        roles.insert("gen-a".to_string(), PartyRole::Generator);
        roles.insert("gen-b".to_string(), PartyRole::Generator);
        Binding::new(roles)
    }

    fn step(name: &str, sender: &str, receiver: &str, channel: &str) -> serde_json::Value {
        json!({
            "id": name,
            "name": name,
            "message_name": "m",
            "channel_name": channel,
            "sender_name": sender,
            "receiver_name": receiver,
            "condition": "go"
        })
    }

    fn roles_body(steps: Vec<serde_json::Value>, extra_channel: bool) -> serde_json::Value {
        let mut channels = vec![
            json!({"name": "cross", "description": "c", "transport": "TCP", "connection": "connection_oriented", "endpoint_party_names": ["sut-a", "gen-a"], "initiator_party_name": "sut-a", "acceptor_party_name": "gen-a", "endpoint_sources": []}),
            json!({"name": "sut-only", "description": "s", "transport": "TCP", "connection": "connection_oriented", "endpoint_party_names": ["sut-a", "sut-b"], "initiator_party_name": "sut-a", "acceptor_party_name": "sut-b", "endpoint_sources": []}),
            json!({"name": "gen-only", "description": "g", "transport": "TCP", "connection": "connection_oriented", "endpoint_party_names": ["gen-a", "gen-b"], "initiator_party_name": "gen-a", "acceptor_party_name": "gen-b", "endpoint_sources": []}),
        ];
        if extra_channel {
            channels.push(json!({"name": "blocked", "description": "b", "transport": "TCP", "connection": "connection_oriented", "endpoint_party_names": ["sut-a", "gen-a", "ghost"], "initiator_party_name": "sut-a", "acceptor_party_name": "ghost", "endpoint_sources": []}));
        }
        json!({
            "meta": {"schema_version": "7.0.0", "protocol": "roles", "complete": false, "documents": [{"doc_id": "d", "rfc": "RFC 1", "input_sha256": "a", "document_sha256": "b", "path": "p"}]},
            "parties": [
                {"name": "sut-a", "description": "sa"},
                {"name": "sut-b", "description": "sb"},
                {"name": "gen-a", "description": "ga"},
                {"name": "gen-b", "description": "gb"},
                {"name": "ghost", "description": "missing role"}
            ],
            "messages": [{"name": "m", "associated_fields": []}],
            "channels": channels,
            "steps": steps,
            "step_dependencies": [],
            "unresolved": []
        })
    }

    #[test]
    fn channel_with_an_unassigned_endpoint_is_not_eligible() {
        let model = role_model(roles_body(
            vec![
                step("cross", "sut-a", "gen-a", "cross"),
                step("blocked", "sut-a", "gen-a", "blocked"),
            ],
            true,
        ));
        let names: Vec<_> = model
            .eligible(&both_binding())
            .into_iter()
            .map(|step| step.name.clone())
            .collect();
        assert_eq!(names, vec!["cross".to_string()]);
    }

    #[test]
    fn trace_selection_requires_both_roles_across_the_sequence() {
        let binding = both_binding();
        let sut_only = role_model(roles_body(
            vec![step("sut-aux", "sut-a", "sut-b", "sut-only")],
            false,
        ));
        assert!(RandomTraceSelector
            .select(
                &sut_only,
                &binding,
                &mut StdRand::with_seed(1),
                &SelectionContext::default(),
                8,
                1,
                1
            )
            .is_none());
        let gen_only = role_model(roles_body(
            vec![step("gen-aux", "gen-a", "gen-b", "gen-only")],
            false,
        ));
        assert!(RandomTraceSelector
            .select(
                &gen_only,
                &binding,
                &mut StdRand::with_seed(1),
                &SelectionContext::default(),
                4,
                1,
                1
            )
            .is_none());
        // A generator channel endpoint does not cover the generator role.
        let decoy = role_model(roles_body(
            vec![step("sut-on-cross", "sut-a", "sut-b", "cross")],
            false,
        ));
        let decoy_steps: Vec<_> = decoy.eligible(&binding).into_iter().cloned().collect();
        assert_eq!(decoy_steps.len(), 1);
        assert!(!select::covers_sut_and_generator(&decoy_steps, &binding));
        assert!(RandomTraceSelector
            .select(
                &decoy,
                &binding,
                &mut StdRand::with_seed(1),
                &SelectionContext::default(),
                8,
                1,
                1
            )
            .is_none());
        let cross = role_model(roles_body(
            vec![step("cross", "sut-a", "gen-a", "cross")],
            false,
        ));
        let chosen = RandomTraceSelector
            .select(
                &cross,
                &binding,
                &mut StdRand::with_seed(1),
                &SelectionContext::default(),
                1,
                1,
                1,
            )
            .unwrap();
        assert_eq!(chosen.steps.len(), 1);
        assert!(select::covers_sut_and_generator(&chosen.steps, &binding));

        let mixed = role_model(roles_body(
            vec![
                step("sut-aux", "sut-a", "sut-b", "sut-only"),
                step("gen-aux", "gen-a", "gen-b", "gen-only"),
                step("cross", "sut-a", "gen-a", "cross"),
            ],
            false,
        ));
        let trace = RandomTraceSelector
            .select(
                &mixed,
                &binding,
                &mut StdRand::with_seed(2),
                &SelectionContext::default(),
                8,
                2,
                2,
            )
            .unwrap();
        assert!(select::covers_sut_and_generator(&trace.steps, &binding));
        assert!(trace.steps.iter().any(|step| {
            let sender = binding.role(&step.sender_name);
            let receiver = binding.role(&step.receiver_name);
            sender == receiver
        }));
        assert!(RandomTraceSelector
            .select(
                &sut_only,
                &binding,
                &mut StdRand::with_seed(9),
                &SelectionContext::default(),
                0,
                1,
                2
            )
            .is_none());
    }
}
