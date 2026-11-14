//! Field and step mutations over a concrete step trace.
//!
//! Mutable values live in `GeneratorInput::params`, keyed by step instance
//! name. Field mutations edit one instance; step mutations copy or delete that
//! instance's params entry along with the step.

use std::{borrow::Cow, collections::BTreeSet};

use libafl::{
    mutators::{MutationResult, Mutator},
    state::HasRand,
    Error, HasMetadata,
};
use libafl_bolts::{rands::Rand, Named};

use crate::{
    engine::input::{BytesEncoding, FieldKind, FieldSpec, GeneratorInput, ParamValue},
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

/// Stack 2..=64 field/step edits. Traces take precedence over params.
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
    if input.trace.steps.is_empty() {
        return MutationResult::Skipped;
    }
    if rand.below_or_zero(100) < 70 && mutate_field(rand, input) == MutationResult::Mutated {
        return MutationResult::Mutated;
    }
    mutate_step(rand, input)
}

fn mutate_field<R: Rand>(rand: &mut R, input: &mut GeneratorInput) -> MutationResult {
    let candidates = input
        .params
        .iter()
        .filter(|(_, fields)| !fields.is_empty())
        .map(|(instance, _)| instance.clone())
        .collect::<Vec<_>>();
    if candidates.is_empty() {
        return MutationResult::Skipped;
    }
    let instance = candidates[rand.below_or_zero(candidates.len())].clone();
    let Some(fields) = input.params.get(&instance) else {
        return MutationResult::Skipped;
    };
    let keys = fields.keys().cloned().collect::<Vec<_>>();
    let field = keys[rand.below_or_zero(keys.len())].clone();
    if rand.below_or_zero(5) == 0 {
        if let Some(fields) = input.params.get_mut(&instance) {
            fields.remove(&field);
            if fields.is_empty() {
                input.params.remove(&instance);
                input.param_kinds.remove(&instance);
            }
        }
        if let Some(kinds) = input.param_kinds.get_mut(&instance) {
            kinds.remove(&field);
            if kinds.is_empty() {
                input.param_kinds.remove(&instance);
            }
        }
        if let Some(step) = input
            .trace
            .steps
            .iter_mut()
            .find(|step| step.name == instance)
        {
            if !step.omitted_fields.iter().any(|item| item == &field) {
                step.omitted_fields.push(field);
            }
        }
        return MutationResult::Mutated;
    }
    let spec = input
        .param_kinds
        .get(&instance)
        .and_then(|fields| fields.get(&field))
        .copied();
    let Some(value) = input
        .params
        .get_mut(&instance)
        .and_then(|fields| fields.get_mut(&field))
    else {
        return MutationResult::Skipped;
    };
    match spec {
        Some(spec) => mutate_typed_value(rand, value, spec),
        None => mutate_value(rand, value),
    }
}

const HEX_DIGITS: &[u8] = b"0123456789abcdef";
const BASE64_DIGITS: &[u8] =
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

fn mutate_typed_value<R: Rand>(
    rand: &mut R,
    value: &mut ParamValue,
    spec: FieldSpec,
) -> MutationResult {
    match (spec.kind, &mut *value) {
        (FieldKind::Bytes, ParamValue::Str(text)) => {
            let encoding = spec.bytes_encoding();
            if mutate_encoded_bytes(rand, text, encoding) {
                return MutationResult::Mutated;
            }
            mutate_text(rand, text);
            MutationResult::Mutated
        }
        (FieldKind::Bytes, ParamValue::List(items)) => {
            if items.is_empty() {
                items.push(ParamValue::Int(rand.below_or_zero(256) as i64));
            } else {
                let index = rand.below_or_zero(items.len());
                items[index] = ParamValue::Int(rand.below_or_zero(256) as i64);
            }
            MutationResult::Mutated
        }
        (FieldKind::Integer, ParamValue::Int(number)) => {
            mutate_integer(rand, number);
            MutationResult::Mutated
        }
        (FieldKind::Boolean, ParamValue::Bool(flag)) => {
            *flag = !*flag;
            MutationResult::Mutated
        }
        (FieldKind::List, ParamValue::List(items)) => {
            if items.is_empty() {
                items.push(ParamValue::Int(rand.below_or_zero(256) as i64));
                return MutationResult::Mutated;
            }
            let index = rand.below_or_zero(items.len());
            mutate_value(rand, &mut items[index])
        }
        (FieldKind::String, ParamValue::Str(text)) => {
            mutate_text(rand, text);
            MutationResult::Mutated
        }
        _ => mutate_value(rand, value),
    }
}

fn mutate_encoded_bytes<R: Rand>(rand: &mut R, text: &mut String, encoding: BytesEncoding) -> bool {
    let digits = match encoding {
        BytesEncoding::Hex => HEX_DIGITS,
        BytesEncoding::Base64 => BASE64_DIGITS,
    };
    let bytes = text.as_bytes();
    if bytes.is_empty() {
        return false;
    }
    let valid = bytes.iter().all(|byte| {
        digits.contains(byte) || (encoding == BytesEncoding::Base64 && *byte == b'=')
    });
    if !valid {
        return false;
    }
    let mutable: Vec<usize> = bytes
        .iter()
        .enumerate()
        .filter(|(_, byte)| digits.contains(byte))
        .map(|(index, _)| index)
        .collect();
    if mutable.is_empty() {
        return false;
    }
    let index = mutable[rand.below_or_zero(mutable.len())];
    let replacement = digits[rand.below_or_zero(digits.len())] as char;
    text.replace_range(index..index + 1, &replacement.to_string());
    true
}

fn mutate_integer<R: Rand>(rand: &mut R, number: &mut i64) {
    if rand.below_or_zero(2) == 0 {
        *number = INTERESTING[rand.below_or_zero(INTERESTING.len())];
    } else {
        let delta = rand.below_or_zero(33) as i64 - 16;
        *number = number.saturating_add(if delta == 0 { 1 } else { delta });
    }
}

fn mutate_step<R: Rand>(rand: &mut R, input: &mut GeneratorInput) -> MutationResult {
    let len = input.trace.steps.len();
    if len == 0 {
        return MutationResult::Skipped;
    }
    if len >= 4096 {
        return MutationResult::Skipped;
    }
    let original_trace = input.trace.clone();
    let original_params = input.params.clone();
    let original_kinds = input.param_kinds.clone();
    let choice = rand.below_or_zero(100);
    let index = rand.below_or_zero(len);
    let operation = if choice < 55 || len == 1 {
        duplicate_block(rand, input, index)
    } else if choice < 78 && len > 1 {
        let removed = input.trace.steps.remove(index);
        input.params.remove(&removed.name);
        input.param_kinds.remove(&removed.name);
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
        if let Err(_error) = input.validate() {
            input.trace = original_trace;
            input.params = original_params;
            input.param_kinds = original_kinds;
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
    for (old, new) in &names {
        if let Some(fields) = input.params.get(old).cloned() {
            input.params.insert(new.clone(), fields);
        }
        if let Some(kinds) = input.param_kinds.get(old).cloned() {
            input.param_kinds.insert(new.clone(), kinds);
        }
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
            mutate_integer(rand, number);
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
            BTreeMap::from([(
                "a.1".into(),
                BTreeMap::from([("token".into(), ParamValue::Str("abc".into()))]),
            )]),
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
        assert_eq!(
            duplicate
                .params
                .get("a.1_copy1")
                .and_then(|fields| fields.get("token")),
            Some(&ParamValue::Str("abc".into()))
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
                    omitted_fields: vec![],
                    inputs: BTreeMap::new(),
                    broken_dependencies: vec![],
                },
            ],
            BTreeMap::from([(
                "hello_1".into(),
                BTreeMap::from([("token".into(), ParamValue::Str("abc".into()))]),
            )]),
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
            input.params.clone(),
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
        let mut input = GeneratorInput::with_trace(
            PathBuf::from("/tmp/ftp_session.py"),
            vec![crate::engine::input::StepInstance {
                name: "session_1".into(),
                step: "ftp_session".into(),
                action: "send".into(),
                condition: String::new(),
                message: None,
                channel: "control".into(),
                sender: None,
                receiver: None,
                party: None,
                omitted_fields: vec![],
                inputs: BTreeMap::new(),
                broken_dependencies: vec![],
            }],
            BTreeMap::from([(
                "session_1".into(),
                BTreeMap::from([
                    ("user".to_string(), ParamValue::Str("ubuntu".to_string())),
                    ("tries".to_string(), ParamValue::Int(1)),
                ]),
            )]),
        );
        let path = input.generator.clone();
        let mut rand = StdRand::with_seed(7);
        let result = mutate_generator(&mut rand, &mut input);
        assert_eq!(result, MutationResult::Mutated);
        assert_eq!(input.generator, path);
    }

    #[test]
    fn same_field_name_is_independent_per_instance() {
        let step = |name: &str| crate::engine::input::StepInstance {
            name: name.into(),
            step: name.into(),
            action: "send".into(),
            condition: String::new(),
            message: None,
            channel: "control".into(),
            sender: None,
            receiver: None,
            party: None,
            omitted_fields: vec![],
            inputs: BTreeMap::new(),
            broken_dependencies: vec![],
        };
        let mut input = GeneratorInput::with_trace(
            PathBuf::from("/tmp/generator.py"),
            vec![step("first"), step("second")],
            BTreeMap::from([
                (
                    "first".into(),
                    BTreeMap::from([("body".into(), ParamValue::Str("aa".into()))]),
                ),
                (
                    "second".into(),
                    BTreeMap::from([("body".into(), ParamValue::Str("bb".into()))]),
                ),
            ]),
        );
        let mut rand = StdRand::with_seed(5);
        assert_eq!(
            mutate_field(&mut rand, &mut input),
            MutationResult::Mutated
        );
        let first = match input.params["first"]["body"] {
            ParamValue::Str(ref value) => value.clone(),
            _ => unreachable!(),
        };
        let second = match input.params["second"]["body"] {
            ParamValue::Str(ref value) => value.clone(),
            _ => unreachable!(),
        };
        assert_eq!((first == "aa") as u8 + (second == "bb") as u8, 1);
        input.validate().expect("independent instance params stay valid");
    }

    #[test]
    fn bytes_kind_mutates_without_breaking_encoding() {
        let make = |encoding: BytesEncoding, value: &str| {
            let mut input = GeneratorInput::with_trace(
                PathBuf::from("/tmp/generator.py"),
                vec![crate::engine::input::StepInstance {
                    name: "one".into(),
                    step: "send".into(),
                    action: "send".into(),
                    condition: String::new(),
                    message: None,
                    channel: "control".into(),
                    sender: None,
                    receiver: None,
                    party: None,
                    omitted_fields: vec![],
                    inputs: BTreeMap::new(),
                    broken_dependencies: vec![],
                }],
                BTreeMap::from([(
                    "one".into(),
                    BTreeMap::from([("blob".into(), ParamValue::Str(value.into()))]),
                )]),
            );
            input.param_kinds.insert(
                "one".into(),
                BTreeMap::from([(
                    "blob".into(),
                    FieldSpec {
                        kind: FieldKind::Bytes,
                        encoding: Some(encoding),
                    },
                )]),
            );
            input
        };
        for seed in 0..64 {
            let mut hex = make(BytesEncoding::Hex, "1301");
            assert_eq!(
                mutate_field(&mut StdRand::with_seed(seed), &mut hex),
                MutationResult::Mutated
            );
            if let Some(value) = hex.params.get("one").and_then(|fields| fields.get("blob")) {
                let ParamValue::Str(text) = value else {
                    panic!("hex bytes must stay a string");
                };
                assert_eq!(text.len() % 2, 0, "{text}");
                assert!(text.chars().all(|ch| ch.is_ascii_hexdigit()), "{text}");
            } else {
                assert!(hex.trace.steps[0].omitted_fields.iter().any(|f| f == "blob"));
            }

            let mut base64 = make(BytesEncoding::Base64, "EwE=");
            assert_eq!(
                mutate_field(&mut StdRand::with_seed(seed), &mut base64),
                MutationResult::Mutated
            );
            if let Some(value) = base64.params.get("one").and_then(|fields| fields.get("blob")) {
                let ParamValue::Str(text) = value else {
                    panic!("base64 bytes must stay a string");
                };
                assert_eq!(text.len() % 4, 0, "{text}");
                assert_eq!(text.matches('=').count(), 1, "{text}");
                assert!(text
                    .trim_end_matches('=')
                    .chars()
                    .all(|ch| ch.is_ascii_alphanumeric() || ch == '+' || ch == '/'), "{text}");
            } else {
                assert!(base64.trace.steps[0].omitted_fields.iter().any(|f| f == "blob"));
            }
        }
    }

    use std::{collections::BTreeMap, path::PathBuf};
}
