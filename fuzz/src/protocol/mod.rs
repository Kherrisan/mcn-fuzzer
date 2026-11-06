//! Protocol interaction models, stable identifiers, and the read-only catalog.
//!
//! Exploration decisions live in the coverage MCP service. Rust loads the
//! model once and uses it for identity checks, party binding, and generator
//! artifact validation.

mod catalog;
mod id;
mod model;

pub use catalog::write_catalog;
pub use id::{protocol_id, StepId, TraceId};
pub use model::{load_model, Binding, IndexedModel, ModelError, PartyRole, StepRecord};

/// Ordered step sequence used for display, evaluation fixtures, and identity.
#[derive(Clone, Debug)]
pub struct StepTrace {
    pub steps: Vec<StepRecord>,
    pub id: TraceId,
}

impl StepTrace {
    pub fn new(steps: Vec<StepRecord>) -> Self {
        let id = TraceId::from_steps(&steps);
        Self { steps, id }
    }
}

/// Whether a trace touches at least one SUT party and one generator party.
pub fn covers_sut_and_generator(steps: &[StepRecord], binding: &Binding) -> bool {
    steps.iter().any(|step| {
        [&step.sender_name, &step.receiver_name]
            .iter()
            .any(|party| binding.role(party) == PartyRole::Sut)
    }) && steps.iter().any(|step| {
        [&step.sender_name, &step.receiver_name]
            .iter()
            .any(|party| binding.role(party) == PartyRole::Generator)
    })
}
