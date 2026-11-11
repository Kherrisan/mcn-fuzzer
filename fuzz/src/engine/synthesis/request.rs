//! One generator request and the proposal it must follow.

use std::{fs, path::PathBuf, sync::Arc};

use serde_json::Value;

use super::{prompts, GeneratorInput, TraceOrigin};
use crate::engine::input::StepInstance;
use crate::{
    coverage::proposal::{Proposal, ProposalIdentity},
    protocol::{Binding, IndexedModel},
};

/// Identity and artifact directory shared by dispatch, prompting, and validation.
pub(super) struct GeneratorRequest {
    pub(super) id: String,
    pub(super) output_dir: PathBuf,
    pub(super) session: String,
    pub(super) build: String,
    pub(super) snapshot: String,
    pub(super) model: Arc<IndexedModel>,
    pub(super) binding: Binding,
    pub(super) autonomous: bool,
}

/// Production-validated generator. It carries no HC terminal or channel state.
#[derive(Clone, Debug)]
pub struct ValidatedGenerator {
    pub input: GeneratorInput,
    pub origin: TraceOrigin,
}

impl GeneratorRequest {
    pub(super) fn prompt(
        &self,
        mode: &str,
        context: &prompts::SynthesisPromptContext,
    ) -> Result<String, libafl::Error> {
        let proposal = self.output_dir.join("proposal.json");
        context.generation_prompt(
            &self.output_dir,
            &self.id,
            &self.session,
            mode,
            proposal.is_file().then_some(proposal.as_path()),
            self.autonomous,
        )
    }

    /// Validate and publish this request's generator artifacts.
    pub(super) fn accept(&self) -> Result<ValidatedGenerator, String> {
        let (result, params) = super::artifacts::check(&self.output_dir, &self.id)?;
        let proposal = Proposal::read(&self.output_dir.join("proposal.json"))?;
        if !proposal.mutations.is_empty() {
            return Err(
                "synthesis proposal still contains mutations; Rust must strip them before synthesis"
                    .to_string(),
            );
        }
        // A coverage proposal keeps the analysis request id it was created for;
        // the synthesis request id is recorded by result.json and the directory.
        // Autonomous proposals are authored for this synthesis request.
        let request_id = if self.autonomous {
            self.id.clone()
        } else {
            proposal.request_id.clone()
        };
        let identity = ProposalIdentity {
            session: self.session.clone(),
            request_id,
            build: self.build.clone(),
            snapshot: self.snapshot.clone(),
        };
        proposal.validate(&self.model, &self.binding, &identity)?;
        if self.autonomous {
            if proposal.origin != "autonomous" {
                return Err("autonomous synthesis needs an autonomous proposal".into());
            }
        } else {
            if proposal.origin != "coverage" {
                return Err("coverage synthesis needs a coverage proposal".into());
            }
            if proposal.request_id != self.id {
                return Err("proposal request_id does not match this synthesis request".into());
            }
        }
        let published = self.output_dir.join("published");
        if published.exists() {
            return Err("published generator path already exists".to_string());
        }
        fs::create_dir_all(&published).map_err(|error| error.to_string())?;
        fs::copy(
            self.output_dir.join("generator.py"),
            published.join("generator.py"),
        )
        .map_err(|error| error.to_string())?;
        fs::copy(
            self.output_dir.join("params.schema.json"),
            published.join("params.schema.json"),
        )
        .map_err(|error| error.to_string())?;
        let generator =
            fs::canonicalize(published.join("generator.py")).map_err(|error| error.to_string())?;
        let step_trace = proposal
            .trace
            .iter()
            .map(|step| {
                let fields = step
                    .mutable_fields
                    .iter()
                    .filter_map(|field| {
                        params
                            .get(&field.field)
                            .cloned()
                            .or_else(|| field.default.clone())
                            .map(|value| (field.field.clone(), value))
                    })
                    .collect();
                StepInstance {
                    name: step.name.clone(),
                    step: step.step.clone(),
                    action: step.action.clone(),
                    condition: step.condition.clone(),
                    message: step.message.clone(),
                    channel: step.channel.clone(),
                    sender: step.sender.clone(),
                    receiver: step.receiver.clone(),
                    party: step.party.clone(),
                    fields,
                    omitted_fields: Vec::new(),
                    inputs: std::collections::BTreeMap::new(),
                    broken_dependencies: Vec::new(),
                }
            })
            .collect::<Vec<_>>();
        let mut step_trace = step_trace;
        for dependency in &proposal.dependencies {
            let Some(dependent) = step_trace
                .iter_mut()
                .find(|step| step.name == dependency.dependent)
            else {
                continue;
            };
            for binding in &dependency.fields {
                dependent.inputs.insert(
                    binding.dependent_field.clone(),
                    format!("{}.{}", dependency.prior, binding.prior_field),
                );
            }
        }
        let added = result
            .get("added_steps")
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect()
            })
            .unwrap_or_default();
        let parties = proposal
            .parties
            .iter()
            .map(|(name, model_party)| crate::engine::input::PartyInstance {
                name: name.clone(),
                r#type: model_party.clone(),
            })
            .collect::<Vec<_>>();
        let channels = proposal
            .channels
            .iter()
            .map(|(name, channel)| crate::engine::input::ChannelInstance {
                name: name.clone(),
                r#type: channel.channel.clone(),
                endpoints: channel.endpoints.clone(),
            })
            .collect::<Vec<_>>();
        let dependencies = proposal
            .dependencies
            .iter()
            .map(|dependency| crate::engine::input::DependencyInstance {
                prior: dependency.prior.clone(),
                dependent: dependency.dependent.clone(),
                kind: if dependency.kind.is_empty() {
                    "order".to_string()
                } else {
                    dependency.kind.clone()
                },
                condition: dependency.condition.clone(),
                fields: dependency
                    .fields
                    .iter()
                    .map(|binding| crate::engine::input::FieldBinding {
                        prior_field: binding.prior_field.clone(),
                        dependent_field: binding.dependent_field.clone(),
                        derive: binding.derive.clone(),
                    })
                    .collect(),
                origin: "proposal".into(),
            })
            .collect();
        let mut input =
            GeneratorInput::with_trace(generator, step_trace, proposal.global_context.clone());
        input.trace.parties = parties;
        input.trace.channels = channels;
        input.trace.dependencies = dependencies;
        input.trace.validate()?;
        Ok(ValidatedGenerator {
            input,
            origin: TraceOrigin {
                request_id: self.id.clone(),
                session: proposal.session.clone(),
                step_names: proposal
                    .trace
                    .iter()
                    .map(|step| step.name.clone())
                    .collect(),
                added_steps: added,
            },
        })
    }
}
