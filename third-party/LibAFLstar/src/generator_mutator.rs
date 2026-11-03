//! Mutates values inside a [`crate::input::GeneratorInput`] and leaves the
//! script path alone. One call stacks the same 2..=64 steps as havoc with
//! `max_stack_pow = 6`.

use libafl::mutators::MutationResult;
use libafl_bolts::rands::Rand;

use crate::input::{GeneratorInput, ParamValue};

const INTERESTING: &[i64] = &[
    -128, -1, 0, 1, 16, 32, 64, 100, 127, 128, 255, 256, 512, 1000, 1024, 4096, 32767, 65535,
    65536,
];

/// Stack parameter mutations on `input`.
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
    input.refresh_wire();
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
                let interesting = INTERESTING[rand.below_or_zero(INTERESTING.len())];
                *number = interesting;
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
        text.replace_range(
            text.char_indices().nth(index).map(|(i, _)| i).unwrap_or(0)
                ..text
                    .char_indices()
                    .nth(index + 1)
                    .map(|(i, _)| i)
                    .unwrap_or(text.len()),
            "",
        );
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
