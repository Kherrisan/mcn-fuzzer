//! Read-only identifier catalog shared with the Python MCP service.
//!
//! This is an identity table, not a selector: it contains the protocol ID,
//! stable step IDs, party bindings, and original definitions. No scores,
//! traversal order, or exploration policy live here.

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
                "step_id": step.id().0,
                "source_id": step.source_id,
                "name": step.name,
                "message_name": step.message_name,
                "channel_name": step.channel_name,
                "sender_name": step.sender_name,
                "receiver_name": step.receiver_name,
                "condition": step.condition,
            })
        })
        .collect();
    let by_name: std::collections::BTreeMap<String, String> = model
        .steps
        .iter()
        .map(|step| (step.name.clone(), step.id().0))
        .collect();
    let dependencies: Vec<_> = model
        .dependencies
        .iter()
        .map(|dependency| {
            json!({
                "prior_step_id": by_name.get(&dependency.prior_step).cloned().unwrap_or_default(),
                "dependent_step_id": by_name.get(&dependency.dependent_step).cloned().unwrap_or_default(),
                "prior_step": dependency.prior_step,
                "dependent_step": dependency.dependent_step,
                "condition": dependency.condition,
                "field_relations": dependency.field_relations.iter().map(|relation| json!({
                    "prior_field": relation.prior_field,
                    "dependent_field": relation.dependent_field,
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
        .document_paths()
        .iter()
        .map(|(rfc, path)| json!({"rfc": rfc, "path": path}))
        .collect();
    let catalog = json!({
        "version": 1,
        "protocol_id": model.protocol_id,
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
