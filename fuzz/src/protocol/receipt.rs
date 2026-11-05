//! A receipt records the target trace. It is not a claim about the runtime trace.

use std::{collections::BTreeSet, fs, path::Path};

use serde_json::{json, Value};

use super::{model::IndexedModel, select::StepTrace, Binding};

/// Write one `receipt.json`. Definitions are sorted by name. Steps stay in trace order.
pub fn write_receipt(
    path: &Path,
    model: &IndexedModel,
    binding: &Binding,
    trace: &StepTrace,
) -> Result<(), libafl::Error> {
    let step_names: BTreeSet<&str> = trace.steps.iter().map(|step| step.name.as_str()).collect();
    let mut message_names = BTreeSet::new();
    let mut channel_names = BTreeSet::new();
    let mut party_names = BTreeSet::new();
    for step in &trace.steps {
        message_names.insert(step.message_name.as_str());
        channel_names.insert(step.channel_name.as_str());
        party_names.insert(step.sender_name.as_str());
        party_names.insert(step.receiver_name.as_str());
        if let Some(channel) = model.channel(&step.channel_name) {
            party_names.extend(channel.endpoint_party_names.iter().map(String::as_str));
            if let Some(name) = channel.initiator_party_name.as_deref() {
                party_names.insert(name);
            }
            if let Some(name) = channel.acceptor_party_name.as_deref() {
                party_names.insert(name);
            }
        }
    }
    let messages: Vec<Value> = message_names
        .iter()
        .filter_map(|name| model.message(name))
        .map(|message| {
            json!({
                "name": message.name,
                "associated_fields": message.associated_fields.iter().map(|field| &field.name).collect::<Vec<_>>(),
            })
        })
        .collect();
    let channels: Vec<Value> = channel_names
        .iter()
        .filter_map(|name| model.channel(name))
        .map(|channel| {
            json!({
                "name": channel.name,
                "transport": channel.transport,
                "connection": channel.connection,
                "endpoint_party_names": channel.endpoint_party_names,
                "description": channel.description,
                "initiator_party_name": channel.initiator_party_name,
                "acceptor_party_name": channel.acceptor_party_name,
                "endpoint_sources": channel.endpoint_sources.iter().map(|source| json!({
                    "party_name": source.party_name,
                    "parameter": source.parameter,
                    "from": source.from,
                })).collect::<Vec<_>>(),
            })
        })
        .collect();
    let parties: Vec<Value> = party_names
        .iter()
        .map(|name| {
            json!({
                "name": name,
                "role": match binding.role(name) {
                    super::model::PartyRole::Sut => "sut",
                    super::model::PartyRole::Generator => "generator",
                    super::model::PartyRole::Unavailable => "unavailable",
                },
                "description": model.party(name).map(|party| party.description.as_str()).unwrap_or(""),
            })
        })
        .collect();
    let dependencies: Vec<Value> = model
        .dependencies
        .iter()
        .filter(|dep| {
            step_names.contains(dep.prior_step.as_str())
                && step_names.contains(dep.dependent_step.as_str())
        })
        .map(|dep| {
            json!({
                "prior_step": dep.prior_step,
                "dependent_step": dep.dependent_step,
                "condition": dep.condition,
                "field_relations": dep.field_relations.iter().map(|rel| json!({
                    "prior_field": rel.prior_field,
                    "dependent_field": rel.dependent_field,
                })).collect::<Vec<_>>(),
            })
        })
        .collect();
    let unresolved: Vec<Value> = model
        .unresolved
        .iter()
        .filter(|issue| {
            issue.object_names.is_empty()
                || issue.object_names.iter().any(|name| {
                    message_names.contains(name.as_str())
                        || party_names.contains(name.as_str())
                        || step_names.contains(name.as_str())
                        || channel_names.contains(name.as_str())
                })
        })
        .map(|issue| json!({"description": issue.description, "object_names": issue.object_names}))
        .collect();
    let steps: Vec<Value> = trace
        .steps
        .iter()
        .zip(trace.step_ids.iter())
        .map(|(step, id)| {
            debug_assert_eq!(step.id(), *id);
            json!({
                "name": step.name,
                "step_id": id.0,
                "source_id": step.source_id,
                "message_name": step.message_name,
                "channel_name": step.channel_name,
                "sender_name": step.sender_name,
                "receiver_name": step.receiver_name,
                "condition": step.condition,
            })
        })
        .collect();
    let body = json!({
        "kind": "target-trace",
        "protocol_id": model.protocol_id,
        "trace_id": trace.id.0,
        "steps": steps,
        "messages": messages,
        "channels": channels,
        "parties": parties,
        "dependencies": dependencies,
        "unresolved": unresolved,
        "documents": model.documents.iter().map(|doc| json!({
            "doc_id": doc.doc_id,
            "rfc": doc.rfc,
            "input_sha256": doc.input_sha256,
            "document_sha256": doc.document_sha256,
            "path": model.model_dir().join(&doc.path),
        })).collect::<Vec<_>>(),
        "model_path": model.model_path(),
    });
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp = path.with_extension("json.tmp");
    fs::write(
        &tmp,
        serde_json::to_vec_pretty(&body)
            .map_err(|error| libafl::Error::serialize(error.to_string()))?,
    )?;
    fs::rename(&tmp, path)?;
    Ok(())
}
