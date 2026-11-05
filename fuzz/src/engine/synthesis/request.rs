//! One generator request and the artifacts correlated with it.

use super::{prompts, GeneratorInput, TraceOrigin};
use crate::protocol::StepTrace;
use serde_json::Value;
use std::{fs, path::PathBuf};

/// Identity and artifact directory shared by dispatch, prompting, and result validation.
pub(super) struct GeneratorRequest {
    pub(super) id: String,
    pub(super) output_dir: PathBuf,
    pub(super) protocol_id: String,
    pub(super) trace_id: String,
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
        trace: &StepTrace,
    ) -> Result<String, libafl::Error> {
        context.generation_prompt(&self.output_dir, &self.id, mode, trace)
    }

    /// Validate and publish this request's artifacts as a generator input.
    pub(super) fn accept(&self) -> Result<ValidatedGenerator, String> {
        let Self {
            output_dir,
            protocol_id,
            trace_id,
            ..
        } = self;
        let (result, params) =
            super::artifacts::check(output_dir, &self.id, protocol_id, trace_id)?;
        let published = output_dir.join("published");
        if published.exists() {
            return Err("published generator path already exists".to_string());
        }
        fs::create_dir_all(&published).map_err(|error| error.to_string())?;
        fs::copy(
            output_dir.join("generator.py"),
            published.join("generator.py"),
        )
        .map_err(|error| error.to_string())?;
        fs::copy(
            output_dir.join("params.schema.json"),
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
                protocol_id: protocol_id.to_string(),
                trace_id: trace_id.to_string(),
                step_ids: self.step_ids(),
                added_steps: added,
            },
        })
    }

    fn step_ids(&self) -> Vec<String> {
        let Ok(bytes) = fs::read(self.output_dir.join("receipt.json")) else {
            return Vec::new();
        };
        let Ok(value) = serde_json::from_slice::<Value>(&bytes) else {
            return Vec::new();
        };
        value
            .get("steps")
            .and_then(Value::as_array)
            .map(|steps| {
                steps
                    .iter()
                    .filter_map(|step| {
                        step.get("step_id")
                            .and_then(Value::as_str)
                            .map(str::to_string)
                    })
                    .collect()
            })
            .unwrap_or_default()
    }
}
