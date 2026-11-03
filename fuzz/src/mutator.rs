//! Parameter-only mutations. The generator script path is left unchanged.

use std::borrow::Cow;

use libafl::{
    mutators::{MutationResult, Mutator},
    state::HasRand,
    Error,
};
use libafl_bolts::{rands::Rand, Named};

use crate::input::{GeneratorInput, ParamValue};

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
    S: HasRand,
    S::Rand: Rand,
{
    fn mutate(
        &mut self,
        state: &mut S,
        input: &mut GeneratorInput,
    ) -> Result<MutationResult, Error> {
        Ok(mutate_generator(state.rand_mut(), input))
    }

    fn post_exec(
        &mut self,
        _state: &mut S,
        _new_corpus_id: Option<libafl::corpus::CorpusId>,
    ) -> Result<(), Error> {
        Ok(())
    }
}

/// Stack 2..=64 parameter edits. An empty parameter map is skipped.
pub fn mutate_generator<R: Rand>(rand: &mut R, input: &mut GeneratorInput) -> MutationResult {
    if input.params.is_empty() {
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

fn mutate_value<R: Rand>(rand: &mut R, value: &mut ParamValue) -> MutationResult {
    match value {
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
