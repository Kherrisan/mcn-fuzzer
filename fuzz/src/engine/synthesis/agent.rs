//! Synthesis-specific artifact adapter for the shared agent runtime.
use super::{GeneratorRequest, ReadyInput};
use crate::agents::{AgentClient, AgentTask, SessionInit};
use std::path::Path;

pub(super) type Client = AgentClient<SessionInit, GeneratorRequest>;

impl AgentTask for GeneratorRequest {
    type Prepared = ();
    type Output = ReadyInput;
    fn id(&self) -> &str {
        &self.id
    }
    fn output_dir(&self) -> &Path {
        &self.output_dir
    }
    fn prepare(&self) -> Result<(), String> {
        Ok(())
    }
    fn accept(&self, (): ()) -> Result<ReadyInput, String> {
        GeneratorRequest::accept(self).map(|validated| ReadyInput {
            request: self.id.clone(),
            input: validated.input,
            origin: validated.origin,
        })
    }
}
