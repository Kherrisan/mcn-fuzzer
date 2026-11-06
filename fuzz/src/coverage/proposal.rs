//! Proposal v1: the only generation guidance handed from coverage to synthesis.
//!
//! The Python service validates handler associations and freezes the file. Rust
//! rechecks identity, references, party binding, and both content digests before
//! copying the proposal into a synthesis request.

use std::{
    fs,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
};

use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::protocol::{Binding, IndexedModel, StepRecord, TraceId};

pub const PROPOSAL_VERSION: u32 = 1;
const ID_LEN: usize = 8;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct ProposalGoal {
    #[serde(default)]
    pub handler_ids: Vec<String>,
    pub reason: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct ProposalStep {
    pub step_id: String,
    #[serde(default)]
    pub handler_ids: Vec<String>,
    pub reason: String,
    #[serde(default)]
    pub constraints: Vec<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Proposal {
    pub version: u32,
    pub proposal_id: String,
    pub origin: String,
    pub session: String,
    pub request_id: String,
    pub build: String,
    pub snapshot: String,
    pub protocol_id: String,
    pub graph_revision: u64,
    pub trace_id: String,
    pub goal: ProposalGoal,
    pub trace: Vec<ProposalStep>,
    #[serde(default)]
    pub context: Value,
}

/// Frozen proposal plus the on-disk bytes accepted by Rust.
#[derive(Clone, Debug)]
pub struct AcceptedProposal {
    pub proposal: Proposal,
    pub path: PathBuf,
    pub sha256: String,
}

impl AcceptedProposal {
    pub fn id(&self) -> &str {
        &self.proposal.proposal_id
    }
}

/// One-slot handoff. Coverage replaces the value; synthesis takes it.
#[derive(Clone, Default)]
pub struct ProposalMailbox {
    inner: Arc<Mutex<Option<AcceptedProposal>>>,
}

impl ProposalMailbox {
    pub fn replace(&self, proposal: AcceptedProposal) -> Option<AcceptedProposal> {
        self.inner
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .replace(proposal)
    }

    pub fn take(&self) -> Option<AcceptedProposal> {
        self.inner
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .take()
    }

    pub fn peek(&self) -> Option<AcceptedProposal> {
        self.inner
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .clone()
    }
}

/// Identity of the analysis request a proposal must belong to.
#[derive(Clone, Debug)]
pub struct ProposalIdentity {
    pub session: String,
    pub request_id: String,
    pub build: String,
    pub snapshot: String,
    pub protocol_id: String,
}

impl Proposal {
    pub fn read(path: &Path) -> Result<Self, String> {
        let bytes =
            fs::read(path).map_err(|error| format!("reading {}: {error}", path.display()))?;
        serde_json::from_slice(&bytes)
            .map_err(|error| format!("parsing {}: {error}", path.display()))
    }

    /// Validate references and digests, then return the ordered model steps.
    pub fn validate(
        &self,
        model: &IndexedModel,
        binding: &Binding,
        expected: &ProposalIdentity,
    ) -> Result<Vec<StepRecord>, String> {
        if self.version != PROPOSAL_VERSION {
            return Err(format!("proposal version must be {PROPOSAL_VERSION}"));
        }
        if self.origin != "coverage" && self.origin != "autonomous" {
            return Err("proposal origin must be coverage or autonomous".into());
        }
        for (name, actual, wanted) in [
            ("session", &self.session, &expected.session),
            ("request_id", &self.request_id, &expected.request_id),
            ("build", &self.build, &expected.build),
            ("snapshot", &self.snapshot, &expected.snapshot),
            ("protocol_id", &self.protocol_id, &expected.protocol_id),
        ] {
            if !wanted.is_empty() && actual != wanted {
                return Err(format!("proposal.{name} does not match the frozen request"));
            }
        }
        if self.protocol_id != model.protocol_id {
            return Err("proposal protocol_id does not match the model".into());
        }
        if self.origin == "coverage" && self.goal.handler_ids.is_empty() {
            return Err("coverage proposals need goal.handler_ids".into());
        }
        if self.goal.reason.trim().is_empty() {
            return Err("proposal.goal.reason is required".into());
        }
        if self.trace.is_empty() {
            return Err("proposal trace must not be empty".into());
        }
        let mut steps = Vec::with_capacity(self.trace.len());
        for item in &self.trace {
            let step = model
                .steps
                .iter()
                .find(|step| step.id().0 == item.step_id)
                .ok_or_else(|| format!("unknown step id {}", item.step_id))?;
            if !model.carries_step(step, binding) {
                return Err(format!(
                    "step {} cannot be carried by this campaign",
                    item.step_id
                ));
            }
            if item.reason.trim().is_empty() {
                return Err(format!("step {} needs a reason", item.step_id));
            }
            steps.push(step.clone());
        }
        let trace_id = TraceId::from_steps(&steps);
        if trace_id.0 != self.trace_id {
            return Err("proposal.trace_id does not match the ordered trace".into());
        }
        if self.origin == "coverage" {
            let carried: Vec<&str> = self
                .trace
                .iter()
                .flat_map(|item| item.handler_ids.iter().map(String::as_str))
                .collect();
            for handler in &self.goal.handler_ids {
                if !carried.contains(&handler.as_str()) {
                    return Err(format!(
                        "goal handler {handler} is not associated with any trace step"
                    ));
                }
            }
        }
        let expected_id = proposal_id(self)?;
        if expected_id != self.proposal_id {
            return Err("proposal.proposal_id does not match the content".into());
        }
        Ok(steps)
    }

    pub fn write_copy(&self, path: &Path) -> Result<(), String> {
        let body = serde_json::to_vec_pretty(self).map_err(|error| error.to_string())?;
        fs::write(path, [body.as_slice(), b"\n"].concat())
            .map_err(|error| format!("writing {}: {error}", path.display()))
    }
}

/// Content ID over the proposal without its own `proposal_id` field.
pub fn proposal_id(proposal: &Proposal) -> Result<String, String> {
    let mut value = serde_json::to_value(proposal).map_err(|error| error.to_string())?;
    if let Some(map) = value.as_object_mut() {
        map.remove("proposal_id");
    }
    let mut out = Vec::new();
    push_str(&mut out, "mcn.proposal.v1");
    canonical(&value, &mut out);
    Ok(hex(&out)[..ID_LEN].to_string())
}

pub fn sha256_file(path: &Path) -> Result<String, String> {
    let bytes = fs::read(path).map_err(|error| format!("reading {}: {error}", path.display()))?;
    Ok(hex(&bytes))
}

fn hex(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn push_str(out: &mut Vec<u8>, text: &str) {
    let raw = text.as_bytes();
    out.extend((raw.len() as u64).to_be_bytes());
    out.extend(raw);
}

/// Mirror of the Python canonical encoder used for protocol and proposal IDs.
fn canonical(value: &Value, out: &mut Vec<u8>) {
    match value {
        Value::Null => out.push(b'n'),
        Value::Bool(true) => out.push(b't'),
        Value::Bool(false) => out.push(b'f'),
        Value::Number(number) => push_str(out, &number.to_string()),
        Value::String(text) => push_str(out, text),
        Value::Array(items) => {
            out.push(b'[');
            out.extend((items.len() as u64).to_be_bytes());
            for item in items {
                canonical(item, out);
            }
        }
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            out.push(b'{');
            out.extend((keys.len() as u64).to_be_bytes());
            for key in keys {
                push_str(out, key);
                canonical(&map[key], out);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocol::load_model;
    use serde_json::json;
    use std::path::PathBuf;

    fn fixture_model() -> (IndexedModel, Binding, PathBuf) {
        let dir = std::env::temp_dir().join(format!(
            "mcn-proposal-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .subsec_nanos()
        ));
        fs::create_dir_all(&dir).unwrap();
        let body = json!({
            "meta": {"schema_version": "7.0.0", "protocol": "p", "complete": true, "documents": []},
            "parties": [{"name": "client", "description": ""}, {"name": "server", "description": ""}],
            "messages": [{"name": "hello", "associated_fields": []}],
            "channels": [{"name": "c", "description": "", "transport": "tcp", "connection": "one",
                          "endpoint_party_names": ["client", "server"], "initiator_party_name": null,
                          "acceptor_party_name": null, "endpoint_sources": []}],
            "steps": [{"id": "step-1", "name": "hello", "message_name": "hello",
                       "channel_name": "c", "sender_name": "client", "receiver_name": "server",
                       "condition": ""}],
            "step_dependencies": [],
            "unresolved": []
        });
        let path = dir.join("protocol.json");
        fs::write(&path, body.to_string()).unwrap();
        let model = load_model(&path).unwrap();
        let binding = Binding::new(
            [
                ("client".to_string(), crate::protocol::PartyRole::Generator),
                ("server".to_string(), crate::protocol::PartyRole::Sut),
            ]
            .into_iter()
            .collect(),
        );
        (model, binding, dir)
    }

    #[test]
    fn proposal_id_matches_canonical_content_and_changes_with_constraints() {
        let (model, binding, _dir) = fixture_model();
        let step = &model.steps[0];
        let mut proposal = Proposal {
            version: 1,
            proposal_id: String::new(),
            origin: "autonomous".into(),
            session: "s".into(),
            request_id: "r".into(),
            build: "b".into(),
            snapshot: "000001".into(),
            protocol_id: model.protocol_id.clone(),
            graph_revision: 0,
            trace_id: TraceId::from_steps(std::slice::from_ref(step)).0,
            goal: ProposalGoal {
                handler_ids: vec![],
                reason: "test".into(),
            },
            trace: vec![ProposalStep {
                step_id: step.id().0,
                handler_ids: vec![],
                reason: "prefix".into(),
                constraints: vec![],
            }],
            context: Value::Null,
        };
        proposal.proposal_id = proposal_id(&proposal).unwrap();
        let identity = ProposalIdentity {
            session: "s".into(),
            request_id: "r".into(),
            build: "b".into(),
            snapshot: "000001".into(),
            protocol_id: model.protocol_id.clone(),
        };
        proposal
            .validate(&model, &binding, &identity)
            .expect("proposal validates");
        proposal.trace[0].constraints.push("needs auth".into());
        assert_ne!(proposal.proposal_id, proposal_id(&proposal).unwrap());
    }
    fn python_fixture() -> (IndexedModel, Binding, PathBuf) {
        let dir = std::env::temp_dir().join(format!(
            "mcn-id-fixture-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .subsec_nanos()
        ));
        fs::create_dir_all(&dir).unwrap();
        let body = serde_json::json!({
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
                ("client".to_string(), crate::protocol::PartyRole::Generator),
                ("server".to_string(), crate::protocol::PartyRole::Sut),
            ]
            .into_iter()
            .collect(),
        );
        (model, binding, dir)
    }

    #[test]
    fn ids_match_the_python_service_encoding() {
        let (model, _binding, _dir) = python_fixture();
        assert_eq!(model.protocol_id, "3f0a168a");
        assert_eq!(model.steps[0].id().0, "55f21230");
        assert_eq!(
            TraceId::from_steps(std::slice::from_ref(&model.steps[0])).0,
            "d21f4f84"
        );
    }

    #[test]
    fn autonomous_proposal_allows_empty_build_and_snapshot() {
        let (model, binding, _dir) = python_fixture();
        let step = &model.steps[0];
        let mut proposal = Proposal {
            version: 1,
            proposal_id: String::new(),
            origin: "autonomous".into(),
            session: "s".into(),
            request_id: "r".into(),
            build: String::new(),
            snapshot: String::new(),
            protocol_id: model.protocol_id.clone(),
            graph_revision: 0,
            trace_id: TraceId::from_steps(std::slice::from_ref(step)).0,
            goal: ProposalGoal {
                handler_ids: vec![],
                reason: "bootstrap".into(),
            },
            trace: vec![ProposalStep {
                step_id: step.id().0,
                handler_ids: vec![],
                reason: "target".into(),
                constraints: vec![],
            }],
            context: Value::Null,
        };
        proposal.proposal_id = proposal_id(&proposal).unwrap();
        let identity = ProposalIdentity {
            session: "s".into(),
            request_id: "r".into(),
            build: String::new(),
            snapshot: String::new(),
            protocol_id: model.protocol_id.clone(),
        };
        proposal.validate(&model, &binding, &identity).unwrap();
        let mut wrong = proposal.clone();
        wrong.request_id = "other".into();
        assert!(wrong.validate(&model, &binding, &identity).is_err());
    }

    #[test]
    fn mailbox_keeps_only_the_latest_proposal() {
        let mailbox = ProposalMailbox::default();
        let make = |id: &str| {
            let mut proposal: Proposal = serde_json::from_value(serde_json::json!({
                "version": 1, "proposal_id": id, "origin": "autonomous", "session": "s",
                "request_id": "r", "build": "", "snapshot": "", "protocol_id": "p",
                "graph_revision": 0, "trace_id": "t", "goal": {"handler_ids": [], "reason": "g"},
                "trace": [], "context": null
            }))
            .unwrap();
            proposal.proposal_id = id.to_string();
            AcceptedProposal {
                proposal,
                path: PathBuf::from(id),
                sha256: String::new(),
            }
        };
        mailbox.replace(make("a"));
        mailbox.replace(make("b"));
        assert_eq!(mailbox.peek().unwrap().id(), "b");
        assert_eq!(mailbox.take().unwrap().id(), "b");
        assert!(mailbox.take().is_none());
    }
}
