//! Field and step mutations over a concrete step trace.
//!
//! Legacy parameter dictionaries remain mutable for old local seeds/tests, but
//! synthesis inputs use `GeneratorInput::step_trace`.

use std::{borrow::Cow, collections::BTreeSet};

use libafl::{
    mutators::{MutationResult, Mutator},
    state::HasRand,
    Error, HasMetadata,
};
use libafl_bolts::{rands::Rand, Named};

use crate::{
    engine::input::{GeneratorInput, ParamValue},
    engine::stagnation::BitmapProgress,
};

const INTERESTING: &[i64] = &[
    -128, -1, 0, 1, 16, 32, 64, 100, 127, 128, 255, 256, 512, 1000, 1024, 4096, 32767, 65535, 65536,
];

/// Stacks parameter mutations the same way the previous generator mutator did.
#[derive(Debug)]
pub struct GeneratorMutator {
    name: Cow<'static, str>,
}

impl Default for GeneratorMutator {
    fn default() -> Self {
        Self::new()
    }
}

impl GeneratorMutator {
    /// Create the mutator.
    #[must_use]
    pub fn new() -> Self {
        Self {
            name: Cow::Borrowed("GeneratorMutator"),
        }
    }
}

impl Named for GeneratorMutator {
    fn name(&self) -> &Cow<'static, str> {
        &self.name
    }
}

impl<S> Mutator<GeneratorInput, S> for GeneratorMutator
where
    S: HasRand + HasMetadata,
    S::Rand: Rand,
{
    fn mutate(
        &mut self,
        state: &mut S,
        input: &mut GeneratorInput,
    ) -> Result<MutationResult, Error> {
        let result = mutate_generator(state.rand_mut(), input);
        state
            .metadata_or_insert_with(BitmapProgress::default)
            .set_mutation_pending(result == MutationResult::Mutated);
        Ok(result)
    }

    fn post_exec(
        &mut self,
        state: &mut S,
        _new_corpus_id: Option<libafl::corpus::CorpusId>,
    ) -> Result<(), Error> {
        state
            .metadata_or_insert_with(BitmapProgress::default)
            .set_mutation_pending(false);
        Ok(())
    }
}

/// Stack 2..=64 field/step edits. Traces take precedence over legacy params.
pub fn mutate_generator<R: Rand>(rand: &mut R, input: &mut GeneratorInput) -> MutationResult {
    if input.trace.steps.is_empty() && input.params.is_empty() {
        return MutationResult::Skipped;
    }
    let steps = 1u64 << (1 + rand.below_or_zero(6) as u64);
    let mut mutated = MutationResult::Skipped;
    for _ in 0..steps {
        if mutate_once(rand, input) == MutationResult::Mutated {
            mutated = MutationResult::Mutated;
        }
    }
    mutated
}

fn mutate_once<R: Rand>(rand: &mut R, input: &mut GeneratorInput) -> MutationResult {
    if !input.trace.steps.is_empty() {
        if rand.below_or_zero(100) < 70 && mutate_field(rand, input) == MutationResult::Mutated {
            return MutationResult::Mutated;
        }
        return mutate_step(rand, input);
    }
    let keys: Vec<String> = input.params.keys().cloned().collect();
    if keys.is_empty() {
        return MutationResult::Skipped;
    }
    let key = keys[rand.below_or_zero(keys.len())].clone();
    let Some(value) = input.params.get_mut(&key) else {
        return MutationResult::Skipped;
    };
    mutate_value(rand, value)
}

fn mutate_field<R: Rand>(rand: &mut R, input: &mut GeneratorInput) -> MutationResult {
    let candidates = input
        .trace
        .steps
        .iter()
        .enumerate()
        .filter(|(_, instance)| !instance.fields.is_empty())
        .map(|(index, _)| index)
        .collect::<Vec<_>>();
    if candidates.is_empty() {
        return MutationResult::Skipped;
    }
    let index = candidates[rand.below_or_zero(candidates.len())];
    let keys = input.trace.steps[index]
        .fields
        .keys()
        .cloned()
        .collect::<Vec<_>>();
    let field = keys[rand.below_or_zero(keys.len())].clone();
    if rand.below_or_zero(5) == 0 {
        input.trace.steps[index].fields.remove(&field);
        if !input.trace.steps[index]
            .omitted_fields
            .iter()
            .any(|item| item == &field)
        {
            input.trace.steps[index].omitted_fields.push(field);
        }
        return MutationResult::Mutated;
    }
    let Some(value) = input.trace.steps[index].fields.get_mut(&field) else {
        return MutationResult::Skipped;
    };
    mutate_value(rand, value)
}

fn mutate_step<R: Rand>(rand: &mut R, input: &mut GeneratorInput) -> MutationResult {
    let len = input.trace.steps.len();
    if len == 0 {
        return MutationResult::Skipped;
    }
    if len >= 4096 {
        return MutationResult::Skipped;
    }
    let original = input.trace.clone();
    let choice = rand.below_or_zero(100);
    let index = rand.below_or_zero(len);
    let operation = if choice < 55 || len == 1 {
        duplicate_block(rand, input, index)
    } else if choice < 78 && len > 1 {
        let removed = input.trace.steps.remove(index);
        input.trace.dependencies.retain(|dependency| {
            dependency.prior != removed.name && dependency.dependent != removed.name
        });
        "delete"
    } else if choice < 100 && len > 1 {
        let other = if index + 1 < len {
            index + 1
        } else {
            index - 1
        };
        input.trace.steps.swap(index, other);
        "reorder"
    } else {
        duplicate_block(rand, input, index)
    };
    let broken = broken_dependencies(input);
    let violate = rand.below_or_zero(100) >= 90;
    if !violate {
        if let Err(_error) = input.trace.validate() {
            input.trace = original;
            return MutationResult::Skipped;
        }
    } else {
        for (name, label) in broken {
            if let Some(instance) = input.trace.steps.iter_mut().find(|item| item.name == name) {
                instance
                    .broken_dependencies
                    .push(format!("{operation}: {label}"));
            }
        }
    }
    MutationResult::Mutated
}

fn duplicate_block<R: Rand>(
    rand: &mut R,
    input: &mut GeneratorInput,
    start: usize,
) -> &'static str {
    let available = input.trace.steps.len().saturating_sub(start).min(4);
    let length = 1 + rand.below_or_zero(available.max(1));
    let end = start + length;
    let block = input.trace.steps[start..end].to_vec();
    let mut names = std::collections::BTreeMap::new();
    let mut copies = Vec::with_capacity(block.len());
    for item in block {
        let old = item.name.clone();
        let mut copy = item.clone();
        copy.name = unique_name(&input.trace.steps, &copy.name);
        copy.broken_dependencies.clear();
        names.insert(old, copy.name.clone());
        copies.push(copy);
    }
    for copy in &mut copies {
        for (field, source) in &mut copy.inputs {
            for (original, mapped) in &names {
                if source == &format!("{original}.{field}") {
                    *source = format!("{mapped}.{field}");
                    break;
                }
            }
        }
    }
    input.trace.steps.splice(end..end, copies.iter().cloned());
    let inherited = input
        .trace
        .dependencies
        .iter()
        .filter(|dependency| {
            names.contains_key(&dependency.prior) && names.contains_key(&dependency.dependent)
        })
        .cloned()
        .map(|mut dependency| {
            dependency.prior = names[&dependency.prior].clone();
            dependency.dependent = names[&dependency.dependent].clone();
            dependency.origin = "mutation".into();
            dependency
        })
        .collect::<Vec<_>>();
    input.trace.dependencies.extend(inherited);
    add_compatible_dependency(rand, input, &names);
    "duplicate"
}

fn add_compatible_dependency<R: Rand>(
    rand: &mut R,
    input: &mut GeneratorInput,
    names: &std::collections::BTreeMap<String, String>,
) {
    let Some((dependent_index, dependent)) = input
        .trace
        .steps
        .iter()
        .enumerate()
        .find(|(_, step)| names.values().any(|name| name == &step.name))
    else {
        return;
    };
    let dependent_name = dependent.name.clone();
    let dependent = dependent.clone();
    let candidates = input
        .trace
        .steps
        .iter()
        .take(dependent_index)
        .enumerate()
        .filter(|(_, step)| {
            step.channel == dependent.channel
                && !input.trace.dependencies.iter().any(|dependency| {
                    dependency.prior == step.name && dependency.dependent == dependent_name
                })
        })
        .map(|(index, _)| index)
        .collect::<Vec<_>>();
    if candidates.is_empty() {
        return;
    }
    let prior_index = candidates[rand.below_or_zero(candidates.len())];
    let prior = input.trace.steps[prior_index].clone();
    // Only materialize a data dependency when the dependent already has an
    // explicit input binding to this prior. A field that merely exists on both
    // steps is not proof that the prior produces/saves it.
    let binding = dependent.inputs.iter().find_map(|(field, source)| {
        (source == &format!("{}.{field}", prior.name)).then(|| field.clone())
    });
    if let Some(binding) = binding {
        input
            .trace
            .dependencies
            .push(crate::engine::input::DependencyInstance {
                prior: prior.name.clone(),
                dependent: dependent_name.clone(),
                kind: "data".into(),
                condition: String::new(),
                fields: vec![binding.clone()],
                origin: "mutation".into(),
            });
        if let Some(step) = input
            .trace
            .steps
            .iter_mut()
            .find(|step| step.name == *dependent_name)
        {
            step.inputs
                .insert(binding.clone(), format!("{}.{binding}", prior.name));
        }
    } else {
        input
            .trace
            .dependencies
            .push(crate::engine::input::DependencyInstance {
                prior: prior.name.clone(),
                dependent: dependent_name.clone(),
                kind: "order".into(),
                condition: String::new(),
                fields: Vec::new(),
                origin: "mutation".into(),
            });
    }
}

fn broken_dependencies(input: &GeneratorInput) -> Vec<(String, String)> {
    let indexes: std::collections::BTreeMap<&str, usize> = input
        .trace
        .steps
        .iter()
        .enumerate()
        .map(|(index, item)| (item.name.as_str(), index))
        .collect();
    let mut broken = Vec::new();
    for dependency in &input.trace.dependencies {
        let prior = indexes.get(dependency.prior.as_str());
        let dependent = indexes.get(dependency.dependent.as_str());
        let label = format!("{} -> {}", dependency.prior, dependency.dependent);
        match (prior, dependent) {
            (Some(prior), Some(dependent)) if prior < dependent => {}
            _ => {
                let target = dependency.dependent.clone();
                broken.push((target, label));
            }
        }
    }
    broken
}

fn unique_name(trace: &[crate::engine::input::StepInstance], base: &str) -> String {
    let used = trace
        .iter()
        .map(|item| item.name.as_str())
        .collect::<BTreeSet<_>>();
    let mut index = 1usize;
    loop {
        let candidate = format!("{base}_copy{index}");
        if !used.contains(candidate.as_str()) {
            return candidate;
        }
        index += 1;
    }
}

pub(crate) fn mutate_value<R: Rand>(rand: &mut R, value: &mut ParamValue) -> MutationResult {
    match value {
        ParamValue::Null => MutationResult::Skipped,
        ParamValue::Int(number) => {
            if rand.below_or_zero(2) == 0 {
                *number = INTERESTING[rand.below_or_zero(INTERESTING.len())];
            } else {
                let delta = rand.below_or_zero(33) as i64 - 16;
                *number = number.saturating_add(if delta == 0 { 1 } else { delta });
            }
            MutationResult::Mutated
        }
        ParamValue::Bool(flag) => {
            *flag = !*flag;
            MutationResult::Mutated
        }
        ParamValue::Str(text) => {
            mutate_text(rand, text);
            MutationResult::Mutated
        }
        ParamValue::Bytes(bytes) => {
            mutate_bytes(rand, bytes);
            MutationResult::Mutated
        }
        ParamValue::List(items) => {
            if items.is_empty() {
                items.push(ParamValue::Int(rand.below_or_zero(256) as i64));
                return MutationResult::Mutated;
            }
            let index = rand.below_or_zero(items.len());
            mutate_value(rand, &mut items[index])
        }
    }
}

fn mutate_text<R: Rand>(rand: &mut R, text: &mut String) {
    let choice = rand.below_or_zero(3);
    if text.is_empty() || choice == 0 {
        let byte = b'a' + rand.below_or_zero(26) as u8;
        text.push(byte as char);
        return;
    }
    if choice == 1 {
        let index = rand.below_or_zero(text.chars().count());
        let start = text
            .char_indices()
            .nth(index)
            .map(|(offset, _)| offset)
            .unwrap_or(0);
        let end = text
            .char_indices()
            .nth(index + 1)
            .map(|(offset, _)| offset)
            .unwrap_or(text.len());
        text.replace_range(start..end, "");
        return;
    }
    let index = rand.below_or_zero(text.chars().count());
    let replacement = (b'A' + rand.below_or_zero(26) as u8) as char;
    if let Some((start, ch)) = text.char_indices().nth(index) {
        let end = start + ch.len_utf8();
        text.replace_range(start..end, &replacement.to_string());
    }
}

fn mutate_bytes<R: Rand>(rand: &mut R, bytes: &mut Vec<u8>) {
    let choice = rand.below_or_zero(3);
    if bytes.is_empty() || choice == 0 {
        bytes.push(rand.below_or_zero(256) as u8);
        return;
    }
    if choice == 1 {
        let index = rand.below_or_zero(bytes.len());
        bytes.remove(index);
        return;
    }
    let index = rand.below_or_zero(bytes.len());
    bytes[index] ^= 1 << rand.below_or_zero(8);
}

#[cfg(test)]
mod tests {
    use super::*;
    use libafl_bolts::rands::StdRand;

    #[test]
    fn duplicate_block_remaps_internal_input_to_copied_instance() {
        let step =
            |name: &str, inputs: BTreeMap<String, String>| crate::engine::input::StepInstance {
                name: name.into(),
                step: name.into(),
                action: "send".into(),
                condition: String::new(),
                message: None,
                channel: "control".into(),
                sender: Some("client".into()),
                receiver: Some("server".into()),
                party: None,
                fields: BTreeMap::new(),
                omitted_fields: vec![],
                inputs,
                broken_dependencies: vec![],
            };
        let original = GeneratorInput::with_trace(
            PathBuf::from("/tmp/generator.py"),
            vec![
                step("a.1", BTreeMap::new()),
                step(
                    "b",
                    BTreeMap::from([("state.key".into(), "a.1.state.key".into())]),
                ),
            ],
            BTreeMap::new(),
        );
        let duplicate = (0..128)
            .find_map(|seed| {
                let mut input = original.clone();
                duplicate_block(&mut StdRand::with_seed(seed), &mut input, 0);
                (input.trace.steps.len() == 4).then_some(input)
            })
            .expect("a two-step block should be selected");
        assert_eq!(
            duplicate.trace.steps[1].inputs["state.key"],
            "a.1.state.key"
        );
        assert_eq!(
            duplicate.trace.steps[3].inputs["state.key"],
            "a.1_copy1.state.key"
        );
    }

    #[test]
    fn step_trace_field_and_step_mutations_preserve_path() {
        let mut input = GeneratorInput::with_trace(
            PathBuf::from("/tmp/ftp_session.py"),
            vec![
                crate::engine::input::StepInstance {
                    name: "hello_1".into(),
                    step: "hello".into(),
                    action: "send".into(),
                    condition: String::new(),
                    message: Some("hello".into()),
                    channel: "control".into(),
                    sender: Some("client".into()),
                    receiver: Some("server".into()),
                    party: None,
                    fields: BTreeMap::from([("token".into(), ParamValue::Str("abc".into()))]),
                    omitted_fields: vec![],
                    inputs: BTreeMap::new(),
                    broken_dependencies: vec![],
                },
                crate::engine::input::StepInstance {
                    name: "bye_1".into(),
                    step: "bye".into(),
                    action: "send".into(),
                    condition: String::new(),
                    message: Some("bye".into()),
                    channel: "control".into(),
                    sender: Some("client".into()),
                    receiver: Some("server".into()),
                    party: None,
                    fields: BTreeMap::new(),
                    omitted_fields: vec![],
                    inputs: BTreeMap::new(),
                    broken_dependencies: vec![],
                },
            ],
            BTreeMap::new(),
        );
        let mut rand = StdRand::with_seed(11);
        assert_eq!(
            mutate_generator(&mut rand, &mut input),
            MutationResult::Mutated
        );
        assert_eq!(input.generator, PathBuf::from("/tmp/ftp_session.py"));
        assert!(!input.trace.steps.is_empty());

        let mut duplicate = GeneratorInput::with_trace(
            PathBuf::from("/tmp/ftp_session.py"),
            input.trace.steps.clone(),
            BTreeMap::new(),
        );
        let mut rand = StdRand::with_seed(3);
        duplicate_block(&mut rand, &mut duplicate, 0);
        duplicate.trace.validate().expect("duplicated trace");
        assert!(duplicate
            .trace
            .dependencies
            .iter()
            .any(|dependency| dependency.origin == "mutation"));
    }

    #[test]
    fn mutation_keeps_the_script_path() {
        let mut input = GeneratorInput::new(
            PathBuf::from("/tmp/ftp_session.py"),
            BTreeMap::from([
                ("user".to_string(), ParamValue::Str("ubuntu".to_string())),
                ("tries".to_string(), ParamValue::Int(1)),
            ]),
        );
        let path = input.generator.clone();
        let mut rand = StdRand::with_seed(7);
        let result = mutate_generator(&mut rand, &mut input);
        assert_eq!(result, MutationResult::Mutated);
        assert_eq!(input.generator, path);
    }

    use std::{collections::BTreeMap, path::PathBuf};
}
