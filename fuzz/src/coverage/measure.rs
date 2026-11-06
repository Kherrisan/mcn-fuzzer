//! In-process handler and uncovered-region measurement.
//!
//! The fuzzer has already exported `coverage.json`. This module only reads
//! files and writes the sealed reports. It does not start a process.

use std::{
    collections::{BTreeMap, BTreeSet, HashMap, HashSet, VecDeque},
    fs,
    io::Read,
    path::{Path, PathBuf},
    time::Instant,
};

use flate2::read::ZlibDecoder;
use petgraph::{graph::NodeIndex, visit::Dfs, Graph};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::agents::artifacts::read_json;

pub(crate) struct MeasureRequest<'a> {
    pub snapshot: &'a Path,
    pub callgraph: &'a Path,
    pub cfg: &'a Path,
    pub manifest: &'a Path,
    pub binary: &'a Path,
    pub model: &'a Path,
    pub handlers: &'a Path,
    pub source: &'a Path,
    pub output_dir: &'a Path,
}

pub(crate) struct MeasureStats {
    pub handlers: usize,
    pub regions: usize,
    pub missed_branches: u64,
    pub functions: u64,
    pub unknown: u64,
    pub elapsed_ms: u128,
}

pub(crate) fn run(request: &MeasureRequest<'_>) -> Result<MeasureStats, String> {
    let started = Instant::now();
    let snapshot = request.snapshot;
    let output_dir = request.output_dir;
    if output_dir == snapshot
        || snapshot.starts_with(output_dir)
        || output_dir.starts_with(snapshot)
    {
        return Err("refusing to write inside the coverage snapshot".into());
    }
    let before = snapshot_digest(snapshot)?;
    let manifest = read_json(request.manifest)?;
    if file_sha256(request.binary)? != text(&manifest, "binary_sha256") {
        return Err("coverage binary does not match manifest binary_sha256".into());
    }
    let source_sha = source_digest(request.source)?;
    if source_sha != text(&manifest, "source_sha256") {
        return Err(format!(
            "source tree does not match manifest source_sha256\nmanifest {}\nsource   {source_sha}",
            text(&manifest, "source_sha256")
        ));
    }
    let (profile, profile_name) = read_profile(snapshot)?;
    let graphs = load_graphs(request.callgraph, request.cfg, &manifest)?;
    let export = read_json(&snapshot.join("coverage.json")).map_err(|_| {
        format!(
            "snapshot {} has no usable coverage.json",
            snapshot.display()
        )
    })?;
    if export.get("data").and_then(Value::as_array).is_none() {
        return Err("coverage.json is missing the llvm-cov data array".into());
    }
    let hits = hits_from_coverage(&export);
    let model = read_json(request.model)?;
    let matched = load_handlers(request.handlers, &model)?;
    let flows = handler_flows(&matched, &hits, &graphs, "llvm-cov");
    let regions = uncovered_regions(&flows);
    let profile_sha = format!("{:x}", Sha256::digest(&profile));
    let handlers_doc = json!({
        "version": 1,
        "profile_sha256": profile_sha,
        "profile_file": profile_name,
        "manifest_graphs_sha256": text(&manifest, "graphs_sha256"),
        "source_sha256": source_sha,
        "hit_source": "llvm-cov",
        "mappings": matched["mappings"],
        "unmatched": matched["unmatched"],
    });
    let regions_doc = json!({
        "version": 1,
        "profile_sha256": profile_sha,
        "hit_source": "llvm-cov",
        "handlers": regions,
    });
    fs::create_dir_all(output_dir).map_err(|error| error.to_string())?;
    atomic_json(&output_dir.join("handlers.json"), &handlers_doc)?;
    atomic_json(&output_dir.join("regions.json"), &regions_doc)?;
    if snapshot_digest(snapshot)? != before {
        return Err("snapshot changed while it was being measured".into());
    }
    let region_rows = regions_doc["handlers"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    Ok(MeasureStats {
        handlers: handlers_doc["mappings"]
            .as_array()
            .map(Vec::len)
            .unwrap_or(0),
        regions: region_rows.len(),
        missed_branches: region_rows
            .iter()
            .map(|row| {
                row.get("branch_directions")
                    .and_then(Value::as_u64)
                    .unwrap_or(0)
            })
            .sum(),
        functions: region_rows
            .iter()
            .map(|row| {
                row.get("function_count")
                    .and_then(Value::as_u64)
                    .unwrap_or(0)
            })
            .sum(),
        unknown: region_rows
            .iter()
            .map(|row| {
                row.get("unknown")
                    .and_then(Value::as_array)
                    .map(Vec::len)
                    .unwrap_or(0) as u64
            })
            .sum(),
        elapsed_ms: started.elapsed().as_millis(),
    })
}

pub(crate) fn source_digest(source_dir: &Path) -> Result<String, String> {
    if !source_dir.is_dir() {
        return Err(format!(
            "source directory {} is not a directory",
            source_dir.display()
        ));
    }
    let mut files = Vec::new();
    collect_files(source_dir, source_dir, &mut files)?;
    files.sort_by(|left, right| left.0.cmp(&right.0));
    let mut digest = Sha256::new();
    for (relative, path) in files {
        let data =
            fs::read(&path).map_err(|error| format!("reading {}: {error}", path.display()))?;
        digest.update((relative.len() as u64).to_be_bytes());
        digest.update(&relative);
        digest.update((data.len() as u64).to_be_bytes());
        digest.update(&data);
    }
    Ok(format!("{:x}", digest.finalize()))
}

fn collect_files(
    dir: &Path,
    root: &Path,
    files: &mut Vec<(Vec<u8>, PathBuf)>,
) -> Result<(), String> {
    let mut entries = fs::read_dir(dir)
        .map_err(|error| format!("reading {}: {error}", dir.display()))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| error.to_string())?;
    entries.sort_by_key(|entry| entry.file_name());
    for entry in entries {
        let path = entry.path();
        let meta = fs::symlink_metadata(&path)
            .map_err(|error| format!("reading {}: {error}", path.display()))?;
        if meta.file_type().is_symlink() {
            continue;
        }
        if meta.is_dir() {
            collect_files(&path, root, files)?;
        } else if meta.is_file() {
            let relative = path.strip_prefix(root).unwrap_or(&path);
            let posix = relative
                .components()
                .map(|component| component.as_os_str().to_string_lossy())
                .collect::<Vec<_>>()
                .join("/");
            files.push((posix.into_bytes(), path));
        }
    }
    Ok(())
}

pub(crate) fn hits_from_coverage(export: &Value) -> BTreeMap<String, Value> {
    let mut functions = BTreeMap::new();
    for item in export
        .get("data")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        for function in item
            .get("functions")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            let filenames = function.get("filenames").cloned().unwrap_or(json!([]));
            let names = filenames.as_array().cloned().unwrap_or_default();
            let mut edges = Vec::new();
            for (number, branch) in function
                .get("branches")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .enumerate()
            {
                let Some(branch) = branch.as_array() else {
                    continue;
                };
                if branch.len() < 9 {
                    continue;
                }
                let file_id = json_i64(&branch[6]).unwrap_or(-1);
                let file = usize::try_from(file_id)
                    .ok()
                    .and_then(|index| names.get(index))
                    .and_then(Value::as_str);
                for (direction, count) in [("true", &branch[4]), ("false", &branch[5])] {
                    let known = json_i64(count).is_some_and(|value| value >= 0);
                    edges.push(json!({
                        "src": format!("condition:{number}"),
                        "dst": format!("condition:{number}:{direction}"),
                        "branch": true,
                        "hits": if known { count.clone() } else { Value::Null },
                        "known": known,
                        "condition_id": number,
                        "direction": direction,
                        "file": file,
                        "range": json!(branch[..4].to_vec()),
                        "location": format!("{}:{}:{}", file.unwrap_or(""), json_i64(&branch[0]).unwrap_or(0), json_i64(&branch[1]).unwrap_or(0)),
                        "reason": if known { Value::Null } else { json!("llvm-cov branch count missing") },
                    }));
                }
            }
            functions.insert(
                function
                    .get("name")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
                json!({
                    "edges": edges,
                    "count": function.get("count").cloned().unwrap_or(Value::Null),
                    "regions": function.get("regions").cloned().unwrap_or(json!([])),
                    "filenames": filenames,
                }),
            );
        }
    }
    functions
}

pub(crate) fn load_handlers(path: &Path, model: &Value) -> Result<Value, String> {
    let mut document = read_json(path)?;
    if document.get("version").and_then(Value::as_u64) != Some(1) {
        return Err("handlers.json must have version 1".into());
    }
    let rows = document
        .get("mappings")
        .and_then(Value::as_array)
        .cloned()
        .ok_or("handlers.json must map every model message exactly once")?;
    let expected: BTreeSet<_> = model
        .get("messages")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|message| message.get("name").and_then(Value::as_str))
        .map(str::to_string)
        .collect();
    let names: Vec<_> = rows
        .iter()
        .filter_map(|row| {
            row.get("message")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .collect();
    if names.len() != names.iter().collect::<BTreeSet<_>>().len()
        || names.iter().cloned().collect::<BTreeSet<_>>() != expected
    {
        return Err("handlers.json must map every model message exactly once".into());
    }
    let mut mappings = rows;
    let steps = model.get("steps").and_then(Value::as_array);
    for row in &mut mappings {
        let handlers = row.get("handlers").and_then(Value::as_array).cloned();
        let Some(handlers) = handlers else {
            return Err("message mapping requires a handlers array".into());
        };
        for handler in &handlers {
            for key in ["symbol", "location", "evidence"] {
                if !nonempty(handler.get(key)) {
                    return Err("handler requires symbol, location and dispatch evidence".into());
                }
            }
        }
        if handlers.is_empty() && !nonempty(row.get("reason")) {
            return Err("unmatched message requires a reason".into());
        }
        let message = row.get("message").cloned();
        let matching: Vec<_> = steps
            .into_iter()
            .flatten()
            .filter(|step| step.get("message_name") == message.as_ref())
            .collect();
        row["step_ids"] = json!(matching
            .iter()
            .filter_map(|step| step.get("id"))
            .collect::<Vec<_>>());
        row["step_names"] = json!(matching
            .iter()
            .filter_map(|step| step.get("name"))
            .collect::<Vec<_>>());
        row["unmatched"] = json!(handlers.is_empty());
        row["handlers"] = json!(handlers);
    }
    let unmatched: Vec<_> = mappings
        .iter()
        .filter(|row| row.get("unmatched").and_then(Value::as_bool) == Some(true))
        .map(|row| json!({"message": row.get("message"), "reason": row.get("reason")}))
        .collect();
    document["mappings"] = json!(mappings);
    document["unmatched"] = json!(unmatched);
    Ok(document)
}

pub(crate) fn handler_flows(
    mappings: &Value,
    hits: &BTreeMap<String, Value>,
    graphs: &Value,
    hit_source: &str,
) -> Vec<Value> {
    let cfgs = graphs.get("cfgs").and_then(Value::as_object);
    let mut functions = BTreeMap::new();
    if let Some(cfgs) = cfgs {
        for (symbol, cfg) in cfgs {
            functions.insert(
                symbol.clone(),
                join_function(symbol, cfg, hits.get(symbol).unwrap_or(&Value::Null)),
            );
        }
    }
    let symbols: HashMap<String, String> = graphs
        .get("functions")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|function| {
            Some((
                function.get("node")?.as_str()?.to_string(),
                function.get("symbol")?.as_str()?.to_string(),
            ))
        })
        .collect();
    let calls: HashSet<(String, String)> = graphs
        .get("edges")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|edge| {
            let kind = edge.get("kind").and_then(Value::as_str).unwrap_or("direct");
            if kind != "direct" {
                return None;
            }
            let source = symbols.get(edge.get("source")?.as_str()?)?;
            let target = symbols.get(edge.get("target")?.as_str()?)?;
            Some((source.clone(), target.clone()))
        })
        .collect();
    let mut reports = Vec::new();
    for row in mappings
        .get("mappings")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let roots: Vec<String> = row
            .get("handlers")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(|handler| {
                handler
                    .get("symbol")
                    .and_then(Value::as_str)
                    .map(str::to_string)
            })
            .collect();
        let mut pending: VecDeque<String> = roots.iter().cloned().collect();
        let mut visited = BTreeSet::new();
        let mut selected = Vec::new();
        let mut unknown = Vec::new();
        if roots.is_empty() {
            unknown.push(json!({
                "function": Value::Null,
                "location": Value::Null,
                "reason": row.get("reason").and_then(Value::as_str).unwrap_or("no handler mapping"),
            }));
        }
        while let Some(symbol) = pending.pop_front() {
            if !visited.insert(symbol.clone()) {
                continue;
            }
            let Some(function) = functions.get(&symbol) else {
                unknown.push(json!({"function": symbol, "location": Value::Null, "reason": "no CFG for reachable function"}));
                continue;
            };
            if function.get("entry").and_then(Value::as_str).is_none() {
                unknown.push(json!({"function": symbol, "location": Value::Null, "reason": "no CFG for reachable function"}));
                continue;
            }
            let entry = function["entry"].as_str().unwrap();
            if function
                .pointer(&format!("/blocks/{entry}/state"))
                .and_then(Value::as_str)
                == Some("unknown")
            {
                unknown.push(json!({
                    "function": symbol,
                    "location": Value::Null,
                    "reason": format!("no reliable {hit_source} entry coverage for function"),
                }));
            }
            let mut adjacency: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
            for edge in function
                .get("edges")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                if let (Some(src), Some(dst)) = (
                    edge.get("src").and_then(Value::as_str),
                    edge.get("dst").and_then(Value::as_str),
                ) {
                    adjacency.entry(src).or_default().push(dst);
                }
            }
            let mut reachable = BTreeSet::new();
            let mut todo = vec![entry];
            while let Some(block) = todo.pop() {
                if !reachable.insert(block) {
                    continue;
                }
                if let Some(next) = adjacency.get(block) {
                    todo.extend(next.iter().copied());
                }
            }
            let mut kept_blocks = Map::new();
            if let Some(blocks) = function.get("blocks").and_then(Value::as_object) {
                for (name, block) in blocks {
                    if reachable.contains(name.as_str()) {
                        kept_blocks.insert(name.clone(), block.clone());
                    }
                }
            }
            let kept_edges: Vec<_> = function
                .get("edges")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter(|edge| {
                    edge.get("src")
                        .and_then(Value::as_str)
                        .is_some_and(|src| reachable.contains(src))
                })
                .cloned()
                .collect();
            let mut calls_here = Vec::new();
            for (name, block) in &kept_blocks {
                for call in block
                    .get("calls")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                {
                    let target = call.get("target").and_then(Value::as_str);
                    let location = call.get("location").cloned().unwrap_or(Value::Null);
                    if let Some(target) = target {
                        if calls.contains(&(symbol.clone(), target.to_string()))
                            && functions.contains_key(target)
                        {
                            calls_here
                                .push(json!({"src": name, "target": target, "location": location}));
                            pending.push_back(target.to_string());
                        } else {
                            unknown.push(json!({
                                "function": symbol,
                                "location": location,
                                "reason": format!("unresolved, external, or unindexed call: {target}"),
                            }));
                        }
                    } else {
                        unknown.push(json!({
                            "function": symbol,
                            "location": location,
                            "reason": "unresolved, external, or unindexed call: indirect",
                        }));
                    }
                }
            }
            let mut selected_function = function.clone();
            selected_function["blocks"] = json!(kept_blocks);
            selected_function["edges"] = json!(kept_edges);
            selected_function["calls"] = json!(calls_here);
            selected.push(selected_function);
        }
        reports.push(json!({
            "id": row.get("message"),
            "functions": selected,
            "unknown": unknown,
            "roots": roots,
        }));
    }
    reports
}

pub(crate) fn uncovered_regions(handlers: &[Value]) -> Vec<Value> {
    let mut reports = Vec::new();
    for handler in handlers {
        let function_rows = handler
            .get("functions")
            .and_then(Value::as_array)
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        let mut graph = Graph::<RegionNode, (), petgraph::Undirected>::new_undirected();
        let mut index = HashMap::new();
        let mut unknown = handler
            .get("unknown")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let mut branch_records = HashMap::new();
        let mut root_nodes = HashSet::new();
        let roots: HashSet<_> = handler
            .get("roots")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(|item| item.as_str())
            .collect();
        let add = |graph: &mut Graph<RegionNode, (), petgraph::Undirected>,
                   index: &mut HashMap<RegionNode, NodeIndex>,
                   node: RegionNode| {
            *index
                .entry(node.clone())
                .or_insert_with(|| graph.add_node(node))
        };
        for function in function_rows {
            let symbol = function.get("id").and_then(Value::as_str).unwrap_or("");
            let blocks = function.get("blocks").and_then(Value::as_object);
            if let Some(blocks) = blocks {
                for (name, block) in blocks {
                    if block.get("state").and_then(Value::as_str) == Some("miss") {
                        add(
                            &mut graph,
                            &mut index,
                            RegionNode::Block(symbol.into(), name.clone()),
                        );
                    }
                }
            }
            if roots.contains(symbol) {
                if let Some(entry) = function.get("entry").and_then(Value::as_str) {
                    root_nodes.insert(RegionNode::Block(symbol.into(), entry.into()));
                }
            }
            for edge in function
                .get("unknown")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                unknown.push(json!({
                    "function": symbol,
                    "location": edge.get("location"),
                    "reason": edge.get("reason").and_then(Value::as_str).unwrap_or("unknown source mapping"),
                }));
            }
            for (number, edge) in function
                .get("edges")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .enumerate()
            {
                let src_name = edge.get("src").and_then(Value::as_str).unwrap_or("");
                let dst_name = edge.get("dst").and_then(Value::as_str).unwrap_or("");
                let src = RegionNode::Block(symbol.into(), src_name.into());
                let dst = RegionNode::Block(symbol.into(), dst_name.into());
                if edge.get("branch").and_then(Value::as_bool) != Some(true) {
                    if index.contains_key(&src) && index.contains_key(&dst) {
                        let left = index[&src];
                        let right = index[&dst];
                        graph.add_edge(left, right, ());
                    } else if index.contains_key(&src)
                        && blocks_state(function, dst_name) != Some("hit")
                    {
                        unknown.push(json!({
                            "function": symbol,
                            "location": edge.get("location"),
                            "reason": "unknown successor block coverage",
                        }));
                    }
                    continue;
                }
                match edge_state(edge) {
                    "unknown" => unknown.push(json!({
                        "function": symbol,
                        "location": edge.get("location"),
                        "reason": edge.get("reason").and_then(Value::as_str).unwrap_or("unknown branch coverage"),
                    })),
                    "miss" => {
                        let node = RegionNode::Branch(symbol.into(), number);
                        let branch = add(&mut graph, &mut index, node.clone());
                        branch_records.insert(node, json!({
                            "function": symbol,
                            "src": edge.get("src"),
                            "dst": edge.get("dst"),
                            "location": edge.get("location"),
                            "direction": edge.get("direction").cloned().unwrap_or(Value::Null),
                            "hits": 0,
                            "source_counts": edge.get("source_counts").cloned().unwrap_or(Value::Null),
                        }));
                        if let Some(left) = index.get(&src) {
                            graph.add_edge(*left, branch, ());
                        }
                        if let Some(right) = index.get(&dst) {
                            graph.add_edge(branch, *right, ());
                        } else if blocks_state(function, dst_name) != Some("hit") {
                            unknown.push(json!({
                                "function": symbol,
                                "location": edge.get("location"),
                                "reason": "unknown successor block coverage",
                            }));
                        }
                    }
                    _ => {}
                }
            }
        }
        for function in function_rows {
            let symbol = function.get("id").and_then(Value::as_str).unwrap_or("");
            for call in function
                .get("calls")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                let src_name = call.get("src").and_then(Value::as_str).unwrap_or("");
                let target = call.get("target").and_then(Value::as_str).unwrap_or("");
                let src = RegionNode::Block(symbol.into(), src_name.into());
                let callee = function_rows
                    .iter()
                    .find(|item| item.get("id").and_then(Value::as_str) == Some(target));
                let entry = callee
                    .and_then(|item| item.get("entry").and_then(Value::as_str))
                    .unwrap_or("");
                let dst = RegionNode::Block(target.into(), entry.into());
                if index.contains_key(&src) && index.contains_key(&dst) {
                    graph.add_edge(index[&src], index[&dst], ());
                } else if index.contains_key(&src) {
                    let callee_state = callee.and_then(|item| blocks_state(item, entry));
                    if callee_state != Some("hit") {
                        unknown.push(json!({
                            "function": symbol,
                            "location": call.get("location"),
                            "reason": format!("unknown callee entry coverage: {target}"),
                        }));
                    }
                }
            }
        }
        let mut regions = Vec::new();
        let mut seen_functions = BTreeSet::new();
        for component in components(&graph) {
            let branches: Vec<_> = component
                .iter()
                .filter_map(|node| branch_records.get(&graph[*node]).cloned())
                .collect();
            let rooted = component
                .iter()
                .any(|node| root_nodes.contains(&graph[*node]));
            if branches.is_empty() && !rooted {
                continue;
            }
            let names: BTreeSet<_> = component
                .iter()
                .map(|node| graph[*node].function())
                .collect();
            seen_functions.extend(names.iter().cloned());
            regions.push(json!({
                "branch_directions": branches.len(),
                "functions": names.iter().cloned().collect::<Vec<_>>(),
                "function_count": names.len(),
                "locations": branches.iter().map(|branch| branch.get("location").cloned().unwrap_or(Value::Null)).collect::<Vec<_>>(),
                "branches": branches,
            }));
        }
        let mut deduplicated = Vec::new();
        let mut seen = BTreeSet::new();
        for item in unknown {
            let key = canonical(&item);
            if seen.insert(key) {
                deduplicated.push(item);
            }
        }
        reports.push(json!({
            "handler": handler.get("id"),
            "regions": regions,
            "branch_directions": 0,
            "function_count": seen_functions.len(),
            "unknown": deduplicated,
            "zero": true,
            "complete": true,
        }));
        let last = reports.last_mut().unwrap();
        let branch_directions: u64 = last["regions"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|region| {
                region
                    .get("branch_directions")
                    .and_then(Value::as_u64)
                    .unwrap_or(0)
            })
            .sum();
        last["branch_directions"] = json!(branch_directions);
        last["zero"] = json!(last["regions"]
            .as_array()
            .is_none_or(|items| items.is_empty()));
        last["complete"] = json!(last["unknown"]
            .as_array()
            .is_none_or(|items| items.is_empty()));
    }
    reports
}

#[derive(Clone, PartialEq, Eq, Hash)]
enum RegionNode {
    Block(String, String),
    Branch(String, usize),
}

impl RegionNode {
    fn function(&self) -> String {
        match self {
            Self::Block(function, _) | Self::Branch(function, _) => function.clone(),
        }
    }
}

fn components(graph: &Graph<RegionNode, (), petgraph::Undirected>) -> Vec<Vec<NodeIndex>> {
    let mut visited = HashSet::new();
    let mut groups = Vec::new();
    for start in graph.node_indices() {
        if visited.contains(&start) {
            continue;
        }
        let mut dfs = Dfs::new(graph, start);
        let mut group = Vec::new();
        while let Some(node) = dfs.next(graph) {
            if visited.insert(node) {
                group.push(node);
            }
        }
        if !group.is_empty() {
            groups.push(group);
        }
    }
    groups
}

fn edge_state(edge: &Value) -> &'static str {
    if edge.get("known").and_then(Value::as_bool) == Some(false) {
        return "unknown";
    }
    if let Some(state) = edge.get("state").and_then(Value::as_str) {
        return match state {
            "miss" => "miss",
            "hit" => "hit",
            _ => "unknown",
        };
    }
    match json_i64(edge.get("hits").unwrap_or(&Value::Null)) {
        Some(0) => "miss",
        Some(value) if value > 0 => "hit",
        _ => "unknown",
    }
}

fn blocks_state<'a>(function: &'a Value, name: &str) -> Option<&'a str> {
    function
        .get("blocks")
        .and_then(Value::as_object)
        .and_then(|blocks| blocks.get(name))
        .and_then(|block| block.get("state"))
        .and_then(Value::as_str)
}

fn join_function(symbol: &str, cfg: &Value, recorded: &Value) -> Value {
    let mut blocks = Map::new();
    if let Some(source) = cfg.get("blocks").and_then(Value::as_object) {
        for (name, block) in source {
            let mut owned = block.clone();
            owned["state"] = json!(block_state(block, recorded));
            blocks.insert(name.clone(), owned);
        }
    }
    if let Some(entry) = cfg.get("entry").and_then(Value::as_str) {
        if let Some(block) = blocks.get_mut(entry) {
            if let Some(count) = json_i64(recorded.get("count").unwrap_or(&Value::Null)) {
                if count >= 0 {
                    block["state"] = json!(if count == 0 { "miss" } else { "hit" });
                }
            }
        }
    }
    let mut outgoing: BTreeMap<&str, Vec<&Value>> = BTreeMap::new();
    for edge in cfg
        .get("edges")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        if let Some(src) = edge.get("src").and_then(Value::as_str) {
            outgoing.entry(src).or_default().push(edge);
        }
    }
    let raw = recorded.get("edges").and_then(Value::as_array);
    let mut explicit = HashMap::new();
    let mut conditions: BTreeMap<i64, Vec<&Value>> = BTreeMap::new();
    for edge in raw.into_iter().flatten() {
        if edge.get("condition_id").is_some() {
            if let Some(number) = json_i64(edge.get("condition_id").unwrap_or(&Value::Null)) {
                conditions.entry(number).or_default().push(edge);
            }
        } else if let (Some(src), Some(dst)) = (
            edge.get("src").and_then(Value::as_str),
            edge.get("dst").and_then(Value::as_str),
        ) {
            explicit.insert((src.to_string(), dst.to_string()), edge.clone());
        }
    }
    let mut matches: BTreeMap<i64, Vec<String>> = BTreeMap::new();
    for (name, block) in &blocks {
        let Some(location) = block.get("condition") else {
            continue;
        };
        if outgoing.get(name.as_str()).map(Vec::len) != Some(2) {
            continue;
        }
        let mut candidates = Vec::new();
        for (number, records) in &conditions {
            let Some(record) = records.first() else {
                continue;
            };
            let range = record.get("range").and_then(Value::as_array);
            let point = (
                json_i64(location.get("line").unwrap_or(&Value::Null)).unwrap_or(0),
                json_i64(location.get("column").unwrap_or(&Value::Null)).unwrap_or(0),
            );
            if same_file(
                record.get("file").and_then(Value::as_str),
                location.get("file").and_then(Value::as_str),
            ) && range.is_some_and(|range| contains_range(range, point))
            {
                candidates.push(*number);
            }
        }
        if candidates.is_empty() {
            continue;
        }
        let rank = |number: i64| {
            let range = conditions[&number][0]
                .get("range")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            (
                json_i64(range.get(2).unwrap_or(&Value::Null)).unwrap_or(0)
                    - json_i64(range.get(0).unwrap_or(&Value::Null)).unwrap_or(0),
                json_i64(range.get(3).unwrap_or(&Value::Null)).unwrap_or(0)
                    - json_i64(range.get(1).unwrap_or(&Value::Null)).unwrap_or(0),
            )
        };
        let best = candidates.iter().copied().map(rank).min().unwrap();
        candidates.retain(|number| rank(*number) == best);
        if candidates.len() == 1 {
            matches.entry(candidates[0]).or_default().push(name.clone());
        }
    }
    let mut consumed = BTreeSet::new();
    let mut mapped = HashMap::new();
    for (number, names) in &matches {
        let records = &conditions[number];
        if names.len() != 1
            || records.len() != 2
            || !records
                .iter()
                .all(|record| record.get("known").and_then(Value::as_bool) == Some(true))
        {
            continue;
        }
        let name = &names[0];
        let successors = outgoing.get(name.as_str()).cloned().unwrap_or_default();
        let mut counts = BTreeMap::new();
        for record in records {
            if let Some(direction) = record.get("direction").and_then(Value::as_str) {
                counts.insert(
                    direction.to_string(),
                    json_i64(record.get("hits").unwrap_or(&Value::Null)).unwrap_or(0),
                );
            }
        }
        let values: Vec<_> = counts.values().copied().collect();
        let assignments: Option<Vec<(&str, Option<i64>, Option<String>)>> = if values
            .iter()
            .all(|value| *value == 0)
        {
            Some(vec![("miss", Some(0), None), ("miss", Some(0), None)])
        } else if values.iter().all(|value| *value > 0) {
            Some(vec![("hit", None, None), ("hit", None, None)])
        } else {
            let states: Vec<_> = successors
                .iter()
                .map(|edge| {
                    blocks
                        .get(edge.get("dst").and_then(Value::as_str).unwrap_or(""))
                        .and_then(|block| block.get("state"))
                        .and_then(Value::as_str)
                        .unwrap_or("unknown")
                })
                .collect();
            if states.iter().copied().collect::<BTreeSet<_>>() != BTreeSet::from(["hit", "miss"]) {
                None
            } else {
                let zero = counts
                    .iter()
                    .find(|(_, value)| **value == 0)
                    .map(|(key, _)| key.clone());
                let positive = counts
                    .iter()
                    .find(|(_, value)| **value > 0)
                    .map(|(key, value)| (key.clone(), *value));
                Some(
                    states
                        .into_iter()
                        .map(|state| {
                            if state == "miss" {
                                ("miss", Some(0), zero.clone())
                            } else {
                                (
                                    "hit",
                                    positive.as_ref().map(|(_, value)| *value),
                                    positive.as_ref().map(|(key, _)| key.clone()),
                                )
                            }
                        })
                        .collect(),
                )
            }
        };
        let Some(assignments) = assignments else {
            continue;
        };
        if assignments.len() != successors.len() {
            continue;
        }
        consumed.insert(*number);
        for (edge, (state, count, direction)) in successors.into_iter().zip(assignments) {
            let dst = edge.get("dst").and_then(Value::as_str).unwrap_or("");
            let mut owned = (*edge).clone();
            owned["branch"] = json!(true);
            owned["known"] = json!(true);
            owned["hits"] = count.map(|value| json!(value)).unwrap_or(Value::Null);
            owned["state"] = json!(state);
            owned["direction"] = direction.map(|value| json!(value)).unwrap_or(Value::Null);
            owned["source_counts"] = json!(counts);
            owned["location"] = records[0].get("location").cloned().unwrap_or(Value::Null);
            mapped.insert((name.clone(), dst.to_string()), owned);
        }
    }
    let mut edges = Vec::new();
    let mut unknown = Vec::new();
    let cfg_edges: HashSet<(String, String)> = cfg
        .get("edges")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|edge| {
            Some((
                edge.get("src")?.as_str()?.to_string(),
                edge.get("dst")?.as_str()?.to_string(),
            ))
        })
        .collect();
    for (src, successors) in &outgoing {
        for edge in successors {
            let dst = edge.get("dst").and_then(Value::as_str).unwrap_or("");
            let key = ((*src).to_string(), dst.to_string());
            if let Some(found) = explicit.get(&key) {
                let mut owned = (*edge).clone();
                if let Value::Object(extra) = found {
                    if let Value::Object(map) = &mut owned {
                        for (name, value) in extra {
                            map.insert(name.clone(), value.clone());
                        }
                    }
                }
                edges.push(owned);
            } else if let Some(found) = mapped.get(&(src.to_string(), dst.to_string())) {
                edges.push(found.clone());
            } else if successors.len() == 1 {
                let mut owned = (*edge).clone();
                owned["branch"] = json!(false);
                owned["known"] = json!(true);
                edges.push(owned);
            } else {
                let mut owned = (*edge).clone();
                owned["branch"] = json!(true);
                owned["known"] = json!(false);
                owned["reason"] = json!("CFG branch has no unambiguous source-counter mapping");
                edges.push(owned);
            }
        }
    }
    for edge in raw.into_iter().flatten() {
        if let Some(number) = json_i64(edge.get("condition_id").unwrap_or(&Value::Null)) {
            if !consumed.contains(&number) {
                let mut owned = edge.clone();
                if owned.get("reason").and_then(Value::as_str).is_none() {
                    owned["reason"] =
                        json!("source branch cannot be mapped uniquely to CFG successors");
                }
                unknown.push(owned);
            }
        } else if let (Some(src), Some(dst)) = (
            edge.get("src").and_then(Value::as_str),
            edge.get("dst").and_then(Value::as_str),
        ) {
            if !cfg_edges.contains(&(src.to_string(), dst.to_string())) {
                let mut owned = edge.clone();
                if owned.get("reason").and_then(Value::as_str).is_none() {
                    owned["reason"] = json!("profile edge is absent from CFG");
                }
                unknown.push(owned);
            }
        }
    }
    json!({
        "id": symbol,
        "entry": cfg.get("entry").cloned().unwrap_or(Value::Null),
        "blocks": blocks,
        "edges": edges,
        "unknown": unknown,
    })
}

fn block_state(block: &Value, recorded: &Value) -> &'static str {
    if json_i64(recorded.get("count").unwrap_or(&Value::Null)) == Some(0) {
        return "miss";
    }
    let filenames = recorded.get("filenames").and_then(Value::as_array);
    let mut states = Vec::new();
    for location in block
        .get("locations")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let point = (
            json_i64(location.get("line").unwrap_or(&Value::Null)).unwrap_or(0),
            json_i64(location.get("column").unwrap_or(&Value::Null)).unwrap_or(0),
        );
        let mut candidates = Vec::new();
        for region in recorded
            .get("regions")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            let Some(region) = region.as_array() else {
                continue;
            };
            if region.len() < 8 || json_i64(&region[7]) != Some(0) {
                continue;
            }
            let file_id = usize::try_from(json_i64(&region[5]).unwrap_or(-1)).ok();
            let file = file_id.and_then(|index| {
                filenames
                    .and_then(|names| names.get(index))
                    .and_then(Value::as_str)
            });
            if same_file(location.get("file").and_then(Value::as_str), file)
                && contains_range(region, point)
                && json_i64(&region[4]).is_some()
            {
                candidates.push(region);
            }
        }
        if candidates.is_empty() {
            continue;
        }
        let rank = |region: &&Vec<Value>| {
            (
                json_i64(&region[2]).unwrap_or(0) - json_i64(&region[0]).unwrap_or(0),
                json_i64(&region[3]).unwrap_or(0) - json_i64(&region[1]).unwrap_or(0),
            )
        };
        let best = candidates.iter().map(rank).min().unwrap();
        let counts: BTreeSet<_> = candidates
            .iter()
            .filter(|region| rank(region) == best)
            .filter_map(|region| json_i64(&region[4]))
            .collect();
        if counts == BTreeSet::from([0]) {
            states.push("miss");
        } else if counts.iter().next().is_some_and(|count| *count > 0)
            && counts.iter().all(|count| *count > 0)
        {
            states.push("hit");
        } else {
            states.push("unknown");
        }
    }
    if states.contains(&"hit") {
        "hit"
    } else if !states.is_empty() && states.iter().all(|state| *state == "miss") {
        "miss"
    } else {
        "unknown"
    }
}

fn contains_range(region: &[Value], point: (i64, i64)) -> bool {
    if region.len() < 4 {
        return false;
    }
    let start = (
        json_i64(&region[0]).unwrap_or(0),
        json_i64(&region[1]).unwrap_or(0),
    );
    let end = (
        json_i64(&region[2]).unwrap_or(0),
        json_i64(&region[3]).unwrap_or(0),
    );
    start <= point && point < end
}

fn same_file(left: Option<&str>, right: Option<&str>) -> bool {
    match (left, right) {
        (Some(left), Some(right)) if !left.is_empty() && !right.is_empty() => {
            Path::new(left) == Path::new(right)
        }
        _ => false,
    }
}

fn load_graphs(callgraph: &Path, cfg: &Path, manifest: &Value) -> Result<Value, String> {
    let calls = read_json(callgraph)?;
    let cfgs = read_json(cfg)?;
    for (name, document) in [("callgraph.json", &calls), ("cfg.json", &cfgs)] {
        if document.get("version").and_then(Value::as_u64) != Some(1) {
            return Err(format!("{name} must have version 1"));
        }
        for field in ["source_sha256", "graphs_sha256"] {
            if document.get(field) != manifest.get(field) {
                return Err(format!("{name} {field} does not match the build manifest"));
            }
        }
    }
    if !calls.get("functions").is_some_and(Value::is_array)
        || !calls.get("edges").is_some_and(Value::is_array)
    {
        return Err("callgraph.json requires functions and edges arrays".into());
    }
    let Some(functions) = cfgs.get("functions").filter(|value| value.is_object()) else {
        return Err("cfg.json requires a functions object".into());
    };
    Ok(json!({
        "functions": calls.get("functions"),
        "edges": calls.get("edges"),
        "cfgs": functions,
    }))
}

fn read_profile(snapshot: &Path) -> Result<(Vec<u8>, &'static str), String> {
    let zlib_path = snapshot.join("coverage.profdata.zlib");
    let raw_path = snapshot.join("coverage.profdata");
    if zlib_path.is_file() {
        let compressed = fs::read(&zlib_path).map_err(|error| error.to_string())?;
        let mut decoder = ZlibDecoder::new(compressed.as_slice());
        let mut profile = Vec::new();
        decoder
            .read_to_end(&mut profile)
            .map_err(|error| format!("coverage profile: {error}"))?;
        return Ok((profile, "coverage.profdata.zlib"));
    }
    if raw_path.is_file() {
        return Ok((
            fs::read(&raw_path).map_err(|error| error.to_string())?,
            "coverage.profdata",
        ));
    }
    Err(format!(
        "snapshot {} has no coverage profile",
        snapshot.display()
    ))
}

fn snapshot_digest(snapshot: &Path) -> Result<String, String> {
    let mut files = Vec::new();
    collect_files(snapshot, snapshot, &mut files)?;
    files.sort_by(|left, right| left.0.cmp(&right.0));
    let mut digest = Sha256::new();
    for (relative, path) in files {
        let data = fs::read(&path).map_err(|error| error.to_string())?;
        digest.update((relative.len() as u64).to_be_bytes());
        digest.update(&relative);
        digest.update((data.len() as u64).to_be_bytes());
        digest.update(&data);
    }
    Ok(format!("{:x}", digest.finalize()))
}

fn atomic_json(path: &Path, value: &Value) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    }
    let temporary = path.with_extension("json.tmp");
    fs::write(
        &temporary,
        serde_json::to_vec_pretty(value).map_err(|error| error.to_string())?,
    )
    .map_err(|error| error.to_string())?;
    fs::rename(&temporary, path).map_err(|error| error.to_string())
}

fn file_sha256(path: &Path) -> Result<String, String> {
    let bytes = fs::read(path).map_err(|error| format!("reading {}: {error}", path.display()))?;
    Ok(format!("{:x}", Sha256::digest(bytes)))
}

fn text(value: &Value, field: &str) -> String {
    value
        .get(field)
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string()
}

fn nonempty(value: Option<&Value>) -> bool {
    value
        .and_then(Value::as_str)
        .is_some_and(|text| !text.trim().is_empty())
}

fn json_i64(value: &Value) -> Option<i64> {
    value
        .as_i64()
        .or_else(|| value.as_u64().and_then(|number| i64::try_from(number).ok()))
}

fn canonical(value: &Value) -> String {
    match value {
        Value::Object(map) => {
            let mut keys: Vec<_> = map.keys().collect();
            keys.sort();
            let body = keys
                .into_iter()
                .map(|key| format!("{key}:{}", canonical(&map[key])))
                .collect::<Vec<_>>()
                .join(",");
            format!("{{{body}}}")
        }
        Value::Array(items) => {
            let body = items.iter().map(canonical).collect::<Vec<_>>().join(",");
            format!("[{body}]")
        }
        other => other.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;
    use std::path::PathBuf;

    const FILE: &str = "/src/server.c";

    fn block(name: &str, line: i64, condition: Option<i64>, calls: Value) -> Value {
        let mut result = json!({
            "id": name,
            "locations": [{"file": FILE, "line": line, "column": 2}],
            "calls": calls,
        });
        if let Some(condition) = condition {
            result["condition"] = json!({"file": FILE, "line": condition, "column": 2});
        }
        result
    }

    fn exported(count: i64) -> Value {
        json!({"data": [{"functions": [{
            "name": "f",
            "count": count,
            "filenames": [FILE],
            "regions": [[1, 1, 10, 1, count, 0, 0, 0]],
            "branches": [[1, 1, 1, 4, 0, if count == 0 { 0 } else { 7 }, 0, 0, 4]]
        }]}]})
    }

    fn index(cfgs: Value, calls: &[(&str, &str)]) -> Value {
        let mut symbols: BTreeSet<&str> = calls.iter().flat_map(|pair| [pair.0, pair.1]).collect();
        if let Some(map) = cfgs.as_object() {
            symbols.extend(map.keys().map(String::as_str));
        }
        json!({
            "functions": symbols.iter().map(|symbol| json!({"node": symbol, "symbol": symbol})).collect::<Vec<_>>(),
            "edges": calls.iter().map(|pair| json!({"source": pair.0, "target": pair.1})).collect::<Vec<_>>(),
            "cfgs": cfgs,
        })
    }

    fn report(cfgs: Value, hits: BTreeMap<String, Value>, calls: &[(&str, &str)]) -> Value {
        let mappings = json!({"mappings": [{"message": "REQUEST", "handlers": [{"symbol": "f"}]}]});
        let graphs = index(cfgs, calls);
        uncovered_regions(&handler_flows(&mappings, &hits, &graphs, "llvm-cov"))
            .into_iter()
            .next()
            .unwrap()
    }

    #[test]
    fn true_false_counts_are_independent_and_code_regions_are_not_branches() {
        let mut data = exported(7);
        data["data"][0]["functions"][0]["branches"]
            .as_array_mut()
            .unwrap()
            .push(json!([4, 1, 4, 5, null, 2, 0, 0, 4]));
        let edges = hits_from_coverage(&data).remove("f").unwrap()["edges"]
            .as_array()
            .unwrap()
            .clone();
        assert_eq!(edges[0]["direction"], "true");
        assert_eq!(edges[0]["hits"], 0);
        assert_eq!(edges[1]["direction"], "false");
        assert_eq!(edges[1]["hits"], 7);
        assert_eq!(edges.len(), 4);
        assert_eq!(edges[2]["known"], false);
        assert_eq!(edges[3]["known"], true);
    }

    #[test]
    fn successor_coverage_assigns_the_zero_direction_without_using_edge_order() {
        let mut data = exported(7);
        data["data"][0]["functions"][0]["regions"]
            .as_array_mut()
            .unwrap()
            .extend([
                json!([2, 1, 2, 9, 0, 0, 0, 0]),
                json!([3, 1, 3, 9, 7, 0, 0, 0]),
            ]);
        let cfg = json!({
            "entry": "entry",
            "blocks": {
                "entry": block("entry", 1, Some(1), json!([])),
                "covered": block("covered", 3, None, json!([])),
                "missed": block("missed", 2, None, json!([])),
            },
            "edges": [{"src": "entry", "dst": "covered"}, {"src": "entry", "dst": "missed"}]
        });
        let value = report(json!({"f": cfg}), hits_from_coverage(&data), &[]);
        let branches = &value["regions"][0]["branches"];
        assert_eq!(value["branch_directions"], 1);
        assert_eq!(branches[0]["dst"], "missed");
        assert_eq!(branches[0]["direction"], "true");
        assert_eq!(branches[0]["source_counts"]["true"], 0);
        assert_eq!(branches[0]["source_counts"]["false"], 7);
    }

    #[test]
    fn ambiguous_successor_coverage_stays_unknown() {
        let cfg = json!({
            "entry": "entry",
            "blocks": {
                "entry": block("entry", 1, Some(1), json!([])),
                "a": block("a", 2, None, json!([])),
                "b": block("b", 3, None, json!([])),
            },
            "edges": [{"src": "entry", "dst": "a"}, {"src": "entry", "dst": "b"}]
        });
        let value = report(json!({"f": cfg}), hits_from_coverage(&exported(7)), &[]);
        assert_eq!(value["branch_directions"], 0);
        assert_eq!(value["complete"], false);
        assert!(value["unknown"].as_array().unwrap().len() > 0);
    }

    #[test]
    fn zero_function_follows_calls_and_counts_a_shared_callee_once() {
        let cfgs = json!({
            "f": {"entry": "entry", "blocks": {
                "entry": block("entry", 1, Some(1), json!([])),
                "a": block("a", 2, None, json!([{"target": "g", "location": null}])),
                "b": block("b", 3, None, json!([])),
            }, "edges": [{"src": "entry", "dst": "a"}, {"src": "entry", "dst": "b"}]},
            "g": {"entry": "entry", "blocks": {
                "entry": block("entry", 1, None, json!([{"target": "f", "location": null}])),
            }, "edges": []},
        });
        let mut hits = hits_from_coverage(&exported(0));
        hits.insert(
            "g".into(),
            json!({"count": 0, "edges": [], "filenames": [], "regions": []}),
        );
        let value = report(cfgs, hits, &[("f", "g"), ("g", "f")]);
        assert_eq!(value["branch_directions"], 2);
        assert_eq!(value["regions"].as_array().unwrap().len(), 1);
        assert_eq!(value["function_count"], 2);
        assert_eq!(value["regions"][0]["functions"], json!(["f", "g"]));
    }

    #[test]
    fn covered_callee_is_outside_the_zero_region() {
        let cfgs = json!({
            "f": {"entry": "entry", "blocks": {"entry": block("entry", 1, None, json!([{"target": "g", "location": null}]))}, "edges": []},
            "g": {"entry": "entry", "blocks": {"entry": block("entry", 1, None, json!([]))}, "edges": []},
        });
        let hits = BTreeMap::from([
            ("f".into(), json!({"count": 0})),
            (
                "g".into(),
                json!({"count": 9, "filenames": [FILE], "regions": [[1, 1, 10, 1, 9, 0, 0, 0]]}),
            ),
        ]);
        let value = report(cfgs, hits, &[("f", "g")]);
        assert_eq!(value["function_count"], 1);
        assert_eq!(value["regions"][0]["functions"], json!(["f"]));
    }

    #[test]
    fn external_or_indirect_calls_are_boundaries() {
        let cfg = json!({"entry": "entry", "blocks": {"entry": block("entry", 1, None, json!([{"target": null, "location": null}]))}, "edges": []});
        let value = report(
            json!({"f": cfg}),
            BTreeMap::from([("f".into(), json!({"count": 0}))]),
            &[],
        );
        assert_eq!(value["complete"], false);
        assert!(value["unknown"]
            .as_array()
            .unwrap()
            .iter()
            .any(|item| item["reason"].as_str().unwrap_or("").contains("indirect")));
    }

    #[test]
    fn missing_profile_is_unknown_rather_than_full_coverage() {
        let cfg = json!({"entry": "entry", "blocks": {"entry": block("entry", 1, None, json!([]))}, "edges": []});
        let value = report(json!({"f": cfg}), BTreeMap::new(), &[]);
        assert_eq!(value["zero"], true);
        assert_eq!(value["complete"], false);
        assert!(value["unknown"]
            .as_array()
            .unwrap()
            .iter()
            .any(|item| item["reason"]
                .as_str()
                .unwrap_or("")
                .contains("entry coverage")));
    }

    #[test]
    fn adjacent_misses_merge_and_unknown_edges_split_regions() {
        let functions = json!([{
            "id": "f",
            "blocks": {"a": {"state": "miss"}, "b": {"state": "miss"}, "c": {"state": "miss"}, "h": {"state": "hit"}, "d": {"state": "miss"}, "end": {"state": "miss"}},
            "edges": [
                {"src": "a", "dst": "b", "branch": true, "hits": 0, "known": true},
                {"src": "b", "dst": "c", "branch": true, "hits": 0, "known": true},
                {"src": "c", "dst": "h", "branch": false},
                {"src": "h", "dst": "d", "branch": false},
                {"src": "d", "dst": "end", "branch": true, "hits": 0, "known": true}
            ]
        }, {
            "id": "g",
            "blocks": {"d": {"state": "miss"}, "end": {"state": "miss"}},
            "edges": [{"src": "d", "dst": "end", "branch": true, "hits": 0, "known": true}]
        }]);
        let value = &uncovered_regions(&[json!({"id": "REQUEST", "functions": functions})])[0];
        let mut counts: Vec<_> = value["regions"]
            .as_array()
            .unwrap()
            .iter()
            .map(|region| region["branch_directions"].as_u64().unwrap())
            .collect();
        counts.sort_unstable();
        assert_eq!(counts, vec![1, 1, 2]);
        assert_eq!(value["branch_directions"], 4);
        assert_eq!(value["function_count"], 2);
        let split = json!([{
            "id": "f",
            "blocks": {"a": {"state": "miss"}, "b": {"state": "miss"}, "c": {"state": "miss"}, "d": {"state": "miss"}},
            "edges": [
                {"src": "a", "dst": "b", "branch": true, "known": true, "hits": 0},
                {"src": "b", "dst": "c", "branch": true, "known": false, "reason": "unmapped"},
                {"src": "c", "dst": "d", "branch": true, "known": true, "hits": 0}
            ]
        }]);
        let split = &uncovered_regions(&[json!({"id": "REQUEST", "functions": split})])[0];
        assert_eq!(split["regions"].as_array().unwrap().len(), 2);
        assert_eq!(split["complete"], false);
    }

    #[test]
    fn source_digest_is_stable_and_changes_with_file_bytes() {
        let dir = std::env::temp_dir().join(format!("mcn-source-digest-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join("nested")).unwrap();
        fs::write(dir.join("b.c"), b"b").unwrap();
        fs::write(dir.join("nested").join("a.c"), b"a").unwrap();
        let first = source_digest(&dir).unwrap();
        let other =
            std::env::temp_dir().join(format!("mcn-source-digest-other-{}", std::process::id()));
        let _ = fs::remove_dir_all(&other);
        fs::create_dir_all(other.join("nested")).unwrap();
        fs::write(other.join("nested").join("a.c"), b"a").unwrap();
        fs::write(other.join("b.c"), b"b").unwrap();
        assert_eq!(source_digest(&other).unwrap(), first);
        fs::write(dir.join("b.c"), b"changed").unwrap();
        assert_ne!(source_digest(&dir).unwrap(), first);
        let _ = fs::remove_dir_all(&dir);
        let _ = fs::remove_dir_all(&other);
    }

    #[test]
    fn lightftp_snapshot_matches_the_saved_region_report() {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../fuzz/test-results");
        let saved = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("test-results/coverage-regions-20261106");
        if !saved
            .join("latest-json-timing/requests/000003/regions.json")
            .is_file()
        {
            panic!("missing LightFTP region baseline under {}", saved.display());
        }
        let _ = root;
        let baseline: Value = serde_json::from_slice(
            &fs::read(saved.join("latest-json-timing/requests/000003/regions.json")).unwrap(),
        )
        .unwrap();
        let produced = serde_json::from_slice::<Value>(
            &fs::read(saved.join("latest-json-timing/requests/000003/handlers.json")).unwrap(),
        )
        .unwrap();
        let dir = std::env::temp_dir().join(format!("mcn-lightftp-measure-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let snapshot = dir.join("snapshot");
        fs::create_dir_all(&snapshot).unwrap();
        fs::copy(
            saved.join("latest-json-timing/requests/000003/tool/coverage-export.json"),
            snapshot.join("coverage.json"),
        )
        .unwrap();
        fs::copy(
            saved.join("latest-json-timing/requests/000003/tool/coverage.profdata"),
            snapshot.join("coverage.profdata"),
        )
        .unwrap();
        let mappings = produced.get("mappings").cloned().unwrap();
        let messages: Vec<_> = mappings
            .as_array()
            .unwrap()
            .iter()
            .map(|row| json!({"name": row["message"], "associated_fields": []}))
            .collect();
        let model = json!({
            "messages": messages,
            "steps": [],
        });
        fs::write(dir.join("model.json"), serde_json::to_vec(&model).unwrap()).unwrap();
        fs::write(
            dir.join("handlers.json"),
            serde_json::to_vec(&json!({"version": 1, "mappings": mappings})).unwrap(),
        )
        .unwrap();
        let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("targets/lightftp/build-cov/manifest.json");
        let binary =
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("targets/lightftp/build-cov/fftp");
        let source = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("targets/lightftp/build-cov/LightFTP/Source");
        run(&MeasureRequest {
            snapshot: &snapshot,
            callgraph: &saved.join("startup-json/graphs/callgraph.json"),
            cfg: &saved.join("startup-json/graphs/cfg.json"),
            manifest: &manifest,
            binary: &binary,
            model: &dir.join("model.json"),
            handlers: &dir.join("handlers.json"),
            source: &source,
            output_dir: &dir.join("out"),
        })
        .unwrap();
        let actual: Value =
            serde_json::from_slice(&fs::read(dir.join("out/regions.json")).unwrap()).unwrap();
        assert_eq!(signature(&actual), signature(&baseline));
        let before = fs::read(snapshot.join("coverage.profdata")).unwrap();
        let after = fs::read(snapshot.join("coverage.profdata")).unwrap();
        assert_eq!(before, after);
        let _ = fs::remove_dir_all(&dir);
    }

    fn signature(document: &Value) -> BTreeMap<String, String> {
        let mut rows = BTreeMap::new();
        for handler in document["handlers"].as_array().unwrap() {
            let mut regions: Vec<_> = handler["regions"]
                .as_array()
                .unwrap()
                .iter()
                .map(|region| {
                    format!(
                        "{}:{}:{}",
                        region["branch_directions"],
                        region["function_count"],
                        region["functions"]
                            .as_array()
                            .unwrap()
                            .iter()
                            .filter_map(Value::as_str)
                            .collect::<Vec<_>>()
                            .join(",")
                    )
                })
                .collect();
            regions.sort();
            let mut unknown: Vec<_> = handler["unknown"]
                .as_array()
                .unwrap()
                .iter()
                .map(|item| {
                    format!(
                        "{}|{}|{}",
                        item.get("function").and_then(Value::as_str).unwrap_or(""),
                        item.get("location").and_then(Value::as_str).unwrap_or(""),
                        item.get("reason").and_then(Value::as_str).unwrap_or("")
                    )
                })
                .collect();
            unknown.sort();
            rows.insert(
                handler["handler"].as_str().unwrap_or("").to_string(),
                format!(
                    "branches={} functions={} zero={} complete={} regions={} unknown={}",
                    handler["branch_directions"],
                    handler["function_count"],
                    handler["zero"],
                    handler["complete"],
                    regions.join(";"),
                    unknown.join(";")
                ),
            );
        }
        rows
    }
}
