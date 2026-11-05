//! Trace selection. The first strategy is random and keeps model order.

use std::collections::{BTreeMap, BTreeSet};

use std::num::NonZeroUsize;

use libafl_bolts::rands::Rand;

use super::{
    id::{StepId, TraceId},
    model::{Binding, IndexedModel, PartyRole, StepRecord},
};

/// True when the selected steps' senders and receivers include both roles.
///
/// Channel endpoints, unselected dependencies, and the rest of the model do not count.
pub(crate) fn covers_sut_and_generator(steps: &[StepRecord], binding: &Binding) -> bool {
    let mut sut = false;
    let mut generator = false;
    for step in steps {
        for name in [&step.sender_name, &step.receiver_name] {
            match binding.role(name) {
                PartyRole::Sut => sut = true,
                PartyRole::Generator => generator = true,
                PartyRole::Unavailable => {}
            }
        }
    }
    sut && generator
}

/// One chosen target sequence. Later selectors may repeat a step.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StepTrace {
    pub id: TraceId,
    pub steps: Vec<StepRecord>,
    pub step_ids: Vec<StepId>,
}

impl StepTrace {
    /// Construct a trace and derive its identifiers from the ordered steps.
    pub fn new(steps: Vec<StepRecord>) -> Self {
        let step_ids: Vec<_> = steps.iter().map(StepRecord::id).collect();
        let id = TraceId::from_steps(&steps);
        Self {
            id,
            steps,
            step_ids,
        }
    }
}

/// Read-only facts a selector may use. Selection is not execution coverage.
#[derive(Clone, Debug, Default)]
pub struct SelectionContext {
    pub attempted: BTreeSet<String>,
    pub selections: u64,
    pub generated_ok: u64,
    pub generated_err: u64,
    pub by_trace: BTreeMap<String, u64>,
}

/// Picks one ordered trace, or skips this round.
pub trait TraceSelector {
    fn select(
        &self,
        model: &IndexedModel,
        binding: &Binding,
        rand: &mut impl Rand,
        context: &SelectionContext,
        attempts: u32,
        min_len: usize,
        max_len: usize,
    ) -> Option<StepTrace>;
}

/// Sample eligible steps without replacement, then restore model order.
#[derive(Clone, Debug, Default)]
pub struct RandomTraceSelector;

impl TraceSelector for RandomTraceSelector {
    fn select(
        &self,
        model: &IndexedModel,
        binding: &Binding,
        rand: &mut impl Rand,
        context: &SelectionContext,
        attempts: u32,
        min_len: usize,
        max_len: usize,
    ) -> Option<StepTrace> {
        let eligible = model.eligible(binding);
        if eligible.is_empty() || max_len == 0 || attempts == 0 {
            return None;
        }
        let min_len = min_len.max(1).min(eligible.len());
        let max_len = max_len.max(min_len).min(eligible.len());
        for _ in 0..attempts {
            let span = max_len - min_len + 1;
            let len = min_len + rand.below(NonZeroUsize::new(span).expect("span"));
            let mut picked = Vec::new();
            let mut used = vec![false; eligible.len()];
            for _ in 0..len {
                let remaining = eligible.len() - picked.len();
                let mut choice = rand.below(NonZeroUsize::new(remaining).expect("remaining"));
                for (index, taken) in used.iter().enumerate() {
                    if *taken {
                        continue;
                    }
                    if choice == 0 {
                        used[index] = true;
                        picked.push(index);
                        break;
                    }
                    choice -= 1;
                }
            }
            picked.sort_unstable();
            let steps: Vec<StepRecord> = picked
                .into_iter()
                .map(|index| eligible[index].clone())
                .collect();
            let trace = StepTrace::new(steps);
            if !covers_sut_and_generator(&trace.steps, binding) {
                continue;
            }
            if context.attempted.contains(&trace.id.0) {
                continue;
            }
            return Some(trace);
        }
        None
    }
}
