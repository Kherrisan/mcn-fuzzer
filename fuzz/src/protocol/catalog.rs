//! Read-only name catalog shared with the Python MCP service.
//!
//! Version 3 names every step, dependency, and party. It has no digest IDs.
//! No scores, traversal order, or exploration policy live here.

use std::{fs, path::Path};

use serde_json::json;

use super::{Binding, IndexedModel, PartyRole};

pub fn write_catalog(
    model: &IndexedModel,
    binding: &Binding,
    path: &Path,
) -> Result<(), libafl::Error> {
    let steps: Vec<_> = model
        .steps
        .iter()
        .map(|step| {
            json!({
                "name": step.name,
                "action": step.action.name(),
                "message_name": step.message_name(),
                "channel_name": step.channel_name,
                "sender_name": step.sender_name(),
                "receiver_name": step.receiver_name(),
                "party_name": step.party_name(),
                "condition": step.condition,
            })
        })
        .collect();
    let dependencies: Vec<_> = model
        .dependencies
        .iter()
        .map(|dependency| {
            json!({
                "prior_step": dependency.prior_step,
                "dependent_step": dependency.dependent_step,
                "condition": dependency.condition,
                "field_relations": dependency.field_relations.iter().map(|relation| json!({
                    "prior_field": relation.prior_field,
                    "dependent_field": relation.dependent_field,
                    "derive": relation.derive,
                })).collect::<Vec<_>>(),
            })
        })
        .collect();
    let binding: serde_json::Map<String, serde_json::Value> = binding
        .entries()
        .map(|(name, role)| {
            let role = match role {
                PartyRole::Sut => "sut",
                PartyRole::Generator => "generator",
                PartyRole::Unavailable => "unavailable",
            };
            (name.to_string(), json!(role))
        })
        .collect();
    let documents: Vec<_> = model
        .documents
        .iter()
        .map(|doc| {
            json!({
                "doc_id": doc.doc_id, "rfc": doc.rfc, "path": model.model_dir().join(&doc.path)
            })
        })
        .collect();
    let catalog = json!({
        "protocol_name": model.protocol_name,
        "binding": binding,
        "parties": model.party_names(),
        "steps": steps,
        "dependencies": dependencies,
        "documents": documents,
    });
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| libafl::Error::unknown(format!("creating catalog dir: {error}")))?;
    }
    let body = serde_json::to_vec_pretty(&catalog)
        .map_err(|error| libafl::Error::unknown(format!("serializing catalog: {error}")))?;
    fs::write(path, [body.as_slice(), b"\n"].concat())
        .map_err(|error| libafl::Error::unknown(format!("writing catalog: {error}")))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocol::load_model;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn catalog_preserves_field_relation_derivations() {
        let dir = std::env::temp_dir().join(format!(
            "mcn-catalog-derive-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&dir).unwrap();
        let model = json!({
            "meta": {"protocol":"demo", "complete":true, "documents":[{"doc_id":"source", "rfc":"RFC 1", "path":"source.json"}]},
            "parties": [{"name":"client", "description":"c"}, {"name":"server", "description":"s"}],
            "messages": [{"name":"msg", "associated_fields":[{"name":"seq"}]}],
            "channels": [{"name":"control", "description":"c", "transport":"TCP",
                "connection":"connection_oriented", "endpoint_party_names":["client","server"],
                "initiator_party_name":"client", "acceptor_party_name":"server", "endpoint_sources":[]}],
            "steps": [
                {"name":"a", "action":"send", "message_name":"msg", "channel_name":"control", "sender_name":"client", "receiver_name":"server", "condition":""},
                {"name":"b", "action":"send", "message_name":"msg", "channel_name":"control", "sender_name":"client", "receiver_name":"server", "condition":""}
            ],
            "step_dependencies": [{"prior_step":"a", "dependent_step":"b", "condition":"",
                "field_relations":[{"prior_field":"seq", "dependent_field":"seq", "derive":"increment by one"},
                                   {"prior_field":"seq", "dependent_field":"seq"}]}],
            "unresolved":[]
        });
        let model_path = dir.join("protocol.json");
        fs::write(&model_path, model.to_string()).unwrap();
        let model = load_model(&model_path).unwrap();
        let binding = Binding::new(
            [
                ("client".into(), PartyRole::Generator),
                ("server".into(), PartyRole::Sut),
            ]
            .into_iter()
            .collect(),
        );
        let catalog_path = dir.join("catalog.json");
        write_catalog(&model, &binding, &catalog_path).unwrap();
        let catalog: serde_json::Value =
            serde_json::from_slice(&fs::read(catalog_path).unwrap()).unwrap();
        assert_eq!(
            catalog["dependencies"][0]["field_relations"][0]["derive"],
            "increment by one"
        );
        assert_eq!(
            catalog["dependencies"][0]["field_relations"][1]["derive"],
            ""
        );
        assert_eq!(catalog["documents"][0]["doc_id"], "source");
        assert_eq!(catalog["documents"][0]["rfc"], "RFC 1");
        assert_eq!(
            catalog["documents"][0]["path"],
            dir.join("source.json").to_str().unwrap()
        );
        fs::remove_dir_all(dir).unwrap();
    }
}
