//! The ordinary generator mutation stage, with a small synthesis hand-off queue.
//!
//! When the synthesis FIFO has an item, this stage consumes it before asking
//! the scheduler for a corpus testcase. It evaluates the initial parameters
//! once through the normal feedback path, then runs the same mutator loop as
//! ordinary corpus inputs. The baseline is kept only when feedback decides it
//! is interesting, exactly like any other executed input.

use std::{borrow::Cow, path::PathBuf};

use libafl::{
    corpus::{Corpus, CorpusId, HasCurrentCorpusId},
    executors::Executor,
    feedbacks::MapNoveltiesMetadata,
    fuzzer::Evaluator,
    mutators::{MutationResult, Mutator},
    stages::{Restartable, Stage},
    state::{HasCorpus, HasCurrentTestcase, HasRand},
    Error, HasMetadata,
};
use libafl_bolts::{rands::Rand, Named};
use serde_json::{json, Value};

use crate::runtime::report::append_event;
use crate::{
    coverage::introspection::{self, IntrospectionHandle},
    engine::{
        input::GeneratorInput,
        mutator::GeneratorMutator,
        stagnation::BitmapProgress,
        synthesis::{SynthesisCandidate, SynthesisCandidates, TraceOrigin},
    },
};

/// Mutation iterations for ordinary corpus input.
const MAX_MUTATIONS: usize = 128;

/// Candidate evaluations granted per declared mutable field.
const CANDIDATE_FIELD_BUDGET: usize = 64;
/// Lower bound for one synthesis candidate's evaluation budget.
const CANDIDATE_MIN_MUTATIONS: usize = 512;
/// Upper bound for one synthesis candidate's evaluation budget.
const CANDIDATE_MAX_MUTATIONS: usize = 2048;

/// Evaluation budget for one synthesis candidate: `clamp(64 * fields, 512, 2048)`.
///
/// A candidate is the only source of structurally new traces, so it gets a
/// budget that scales with how many values the generator exposes instead of the
/// ordinary corpus schedule.
/// Split the first mutation budget between analyzed and synthesis-discovered
/// fields.
///
/// When both groups have fields the budget halves, with the odd evaluation
/// going to the analyzed group. A generator that declares only one group keeps
/// the whole budget for it.
fn budget_split(input: &GeneratorInput) -> (usize, usize) {
    let total = candidate_budget(input);
    let mut analyzed = 0usize;
    let mut adhoc = 0usize;
    for (instance, fields) in &input.params {
        for field in fields.keys() {
            let step = input
                .trace
                .steps
                .iter()
                .find(|step| &step.name == instance)
                .map(|step| step.step.as_str())
                .unwrap_or("");
            let origin = input
                .field_origin
                .get(step)
                .and_then(|row| row.get(field))
                .copied()
                .unwrap_or(crate::engine::input::FieldOrigin::Analyzed);
            match origin {
                crate::engine::input::FieldOrigin::Analyzed => analyzed += 1,
                crate::engine::input::FieldOrigin::Adhoc => adhoc += 1,
            }
        }
    }
    match (analyzed > 0, adhoc > 0) {
        (true, true) => {
            let first = total.div_ceil(2);
            (first, total - first)
        }
        (true, false) => (total, 0),
        (false, true) => (0, total),
        (false, false) => (total, 0),
    }
}

fn candidate_budget(input: &GeneratorInput) -> usize {
    let fields = input
        .params
        .values()
        .map(std::collections::BTreeMap::len)
        .sum::<usize>();
    fields
        .saturating_mul(CANDIDATE_FIELD_BUDGET)
        .clamp(CANDIDATE_MIN_MUTATIONS, CANDIDATE_MAX_MUTATIONS)
}

pub struct GeneratorMutationStage {
    mutator: GeneratorMutator,
    queue_path: Option<PathBuf>,
    event_path: Option<PathBuf>,
    introspection: Option<IntrospectionHandle>,
    name: Cow<'static, str>,
}

impl GeneratorMutationStage {
    pub fn new() -> Self {
        Self {
            mutator: GeneratorMutator::new(),
            queue_path: None,
            event_path: None,
            introspection: None,
            name: Cow::Borrowed("generator-mutation"),
        }
    }

    pub(crate) fn set_introspection(&mut self, handle: IntrospectionHandle) {
        self.introspection = Some(handle);
    }

    pub fn with_queue(path: PathBuf) -> Self {
        Self {
            queue_path: Some(path),
            ..Self::new()
        }
    }

    /// Append one JSONL event per selected corpus case or synthesis candidate.
    pub fn with_events(mut self, path: PathBuf) -> Self {
        self.event_path = Some(path);
        self
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

    fn log_event(&self, kind: &str, body: Value) {
        let Some(path) = &self.event_path else {
            return;
        };
        if let Err(error) = append_event(path, kind, body) {
            log::warn!("mutation event log failed: {error}");
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
            self.note_kept(
                state,
                Some(&candidate.origin),
                &candidate.input.generator,
                id,
            );
        }
        let budget = candidate_budget(&candidate.input);
        log::info!(
            "synthesis input {} gets {budget} mutation evaluations",
            candidate.request
        );
        let (analyzed_budget, adhoc_budget) = budget_split(&candidate.input);
        log::info!(
            "synthesis input {} schedules {analyzed_budget} analyzed and {adhoc_budget} adhoc field evaluations",
            candidate.request
        );
        self.run_mutations(
            fuzzer,
            executor,
            state,
            manager,
            &candidate.input,
            Some(&candidate.origin),
            baseline_id,
            budget,
            Some((analyzed_budget, adhoc_budget)),
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
        parent_id: Option<CorpusId>,
        budget: usize,
        split: Option<(usize, usize)>,
    ) -> Result<(), Error>
    where
        S: HasRand + HasCorpus<GeneratorInput> + HasMetadata,
        S::Rand: Rand,
        E: Executor<EM, GeneratorInput, S, Z>,
        Z: Evaluator<E, EM, GeneratorInput, S>,
    {
        // Skipped or unchanged mutations do not consume the evaluation budget.
        // The attempt cap only guards against a mutator that never changes input.
        let attempt_limit = budget.saturating_mul(4);
        let mut attempts = 0usize;
        let mut evaluations = 0usize;
        while evaluations < budget && attempts < attempt_limit {
            attempts += 1;
            // New generators walk the analyzed group first, then the adhoc
            // group; ordinary corpus inputs may use both from the start.
            self.mutator.set_field_pool(match split {
                Some((analyzed, _adhoc)) if analyzed > 0 && evaluations < analyzed => {
                    crate::engine::mutator::FieldPool::Analyzed
                }
                Some((analyzed, adhoc)) if adhoc > 0 && evaluations >= analyzed => {
                    crate::engine::mutator::FieldPool::Adhoc
                }
                _ => crate::engine::mutator::FieldPool::Any,
            });
            let mut mutated = input.clone();
            if self.mutator.mutate(state, &mut mutated)? == MutationResult::Skipped
                || mutated == *input
            {
                // An empty/boundary parameter can report `Mutated` without a
                // real change. Never evaluate the untouched baseline as if it
                // were a mutation.
                self.note_mutation(origin, &input.generator, false);
                continue;
            }
            self.note_mutation(origin, &mutated.generator, true);
            evaluations += 1;
            let (_, corpus_id) = fuzzer.evaluate_filtered(state, executor, manager, &mutated)?;
            self.mutator.post_exec(state, corpus_id)?;
            // Solutions and filtered inputs return no corpus id. Only a new
            // corpus entry contributes. Replay never reaches this stage.
            if let Some(id) = corpus_id {
                self.log_event(
                    "mutation_corpus_added",
                    json!({
                        "parent_case_id": parent_id.map(|parent| format!("corpus:{}", parent.0)),
                        "candidate_request": if parent_id.is_none() {
                            origin.map(|item| item.request_id.as_str())
                        } else {
                            None
                        },
                        "case_id": format!("corpus:{}", id.0),
                        "operations": self.mutator.operations(),
                    }),
                );
                if let Some(origin) = origin {
                    if let Err(error) = attach_origin(state.corpus_mut(), id, origin) {
                        log::warn!("could not attach TraceOrigin to corpus {id:?}: {error:?}");
                    }
                }
                self.note_kept(state, origin, &mutated.generator, id);
            }
        }
        Ok(())
    }

    fn note_mutation(
        &self,
        origin: Option<&TraceOrigin>,
        generator: &std::path::Path,
        changed: bool,
    ) {
        let Some(handle) = &self.introspection else {
            return;
        };
        if let Err(error) = introspection::note_mutation(
            handle,
            origin.map(|item| item.request_id.as_str()),
            generator,
            changed,
        ) {
            log::error!("introspection mutation attribution: {error}");
        }
    }

    fn note_kept<S: HasCorpus<GeneratorInput>>(
        &self,
        state: &S,
        origin: Option<&TraceOrigin>,
        generator: &std::path::Path,
        id: CorpusId,
    ) {
        let Some(handle) = &self.introspection else {
            return;
        };
        let novelties = match novelty_indexes(state.corpus(), id) {
            Ok(Some(list)) => Some(list),
            Ok(None) => {
                log::warn!(
                    "corpus {id:?} has no MapNoveltiesMetadata; keeping the corpus count without novelty indexes"
                );
                None
            }
            Err(error) => {
                log::warn!("could not read corpus {id:?} for introspection: {error:?}");
                None
            }
        };
        if let Err(error) = introspection::note_evaluated_corpus(
            handle,
            origin.map(|item| item.request_id.as_str()),
            generator,
            Some(id.0 as u64),
            novelties.as_deref(),
        ) {
            log::error!("introspection corpus attribution: {error}");
        }
    }
}

fn novelty_indexes<C: Corpus<GeneratorInput>>(
    corpus: &C,
    id: CorpusId,
) -> Result<Option<Vec<usize>>, Error> {
    let testcase = corpus.get(id)?;
    let borrowed = testcase.borrow();
    Ok(borrowed
        .metadata::<MapNoveltiesMetadata>()
        .ok()
        .map(|meta| meta.list.clone()))
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
    S: HasRand
        + HasCorpus<GeneratorInput>
        + HasMetadata
        + HasCurrentTestcase<GeneratorInput>
        + HasCurrentCorpusId,
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
            self.log_event(
                "candidate_selected",
                json!({
                    "request": candidate.request,
                    "corpus_count": state.corpus().count(),
                }),
            );
            return self.perform_candidate(fuzzer, executor, state, manager, candidate);
        }
        if state.corpus().count() == 0 {
            return Ok(());
        }
        let parent_id = state.current_corpus_id().ok().flatten();
        let case_id = parent_id
            .map(|id| format!("corpus:{}", id.0))
            .unwrap_or_else(|| "corpus:unknown".to_string());
        self.log_event(
            "corpus_selected",
            json!({
                "case_id": case_id,
                "corpus_count": state.corpus().count(),
            }),
        );
        let input = state.current_input_cloned()?;
        let origin = state
            .current_testcase()
            .ok()
            .and_then(|testcase| testcase.metadata::<TraceOrigin>().ok().cloned());
        let budget = 1 + state.rand_mut().below_or_zero(MAX_MUTATIONS);
        self.run_mutations(
            fuzzer,
            executor,
            state,
            manager,
            &input,
            origin.as_ref(),
            parent_id,
            budget,
            None,
        )
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

    fn input_with_fields(fields: usize) -> GeneratorInput {
        let mut values = BTreeMap::new();
        for index in 0..fields {
            values.insert(
                format!("field_{index}"),
                crate::engine::input::ParamValue::Int(index as i64),
            );
        }
        let mut params = BTreeMap::new();
        if !values.is_empty() {
            params.insert("instance".to_string(), values);
        }
        // Origins are keyed by model step, so the fixture needs a step instance
        // that maps the instance name to its model step.
        let step = crate::engine::input::StepInstance {
            name: "instance".into(),
            step: "Login".into(),
            action: "send".into(),
            condition: String::new(),
            message: None,
            processing: None,
            channel: "control".into(),
            sender: Some("client".into()),
            receiver: Some("server".into()),
            party: None,
            omitted_fields: vec![],
            inputs: BTreeMap::new(),
            broken_dependencies: vec![],
        };
        GeneratorInput::with_trace(PathBuf::from("/tmp/generator.py"), vec![step], params)
    }

    #[test]
    fn candidate_budget_scales_with_fields_and_clamps() {
        assert_eq!(candidate_budget(&input_with_fields(0)), 512);
        assert_eq!(candidate_budget(&input_with_fields(1)), 512);
        assert_eq!(candidate_budget(&input_with_fields(7)), 512);
        assert_eq!(candidate_budget(&input_with_fields(8)), 512);
        assert_eq!(candidate_budget(&input_with_fields(16)), 1024);
        assert_eq!(candidate_budget(&input_with_fields(24)), 1536);
        assert_eq!(candidate_budget(&input_with_fields(32)), 2048);
        assert_eq!(candidate_budget(&input_with_fields(200)), 2048);
    }

    #[test]
    fn budget_splits_evenly_and_gives_the_odd_evaluation_to_analyzed() {
        let mut input = input_with_fields(3);
        input.field_origin.insert(
            "Login".into(),
            BTreeMap::from([
                ("field_0".to_string(), crate::engine::input::FieldOrigin::Analyzed),
                ("field_1".to_string(), crate::engine::input::FieldOrigin::Adhoc),
                ("field_2".to_string(), crate::engine::input::FieldOrigin::Adhoc),
            ]),
        );
        let total = candidate_budget(&input);
        let (analyzed, adhoc) = budget_split(&input);
        assert_eq!(analyzed + adhoc, total);
        assert_eq!(analyzed, total.div_ceil(2));
        assert_eq!(adhoc, total / 2);
    }

    #[test]
    fn a_single_group_keeps_the_whole_budget() {
        let analyzed_only = input_with_fields(2);
        let (analyzed, adhoc) = budget_split(&analyzed_only);
        assert_eq!(adhoc, 0);
        assert_eq!(analyzed, candidate_budget(&analyzed_only));

        let mut adhoc_only = input_with_fields(2);
        adhoc_only.field_origin.insert(
            "Login".into(),
            BTreeMap::from([
                ("field_0".to_string(), crate::engine::input::FieldOrigin::Adhoc),
                ("field_1".to_string(), crate::engine::input::FieldOrigin::Adhoc),
            ]),
        );
        let (analyzed, adhoc) = budget_split(&adhoc_only);
        assert_eq!(analyzed, 0);
        assert_eq!(adhoc, candidate_budget(&adhoc_only));
    }

    #[test]
    fn corpus_reload_keeps_field_origins() {
        let dir = std::env::temp_dir().join(format!("mcn-origins-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let mut input = input_with_fields(1);
        input.field_origin.insert(
            "Login".into(),
            BTreeMap::from([("field_0".to_string(), crate::engine::input::FieldOrigin::Adhoc)]),
        );
        let mut corpus: OnDiskCorpus<GeneratorInput> = OnDiskCorpus::new(&dir).unwrap();
        corpus.add(Testcase::new(input)).unwrap();
        drop(corpus);
        // Read the persisted entry the same way a later campaign start would.
        let path = fs::read_dir(&dir)
            .unwrap()
            .flatten()
            .map(|entry| entry.path())
            .find(|path| {
                path.is_file()
                    && !path
                        .file_name()
                        .and_then(|name| name.to_str())
                        .is_some_and(|name| name.starts_with('.'))
            })
            .expect("corpus entry");
        let reloaded: GeneratorInput =
            serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        assert_eq!(
            reloaded.field_origin["Login"]["field_0"],
            crate::engine::input::FieldOrigin::Adhoc
        );
        let _ = fs::remove_dir_all(&dir);
    }

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

    #[test]
    fn mutation_events_append_jsonl() {
        let dir = std::env::temp_dir().join(format!("mcn-events-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let path = dir.join("events.jsonl");
        append_event(
            &path,
            "corpus_selected",
            json!({"case_id": "corpus:1", "corpus_count": 3}),
        )
        .unwrap();
        append_event(
            &path,
            "candidate_selected",
            json!({"request": "000001", "corpus_count": 3}),
        )
        .unwrap();
        let lines: Vec<Value> = fs::read_to_string(&path)
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect();
        assert_eq!(lines.len(), 2);
        assert_eq!(lines[0]["kind"], "corpus_selected");
        assert_eq!(lines[0]["body"]["case_id"], "corpus:1");
        assert_eq!(lines[1]["kind"], "candidate_selected");
        assert_eq!(lines[1]["body"]["request"], "000001");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn map_novelties_metadata_preserves_first_hit_and_bucket_indexes() {
        use libafl::{corpus::InMemoryCorpus, feedbacks::MapNoveltiesMetadata};
        let mut corpus = InMemoryCorpus::new();
        let input = GeneratorInput::new(PathBuf::from("/tmp/generator.py"), BTreeMap::new());
        let mut testcase = Testcase::new(input);
        testcase.add_metadata(MapNoveltiesMetadata::new(vec![1, 8, 8, 0]));
        let id = corpus.add(testcase).unwrap();
        assert_eq!(
            novelty_indexes(&corpus, id).unwrap(),
            Some(vec![1, 8, 8, 0])
        );
    }
}
