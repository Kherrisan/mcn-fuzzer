//! One generator request and the proposal it must follow.

use std::{fs, path::PathBuf, sync::Arc};

use serde_json::Value;

use super::{prompts, GeneratorInput, TraceOrigin};
use crate::{
    coverage::proposal::{Proposal, ProposalIdentity},
    protocol::{Binding, IndexedModel},
};

/// Identity and artifact directory shared by dispatch, prompting, and validation.
pub(super) struct GeneratorRequest {
    pub(super) id: String,
    pub(super) output_dir: PathBuf,
    pub(super) protocol_id: String,
    pub(super) session: String,
    pub(super) build: String,
    pub(super) snapshot: String,
    /// Expected proposal identity for coverage-driven requests.
    pub(super) proposal_id: Option<String>,
    pub(super) trace_id: Option<String>,
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
        let (result, params) = super::artifacts::check(
            &self.output_dir,
            &self.id,
            &self.protocol_id,
            self.trace_id.as_deref().unwrap_or(""),
            self.proposal_id.as_deref().unwrap_or(""),
        )?;
        let proposal = Proposal::read(&self.output_dir.join("proposal.json"))?;
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
            protocol_id: self.protocol_id.clone(),
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
            if self.proposal_id.as_deref() != Some(proposal.proposal_id.as_str()) {
                return Err("proposal id changed between dispatch and acceptance".into());
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
        Ok(ValidatedGenerator {
            input: GeneratorInput::new(generator, params),
            origin: TraceOrigin {
                protocol_id: proposal.protocol_id.clone(),
                trace_id: proposal.trace_id.clone(),
                step_ids: proposal
                    .trace
                    .iter()
                    .map(|step| step.step_id.clone())
                    .collect(),
                added_steps: added,
            },
        })
    }
}
