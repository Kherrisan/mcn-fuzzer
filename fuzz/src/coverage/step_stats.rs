//! Live per-step and per-dependency execution counters.
//!
//! Three signals are tracked for every model step and directed dependency:
//!
//! * `present_cnt` — instances in the original trace of a published generator.
//! * `exec_cnt` — instances contained in ordinary execs, including instances the
//!   run never reached because an earlier step failed.
//! * `succ_cnt` — instances whose handler returned normally, read back from the
//!   generator progress JSONL. A dependency succeeds when both endpoints
//!   succeeded and the prior instance finished first.
//!
//! Coverage replay never feeds these counters, so the numbers describe the
//! campaign's own executions only.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};

use crate::engine::input::{GeneratorInput, StepInstance};
use crate::protocol::{IndexedModel, StepAction};

/// One model step as the tools report it.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq, schemars::JsonSchema)]
pub struct StepEntry {
    pub step: String,
    pub channel: String,
    pub parties: Vec<String>,
    pub present_cnt: u64,
    pub exec_cnt: u64,
    pub succ_cnt: u64,
}

/// One directed dependency as the tools report it.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq, schemars::JsonSchema)]
pub struct DependencyEntry {
    pub prior_step: String,
    pub dependent_step: String,
    pub condition: String,
    pub fields: Vec<String>,
    pub present_cnt: u64,
    pub exec_cnt: u64,
    pub succ_cnt: u64,
}

/// Aggregated counters keyed by model name.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct StepStats {
    #[serde(default)]
    steps: BTreeMap<String, [u64; 3]>,
    #[serde(default)]
    dependencies: BTreeMap<String, [u64; 3]>,
    #[serde(default)]
    present_sources: BTreeSet<String>,
    #[serde(default)]
    model: Vec<StepEntry>,
    #[serde(default)]
    edges: Vec<DependencyEntry>,
    #[serde(default)]
    unobservable_runs: u64,
    #[serde(default)]
    observable_runs: u64,
}

/// Instance-level view of one trace, used for all three increments.
pub struct TraceView {
    /// `(instance index, instance name, model step)` in trace order.
    pub steps: Vec<(usize, String, String)>,
    /// `(prior index, dependent index, condition, fields)`.
    pub dependencies: Vec<(usize, usize, String, Vec<String>)>,
}

impl TraceView {
    pub fn of(input: &GeneratorInput) -> Self {
        let steps: Vec<(usize, String, String)> = input
            .trace
            .steps
            .iter()
            .enumerate()
            .map(|(index, step)| (index, step.name.clone(), step.step.clone()))
            .collect();
        let position: BTreeMap<&str, usize> = steps
            .iter()
            .map(|(index, name, _)| (name.as_str(), *index))
            .collect();
        let mut dependencies = Vec::new();
        for dependency in &input.trace.dependencies {
            let (Some(prior), Some(dependent)) = (
                position.get(dependency.prior.as_str()),
                position.get(dependency.dependent.as_str()),
            ) else {
                continue;
            };
            dependencies.push((
                *prior,
                *dependent,
                dependency.condition.clone(),
                dependency.fields.clone(),
            ));
        }
        Self { steps, dependencies }
    }

    pub fn from_steps(steps: &[StepInstance], dependencies: &[(usize, usize, String, Vec<String>)]) -> Self {
        let steps = steps
            .iter()
            .enumerate()
            .map(|(index, step)| (index, step.name.clone(), step.step.clone()))
            .collect();
        Self { steps, dependencies: dependencies.to_vec() }
    }
}

fn step_key(step: &str) -> String {
    step.to_string()
}

fn dependency_key(prior: &str, dependent: &str, condition: &str, fields: &[String]) -> String {
    format!("{prior}\u{1f}{dependent}\u{1f}{condition}\u{1f}{}", fields.join(","))
}

impl StepStats {
    /// Record the campaign's protocol model once, so zero-count entries appear.
    pub fn set_model(&mut self, model: &IndexedModel) {
        let mut entries = Vec::new();
        for step in &model.steps {
            let mut parties = Vec::new();
            match &step.action {
                StepAction::Send { sender_name, receiver_name, .. } => {
                    parties.push(sender_name.clone());
                    parties.push(receiver_name.clone());
                }
                StepAction::Listen { party_name }
                | StepAction::Connect { party_name }
                | StepAction::Close { party_name } => parties.push(party_name.clone()),
            }
            entries.push(StepEntry {
                step: step.name.clone(),
                channel: step.channel_name.clone(),
                parties,
                present_cnt: 0,
                exec_cnt: 0,
                succ_cnt: 0,
            });
        }
        self.model = entries;
        self.edges = model
            .dependencies
            .iter()
            .map(|dependency| DependencyEntry {
                prior_step: dependency.prior_step.clone(),
                dependent_step: dependency.dependent_step.clone(),
                condition: dependency.condition.clone(),
                fields: dependency
                    .field_relations
                    .iter()
                    .map(|relation| relation.dependent_field.clone())
                    .collect(),
                present_cnt: 0,
                exec_cnt: 0,
                succ_cnt: 0,
            })
            .collect();
    }

    /// Count one published generator's original trace once per source id.
    pub fn record_present(&mut self, source: &str, trace: &TraceView) -> bool {
        if !self.present_sources.insert(source.to_string()) {
            return false;
        }
        for (_, _, step) in &trace.steps {
            self.bump_step(step, 0);
        }
        for (prior, dependent, condition, fields) in &trace.dependencies {
            let (Some(prior_step), Some(dependent_step)) = (
                trace.steps.get(*prior).map(|row| &row.2),
                trace.steps.get(*dependent).map(|row| &row.2),
            ) else {
                continue;
            };
            self.bump_dependency(prior_step, dependent_step, condition, fields, 0);
        }
        true
    }

    /// Count one exec's mutated trace. Call only after the request was sent.
    pub fn record_exec(&mut self, trace: &TraceView) {
        for (_, _, step) in &trace.steps {
            self.bump_step(step, 1);
        }
        for (prior, dependent, condition, fields) in &trace.dependencies {
            let (Some(prior_step), Some(dependent_step)) = (
                trace.steps.get(*prior).map(|row| &row.2),
                trace.steps.get(*dependent).map(|row| &row.2),
            ) else {
                continue;
            };
            self.bump_dependency(prior_step, dependent_step, condition, fields, 1);
        }
    }

    /// Count successes for the instance indexes the generator reported.
    ///
    /// `observable` is false for generators without step handlers or when the
    /// progress file was missing, so their runs are reported separately instead
    /// of being treated as failures.
    pub fn record_successes(&mut self, trace: &TraceView, succeeded: &BTreeSet<usize>, observable: bool) {
        // A legacy generator reports its own internal step names instead of
        // model steps. Those runs cannot be attributed to the model, so they are
        // counted as unobservable rather than as failures or successes.
        let matches_model = trace
            .steps
            .iter()
            .any(|(_, _, step)| self.model.iter().any(|row| &row.step == step));
        if observable && matches_model {
            self.observable_runs += 1;
        } else {
            self.unobservable_runs += 1;
            return;
        }
        if succeeded.is_empty() && !observable {
            return;
        }
        for (index, _, step) in &trace.steps {
            if succeeded.contains(index) {
                self.bump_step(step, 2);
            }
        }
        for (prior, dependent, condition, fields) in &trace.dependencies {
            if !(succeeded.contains(prior) && succeeded.contains(dependent) && prior < dependent) {
                continue;
            }
            let (Some(prior_step), Some(dependent_step)) = (
                trace.steps.get(*prior).map(|row| &row.2),
                trace.steps.get(*dependent).map(|row| &row.2),
            ) else {
                continue;
            };
            self.bump_dependency(prior_step, dependent_step, condition, fields, 2);
        }
    }

    fn bump_step(&mut self, step: &str, slot: usize) {
        if let Some(row) = self.model.iter_mut().find(|row| row.step == step) {
            row_count(&mut row.present_cnt, &mut row.exec_cnt, &mut row.succ_cnt, slot);
        }
        self.steps.entry(step_key(step)).or_default()[slot] += 1;
    }

    fn bump_dependency(&mut self, prior: &str, dependent: &str, condition: &str, fields: &[String], slot: usize) {
        let key = dependency_key(prior, dependent, condition, fields);
        if let Some(row) = self.edges.iter_mut().find(|row| {
            row.prior_step == prior
                && row.dependent_step == dependent
                && row.condition == condition
                && row.fields == fields
        }) {
            row_count(&mut row.present_cnt, &mut row.exec_cnt, &mut row.succ_cnt, slot);
        }
        self.dependencies.entry(key).or_default()[slot] += 1;
    }

    pub fn steps(&self) -> &[StepEntry] {
        &self.model
    }

    pub fn edges(&self) -> &[DependencyEntry] {
        &self.edges
    }

    pub fn unobservable_runs(&self) -> u64 {
        self.unobservable_runs
    }

    pub fn observable_runs(&self) -> u64 {
        self.observable_runs
    }
}

fn row_count(present: &mut u64, exec: &mut u64, succ: &mut u64, slot: usize) {
    match slot {
        0 => *present += 1,
        1 => *exec += 1,
        _ => *succ += 1,
    }
}

/// Parse the generator progress JSONL.
///
/// Returns the instance indexes whose handler returned normally and whether the
/// file shows that a dispatcher started. A truncated trailing line and broken
/// lines are ignored; a missing scheduler start means the generator has no step
/// handlers, so its successes are unobservable.
pub fn read_progress(path: &std::path::Path) -> (BTreeSet<usize>, bool) {
    let mut succeeded = BTreeSet::new();
    let mut started = false;
    let Ok(text) = std::fs::read_to_string(path) else {
        return (succeeded, started);
    };
    for line in text.lines() {
        let Ok(value) = serde_json::from_str::<serde_json::Value>(line) else {
            continue;
        };
        match value["event"].as_str() {
            Some("scheduler_start") => started = true,
            Some("step_ok") => {
                if let Some(index) = value["index"].as_u64() {
                    succeeded.insert(index as usize);
                }
            }
            _ => {}
        }
    }
    (succeeded, started)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn step(index: usize, name: &str, model: &str) -> StepInstance {
        let mut instance: StepInstance = serde_json::from_value(serde_json::json!({
            "name": name,
            "step": model,
            "action": "send",
            "channel": "control",
        }))
        .expect("step instance");
        instance.name = format!("{name}");
        let _ = index;
        instance
    }

    fn view(steps: &[(&str, &str)], dependencies: &[(usize, usize)]) -> TraceView {
        let steps = steps
            .iter()
            .enumerate()
            .map(|(index, (name, model))| (index, name.to_string(), model.to_string()))
            .collect::<Vec<_>>();
        TraceView {
            steps,
            dependencies: dependencies
                .iter()
                .map(|(prior, dependent)| (*prior, *dependent, String::new(), Vec::new()))
                .collect(),
        }
    }

    #[test]
    fn present_is_deduplicated_per_source_but_exec_is_not() {
        let mut stats = StepStats::default();
        let trace = view(&[("a", "Login"), ("b", "Login")], &[(0, 1)]);
        assert!(stats.record_present("gen-1", &trace));
        assert!(!stats.record_present("gen-1", &trace));
        stats.record_exec(&trace);
        stats.record_exec(&trace);
        let dependencies = stats.dependencies.values().next().copied().expect("dependency");
        assert_eq!(dependencies, [1, 2, 0]);
    }

    #[test]
    fn step_success_counts_duplicate_instances_and_dependencies_need_order() {
        let mut stats = StepStats::default();
        stats.model = vec![
            StepEntry {
                step: "Login".into(),
                channel: "control".into(),
                parties: vec![],
                present_cnt: 0,
                exec_cnt: 0,
                succ_cnt: 0,
            },
            StepEntry {
                step: "Data".into(),
                channel: "control".into(),
                parties: vec![],
                present_cnt: 0,
                exec_cnt: 0,
                succ_cnt: 0,
            },
        ];
        let trace = view(&[("a", "Login"), ("b", "Login"), ("c", "Data")], &[(0, 1), (2, 0), (0, 2)]);
        stats.record_exec(&trace);
        stats.record_successes(&trace, &BTreeSet::from([0, 1, 2]), true);
        assert_eq!(stats.observable_runs, 1);
        // (0,1) succeeds because both endpoints ran in order; (2,0) is reversed.
        let mut successes: Vec<_> = stats.dependencies.values().map(|row| (row[2], row[1])).collect();
        successes.sort();
        assert_eq!(successes, vec![(0, 1), (1, 1), (1, 1)]);
    }

    #[test]
    fn unobservable_generator_is_not_a_failure() {
        let mut stats = StepStats::default();
        let trace = view(&[("a", "Login")], &[]);
        stats.record_exec(&trace);
        stats.record_successes(&trace, &BTreeSet::new(), false);
        assert_eq!(stats.unobservable_runs, 1);
        assert_eq!(stats.observable_runs, 0);
        assert_eq!(stats.steps.values().next().copied(), Some([0, 1, 0]));
    }

    #[test]
    fn progress_reader_ignores_truncated_and_broken_lines() {
        let dir = std::env::temp_dir().join(format!("mcn-step-progress-{}", std::process::id()));
        std::fs::create_dir_all(&dir).expect("temp dir");
        let path = dir.join("progress.jsonl");
        std::fs::write(
            &path,
            "{\"event\":\"scheduler_start\"}\n{\"event\":\"step_ok\",\"index\":0}\nnot json\n{\"event\":\"step_ok\",\"index\":1}\n{\"event\":\"step_",
        )
        .expect("write");
        let (succeeded, started) = read_progress(&path);
        assert!(started);
        assert_eq!(succeeded, BTreeSet::from([0, 1]));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn legacy_generator_steps_are_unobservable_not_failed() {
        let mut stats = StepStats::default();
        stats.model = vec![StepEntry {
            step: "Send USER".into(),
            channel: "control".into(),
            parties: vec!["User-PI".into(), "Server-PI".into()],
            present_cnt: 0,
            exec_cnt: 0,
            succ_cnt: 0,
        }];
        let trace = view(&[("session_1", "ftp_session")], &[]);
        stats.record_exec(&trace);
        stats.record_successes(&trace, &BTreeSet::from([0]), true);
        assert_eq!(stats.observable_runs, 0);
        assert_eq!(stats.unobservable_runs, 1);
        assert_eq!(stats.model[0].exec_cnt, 0);
        assert_eq!(stats.model[0].succ_cnt, 0);
    }

    #[test]
    fn model_steps_receive_exec_and_success_counts() {
        let mut stats = StepStats::default();
        stats.model = vec![StepEntry {
            step: "Send USER".into(),
            channel: "control".into(),
            parties: vec!["User-PI".into(), "Server-PI".into()],
            present_cnt: 0,
            exec_cnt: 0,
            succ_cnt: 0,
        }];
        let trace = view(&[("login_1", "Send USER")], &[]);
        stats.record_present("gen", &trace);
        stats.record_exec(&trace);
        stats.record_successes(&trace, &BTreeSet::from([0]), true);
        assert_eq!(stats.model[0].present_cnt, 1);
        assert_eq!(stats.model[0].exec_cnt, 1);
        assert_eq!(stats.model[0].succ_cnt, 1);
    }

    #[test]
    fn duplicate_instances_are_counted_separately_by_name() {
        let mut stats = StepStats::default();
        let steps = vec![step(0, "one", "Login"), step(1, "two", "Login")];
        let trace = TraceView::from_steps(&steps, &[]);
        stats.record_present("gen", &trace);
        stats.record_exec(&trace);
        assert_eq!(stats.steps["Login"], [2, 2, 0]);
    }
}
