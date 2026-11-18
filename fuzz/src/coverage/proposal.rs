//! Proposal v2: ordered step instances with their mutable fields.
//!
//! The Python service validates the request and freezes the file, including
//! the projected mutable field declarations. Rust trusts that validation and
//! only projects the trace onto generator-owned actions.

use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
};

use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::{
    engine::input::{BytesEncoding, FieldKind, ParamValue},
    protocol::{Binding, IndexedModel, PartyRole, Processing, StepRecord},
};

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ProposalGoal {
    #[serde(default)]
    pub handler_ids: Vec<String>,
    pub reason: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ProposalChannel {
    pub channel: String,
    pub endpoints: Vec<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct MutableField {
    pub field: String,
    #[serde(default)]
    pub kind: FieldKind,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub encoding: Option<BytesEncoding>,
    #[serde(default)]
    pub default: Option<ParamValue>,
    /// Why the field is mutable, carried from the analysis proposal.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub suggestion: Option<String>,
    /// Decisive source location for the field.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub evidence: Option<String>,
    #[serde(default)]
    pub source: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ProposalDependency {
    pub prior: String,
    pub dependent: String,
    #[serde(default)]
    pub condition: String,
    #[serde(default)]
    pub kind: String,
    #[serde(default)]
    pub fields: Vec<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ProposalStep {
    pub name: String,
    pub step: String,
    pub action: String,
    #[serde(default)]
    pub condition: String,
    #[serde(default)]
    pub message: Option<String>,
    pub channel: String,
    #[serde(default)]
    pub sender: Option<String>,
    #[serde(default)]
    pub receiver: Option<String>,
    #[serde(default)]
    pub party: Option<String>,
    pub reason: String,
    #[serde(default)]
    pub constraints: Vec<String>,
    /// Local state transitions copied from the model step, when declared.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub processing: Option<Processing>,
    /// Mutable field descriptions after Rust strips the mutation strategy.
    #[serde(default)]
    pub mutable_fields: Vec<MutableField>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Proposal {
    pub origin: String,
    pub session: String,
    pub build: String,
    pub snapshot: String,
    pub goal: ProposalGoal,
    pub parties: BTreeMap<String, String>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub party_aliases: BTreeMap<String, Vec<String>>,
    pub channels: BTreeMap<String, ProposalChannel>,
    pub trace: Vec<ProposalStep>,
    #[serde(default)]
    pub dependencies: Vec<ProposalDependency>,
}

/// Frozen proposal plus the on-disk bytes accepted by Rust.
#[derive(Clone, Debug)]
pub struct AcceptedProposal {
    pub proposal: Proposal,
    pub path: PathBuf,
    pub request_id: String,
}

impl AcceptedProposal {
    pub fn request_id(&self) -> &str {
        &self.request_id
    }
}

/// Immutable model constraints consumed by Rust instantiation/mutation.
pub type TraceModel = Proposal;

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

/// One party instance, one channel instance, and one step instance per model step.
pub fn instantiate(model: &IndexedModel, steps: &[StepRecord]) -> Result<Proposal, String> {
    let mut parties = BTreeMap::new();
    let mut channels: BTreeMap<String, ProposalChannel> = BTreeMap::new();
    let mut seen = BTreeMap::<String, usize>::new();
    let mut trace = Vec::new();
    for step in steps {
        for party in step.participants() {
            parties
                .entry(party.to_string())
                .or_insert_with(|| party.to_string());
        }
        let entry = channels
            .entry(step.channel_name.clone())
            .or_insert_with(|| ProposalChannel {
                channel: step.channel_name.clone(),
                endpoints: Vec::new(),
            });
        for party in step.participants() {
            if !entry.endpoints.iter().any(|name| name == party) {
                entry.endpoints.push(party.to_string());
            }
        }
        let count = seen.entry(step.name.clone()).or_insert(0);
        *count += 1;
        let name = if steps.iter().filter(|item| item.name == step.name).count() == 1 {
            step.name.clone()
        } else {
            format!("{}_{}", step.name, *count)
        };
        trace.push(ProposalStep {
            name,
            step: step.name.clone(),
            action: step.action.name().to_string(),
            condition: step.condition.clone(),
            message: step.message_name().map(str::to_string),
            channel: step.channel_name.clone(),
            sender: step.sender_name().map(str::to_string),
            receiver: step.receiver_name().map(str::to_string),
            party: step.party_name().map(str::to_string),
            reason: format!("step {}", step.name),
            constraints: Vec::new(),
            processing: step.processing.clone(),
            mutable_fields: Vec::new(),
        });
    }
    let step_to_instance: BTreeMap<String, String> = trace
        .iter()
        .map(|item| (item.step.clone(), item.name.clone()))
        .collect();
    let dependencies = model
        .dependency_rows()
        .into_iter()
        .filter_map(|(prior, dependent, condition, fields)| {
            let prior = step_to_instance.get(&prior)?.clone();
            let dependent = step_to_instance.get(&dependent)?.clone();
            let bindings = fields
                .into_iter()
                .map(|(prior_field, _dependent_field)| prior_field)
                .collect::<BTreeSet<_>>()
                .into_iter()
                .collect::<Vec<_>>();
            Some(ProposalDependency {
                prior,
                dependent,
                condition,
                kind: if bindings.is_empty() {
                    "order".into()
                } else {
                    "data".into()
                },
                fields: bindings,
            })
        })
        .collect();
    Ok(Proposal {
        origin: "autonomous".into(),
        session: String::new(),
        build: String::new(),
        snapshot: String::new(),
        goal: ProposalGoal {
            handler_ids: Vec::new(),
            reason: String::new(),
        },
        parties,
        party_aliases: model.party_aliases(),
        channels,
        trace,
        dependencies,
    })
}

impl Proposal {
    pub fn read(path: &Path) -> Result<Self, String> {
        let bytes =
            fs::read(path).map_err(|error| format!("reading {}: {error}", path.display()))?;
        serde_json::from_slice(&bytes)
            .map_err(|error| format!("parsing {}: {error}", path.display()))
    }

    /// Downstream copy for synthesis: drop the mutation strategy and project the
    /// trace onto generator-owned actions.
    ///
    /// The Python service validated the frozen proposal, including the mutable
    /// field declarations, so this projection is mechanical.
    ///
    /// Model `send` steps keep their direction in `sender`/`receiver`:
    /// - sender is the generator: `action = "send"`, drop `sender`;
    /// - sender is the SUT: `action = "recv"`, drop `receiver` and its fields;
    /// - SUT `listen`/`connect`/`close` steps are omitted;
    /// - generator network actions are kept as-is.
    pub fn synthesis_copy(&self, binding: &Binding) -> Self {
        let role_of = |instance: &str| {
            self.parties
                .get(instance)
                .map(|party| binding.role(party))
                .unwrap_or(PartyRole::Unavailable)
        };
        let mut trace = Vec::new();
        for item in &self.trace {
            match item.action.as_str() {
                "send" => match role_of(item.sender.as_deref().unwrap_or("")) {
                    PartyRole::Generator => {
                        let mut step = item.clone();
                        step.sender = None;
                        trace.push(step);
                    }
                    PartyRole::Sut => {
                        let mut step = item.clone();
                        step.action = "recv".into();
                        step.receiver = None;
                        step.mutable_fields.clear();
                        trace.push(step);
                    }
                    PartyRole::Unavailable => {}
                },
                "listen" | "connect" | "close" => {
                    if role_of(item.party.as_deref().unwrap_or("")) == PartyRole::Generator {
                        let mut step = item.clone();
                        step.mutable_fields.clear();
                        trace.push(step);
                    }
                }
                _ => {}
            }
        }
        let kept: BTreeSet<String> = trace.iter().map(|step| step.name.clone()).collect();
        let mut copy = self.clone();
        copy.trace = trace;
        copy.dependencies.retain(|dependency| {
            kept.contains(&dependency.prior) && kept.contains(&dependency.dependent)
        });
        copy
    }

    pub fn write_copy(&self, path: &Path) -> Result<(), String> {
        let body = serde_json::to_vec_pretty(self).map_err(|error| error.to_string())?;
        fs::write(path, [body.as_slice(), b"\n"].concat())
            .map_err(|error| format!("writing {}: {error}", path.display()))
    }
}

pub fn sha256_file(path: &Path) -> Result<String, String> {
    let bytes = fs::read(path).map_err(|error| format!("reading {}: {error}", path.display()))?;
    Ok(format!("{:x}", Sha256::digest(bytes)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocol::{load_model, Binding, PartyRole};
    use serde_json::json;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn fixture() -> (IndexedModel, Binding) {
        let dir = std::env::temp_dir().join(format!(
            "mcn-proposal-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .subsec_nanos()
        ));
        fs::create_dir_all(&dir).unwrap();
        let body = json!({
            "meta": {"protocol": "demo", "complete": true, "documents": []},
            "parties": [
                {"name": "client", "description": "c", "alias": ["peer"]},
                {"name": "server", "description": "s"},
                {"name": "peer", "description": "p"}
            ],
            "messages": [
                {"name": "hello", "associated_fields": []},
                {"name": "bye", "associated_fields": []}
            ],
            "channels": [
                {"name": "control", "description": "c", "transport": "TCP",
                 "connection": "connection_oriented",
                 "endpoint_party_names": ["client", "server"],
                 "initiator_party_name": "client", "acceptor_party_name": "server",
                 "endpoint_sources": []},
                {"name": "data", "description": "d", "transport": "TCP",
                 "connection": "connection_oriented",
                 "endpoint_party_names": ["peer", "server"],
                 "initiator_party_name": "peer", "acceptor_party_name": "server",
                 "endpoint_sources": []}
            ],
            "steps": [
                {"name": "hello", "action": "send", "message_name": "hello",
                 "channel_name": "control", "sender_name": "client",
                 "receiver_name": "server", "condition": "start"},
                {"name": "listen_data", "action": "listen", "party_name": "peer",
                 "channel_name": "data", "condition": "after hello"},
                {"name": "bye", "action": "send", "message_name": "bye",
                 "channel_name": "control", "sender_name": "client",
                 "receiver_name": "server", "condition": "done"}
            ],
            "step_dependencies": [],
            "unresolved": []
        });
        let path = dir.join("protocol.json");
        fs::write(&path, body.to_string()).unwrap();
        let model = load_model(&path).unwrap();
        let binding = Binding::new(
            [
                ("client".into(), PartyRole::Generator),
                ("server".into(), PartyRole::Sut),
                ("peer".into(), PartyRole::Generator),
            ]
            .into_iter()
            .collect(),
        );
        (model, binding)
    }

    fn base(model: &IndexedModel) -> Proposal {
        let mut proposal = instantiate(model, &model.steps).unwrap();
        proposal.origin = "coverage".into();
        proposal.session = "s".into();
        proposal.build = "b".into();
        proposal.snapshot = "000001".into();
        proposal.goal.reason = "reach auth".into();
        proposal.goal.handler_ids = vec!["handle_hello".into()];
        for item in &mut proposal.trace {
            item.reason = format!("exercise {}", item.name);
        }
        proposal
    }

    #[test]
    fn party_aliases_are_symmetric_and_survive_synthesis_projection() {
        let (model, binding) = fixture();
        let proposal = base(&model).synthesis_copy(&binding);
        assert_eq!(proposal.party_aliases["client"], vec!["peer"]);
        assert_eq!(proposal.party_aliases["peer"], vec!["client"]);
        let encoded = serde_json::to_vec(&proposal).unwrap();
        let decoded: Proposal = serde_json::from_slice(&encoded).unwrap();
        assert_eq!(decoded.party_aliases, proposal.party_aliases);
    }

    #[test]
    fn illegal_endpoint_action_and_network_handler_ids_fail() {
        let raw = json!({
            "origin": "coverage", "session": "s",
            "build": "b", "snapshot": "000001",
            "goal": {"handler_ids": ["h"], "reason": "g"},
            "parties": {"peer": "peer"},
            "channels": {"data": {"channel": "data", "endpoints": ["peer"]}},
            "trace": [{
                "name": "listen_once", "step": "listen_data", "action": "listen",
                "condition": "after hello", "message": null, "channel": "data",
                "party": "peer", "reason": "listen", "constraints": [],
                "handler_ids": ["h"]
            }],
        });
        let error = serde_json::from_value::<Proposal>(raw).unwrap_err();
        assert!(error.to_string().contains("handler_ids"), "{error}");
    }

    #[test]
    fn proposal_rejects_removed_request_id_and_version_fields() {
        let raw = json!({
            "origin": "coverage", "session": "s", "request_id": "r",
            "build": "b", "snapshot": "000001"
        });
        let error = serde_json::from_value::<Proposal>(raw).unwrap_err();
        assert!(error.to_string().contains("request_id"), "{error}");
    }

    #[test]
    fn synthesis_copy_copies_validated_mutable_fields() {
        let (model, binding) = fixture();
        let mut proposal = base(&model);
        let field = MutableField {
            field: "username".into(),
            kind: FieldKind::String,
            encoding: None,
            default: Some(ParamValue::Str("ubuntu".into())),
            suggestion: None,
            evidence: None,
            source: "proposal".into(),
        };
        proposal.trace[0].mutable_fields = vec![field.clone()];
        let copy = proposal.synthesis_copy(&binding);
        let hello = copy.trace.iter().find(|step| step.name == "hello").unwrap();
        assert_eq!(hello.mutable_fields, vec![field]);
        for step in &copy.trace {
            if step.action == "recv" {
                assert!(step.mutable_fields.is_empty(), "{}", step.name);
            }
        }
    }

    #[test]
    fn synthesis_copy_projects_direction_and_drops_sut_setup() {
        let (model, binding) = fixture();
        let mut proposal = base(&model);

        let mut server_reply = proposal.trace[0].clone();
        server_reply.name = "server_reply".into();
        server_reply.step = "server_reply".into();
        server_reply.sender = Some("server".into());
        server_reply.receiver = Some("client".into());
        proposal.trace.push(server_reply);

        let mut server_listen = proposal.trace[1].clone();
        server_listen.name = "server_listen".into();
        server_listen.step = "server_listen".into();
        server_listen.action = "listen".into();
        server_listen.party = Some("server".into());
        server_listen.sender = None;
        server_listen.receiver = None;
        server_listen.mutable_fields.clear();
        proposal.trace.push(server_listen);

        proposal.dependencies.push(ProposalDependency {
            prior: "bye".into(),
            dependent: "server_reply".into(),
            condition: String::new(),
            kind: "order".into(),
            fields: Vec::new(),
        });
        proposal.dependencies.push(ProposalDependency {
            prior: "server_listen".into(),
            dependent: "bye".into(),
            condition: String::new(),
            kind: "order".into(),
            fields: Vec::new(),
        });

        let copy = proposal.synthesis_copy(&binding);
        assert_eq!(copy.trace.len(), 4, "{:?}", copy.trace);
        assert!(copy.trace.iter().all(|step| step.name != "server_listen"));

        let hello = copy.trace.iter().find(|step| step.name == "hello").unwrap();
        assert_eq!(hello.action, "send");
        assert_eq!(hello.sender, None);
        assert_eq!(hello.receiver.as_deref(), Some("server"));

        let listen = copy
            .trace
            .iter()
            .find(|step| step.name == "listen_data")
            .unwrap();
        assert_eq!(listen.action, "listen");
        assert_eq!(listen.party.as_deref(), Some("peer"));

        let reply = copy
            .trace
            .iter()
            .find(|step| step.name == "server_reply")
            .unwrap();
        assert_eq!(reply.action, "recv");
        assert_eq!(reply.sender.as_deref(), Some("server"));
        assert_eq!(reply.receiver, None);
        assert_eq!(copy.dependencies.len(), 1);
        assert_eq!(copy.dependencies[0].dependent, "server_reply");
    }

    #[test]
    fn synthesis_copy_resolves_party_instance_aliases() {
        let (model, binding) = fixture();
        let mut proposal = base(&model);
        proposal.parties.insert("client_1".into(), "client".into());
        proposal.parties.insert("server_1".into(), "server".into());
        proposal.parties.insert("peer_1".into(), "peer".into());
        for step in &mut proposal.trace {
            if let Some(sender) = step.sender.as_mut() {
                *sender = match sender.as_str() {
                    "client" => "client_1".into(),
                    "server" => "server_1".into(),
                    other => other.into(),
                };
            }
            if let Some(receiver) = step.receiver.as_mut() {
                *receiver = match receiver.as_str() {
                    "client" => "client_1".into(),
                    "server" => "server_1".into(),
                    other => other.into(),
                };
            }
            if let Some(party) = step.party.as_mut() {
                *party = match party.as_str() {
                    "peer" => "peer_1".into(),
                    other => other.into(),
                };
            }
        }
        let mut server_reply = proposal.trace[0].clone();
        server_reply.name = "server_reply".into();
        server_reply.step = "server_reply".into();
        server_reply.sender = Some("server_1".into());
        server_reply.receiver = Some("client_1".into());
        proposal.trace.push(server_reply);

        let copy = proposal.synthesis_copy(&binding);
        assert_eq!(copy.trace[0].action, "send");
        assert_eq!(copy.trace[0].receiver.as_deref(), Some("server_1"));
        assert_eq!(copy.trace[1].party.as_deref(), Some("peer_1"));
        let reply = copy
            .trace
            .iter()
            .find(|step| step.name == "server_reply")
            .unwrap();
        assert_eq!(reply.action, "recv");
        assert_eq!(reply.sender.as_deref(), Some("server_1"));
    }

    #[test]
    fn synthesis_copy_keeps_step_processing() {
        let (model, binding) = fixture();
        let mut proposal = base(&model);
        let processing = crate::protocol::Processing {
            before_send: vec![crate::protocol::ProcessingOperation {
                reads: vec!["state.transcript".into()],
                writes: vec!["message.seq".into()],
                rule: "set seq".into(),
            }],
            after_send: Vec::new(),
            on_receive: Vec::new(),
        };
        proposal.trace[0].processing = Some(processing.clone());
        proposal.trace[0].mutable_fields = vec![MutableField {
            field: "username".into(),
            kind: FieldKind::String,
            encoding: None,
            default: Some(ParamValue::Str("ubuntu".into())),
            suggestion: Some("boundary".into()),
            evidence: Some("src/a.c:1".into()),
            source: "proposal".into(),
        }];
        let copy = proposal.synthesis_copy(&binding);
        let hello = copy.trace.iter().find(|step| step.name == "hello").unwrap();
        assert_eq!(hello.processing.as_ref(), Some(&processing));
    }

    #[test]
    fn mailbox_keeps_only_the_latest_proposal() {
        let mailbox = ProposalMailbox::default();
        let make = |id: &str| AcceptedProposal {
            proposal: Proposal {
                origin: "autonomous".into(),
                session: "s".into(),
                build: String::new(),
                snapshot: String::new(),
                goal: ProposalGoal {
                    handler_ids: vec![],
                    reason: "g".into(),
                },
                parties: BTreeMap::new(),
                party_aliases: BTreeMap::new(),
                channels: BTreeMap::new(),
                trace: vec![],
                dependencies: vec![],
            },
            path: PathBuf::from(id),
            request_id: id.into(),
        };
        mailbox.replace(make("a"));
        mailbox.replace(make("b"));
        assert_eq!(mailbox.peek().unwrap().request_id(), "b");
        assert_eq!(mailbox.take().unwrap().request_id(), "b");
        assert!(mailbox.take().is_none());
    }
}
