//! Resident protocol-step graph and priority trace selection.
//!
//! Callgraph analysis stays in Python. This graph is only protocol steps.

use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    fs,
    path::Path,
};

use petgraph::stable_graph::{NodeIndex, StableDiGraph};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use super::{
    model::{Binding, Dependency, IndexedModel, StepRecord},
    select::{covers_sut_and_generator, SelectionContext, StepTrace},
};
use libafl_bolts::rands::Rand;
use std::num::NonZeroUsize;

/// Hard cap on Steiner-search checks. Past this the best set found is used.
pub const SEARCH_BUDGET: u64 = 50_000;

#[derive(Clone, Debug)]
pub struct StepNode {
    pub step_id: String,
    pub name: String,
    pub index: usize,
    pub score: f64,
    pub record: StepRecord,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StepDependency {
    pub condition: String,
    pub field_relations: Vec<(String, String)>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct DepEdge {
    pub prior: String,
    pub dependent: String,
    pub condition: String,
    pub field_relations: Vec<(String, String)>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ExplorationCheckpoint {
    pub version: u32,
    pub model_version: String,
    pub priority_version: u64,
    pub revision: u64,
    pub build: String,
    pub applied: BTreeSet<String>,
    pub scores: BTreeMap<String, f64>,
    pub extra_dependencies: Vec<DepEdge>,
    pub log_seq: u64,
}

pub struct ProtocolExplorationGraph {
    graph: StableDiGraph<StepNode, StepDependency>,
    by_id: BTreeMap<String, NodeIndex>,
    by_name: BTreeMap<String, NodeIndex>,
    pub revision: u64,
    pub model_version: String,
    pub priority_version: u64,
    pub applied: BTreeSet<String>,
    pub build: String,
    pub log_seq: u64,
    base_edges: Vec<DepEdge>,
}

#[derive(Clone, Debug)]
pub struct SelectionReport {
    pub trace: Option<StepTrace>,
    pub events: Vec<Value>,
    pub revision: u64,
    pub proven: bool,
}

impl ProtocolExplorationGraph {
    pub fn from_model(model: &IndexedModel) -> Self {
        let mut graph = StableDiGraph::new();
        let mut by_id = BTreeMap::new();
        let mut by_name = BTreeMap::new();
        for step in &model.steps {
            let id = step.id().0;
            let index = graph.add_node(StepNode {
                step_id: id.clone(),
                name: step.name.clone(),
                index: step.index,
                score: 0.0,
                record: step.clone(),
            });
            by_id.insert(id, index);
            by_name.insert(step.name.clone(), index);
        }
        let mut seen = BTreeSet::new();
        let mut base_edges = Vec::new();
        for dep in &model.dependencies {
            let edge = dep_from(dep);
            if !seen.insert(edge_key(&edge)) {
                continue;
            }
            add_edge(&mut graph, &by_name, &edge);
            base_edges.push(edge);
        }
        Self {
            graph,
            by_id,
            by_name,
            revision: 1,
            model_version: model.protocol_id.clone(),
            priority_version: 1,
            applied: BTreeSet::new(),
            build: String::new(),
            log_seq: 0,
            base_edges,
        }
    }

    pub fn node_count(&self) -> usize {
        self.graph.node_count()
    }

    pub fn edge_count(&self) -> usize {
        self.graph.edge_count()
    }

    pub fn score_of(&self, step_id: &str) -> Option<f64> {
        self.by_id
            .get(step_id)
            .and_then(|index| self.graph.node_weight(*index))
            .map(|node| node.score)
    }

    pub fn eligible_scores(&self, model: &IndexedModel, binding: &Binding) -> Vec<Value> {
        model
            .eligible(binding)
            .into_iter()
            .map(|step| {
                let id = step.id().0;
                let score = self.score_of(&id).unwrap_or(0.0);
                json!({
                    "step_id": id,
                    "name": step.name,
                    "score": score,
                    "weight": 1.0 + score,
                })
            })
            .collect()
    }

    /// Apply accepted dependency edges. Returns an error without mutating on a new cycle.
    pub fn try_add_dependencies(&mut self, edges: &[DepEdge]) -> Result<Vec<DepEdge>, String> {
        let mut trial = self.graph.clone();
        let mut added = Vec::new();
        for edge in edges {
            if self.by_name.get(&edge.prior).is_none()
                || self.by_name.get(&edge.dependent).is_none()
            {
                return Err(format!(
                    "dependency {} -> {} references a missing step",
                    edge.prior, edge.dependent
                ));
            }
            // Same endpoints are one topology edge. A different condition or
            // field relation is not a new dependency, so it must not be
            // reported as a cycle when a path already runs back the other way.
            if pair_connected(&trial, &self.by_name, edge) {
                continue;
            }
            if edge.prior != edge.dependent
                && reaches(&trial, &self.by_name, &edge.dependent, &edge.prior)
            {
                return Err("new dependency edges introduce a cycle".to_string());
            }
            add_edge(&mut trial, &self.by_name, edge);
            added.push(edge.clone());
        }
        self.graph = trial;
        if !added.is_empty() {
            self.revision += 1;
        }
        Ok(added)
    }

    /// Apply a priority delta once per request id. Duplicate requests do not change scores.
    pub fn apply_deltas(
        &mut self,
        request: &str,
        deltas: &[(String, f64, String)],
    ) -> Result<Vec<Value>, String> {
        if self.applied.contains(request) {
            return Ok(vec![json!({
                "kind": "priority_ignored",
                "request": request,
                "reason": "duplicate result",
            })]);
        }
        let mut changes = Vec::new();
        for (step_id, delta, reason) in deltas {
            let Some(index) = self.by_id.get(step_id).copied() else {
                return Err(format!("unknown step id {step_id}"));
            };
            if !delta.is_finite() {
                return Err(format!("non-finite delta for {step_id}"));
            }
            let node = self.graph.node_weight(index).expect("node");
            let before = node.score;
            let after = (before + *delta).max(0.0);
            changes.push((
                index,
                after,
                json!({
                    "step_id": step_id,
                    "name": node.name,
                    "before": before,
                    "delta": delta,
                    "after": after,
                    "weight": 1.0 + after,
                    "reason": reason,
                }),
            ));
        }
        for (index, after, _) in &changes {
            self.graph.node_weight_mut(*index).expect("node").score = *after;
        }
        self.applied.insert(request.to_string());
        let changes: Vec<_> = changes.into_iter().map(|(_, _, value)| value).collect();
        self.priority_version += 1;
        self.revision += 1;
        Ok(changes)
    }

    pub fn checkpoint(&self) -> ExplorationCheckpoint {
        let mut extra = Vec::new();
        let base: BTreeSet<_> = self.base_edges.iter().map(edge_key).collect();
        for edge in self.graph.edge_indices() {
            let (from, to) = self.graph.edge_endpoints(edge).expect("edge");
            let prior = self.graph.node_weight(from).expect("node").name.clone();
            let dependent = self.graph.node_weight(to).expect("node").name.clone();
            let weight = self.graph.edge_weight(edge).expect("weight");
            let record = DepEdge {
                prior,
                dependent,
                condition: weight.condition.clone(),
                field_relations: weight.field_relations.clone(),
            };
            if !base.contains(&edge_key(&record)) {
                extra.push(record);
            }
        }
        let mut scores = BTreeMap::new();
        for node in self.graph.node_weights() {
            scores.insert(node.step_id.clone(), node.score);
        }
        ExplorationCheckpoint {
            version: 1,
            model_version: self.model_version.clone(),
            priority_version: self.priority_version,
            revision: self.revision,
            build: self.build.clone(),
            applied: self.applied.clone(),
            scores,
            extra_dependencies: extra,
            log_seq: self.log_seq,
        }
    }

    pub fn save(&self, path: &Path) -> Result<(), String> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).map_err(|error| error.to_string())?;
        }
        let bytes =
            serde_json::to_vec_pretty(&self.checkpoint()).map_err(|error| error.to_string())?;
        let tmp = path.with_extension("tmp");
        fs::write(&tmp, &bytes).map_err(|error| error.to_string())?;
        fs::rename(&tmp, path).map_err(|error| error.to_string())
    }

    pub fn restore(model: &IndexedModel, path: &Path) -> Result<Self, String> {
        let bytes = fs::read(path).map_err(|error| error.to_string())?;
        let checkpoint: ExplorationCheckpoint =
            serde_json::from_slice(&bytes).map_err(|error| error.to_string())?;
        if checkpoint.version != 1 {
            return Err(format!(
                "unsupported exploration checkpoint {}",
                checkpoint.version
            ));
        }
        if checkpoint.model_version != model.protocol_id {
            return Err("checkpoint model version does not match the base model".to_string());
        }
        let mut graph = Self::from_model(model);
        graph.try_add_dependencies(&checkpoint.extra_dependencies)?;
        for (step_id, score) in &checkpoint.scores {
            let Some(index) = graph.by_id.get(step_id).copied() else {
                return Err(format!("checkpoint score for unknown step {step_id}"));
            };
            graph.graph.node_weight_mut(index).expect("node").score = *score;
        }
        graph.priority_version = checkpoint.priority_version;
        graph.revision = checkpoint.revision;
        graph.build = checkpoint.build;
        graph.applied = checkpoint.applied;
        graph.log_seq = checkpoint.log_seq;
        // from_model and try_add bumped revision; restore the checkpoint revision.
        graph.revision = checkpoint.revision;
        Ok(graph)
    }

    pub fn select(
        &self,
        model: &IndexedModel,
        binding: &Binding,
        rand: &mut impl Rand,
        context: &SelectionContext,
        attempts: u32,
        min_len: usize,
        max_len: usize,
    ) -> SelectionReport {
        let mut events = Vec::new();
        let scores = self.eligible_scores(model, binding);
        events.push(json!({
            "kind": "priority_snapshot",
            "revision": self.revision,
            "priority_version": self.priority_version,
            "steps": scores,
        }));
        let eligible = model.eligible(binding);
        let requested_min = min_len.max(1);
        if eligible.is_empty() || max_len == 0 || attempts == 0 || eligible.len() < requested_min {
            events.push(json!({"kind": "trace_skipped", "reason": "no eligible steps"}));
            return SelectionReport {
                trace: None,
                events,
                revision: self.revision,
                proven: true,
            };
        }
        let view = compact_view(self, &eligible);
        let min_len = requested_min;
        let max_len = max_len.max(min_len).min(eligible.len());
        let mut proven_all = true;
        for attempt in 0..attempts {
            let span = max_len.saturating_sub(min_len) + 1;
            let len = min_len
                + rand
                    .below(NonZeroUsize::new(span.max(1)).expect("span"))
                    .min(eligible.len());
            let len = len.min(eligible.len()).max(1);
            let picked = weighted_sample(&view, len, rand);
            let outcome = connect(&view, &picked, max_len, SEARCH_BUDGET);
            proven_all &= outcome.proven;
            let kept = highest_mean(&view, &outcome.members);
            let ordered = order_component(self, &view, &kept);
            let steps: Vec<StepRecord> = ordered.iter().map(|item| item.record.clone()).collect();
            let sampled_ids: BTreeSet<_> = picked
                .iter()
                .map(|index| view.nodes[*index].step_id.clone())
                .collect();
            let trace = if steps.is_empty() {
                None
            } else {
                Some(StepTrace::new(steps))
            };
            let mut reason = None;
            if let Some(trace) = &trace {
                if trace.steps.len() < min_len {
                    reason = Some("below minimum length after component selection");
                } else if trace.steps.len() > max_len {
                    reason = Some("above maximum length");
                } else if !covers_sut_and_generator(&trace.steps, binding) {
                    reason = Some("selected component does not cover both party roles");
                } else if context.attempted.contains(&trace.id.0) {
                    reason = Some("duplicate trace");
                }
            } else {
                reason = Some("empty component");
            }
            events.push(json!({
                "kind": "components",
                "attempt": attempt,
                "revision": self.revision,
                "priority_version": self.priority_version,
                "sampled": picked.iter().map(|index| node_json(&view, *index)).collect::<Vec<_>>(),
                "before": component_json(&view, &outcome.before),
                "after": component_json(&view, &outcome.after),
                "extras": outcome.extras.iter().map(|index| node_json(&view, *index)).collect::<Vec<_>>(),
                "proven": outcome.proven,
                "kept": kept.iter().map(|index| node_json(&view, *index)).collect::<Vec<_>>(),
                "reason": reason,
            }));
            if reason.is_none() {
                let trace = trace.expect("trace");
                events.push(json!({
                    "kind": "trace",
                    "attempt": attempt,
                    "revision": self.revision,
                    "trace_id": trace.id.0,
                    "mean": mean_of(&view, &kept),
                    "steps": trace.steps.iter().map(|step| {
                        let sampled = sampled_ids.contains(&step.id().0);
                        json!({
                            "step_id": step.id().0,
                            "name": step.name,
                            "role": if sampled { "sampled" } else { "bridge" },
                        })
                    }).collect::<Vec<_>>(),
                }));
                return SelectionReport {
                    trace: Some(trace),
                    events,
                    revision: self.revision,
                    proven: proven_all,
                };
            }
            events.push(json!({
                "kind": "trace_attempt_failed",
                "attempt": attempt,
                "reason": reason,
            }));
        }
        events.push(json!({"kind": "trace_skipped", "reason": "sample attempts exhausted"}));
        SelectionReport {
            trace: None,
            events,
            revision: self.revision,
            proven: proven_all,
        }
    }
}

/// Production selector. It reads one revision of the resident graph.
#[derive(Clone, Debug, Default)]
pub struct PriorityTraceSelector;

fn dep_from(dep: &Dependency) -> DepEdge {
    DepEdge {
        prior: dep.prior_step.clone(),
        dependent: dep.dependent_step.clone(),
        condition: dep.condition.clone(),
        field_relations: dep
            .field_relations
            .iter()
            .map(|field| (field.prior_field.clone(), field.dependent_field.clone()))
            .collect(),
    }
}

fn edge_key(edge: &DepEdge) -> (String, String, String, Vec<(String, String)>) {
    (
        edge.prior.clone(),
        edge.dependent.clone(),
        edge.condition.clone(),
        edge.field_relations.clone(),
    )
}

fn add_edge(
    graph: &mut StableDiGraph<StepNode, StepDependency>,
    by_name: &BTreeMap<String, NodeIndex>,
    edge: &DepEdge,
) {
    let Some(from) = by_name.get(&edge.prior).copied() else {
        return;
    };
    let Some(to) = by_name.get(&edge.dependent).copied() else {
        return;
    };
    graph.add_edge(
        from,
        to,
        StepDependency {
            condition: edge.condition.clone(),
            field_relations: edge.field_relations.clone(),
        },
    );
}

fn pair_connected(
    graph: &StableDiGraph<StepNode, StepDependency>,
    by_name: &BTreeMap<String, NodeIndex>,
    edge: &DepEdge,
) -> bool {
    let Some(from) = by_name.get(&edge.prior).copied() else {
        return false;
    };
    let Some(to) = by_name.get(&edge.dependent).copied() else {
        return false;
    };
    graph.edges_connecting(from, to).next().is_some()
}

#[allow(dead_code)]
fn has_cycle(graph: &StableDiGraph<StepNode, StepDependency>) -> bool {
    let mut indegree = BTreeMap::new();
    for index in graph.node_indices() {
        indegree.insert(index, 0usize);
    }
    for edge in graph.edge_indices() {
        let (from, to) = graph.edge_endpoints(edge).expect("edge");
        if from == to {
            continue;
        }
        *indegree.entry(to).or_default() += 1;
    }
    let mut ready: VecDeque<_> = indegree
        .iter()
        .filter(|(_, degree)| **degree == 0)
        .map(|(index, _)| *index)
        .collect();
    let mut seen = 0usize;
    while let Some(node) = ready.pop_front() {
        seen += 1;
        for next in graph.neighbors(node) {
            if next == node {
                continue;
            }
            let degree = indegree.get_mut(&next).expect("degree");
            *degree -= 1;
            if *degree == 0 {
                ready.push_back(next);
            }
        }
    }
    seen != graph.node_count()
}

#[derive(Clone)]
struct CompactNode {
    index: NodeIndex,
    step_id: String,
    name: String,
    model_index: usize,
    score: f64,
    record: StepRecord,
}

struct View {
    nodes: Vec<CompactNode>,
    adj: Vec<Vec<usize>>,
}

fn compact_view(graph: &ProtocolExplorationGraph, eligible: &[&StepRecord]) -> View {
    let mut nodes = Vec::new();
    let mut place = BTreeMap::new();
    for step in eligible {
        let id = step.id().0;
        let Some(index) = graph.by_id.get(&id).copied() else {
            continue;
        };
        let weight = graph.graph.node_weight(index).expect("node");
        place.insert(index, nodes.len());
        nodes.push(CompactNode {
            index,
            step_id: id,
            name: step.name.clone(),
            model_index: step.index,
            score: weight.score,
            record: (*step).clone(),
        });
    }
    let mut adj = vec![Vec::new(); nodes.len()];
    for edge in graph.graph.edge_indices() {
        let (from, to) = graph.graph.edge_endpoints(edge).expect("edge");
        if from == to {
            continue;
        }
        let Some(&left) = place.get(&from) else {
            continue;
        };
        let Some(&right) = place.get(&to) else {
            continue;
        };
        if !adj[left].contains(&right) {
            adj[left].push(right);
            adj[right].push(left);
        }
    }
    View { nodes, adj }
}

fn weighted_sample(view: &View, len: usize, rand: &mut impl Rand) -> Vec<usize> {
    let mut remaining: Vec<usize> = (0..view.nodes.len()).collect();
    let mut picked = Vec::new();
    while picked.len() < len && !remaining.is_empty() {
        let weights: Vec<f64> = remaining
            .iter()
            .map(|index| 1.0 + view.nodes[*index].score)
            .collect();
        let choice = weighted_index(&weights, rand);
        picked.push(remaining.remove(choice));
    }
    picked
}

fn weighted_index(weights: &[f64], rand: &mut impl Rand) -> usize {
    let total: f64 = weights.iter().sum();
    if total <= 0.0 {
        return 0;
    }
    let mut ticket = (rand.next() as f64 / (u64::MAX as f64)) * total;
    for (index, weight) in weights.iter().enumerate() {
        if ticket <= *weight {
            return index;
        }
        ticket -= weight;
    }
    weights.len() - 1
}

struct ConnectOutcome {
    members: Vec<usize>,
    extras: Vec<usize>,
    before: Vec<Vec<usize>>,
    after: Vec<Vec<usize>>,
    proven: bool,
}

fn connect(view: &View, terminals: &[usize], max_len: usize, budget: u64) -> ConnectOutcome {
    let before = components_of(view, terminals);
    let mut extras_pool: Vec<usize> = (0..view.nodes.len())
        .filter(|index| !terminals.contains(index))
        .collect();
    extras_pool.sort_by_key(|index| view.nodes[*index].model_index);
    let mut checks = 0u64;
    let mut proven = true;
    let room = max_len.saturating_sub(terminals.len());
    let mut best_members = terminals.to_vec();
    let mut best_extras = Vec::new();
    let mut best_components = components_of(view, terminals).len();
    let found_connected = best_components <= 1;
    if !found_connected {
        for extra_count in 1..=room {
            let mut stop_count = false;
            walk_combinations(&extras_pool, extra_count, &mut |combo| {
                checks += 1;
                if checks > budget {
                    proven = false;
                    return false;
                }
                let mut members = terminals.to_vec();
                members.extend(combo.iter().copied());
                let parts = components_of(view, &members);
                if parts.len() < best_components
                    || (parts.len() == best_components
                        && combo.len() == best_extras.len()
                        && combo_key(view, combo) < combo_key(view, &best_extras))
                {
                    best_components = parts.len();
                    best_members = members;
                    best_extras = combo.to_vec();
                }
                if parts.len() == 1 {
                    stop_count = true;
                    return false;
                }
                true
            });
            if !proven || stop_count {
                break;
            }
        }
    }
    let after = components_of(view, &best_members);
    ConnectOutcome {
        members: best_members,
        extras: best_extras,
        before,
        after,
        proven,
    }
}

fn combo_key(view: &View, combo: &[usize]) -> Vec<usize> {
    let mut keys: Vec<_> = combo
        .iter()
        .map(|index| view.nodes[*index].model_index)
        .collect();
    keys.sort_unstable();
    keys
}

fn walk_combinations(pool: &[usize], count: usize, visit: &mut dyn FnMut(&[usize]) -> bool) {
    let mut current = Vec::new();
    fn walk(
        pool: &[usize],
        start: usize,
        count: usize,
        current: &mut Vec<usize>,
        visit: &mut dyn FnMut(&[usize]) -> bool,
    ) -> bool {
        if current.len() == count {
            return visit(current);
        }
        for index in start..pool.len() {
            current.push(pool[index]);
            if !walk(pool, index + 1, count, current, visit) {
                return false;
            }
            current.pop();
        }
        true
    }
    if count <= pool.len() {
        walk(pool, 0, count, &mut current, visit);
    }
}

fn reaches(
    graph: &StableDiGraph<StepNode, StepDependency>,
    by_name: &BTreeMap<String, NodeIndex>,
    start: &str,
    goal: &str,
) -> bool {
    let Some(start) = by_name.get(start).copied() else {
        return false;
    };
    let Some(goal) = by_name.get(goal).copied() else {
        return false;
    };
    let mut seen = BTreeSet::new();
    let mut queue = VecDeque::from([start]);
    while let Some(node) = queue.pop_front() {
        if node == goal && seen.contains(&node) {
            return true;
        }
        if !seen.insert(node) {
            continue;
        }
        if node == goal {
            return true;
        }
        for next in graph.neighbors(node) {
            if next != node {
                queue.push_back(next);
            }
        }
    }
    false
}

fn components_of(view: &View, members: &[usize]) -> Vec<Vec<usize>> {
    let set: BTreeSet<_> = members.iter().copied().collect();
    let mut seen = BTreeSet::new();
    let mut parts = Vec::new();
    for start in members {
        if !seen.insert(*start) {
            continue;
        }
        let mut queue = VecDeque::from([*start]);
        let mut part = vec![*start];
        while let Some(node) = queue.pop_front() {
            for next in &view.adj[node] {
                if set.contains(next) && seen.insert(*next) {
                    queue.push_back(*next);
                    part.push(*next);
                }
            }
        }
        part.sort_by_key(|index| view.nodes[*index].model_index);
        parts.push(part);
    }
    parts.sort_by_key(|part| view.nodes[part[0]].model_index);
    parts
}

fn mean_of(view: &View, members: &[usize]) -> f64 {
    if members.is_empty() {
        return f64::NEG_INFINITY;
    }
    let sum: f64 = members.iter().map(|index| view.nodes[*index].score).sum();
    sum / members.len() as f64
}

fn highest_mean(view: &View, members: &[usize]) -> Vec<usize> {
    let parts = components_of(view, members);
    parts
        .into_iter()
        .max_by(|left, right| {
            let mean = mean_of(view, left)
                .partial_cmp(&mean_of(view, right))
                .unwrap_or(std::cmp::Ordering::Equal);
            if mean != std::cmp::Ordering::Equal {
                return mean;
            }
            let left_index = left
                .iter()
                .map(|index| view.nodes[*index].model_index)
                .min()
                .unwrap_or(usize::MAX);
            let right_index = right
                .iter()
                .map(|index| view.nodes[*index].model_index)
                .min()
                .unwrap_or(usize::MAX);
            right_index.cmp(&left_index)
        })
        .unwrap_or_default()
}

fn order_component(
    graph: &ProtocolExplorationGraph,
    view: &View,
    members: &[usize],
) -> Vec<CompactNode> {
    let set: BTreeSet<_> = members.iter().copied().collect();
    let mut indegree = BTreeMap::new();
    for index in members {
        indegree.insert(*index, 0usize);
    }
    for index in members {
        let from = view.nodes[*index].index;
        for next in graph.graph.neighbors(from) {
            if next == from {
                continue;
            }
            let Some(compact) = view.nodes.iter().position(|node| node.index == next) else {
                continue;
            };
            if set.contains(&compact) {
                *indegree.entry(compact).or_default() += 1;
            }
        }
    }
    let mut ready: Vec<usize> = indegree
        .iter()
        .filter(|(_, degree)| **degree == 0)
        .map(|(index, _)| *index)
        .collect();
    let mut ordered = Vec::new();
    while !ready.is_empty() {
        ready.sort_by_key(|index| view.nodes[*index].model_index);
        let node = ready.remove(0);
        ordered.push(view.nodes[node].clone());
        let from = view.nodes[node].index;
        for next in graph.graph.neighbors(from) {
            if next == from {
                continue;
            }
            let Some(compact) = view.nodes.iter().position(|item| item.index == next) else {
                continue;
            };
            if let Some(degree) = indegree.get_mut(&compact) {
                *degree = degree.saturating_sub(1);
                if *degree == 0 {
                    ready.push(compact);
                }
            }
        }
    }
    if ordered.len() != members.len() {
        let mut fallback: Vec<_> = members
            .iter()
            .map(|index| view.nodes[*index].clone())
            .collect();
        fallback.sort_by_key(|node| node.model_index);
        return fallback;
    }
    ordered
}

fn node_json(view: &View, index: usize) -> Value {
    let node = &view.nodes[index];
    json!({
        "step_id": node.step_id,
        "name": node.name,
        "score": node.score,
        "weight": 1.0 + node.score,
        "model_index": node.model_index,
    })
}

fn component_json(view: &View, parts: &[Vec<usize>]) -> Vec<Value> {
    parts
        .iter()
        .map(|part| {
            let sum: f64 = part.iter().map(|index| view.nodes[*index].score).sum();
            json!({
                "members": part.iter().map(|index| node_json(view, *index)).collect::<Vec<_>>(),
                "count": part.len(),
                "priority_sum": sum,
                "mean": sum / part.len() as f64,
            })
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocol::{load_model, Binding, PartyRole};
    use libafl_bolts::rands::StdRand;
    use serde_json::json;
    use std::collections::BTreeMap;
    use std::sync::atomic::{AtomicU64, Ordering};

    static TEMP: AtomicU64 = AtomicU64::new(0);

    fn model(steps: &[&str], deps: &[(&str, &str)]) -> IndexedModel {
        let step_json: Vec<_> = steps
            .iter()
            .enumerate()
            .map(|(index, name)| {
                json!({
                    "id": format!("s{index}"),
                    "name": name,
                    "message_name": "m",
                    "channel_name": "c",
                    "sender_name": if index % 2 == 0 { "client" } else { "server" },
                    "receiver_name": if index % 2 == 0 { "server" } else { "client" },
                    "condition": "go",
                })
            })
            .collect();
        let dep_json: Vec<_> = deps
            .iter()
            .map(|(prior, dependent)| {
                json!({
                    "prior_step": prior,
                    "dependent_step": dependent,
                    "condition": format!("{prior}->{dependent}"),
                    "field_relations": [],
                })
            })
            .collect();
        let value = json!({
            "meta": {"schema_version": "7.0.0", "protocol": "p", "complete": false, "documents": [{"doc_id": "d", "rfc": "RFC 1", "input_sha256": "a", "document_sha256": "b", "path": "doc.json"}]},
            "parties": [{"name": "client", "description": "c"}, {"name": "server", "description": "s"}],
            "messages": [{"name": "m", "associated_fields": []}],
            "channels": [{"name": "c", "description": "c", "transport": "TCP", "connection": "connection_oriented", "endpoint_party_names": ["client", "server"], "initiator_party_name": "client", "acceptor_party_name": "server", "endpoint_sources": []}],
            "steps": step_json,
            "step_dependencies": dep_json,
            "unresolved": [],
        });
        let path = std::env::temp_dir().join(format!(
            "mcn-explore-{}-{}.json",
            std::process::id(),
            TEMP.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::write(&path, serde_json::to_vec(&value).unwrap()).unwrap();
        load_model(&path).unwrap()
    }

    fn binding() -> Binding {
        let mut roles = BTreeMap::new();
        roles.insert("client".to_string(), PartyRole::Generator);
        roles.insert("server".to_string(), PartyRole::Sut);
        Binding::new(roles)
    }

    fn names(trace: &StepTrace) -> Vec<String> {
        trace.steps.iter().map(|step| step.name.clone()).collect()
    }

    #[test]
    fn single_component_needs_no_bridge() {
        let model = model(&["a", "b"], &[("a", "b")]);
        let graph = ProtocolExplorationGraph::from_model(&model);
        let mut rand = StdRand::with_seed(1);
        let report = graph.select(
            &model,
            &binding(),
            &mut rand,
            &SelectionContext::default(),
            4,
            2,
            2,
        );
        let trace = report.trace.expect("trace");
        assert_eq!(names(&trace), vec!["a".to_string(), "b".to_string()]);
    }

    #[test]
    fn two_targets_take_the_shortest_bridge() {
        let model = model(&["a", "b", "c"], &[("a", "b"), ("b", "c")]);
        let graph = ProtocolExplorationGraph::from_model(&model);
        let mut rand = StdRand::with_seed(7);
        let mut context = SelectionContext::default();
        // Force several attempts until a and c are both sampled, by rejecting shorter traces.
        let report = graph.select(&model, &binding(), &mut rand, &mut context, 20, 3, 3);
        let trace = report.trace.expect("trace");
        assert_eq!(
            names(&trace),
            vec!["a".to_string(), "b".to_string(), "c".to_string()]
        );
        assert!(report.proven);
    }

    #[test]
    fn shared_bridge_is_counted_once() {
        let model = model(
            &["a", "b", "c", "d", "m"],
            &[("a", "m"), ("b", "m"), ("c", "m"), ("d", "m")],
        );
        let graph = ProtocolExplorationGraph::from_model(&model);
        let view_steps = model.eligible(&binding());
        let view = compact_view(&graph, &view_steps);
        let picked: Vec<usize> = (0..4).collect();
        let outcome = connect(&view, &picked, 5, SEARCH_BUDGET);
        assert_eq!(outcome.extras.len(), 1);
        assert!(outcome.proven);
        assert_eq!(view.nodes[outcome.extras[0]].name, "m");
    }

    #[test]
    fn disconnected_targets_keep_the_higher_mean_not_the_higher_sum() {
        let model = model(&["solo", "x", "y", "z"], &[]);
        let mut graph = ProtocolExplorationGraph::from_model(&model);
        let ids: Vec<_> = model.steps.iter().map(|step| step.id().0).collect();
        graph
            .apply_deltas(
                "req",
                &[
                    (ids[0].clone(), 5.0, "solo".to_string()),
                    (ids[1].clone(), 2.0, "x".to_string()),
                    (ids[2].clone(), 2.0, "y".to_string()),
                    (ids[3].clone(), 2.0, "z".to_string()),
                ],
            )
            .unwrap();
        // Connect x-y-z so their total is 6 but mean is 2.
        graph
            .try_add_dependencies(&[
                DepEdge {
                    prior: "x".into(),
                    dependent: "y".into(),
                    condition: "xy".into(),
                    field_relations: vec![],
                },
                DepEdge {
                    prior: "y".into(),
                    dependent: "z".into(),
                    condition: "yz".into(),
                    field_relations: vec![],
                },
            ])
            .unwrap();
        let view_steps = model.eligible(&binding());
        let view = compact_view(&graph, &view_steps);
        let members: Vec<usize> = (0..4).collect();
        let kept = highest_mean(&view, &members);
        assert_eq!(kept.len(), 1);
        assert_eq!(view.nodes[kept[0]].name, "solo");
    }

    #[test]
    fn bridge_score_is_part_of_the_mean_and_ties_use_model_index() {
        let model = model(
            &["a", "bridge", "c", "other"],
            &[("a", "bridge"), ("bridge", "c")],
        );
        let mut graph = ProtocolExplorationGraph::from_model(&model);
        let ids: Vec<_> = model.steps.iter().map(|step| step.id().0).collect();
        graph
            .apply_deltas(
                "req",
                &[
                    (ids[1].clone(), 9.0, "bridge".into()),
                    (ids[3].clone(), 2.0, "other".into()),
                ],
            )
            .unwrap();
        let view_steps = model.eligible(&binding());
        let view = compact_view(&graph, &view_steps);
        let members = vec![0usize, 1, 2, 3];
        let kept = highest_mean(&view, &members);
        let names: Vec<_> = kept
            .iter()
            .map(|index| view.nodes[*index].name.as_str())
            .collect();
        assert!(names.contains(&"bridge"));
        assert!(!names.contains(&"other"));
    }

    #[test]
    fn duplicate_request_does_not_apply_delta_twice_and_checkpoint_rebuilds_indexes() {
        let model = model(&["a", "b"], &[("a", "b")]);
        let mut graph = ProtocolExplorationGraph::from_model(&model);
        let id = model.steps[0].id().0;
        graph
            .apply_deltas("req", &[(id.clone(), 1.5, "up".into())])
            .unwrap();
        let again = graph
            .apply_deltas("req", &[(id.clone(), 1.5, "up".into())])
            .unwrap();
        assert_eq!(again[0]["reason"], "duplicate result");
        assert_eq!(graph.score_of(&id), Some(1.5));
        let path = std::env::temp_dir().join(format!("mcn-explore-ck-{}", std::process::id()));
        graph.build = "build-1".into();
        graph.save(&path).unwrap();
        let restored = ProtocolExplorationGraph::restore(&model, &path).unwrap();
        assert_eq!(restored.score_of(&id), Some(1.5));
        assert_eq!(restored.by_id.len(), 2);
        assert!(restored.by_name.contains_key("a"));
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn existing_edges_on_a_cycle_are_idempotent_and_new_cycles_are_rejected() {
        let model = model(&["a", "b", "c", "d"], &[("a", "b"), ("b", "c"), ("c", "a")]);
        let mut graph = ProtocolExplorationGraph::from_model(&model);
        let revision = graph.revision;
        let edges = graph.edge_count();
        let added = graph
            .try_add_dependencies(&[
                DepEdge {
                    prior: "a".into(),
                    dependent: "b".into(),
                    condition: "a->b".into(),
                    field_relations: vec![],
                },
                DepEdge {
                    prior: "c".into(),
                    dependent: "a".into(),
                    condition: "c->a".into(),
                    field_relations: vec![],
                },
            ])
            .unwrap();
        assert!(added.is_empty());
        assert_eq!(graph.edge_count(), edges);
        assert_eq!(graph.revision, revision);
        // Same endpoints with different evidence stay a no-op even though
        // b already reaches a through the existing cycle.
        let parallel = graph
            .try_add_dependencies(&[DepEdge {
                prior: "a".into(),
                dependent: "b".into(),
                condition: "other evidence".into(),
                field_relations: vec![("p".into(), "d".into())],
            }])
            .unwrap();
        assert!(parallel.is_empty());
        assert_eq!(graph.edge_count(), edges);
        assert_eq!(graph.revision, revision);
        let after_parallel = graph.edge_count();
        let revision_after = graph.revision;
        // The duplicate must not be committed when a later new edge closes a cycle.
        let error = graph
            .try_add_dependencies(&[
                DepEdge {
                    prior: "a".into(),
                    dependent: "b".into(),
                    condition: "a->b".into(),
                    field_relations: vec![],
                },
                DepEdge {
                    prior: "a".into(),
                    dependent: "c".into(),
                    condition: "closes".into(),
                    field_relations: vec![],
                },
            ])
            .unwrap_err();
        assert!(error.contains("cycle"), "{error}");
        assert_eq!(graph.edge_count(), after_parallel);
        assert_eq!(graph.revision, revision_after);
        // A new edge into an unreached step is not a cycle.
        graph
            .try_add_dependencies(&[DepEdge {
                prior: "a".into(),
                dependent: "d".into(),
                condition: "out".into(),
                field_relations: vec![],
            }])
            .unwrap();
        assert_eq!(graph.edge_count(), after_parallel + 1);
    }

    #[test]
    fn new_cycle_is_rejected_and_same_endpoints_are_ignored() {
        let model = model(&["a", "b"], &[("a", "b")]);
        let mut graph = ProtocolExplorationGraph::from_model(&model);
        let error = graph
            .try_add_dependencies(&[DepEdge {
                prior: "b".into(),
                dependent: "a".into(),
                condition: "back".into(),
                field_relations: vec![],
            }])
            .unwrap_err();
        assert!(error.contains("cycle"));
        assert_eq!(graph.edge_count(), 1);
        let again = graph
            .try_add_dependencies(&[DepEdge {
                prior: "a".into(),
                dependent: "b".into(),
                condition: "other".into(),
                field_relations: vec![("p".into(), "d".into())],
            }])
            .unwrap();
        assert!(again.is_empty());
        assert_eq!(graph.edge_count(), 1);
    }

    #[test]
    fn length_and_duplicate_failures_skip_the_round() {
        let only = model(&["only"], &[]);
        let graph = ProtocolExplorationGraph::from_model(&only);
        let mut rand = StdRand::with_seed(1);
        let report = graph.select(
            &only,
            &binding(),
            &mut rand,
            &SelectionContext::default(),
            2,
            2,
            2,
        );
        assert!(report.trace.is_none());
        let model = model(&["a", "b"], &[("a", "b")]);
        let graph = ProtocolExplorationGraph::from_model(&model);
        let mut rand = StdRand::with_seed(1);
        let first = graph
            .select(
                &model,
                &binding(),
                &mut rand,
                &SelectionContext::default(),
                1,
                2,
                2,
            )
            .trace
            .unwrap();
        let mut context = SelectionContext::default();
        context.attempted.insert(first.id.0);
        let mut rand = StdRand::with_seed(1);
        let second = graph.select(&model, &binding(), &mut rand, &context, 1, 2, 2);
        assert!(second.trace.is_none());
        assert!(second
            .events
            .iter()
            .any(|event| event["kind"] == "trace_skipped"));
    }
}
