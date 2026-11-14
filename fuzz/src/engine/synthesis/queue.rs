//! FIFO of accepted synthesis inputs waiting for their first mutation round.
//!
//! The synthesis stage only enqueues here. The ordinary mutation stage takes
//! one input, evaluates it once through the normal feedback path, then applies
//! the normal generator mutations and evaluator. The baseline reaches the
//! corpus only when feedback keeps it, like any other executed input.

use std::{collections::VecDeque, fs, path::Path};

use libafl::Error;
use libafl_bolts::impl_serdeany;
use serde::{Deserialize, Serialize};

use super::TraceOrigin;
use crate::engine::input::GeneratorInput;

/// A published generator plus the initial parameters produced with it.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SynthesisCandidate {
    pub request: String,
    pub input: GeneratorInput,
    pub origin: TraceOrigin,
}

/// LibAFL metadata queue. Only pending inputs are stored here; a consumed
/// baseline is never reinserted.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct SynthesisCandidates {
    pub queue: VecDeque<SynthesisCandidate>,
}

impl_serdeany!(SynthesisCandidates);

impl SynthesisCandidates {
    pub fn enqueue(&mut self, request: String, input: GeneratorInput, origin: TraceOrigin) {
        self.queue.push_back(SynthesisCandidate {
            request,
            input,
            origin,
        });
    }

    pub fn take_next(&mut self) -> Option<SynthesisCandidate> {
        self.queue.pop_front()
    }

    pub fn is_empty(&self) -> bool {
        self.queue.is_empty()
    }

    /// Load a queue written by this version or by the earlier candidate-stage
    /// format. The older active entry is retained only if its baseline had not
    /// run yet; an already-baselined active entry cannot be replayed safely and
    /// is dropped. Old queued entries were never baselined and are preserved.
    pub fn load(path: &Path) -> Result<Self, Error> {
        let bytes = match fs::read(path) {
            Ok(bytes) => bytes,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                return Ok(Self::default())
            }
            Err(error) => {
                return Err(Error::unknown(format!(
                    "reading candidate queue {}: {error}",
                    path.display()
                )))
            }
        };
        let stored: StoredCandidates = serde_json::from_slice(&bytes).map_err(|error| {
            Error::illegal_state(format!(
                "reading candidate queue {}: {error}",
                path.display()
            ))
        })?;
        let mut queue = VecDeque::new();
        if let Some(active) = stored.active {
            if active.baseline_done {
                log::info!(
                    "dropping already-baselined legacy synthesis candidate {} from {}",
                    active.request,
                    path.display()
                );
            } else {
                queue.push_back(active.into_candidate());
            }
        }
        queue.extend(
            stored
                .queue
                .into_iter()
                .map(StoredCandidate::into_candidate),
        );
        Ok(Self { queue })
    }

    pub fn save(&self, path: &Path) -> Result<(), Error> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).map_err(|error| {
                Error::unknown(format!("creating candidate queue dir: {error}"))
            })?;
        }
        let temporary = path.with_extension("json.tmp");
        fs::write(
            &temporary,
            serde_json::to_vec_pretty(self).map_err(|error| {
                Error::serialize(format!("serializing candidate queue: {error}"))
            })?,
        )
        .map_err(|error| Error::unknown(format!("writing candidate queue: {error}")))?;
        fs::rename(&temporary, path)
            .map_err(|error| Error::unknown(format!("publishing candidate queue: {error}")))
    }
}

#[derive(Debug, Deserialize)]
struct StoredCandidates {
    #[serde(default)]
    queue: VecDeque<StoredCandidate>,
    #[serde(default)]
    active: Option<StoredCandidate>,
}

#[derive(Debug, Deserialize)]
struct StoredCandidate {
    request: String,
    input: GeneratorInput,
    origin: TraceOrigin,
    #[serde(default)]
    #[allow(dead_code)]
    baseline_done: bool,
}

impl StoredCandidate {
    fn into_candidate(self) -> SynthesisCandidate {
        SynthesisCandidate {
            request: self.request,
            input: self.input,
            origin: self.origin,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{collections::BTreeMap, path::PathBuf};

    fn candidate(name: &str) -> (String, GeneratorInput, TraceOrigin) {
        (
            name.to_string(),
            GeneratorInput::new(PathBuf::from(format!("/tmp/{name}.py")), BTreeMap::new()),
            TraceOrigin {
                request_id: name.into(),
                session: "s".into(),
                step_names: vec!["s".into()],
                added_steps: vec![],
            },
        )
    }

    #[test]
    fn queue_round_trips_and_is_fifo() {
        let dir = std::env::temp_dir().join(format!("mcn-queue-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("candidates.json");
        let mut queue = SynthesisCandidates::default();
        let (request, input, origin) = candidate("one");
        queue.enqueue(request, input, origin);
        let (request, input, origin) = candidate("two");
        queue.enqueue(request, input, origin);
        queue.save(&path).unwrap();
        let mut loaded = SynthesisCandidates::load(&path).unwrap();
        assert_eq!(loaded.take_next().unwrap().request, "one");
        assert_eq!(loaded.take_next().unwrap().request, "two");
        assert!(loaded.is_empty());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn legacy_candidate_file_drops_baselined_active_and_keeps_pending() {
        let dir = std::env::temp_dir().join(format!("mcn-queue-legacy-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("candidates.json");
        let (request, input, origin) = candidate("active");
        let (pending_request, pending_input, pending_origin) = candidate("pending");
        let value = serde_json::json!({
            "queue": [{
                "request": pending_request,
                "input": pending_input,
                "origin": pending_origin,
                "baseline_done": false,
                "rounds": 0,
                "novel_children": 0,
            }],
            "active": {
                "request": request,
                "input": input,
                "origin": origin,
                "baseline_done": true,
                "rounds": 2,
                "novel_children": 0,
            },
            "retired": 3,
        });
        fs::write(&path, serde_json::to_vec(&value).unwrap()).unwrap();
        let mut loaded = SynthesisCandidates::load(&path).unwrap();
        assert_eq!(loaded.take_next().unwrap().request, "pending");
        assert!(loaded.is_empty());
        let _ = fs::remove_dir_all(&dir);
    }
}
