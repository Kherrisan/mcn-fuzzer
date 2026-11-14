//! The ordinary generator mutation stage, with a small synthesis hand-off queue.
//!
//! When the synthesis FIFO has an item, this stage consumes it before asking
//! the scheduler for a corpus testcase. It evaluates the initial parameters
//! once through the normal feedback path, then runs the same mutator loop as
//! ordinary corpus inputs. The baseline is kept only when feedback decides it
//! is interesting, exactly like any other executed input.

use std::borrow::Cow;
use std::path::PathBuf;

use libafl::{
    corpus::{Corpus, CorpusId},
    executors::Executor,
    fuzzer::Evaluator,
    mutators::{MutationResult, Mutator},
    stages::{Restartable, Stage},
    state::{HasCorpus, HasCurrentTestcase, HasRand},
    Error, HasMetadata,
};
use libafl_bolts::{rands::Rand, Named};

use crate::engine::{
    input::GeneratorInput,
    mutator::GeneratorMutator,
    stagnation::BitmapProgress,
    synthesis::{SynthesisCandidate, SynthesisCandidates, TraceOrigin},
};

/// Mutation iterations for both candidate baselines and ordinary corpus input.
const MAX_MUTATIONS: usize = 128;

pub struct GeneratorMutationStage {
    mutator: GeneratorMutator,
    queue_path: Option<PathBuf>,
    name: Cow<'static, str>,
}

impl GeneratorMutationStage {
    pub fn new() -> Self {
        Self {
            mutator: GeneratorMutator::new(),
            queue_path: None,
            name: Cow::Borrowed("generator-mutation"),
        }
    }

    pub fn with_queue(path: PathBuf) -> Self {
        Self {
            queue_path: Some(path),
            ..Self::new()
        }
    }

    /// Restore pending synthesis inputs from the persistent FIFO.
    pub fn load_queue<S: HasMetadata>(&self, state: &mut S) -> Result<(), Error> {
        let Some(path) = &self.queue_path else {
            return Ok(());
        };
        let candidates = SynthesisCandidates::load(path)?;
        if !candidates.is_empty() {
            state.add_metadata(candidates);
        }
        Ok(())
    }

    /// Hand off a published synthesis input to the mutation stage.
    pub fn enqueue<S: HasMetadata>(
        &self,
        state: &mut S,
        request: String,
        input: GeneratorInput,
        origin: TraceOrigin,
    ) -> Result<(), Error> {
        {
            state
                .metadata_or_insert_with(SynthesisCandidates::default)
                .enqueue(request, input, origin);
        }
        self.persist(state);
        Ok(())
    }

    pub fn has_queue<S: HasMetadata>(&self, state: &S) -> bool {
        state
            .metadata::<SynthesisCandidates>()
            .map(|candidates| !candidates.is_empty())
            .unwrap_or(false)
    }

    fn take_candidate<S: HasMetadata>(&self, state: &mut S) -> Option<SynthesisCandidate> {
        let candidate = state
            .metadata_or_insert_with(SynthesisCandidates::default)
            .take_next();
        if candidate.is_some() {
            // The hand-off is at-most-once: once an input is consumed it is no
            // longer in candidates.json, so a restart cannot run its baseline
            // twice.
            self.persist(state);
        }
        candidate
    }

    fn persist<S: HasMetadata>(&self, state: &S) {
        let Some(path) = &self.queue_path else {
            return;
        };
        if let Ok(candidates) = state.metadata::<SynthesisCandidates>() {
            if let Err(error) = candidates.save(path) {
                log::warn!("persisting synthesis candidate queue failed: {error:?}");
            }
        }
    }

    fn perform_candidate<E, EM, S, Z>(
        &mut self,
        fuzzer: &mut Z,
        executor: &mut E,
        state: &mut S,
        manager: &mut EM,
        candidate: SynthesisCandidate,
    ) -> Result<(), Error>
    where
        S: HasRand + HasCorpus<GeneratorInput> + HasMetadata,
        S::Rand: Rand,
        E: Executor<EM, GeneratorInput, S, Z>,
        Z: Evaluator<E, EM, GeneratorInput, S>,
    {
        state
            .metadata_or_insert_with(BitmapProgress::default)
            .set_mutation_pending(false);
        let (_, baseline_id) =
            match fuzzer.evaluate_filtered(state, executor, manager, &candidate.input) {
                Ok(result) => result,
                Err(error) => {
                    log::warn!(
                        "synthesis input {} baseline failed: {error:?}; dropping it",
                        candidate.request
                    );
                    return Ok(());
                }
            };
        log::info!(
            "synthesis input {} baseline evaluated once (kept in corpus: {})",
            candidate.request,
            baseline_id.is_some()
        );
        if let Some(id) = baseline_id {
            if let Err(error) = attach_origin(state.corpus_mut(), id, &candidate.origin) {
                log::warn!("could not attach TraceOrigin to baseline corpus {id:?}: {error:?}");
            }
        }
        self.run_mutations(
            fuzzer,
            executor,
            state,
            manager,
            &candidate.input,
            Some(&candidate.origin),
        )
    }

    fn run_mutations<E, EM, S, Z>(
        &mut self,
        fuzzer: &mut Z,
        executor: &mut E,
        state: &mut S,
        manager: &mut EM,
        input: &GeneratorInput,
        origin: Option<&TraceOrigin>,
    ) -> Result<(), Error>
    where
        S: HasRand + HasCorpus<GeneratorInput> + HasMetadata,
        S::Rand: Rand,
        E: Executor<EM, GeneratorInput, S, Z>,
        Z: Evaluator<E, EM, GeneratorInput, S>,
    {
        let iterations = 1 + state.rand_mut().below_or_zero(MAX_MUTATIONS);
        for _ in 0..iterations {
            let mut mutated = input.clone();
            if self.mutator.mutate(state, &mut mutated)? == MutationResult::Skipped
                || mutated == *input
            {
                // An empty/boundary parameter can report `Mutated` without a
                // real change. Never evaluate the untouched baseline as if it
                // were a mutation.
                continue;
            }
            let (_, corpus_id) = fuzzer.evaluate_filtered(state, executor, manager, &mutated)?;
            self.mutator.post_exec(state, corpus_id)?;
            if let (Some(id), Some(origin)) = (corpus_id, origin) {
                if let Err(error) = attach_origin(state.corpus_mut(), id, origin) {
                    log::warn!("could not attach TraceOrigin to corpus {id:?}: {error:?}");
                }
            }
        }
        Ok(())
    }
}

impl Default for GeneratorMutationStage {
    fn default() -> Self {
        Self::new()
    }
}

impl Named for GeneratorMutationStage {
    fn name(&self) -> &Cow<'static, str> {
        &self.name
    }
}

impl<S> Restartable<S> for GeneratorMutationStage {
    fn should_restart(&mut self, _state: &mut S) -> Result<bool, Error> {
        Ok(true)
    }

    fn clear_progress(&mut self, _state: &mut S) -> Result<(), Error> {
        Ok(())
    }
}

impl<E, EM, S, Z> Stage<E, EM, S, Z> for GeneratorMutationStage
where
    S: HasRand + HasCorpus<GeneratorInput> + HasMetadata + HasCurrentTestcase<GeneratorInput>,
    S::Rand: Rand,
    E: Executor<EM, GeneratorInput, S, Z>,
    Z: Evaluator<E, EM, GeneratorInput, S>,
{
    fn perform(
        &mut self,
        fuzzer: &mut Z,
        executor: &mut E,
        state: &mut S,
        manager: &mut EM,
    ) -> Result<(), Error> {
        if let Some(candidate) = self.take_candidate(state) {
            return self.perform_candidate(fuzzer, executor, state, manager, candidate);
        }
        if state.corpus().count() == 0 {
            return Ok(());
        }
        let input = state.current_input_cloned()?;
        let origin = state
            .current_testcase()
            .ok()
            .and_then(|testcase| testcase.metadata::<TraceOrigin>().ok().cloned());
        self.run_mutations(fuzzer, executor, state, manager, &input, origin.as_ref())
    }
}

fn attach_origin<C: Corpus<GeneratorInput>>(
    corpus: &mut C,
    id: CorpusId,
    origin: &TraceOrigin,
) -> Result<(), Error> {
    // OnDiskCorpus writes the testcase during `add`, before this metadata is
    // known. Load the input back, add the origin, and replace it to persist.
    let mut testcase = corpus.get(id)?.borrow().clone();
    testcase.load_input(corpus)?;
    testcase.add_metadata(origin.clone());
    corpus.replace(id, testcase)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use libafl::corpus::{OnDiskCorpus, Testcase};
    use std::{collections::BTreeMap, fs, path::PathBuf};

    #[test]
    fn trace_origin_survives_ondisk_corpus_replace() {
        let dir = std::env::temp_dir().join(format!("mcn-origin-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let input = GeneratorInput::new(PathBuf::from("/tmp/generator.py"), BTreeMap::new());
        let origin = TraceOrigin {
            request_id: "r".into(),
            session: "s".into(),
            step_names: vec!["login_first".into()],
            added_steps: vec![],
        };
        let mut corpus: OnDiskCorpus<GeneratorInput> = OnDiskCorpus::new(&dir).unwrap();
        let id = corpus.add(Testcase::new(input)).unwrap();
        attach_origin(&mut corpus, id, &origin).unwrap();
        drop(corpus);
        let metadata_path = fs::read_dir(&dir)
            .unwrap()
            .flatten()
            .map(|entry| entry.path())
            .find(|path| path.to_string_lossy().ends_with(".metadata"))
            .expect("metadata sidecar");
        let text = fs::read_to_string(metadata_path).unwrap();
        assert!(text.contains("\"request_id\": \"r\""), "{text}");
        let _ = fs::remove_dir_all(&dir);
    }
}
