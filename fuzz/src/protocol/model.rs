//! Schema 7.0.0 protocol models and name indexes.

use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
};

use serde::Deserialize;
use serde_json::Value;

use super::id::protocol_id;

/// How a campaign carries one named party.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PartyRole {
    /// The party is the system under test.
    Sut,
    /// The party is implemented by the generator.
    Generator,
    /// The party cannot be executed in this campaign.
    Unavailable,
}

impl PartyRole {
    /// Parse a campaign binding value.
    pub fn parse(text: &str) -> Result<Self, ModelError> {
        match text {
            "sut" => Ok(Self::Sut),
            "generator" => Ok(Self::Generator),
            "unavailable" => Ok(Self::Unavailable),
            other => Err(ModelError::new(format!(
                "party role {other} is not sut, generator, or unavailable"
            ))),
        }
    }

    /// The step can be assigned to this campaign.
    pub fn can_carry(self) -> bool {
        !matches!(self, Self::Unavailable)
    }
}

/// A role assignment for every party the campaign names.
#[derive(Clone, Debug, Default)]
pub struct Binding {
    roles: BTreeMap<String, PartyRole>,
}

impl Binding {
    /// Build bindings. Parties omitted from the map are unavailable.
    pub fn new(roles: BTreeMap<String, PartyRole>) -> Self {
        Self { roles }
    }

    /// Role of one party. Unknown names are unavailable.
    pub fn role(&self, name: &str) -> PartyRole {
        self.roles
            .get(name)
            .copied()
            .unwrap_or(PartyRole::Unavailable)
    }

    /// Explicit campaign bindings. Omitted parties stay unavailable.
    pub fn entries(&self) -> impl Iterator<Item = (&str, PartyRole)> {
        self.roles.iter().map(|(name, role)| (name.as_str(), *role))
    }
}

#[derive(Debug)]
pub struct ModelError {
    message: String,
}

impl ModelError {
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }

    pub fn message(&self) -> &str {
        &self.message
    }
}

impl From<ModelError> for libafl::Error {
    fn from(error: ModelError) -> Self {
        libafl::Error::illegal_argument(error.message)
    }
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ModelFile {
    meta: Meta,
    parties: Vec<Party>,
    messages: Vec<Message>,
    channels: Vec<Channel>,
    steps: Vec<Step>,
    step_dependencies: Vec<Dependency>,
    unresolved: Vec<Issue>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Meta {
    schema_version: String,
    protocol: String,
    complete: bool,
    documents: Vec<SourceDoc>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct SourceDoc {
    pub(crate) doc_id: String,
    pub(crate) rfc: String,
    pub(crate) input_sha256: String,
    pub(crate) document_sha256: String,
    pub(crate) path: String,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Party {
    pub(crate) name: String,
    pub(crate) description: String,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Message {
    pub(crate) name: String,
    pub(crate) associated_fields: Vec<Field>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Field {
    pub(crate) name: String,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Channel {
    pub(crate) name: String,
    pub(crate) description: String,
    pub(crate) transport: String,
    pub(crate) connection: String,
    pub(crate) endpoint_party_names: Vec<String>,
    pub(crate) initiator_party_name: Option<String>,
    pub(crate) acceptor_party_name: Option<String>,
    pub(crate) endpoint_sources: Vec<EndpointSource>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct EndpointSource {
    pub(crate) party_name: String,
    pub(crate) parameter: String,
    pub(crate) from: String,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Step {
    id: String,
    name: String,
    message_name: String,
    channel_name: String,
    sender_name: String,
    receiver_name: String,
    condition: String,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Dependency {
    pub(crate) prior_step: String,
    pub(crate) dependent_step: String,
    pub(crate) condition: String,
    pub(crate) field_relations: Vec<FieldRelation>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct FieldRelation {
    pub(crate) prior_field: String,
    pub(crate) dependent_field: String,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Issue {
    pub(crate) description: String,
    pub(crate) object_names: Vec<String>,
}

/// One step after the model has been indexed.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StepRecord {
    /// Original `id` from the model. Not part of [`super::StepId`].
    pub source_id: String,
    pub name: String,
    pub message_name: String,
    pub channel_name: String,
    pub sender_name: String,
    pub receiver_name: String,
    pub condition: String,
    /// Position in the model. Selection preserves this order.
    pub index: usize,
}

/// Immutable campaign snapshot.
#[derive(Clone, Debug)]
pub struct IndexedModel {
    pub protocol_name: String,
    pub complete: bool,
    pub protocol_id: String,
    pub steps: Vec<StepRecord>,
    pub(crate) messages: BTreeMap<String, Message>,
    pub(crate) channels: BTreeMap<String, Channel>,
    pub(crate) parties: BTreeMap<String, Party>,
    pub(crate) dependencies: Vec<Dependency>,
    pub(crate) unresolved: Vec<Issue>,
    pub(crate) documents: Vec<SourceDoc>,
    pub(crate) snapshot: Value,
    model_path: PathBuf,
    model_dir: PathBuf,
}

impl IndexedModel {
    pub fn model_path(&self) -> &Path {
        &self.model_path
    }

    pub fn model_dir(&self) -> &Path {
        &self.model_dir
    }

    pub fn snapshot(&self) -> &Value {
        &self.snapshot
    }

    pub fn document_paths(&self) -> Vec<(String, PathBuf)> {
        self.documents
            .iter()
            .map(|doc| (doc.rfc.clone(), self.model_dir.join(&doc.path)))
            .collect()
    }

    /// Party names defined by the model, in name order.
    pub fn party_names(&self) -> BTreeSet<String> {
        self.parties.keys().cloned().collect()
    }

    /// Party name and description, in name order.
    pub fn described_parties(&self) -> Vec<(String, String)> {
        self.parties
            .iter()
            .map(|(name, party)| (name.clone(), party.description.clone()))
            .collect()
    }

    /// Endpoint, initiator, and acceptor names for one channel.
    pub fn channel_party_names(&self, channel_name: &str) -> Vec<String> {
        let Some(channel) = self.channels.get(channel_name) else {
            return Vec::new();
        };
        let mut names = channel.endpoint_party_names.clone();
        if let Some(name) = &channel.initiator_party_name {
            names.push(name.clone());
        }
        if let Some(name) = &channel.acceptor_party_name {
            names.push(name.clone());
        }
        names
    }

    /// Whether this step's sender, receiver, and channel parties can be carried.
    pub fn carries_step(&self, step: &StepRecord, binding: &Binding) -> bool {
        self.step_is_eligible(step, binding)
    }

    /// Steps whose parties and channel endpoints the binding can carry.
    pub fn eligible<'a>(&'a self, binding: &Binding) -> Vec<&'a StepRecord> {
        self.steps
            .iter()
            .filter(|step| self.step_is_eligible(step, binding))
            .collect()
    }

    fn step_is_eligible(&self, step: &StepRecord, binding: &Binding) -> bool {
        if !binding.role(&step.sender_name).can_carry()
            || !binding.role(&step.receiver_name).can_carry()
        {
            return false;
        }
        let Some(channel) = self.channels.get(&step.channel_name) else {
            return false;
        };
        channel
            .endpoint_party_names
            .iter()
            .chain(channel.initiator_party_name.iter())
            .chain(channel.acceptor_party_name.iter())
            .all(|party| binding.role(party).can_carry())
    }

    pub(crate) fn message(&self, name: &str) -> Option<&Message> {
        self.messages.get(name)
    }

    pub(crate) fn channel(&self, name: &str) -> Option<&Channel> {
        self.channels.get(name)
    }

    pub(crate) fn party(&self, name: &str) -> Option<&Party> {
        self.parties.get(name)
    }
}

/// Load `protocol.json` and check references. `meta.complete = false` is allowed.
pub fn load_model(path: &Path) -> Result<IndexedModel, ModelError> {
    let bytes = fs::read(path).map_err(|error| {
        ModelError::new(format!(
            "reading protocol model {}: {error}",
            path.display()
        ))
    })?;
    let snapshot: Value = serde_json::from_slice(&bytes)
        .map_err(|error| ModelError::new(format!("protocol model is not JSON: {error}")))?;
    let file: ModelFile = serde_json::from_value(snapshot.clone()).map_err(|error| {
        ModelError::new(format!(
            "protocol model does not match schema 7.0.0: {error}"
        ))
    })?;
    if file.meta.schema_version != "7.0.0" {
        return Err(ModelError::new(format!(
            "protocol schema_version is {}, expected 7.0.0",
            file.meta.schema_version
        )));
    }
    let mut parties = BTreeMap::new();
    for party in &file.parties {
        if parties.insert(party.name.clone(), party.clone()).is_some() {
            return Err(ModelError::new(format!(
                "duplicate party name {}",
                party.name
            )));
        }
    }
    let mut messages = BTreeMap::new();
    for message in &file.messages {
        if messages
            .insert(message.name.clone(), message.clone())
            .is_some()
        {
            return Err(ModelError::new(format!(
                "duplicate message name {}",
                message.name
            )));
        }
    }
    let mut channels = BTreeMap::new();
    for channel in &file.channels {
        if channels
            .insert(channel.name.clone(), channel.clone())
            .is_some()
        {
            return Err(ModelError::new(format!(
                "duplicate channel name {}",
                channel.name
            )));
        }
        for party in channel
            .endpoint_party_names
            .iter()
            .chain(channel.initiator_party_name.iter())
            .chain(channel.acceptor_party_name.iter())
        {
            if !parties.contains_key(party) {
                return Err(ModelError::new(format!(
                    "channel {} references missing party {party}",
                    channel.name
                )));
            }
        }
    }
    let mut names = BTreeSet::new();
    let mut steps = Vec::new();
    for (index, step) in file.steps.iter().enumerate() {
        if !names.insert(step.name.clone()) {
            return Err(ModelError::new(format!(
                "duplicate step name {}",
                step.name
            )));
        }
        if !messages.contains_key(&step.message_name) {
            return Err(ModelError::new(format!(
                "step {} references missing message {}",
                step.name, step.message_name
            )));
        }
        if !channels.contains_key(&step.channel_name) {
            return Err(ModelError::new(format!(
                "step {} references missing channel {}",
                step.name, step.channel_name
            )));
        }
        for party in [&step.sender_name, &step.receiver_name] {
            if !parties.contains_key(party) {
                return Err(ModelError::new(format!(
                    "step {} references missing party {party}",
                    step.name
                )));
            }
        }
        steps.push(StepRecord {
            source_id: step.id.clone(),
            name: step.name.clone(),
            message_name: step.message_name.clone(),
            channel_name: step.channel_name.clone(),
            sender_name: step.sender_name.clone(),
            receiver_name: step.receiver_name.clone(),
            condition: step.condition.clone(),
            index,
        });
    }
    let known: BTreeSet<&str> = steps.iter().map(|step| step.name.as_str()).collect();
    for dep in &file.step_dependencies {
        if !known.contains(dep.prior_step.as_str()) || !known.contains(dep.dependent_step.as_str())
        {
            return Err(ModelError::new(format!(
                "dependency {} -> {} references a missing step",
                dep.prior_step, dep.dependent_step
            )));
        }
    }
    let model_dir = path
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .to_path_buf();
    Ok(IndexedModel {
        protocol_name: file.meta.protocol,
        complete: file.meta.complete,
        protocol_id: protocol_id(&snapshot),
        steps,
        messages,
        channels,
        parties,
        dependencies: file.step_dependencies,
        unresolved: file.unresolved,
        documents: file.meta.documents,
        snapshot,
        model_path: std::path::absolute(path)
            .map_err(|error| ModelError::new(format!("resolving model path: {error}")))?,
        model_dir,
    })
}
