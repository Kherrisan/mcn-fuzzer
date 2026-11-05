//! Eight-character SHA-256 identifiers. Field order is part of the domain.

use serde_json::Value;
use sha2::{Digest, Sha256};

use super::model::StepRecord;

const ID_LEN: usize = 8;

/// Stable identity of one step definition: the first eight digest hex characters.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct StepId(pub String);

/// Stable identity of an ordered step sequence, including repeats.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct TraceId(pub String);

impl StepRecord {
    /// Stable identity of this definition, excluding `source_id` and model position.
    pub fn id(&self) -> StepId {
        StepId(self.digest()[..ID_LEN].to_string())
    }

    fn digest(&self) -> String {
        let mut bytes = Vec::new();
        push_str(&mut bytes, "mcn.step.v1");
        for field in [
            &self.name,
            &self.message_name,
            &self.channel_name,
            &self.sender_name,
            &self.receiver_name,
            &self.condition,
        ] {
            push_str(&mut bytes, field);
        }
        sha256_hex(&bytes)
    }
}

impl TraceId {
    /// Hash ordered full step digests before shortening. Order and repeats matter.
    pub fn from_steps(steps: &[StepRecord]) -> Self {
        let mut bytes = Vec::new();
        push_str(&mut bytes, "mcn.trace.v1");
        bytes.extend((steps.len() as u64).to_be_bytes());
        for step in steps {
            push_str(&mut bytes, &step.digest());
        }
        Self(sha256_hex(&bytes)[..ID_LEN].to_string())
    }
}

/// Hash the parsed model. Object keys are sorted; array order is preserved.
pub fn protocol_id(snapshot: &Value) -> String {
    let mut bytes = Vec::new();
    push_str(&mut bytes, "mcn.protocol.v1");
    canonical(snapshot, &mut bytes);
    sha256_hex(&bytes)[..ID_LEN].to_string()
}

/// Lower-case hex of the SHA-256 digest. Encoding of the input stays with the caller.
fn sha256_hex(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn push_str(out: &mut Vec<u8>, text: &str) {
    let raw = text.as_bytes();
    out.extend((raw.len() as u64).to_be_bytes());
    out.extend(raw);
}

fn canonical(value: &Value, out: &mut Vec<u8>) {
    match value {
        Value::Null => out.push(b'n'),
        Value::Bool(true) => out.push(b't'),
        Value::Bool(false) => out.push(b'f'),
        Value::Number(number) => push_str(out, &number.to_string()),
        Value::String(text) => push_str(out, text),
        Value::Array(items) => {
            out.push(b'[');
            out.extend((items.len() as u64).to_be_bytes());
            for item in items {
                canonical(item, out);
            }
        }
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            out.push(b'{');
            out.extend((keys.len() as u64).to_be_bytes());
            for key in keys {
                push_str(out, key);
                canonical(&map[key], out);
            }
        }
    }
}
