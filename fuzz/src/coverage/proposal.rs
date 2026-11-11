//! Proposal v2: ordered step instances, party/channel instances, and mutations.
//!
//! The Python service fills action, condition, and message from the model and
//! freezes the file. Rust rechecks names, endpoints, campaign binding, and
//! mutations. Request identity is the session and request id, not a digest.

use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use super::mutations::{self, expand_named};
use crate::{
    engine::input::{GlobalContextValue, ParamValue},
    protocol::{Binding, IndexedModel, StepRecord},
};

pub const PROPOSAL_VERSION: u32 = 2;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct ProposalGoal {
    #[serde(default)]
    pub handler_ids: Vec<String>,
    pub reason: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ProposalChannel {
    pub channel: String,
    pub endpoints: Vec<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct MutableField {
    pub field: String,
    #[serde(default)]
    pub kind: String,
    #[serde(default)]
    pub default: Option<ParamValue>,
    #[serde(default)]
    pub source: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
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

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
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
    /// Mutable field descriptions after Rust strips the mutation strategy.
    #[serde(default)]
    pub mutable_fields: Vec<MutableField>,
}

impl mutations::Named for ProposalStep {
    fn instance_name(&self) -> &str {
        &self.name
    }
    fn model_step(&self) -> &str {
        &self.step
    }
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Proposal {
    pub version: u32,
    pub origin: String,
    pub session: String,
    pub request_id: String,
    pub build: String,
    pub snapshot: String,
    pub graph_revision: u64,
    pub goal: ProposalGoal,
    pub parties: BTreeMap<String, String>,
    pub channels: BTreeMap<String, ProposalChannel>,
    pub trace: Vec<ProposalStep>,
    #[serde(default)]
    pub mutations: Vec<Value>,
    #[serde(default)]
    pub global_context: Vec<GlobalContextValue>,
    #[serde(default)]
    pub dependencies: Vec<ProposalDependency>,
}

/// Frozen proposal plus the on-disk bytes accepted by Rust.
#[derive(Clone, Debug)]
pub struct AcceptedProposal {
    pub proposal: Proposal,
    pub path: PathBuf,
    pub sha256: String,
}

impl AcceptedProposal {
    pub fn request_id(&self) -> &str {
        &self.proposal.request_id
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

/// Identity of the analysis or synthesis request a proposal must belong to.
#[derive(Clone, Debug)]
pub struct ProposalIdentity {
    pub session: String,
    pub request_id: String,
    pub build: String,
    pub snapshot: String,
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
        version: PROPOSAL_VERSION,
        origin: "autonomous".into(),
        session: String::new(),
        request_id: String::new(),
        build: String::new(),
        snapshot: String::new(),
        graph_revision: 0,
        goal: ProposalGoal {
            handler_ids: Vec::new(),
            reason: String::new(),
        },
        parties,
        channels,
        trace,
        mutations: Vec::new(),
        global_context: Vec::new(),
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

    /// Validate names, endpoints, binding, and mutations. Returns the model steps in trace order.
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
        ] {
            if self.origin == "autonomous"
                && (name == "build" || name == "snapshot")
                && wanted.is_empty()
            {
                continue;
            }
            if !wanted.is_empty() && actual != wanted {
                return Err(format!("proposal.{name} does not match the frozen request"));
            }
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
        let names = mutations::instance_names(&self.trace)?;
        if names.len() != names.iter().collect::<BTreeSet<_>>().len() {
            return Err("duplicate instance name".into());
        }
        for (instance, party) in &self.parties {
            if party.is_empty() || model.party_names().get(party).is_none() {
                return Err(format!(
                    "party instance {instance} does not name a model party"
                ));
            }
        }
        for (instance, channel) in &self.channels {
            if model.channel(&channel.channel).is_none() {
                return Err(format!(
                    "channel instance {instance} does not name a model channel"
                ));
            }
            for endpoint in &channel.endpoints {
                if !self.parties.contains_key(endpoint) {
                    return Err(format!(
                        "channel {instance} endpoint {endpoint} is not a party instance"
                    ));
                }
            }
        }
        let mut steps = Vec::with_capacity(self.trace.len());
        for item in &self.trace {
            let step = model
                .steps
                .iter()
                .find(|step| step.name == item.step)
                .ok_or_else(|| format!("unknown step {}", item.step))?
                .clone();
            if !model.carries_step(&step, binding) {
                return Err(format!(
                    "step {} cannot be carried by this campaign",
                    item.step
                ));
            }
            self.check_binding(model, item, &step)?;
            if item.reason.trim().is_empty() {
                return Err(format!("instance {} needs a reason", item.name));
            }
            steps.push(step);
        }
        mutations::validate_mutations(
            &names,
            |index| self.trace[index].action == "send",
            &self.mutations,
        )?;
        let mut globals = BTreeSet::new();
        for value in &self.global_context {
            if value.name.trim().is_empty() || !globals.insert(value.name.clone()) {
                return Err("global_context names must be non-empty and unique".into());
            }
            let source = if value.source.is_empty() {
                "constant"
            } else {
                value.source.as_str()
            };
            if source != "constant" && source != "environment" && source != "generated" {
                return Err(format!(
                    "global_context {} has unknown source {}",
                    value.name, value.source
                ));
            }
            if source == "generated" && value.seed.is_none() {
                return Err(format!(
                    "global_context generated value {} needs a replay seed",
                    value.name
                ));
            }
        }
        for dependency in &self.dependencies {
            if !names.contains(&dependency.prior) || !names.contains(&dependency.dependent) {
                return Err(format!(
                    "dependency {} -> {} references an unknown instance",
                    dependency.prior, dependency.dependent
                ));
            }
            if dependency.prior == dependency.dependent {
                return Err("dependency cannot point an instance at itself".into());
            }
            let kind = if dependency.kind.is_empty() {
                "order"
            } else {
                dependency.kind.as_str()
            };
            if kind != "order" && kind != "data" && kind != "resource" && kind != "state" {
                return Err(format!("unknown dependency kind {}", dependency.kind));
            }
        }
        Ok(steps)
    }

    fn check_binding(
        &self,
        model: &IndexedModel,
        item: &ProposalStep,
        step: &StepRecord,
    ) -> Result<(), String> {
        if item.action != step.action.name() {
            return Err(format!(
                "instance {} action does not match the model step",
                item.name
            ));
        }
        if item.condition != step.condition {
            return Err(format!(
                "instance {} condition does not match the model step",
                item.name
            ));
        }
        if item.message.as_deref() != step.message_name() {
            return Err(format!(
                "instance {} message does not match the model step",
                item.name
            ));
        }
        let channel = self
            .channels
            .get(&item.channel)
            .ok_or_else(|| format!("unknown channel instance {}", item.channel))?;
        if channel.channel != step.channel_name {
            return Err(format!(
                "instance {} channel does not match the model step",
                item.name
            ));
        }
        let party_model = |instance: &str| -> Result<String, String> {
            if !channel.endpoints.iter().any(|name| name == instance) {
                return Err(format!(
                    "party instance {instance} is not an endpoint of {}",
                    item.channel
                ));
            }
            self.parties
                .get(instance)
                .cloned()
                .ok_or_else(|| format!("unknown party instance {instance}"))
        };
        if step.action.name() == "send" {
            let sender = party_model(item.sender.as_deref().unwrap_or(""))?;
            let receiver = party_model(item.receiver.as_deref().unwrap_or(""))?;
            if Some(sender.as_str()) != step.sender_name()
                || Some(receiver.as_str()) != step.receiver_name()
            {
                return Err(format!(
                    "instance {} endpoints do not match the model step",
                    item.name
                ));
            }
            if item.party.is_some() {
                return Err(format!("send instance {} cannot name a party", item.name));
            }
        } else {
            let actor = party_model(item.party.as_deref().unwrap_or(""))?;
            if Some(actor.as_str()) != step.party_name() {
                return Err(format!(
                    "instance {} party does not match the model step",
                    item.name
                ));
            }
            if item.sender.is_some() || item.receiver.is_some() {
                return Err(format!(
                    "network instance {} cannot name a sender or receiver",
                    item.name
                ));
            }
        }
        let _ = model;
        Ok(())
    }

    /// Expand duplicate mutations. `counts` maps each count_param to a runtime value.
    pub fn expand(&self, counts: &BTreeMap<String, Value>) -> Result<Vec<ProposalStep>, String> {
        let names = mutations::instance_names(&self.trace)?;
        expand_named(&self.trace, &names, &self.mutations, &|name| {
            counts.get(name).cloned().unwrap_or(json!(0))
        })
    }

    /// Downstream copy for synthesis: strip mutation strategy and expose only
    /// the mutable field descriptions. The original proposal remains available
    /// for handoff/audit.
    pub fn synthesis_copy(&self) -> Result<Self, String> {
        let names = mutations::instance_names(&self.trace)?;
        let mut copy = self.clone();
        copy.mutations.clear();
        for item in &mut copy.trace {
            item.mutable_fields.clear();
        }
        let mut declared = BTreeSet::new();
        for mutation in &self.mutations {
            if mutation.get("op").and_then(Value::as_str) != Some("field") {
                continue;
            }
            let field = mutation
                .get("field")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim()
                .to_string();
            if field.is_empty() {
                continue;
            }
            match mutations::selection(mutation.get("steps").unwrap_or(&Value::Null), &names)? {
                mutations::Selector::Each(indexes) => {
                    for index in indexes {
                        let key = (index, field.clone());
                        if declared.insert(key) {
                            copy.trace[index].mutable_fields.push(MutableField {
                                field: field.clone(),
                                kind: "string".into(),
                                default: None,
                                source: "proposal".into(),
                            });
                        }
                    }
                }
                mutations::Selector::Range(start, end) => {
                    for index in start..=end {
                        let key = (index, field.clone());
                        if declared.insert(key) {
                            copy.trace[index].mutable_fields.push(MutableField {
                                field: field.clone(),
                                kind: "string".into(),
                                default: None,
                                source: "proposal".into(),
                            });
                        }
                    }
                }
            }
        }
        Ok(copy)
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
            "meta": {"schema_version": "8.0.0", "protocol": "demo", "complete": true, "documents": []},
            "parties": [
                {"name": "client", "description": "c"},
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

    fn identity() -> ProposalIdentity {
        ProposalIdentity {
            session: "s".into(),
            request_id: "r".into(),
            build: "b".into(),
            snapshot: "000001".into(),
        }
    }

    fn base(model: &IndexedModel) -> Proposal {
        let mut proposal = instantiate(model, &model.steps).unwrap();
        proposal.origin = "coverage".into();
        proposal.session = "s".into();
        proposal.request_id = "r".into();
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
    fn repeated_instances_and_distinct_party_channels_validate() {
        let (model, binding) = fixture();
        let mut proposal = base(&model);
        let first = proposal.trace[0].clone();
        let mut second = first.clone();
        second.name = "hello_again".into();
        proposal.trace.insert(1, second);
        proposal
            .validate(&model, &binding, &identity())
            .expect("repeated step instances");
        let mut clients = proposal.clone();
        clients.parties.insert("client_b".into(), "client".into());
        clients
            .channels
            .get_mut("control")
            .unwrap()
            .endpoints
            .push("client_b".into());
        clients.trace[0].sender = Some("client_b".into());
        clients
            .validate(&model, &binding, &identity())
            .expect("two client instances");
    }

    #[test]
    fn illegal_endpoint_action_and_network_handler_ids_fail() {
        let (model, binding) = fixture();
        let mut proposal = base(&model);
        proposal.trace[0].sender = Some("server_side".into());
        proposal
            .parties
            .insert("server_side".into(), "server".into());
        proposal
            .channels
            .get_mut("control")
            .unwrap()
            .endpoints
            .push("server_side".into());
        let error = proposal
            .validate(&model, &binding, &identity())
            .unwrap_err();
        assert!(error.contains("endpoints"), "{error}");
        let mut action = base(&model);
        action.trace[1].action = "send".into();
        let error = action.validate(&model, &binding, &identity()).unwrap_err();
        assert!(error.contains("action"), "{error}");
        let raw = json!({
            "version": 2, "origin": "coverage", "session": "s", "request_id": "r",
            "build": "b", "snapshot": "000001", "graph_revision": 0,
            "goal": {"handler_ids": ["h"], "reason": "g"},
            "parties": {"peer": "peer"},
            "channels": {"data": {"channel": "data", "endpoints": ["peer"]}},
            "trace": [{
                "name": "listen_once", "step": "listen_data", "action": "listen",
                "condition": "after hello", "message": null, "channel": "data",
                "party": "peer", "reason": "listen", "constraints": [],
                "handler_ids": ["h"]
            }],
            "mutations": []
        });
        let error = serde_json::from_value::<Proposal>(raw).unwrap_err();
        assert!(error.to_string().contains("handler_ids"), "{error}");
        let mut old = base(&model);
        old.version = 1;
        assert!(old.validate(&model, &binding, &identity()).is_err());
    }

    #[test]
    fn autonomous_proposal_allows_empty_build_and_snapshot() {
        let (model, binding) = fixture();
        let mut proposal = base(&model);
        proposal.origin = "autonomous".into();
        proposal.goal.handler_ids.clear();
        proposal.build.clear();
        proposal.snapshot.clear();
        let expected = ProposalIdentity {
            build: String::new(),
            snapshot: String::new(),
            ..identity()
        };
        proposal.validate(&model, &binding, &expected).unwrap();
        proposal.request_id = "other".into();
        assert!(proposal.validate(&model, &binding, &expected).is_err());
    }

    #[test]
    fn global_context_and_instance_dependencies_validate() {
        let (model, binding) = fixture();
        let mut proposal = base(&model);
        proposal.global_context.push(GlobalContextValue {
            name: "username".into(),
            kind: "string".into(),
            value: Some(ParamValue::Str("ubuntu".into())),
            source: "constant".into(),
            seed: None,
        });
        proposal.dependencies.push(ProposalDependency {
            prior: "hello".into(),
            dependent: "bye".into(),
            condition: String::new(),
            kind: "data".into(),
            fields: vec!["challenge".into()],
        });
        proposal
            .validate(&model, &binding, &identity())
            .expect("global context and dependencies");
        let mut bad = proposal.clone();
        bad.dependencies[0].dependent = "missing".into();
        assert!(bad.validate(&model, &binding, &identity()).is_err());
        let mut bad = proposal;
        bad.global_context.push(GlobalContextValue {
            name: "username".into(),
            kind: String::new(),
            value: None,
            source: "constant".into(),
            seed: None,
        });
        assert!(bad.validate(&model, &binding, &identity()).is_err());
    }

    #[test]
    fn synthesis_copy_strips_mutations_and_adds_mutable_fields() {
        let (model, _binding) = fixture();
        let mut proposal = base(&model);
        proposal.mutations = vec![json!({
            "op": "field", "steps": ["hello"], "field": "username",
            "suggestion": "empty or boundary", "evidence": "src/a.c:1"
        })];
        let copy = proposal.synthesis_copy().unwrap();
        assert!(copy.mutations.is_empty());
        let hello = copy.trace.iter().find(|step| step.step == "hello").unwrap();
        assert_eq!(hello.mutable_fields.len(), 1);
        assert_eq!(hello.mutable_fields[0].field, "username");
        assert_eq!(proposal.mutations.len(), 1);
    }

    #[test]
    fn mailbox_keeps_only_the_latest_proposal() {
        let mailbox = ProposalMailbox::default();
        let make = |id: &str| AcceptedProposal {
            proposal: Proposal {
                version: 2,
                origin: "autonomous".into(),
                session: "s".into(),
                request_id: id.into(),
                build: String::new(),
                snapshot: String::new(),
                graph_revision: 0,
                goal: ProposalGoal {
                    handler_ids: vec![],
                    reason: "g".into(),
                },
                parties: BTreeMap::new(),
                channels: BTreeMap::new(),
                trace: vec![],
                mutations: vec![],
                global_context: vec![],
                dependencies: vec![],
            },
            path: PathBuf::from(id),
            sha256: String::new(),
        };
        mailbox.replace(make("a"));
        mailbox.replace(make("b"));
        assert_eq!(mailbox.peek().unwrap().request_id(), "b");
        assert_eq!(mailbox.take().unwrap().request_id(), "b");
        assert!(mailbox.take().is_none());
    }
}
