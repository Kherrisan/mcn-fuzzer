//! Startup conversion of build DOTs into two sealed JSON graph inputs.

use std::{
    path::{Path, PathBuf},
    sync::Arc,
    time::Instant,
};

use libafl::Error;
use serde_json::Value;

use crate::{
    agents::artifacts::{read_json, ArtifactSeal},
    app::config::Campaign,
};

#[derive(Clone)]
pub(crate) struct GraphFiles {
    pub callgraph: PathBuf,
    pub cfg: PathBuf,
    seal: Arc<ArtifactSeal>,
}

impl GraphFiles {
    pub fn prepare(campaign: &Campaign, directory: &Path) -> Result<Self, Error> {
        let started = Instant::now();
        let output = std::process::Command::new(&campaign.campaign.python)
            .env("PYTHONPATH", &campaign.campaign.pythonpath)
            .arg("-m")
            .arg("coverage_analysis.graphs")
            .arg("--callgraph-dot")
            .arg(&campaign.coverage.callgraph_dot)
            .arg("--cfg-dir")
            .arg(&campaign.coverage.cfg_dir)
            .arg("--manifest")
            .arg(&campaign.coverage.build_manifest)
            .arg("--output-dir")
            .arg(directory)
            .output()
            .map_err(|error| Error::unknown(format!("starting graph conversion: {error}")))?;
        if !output.status.success() {
            return Err(Error::unknown(format!(
                "coverage graph conversion failed: {}{}",
                String::from_utf8_lossy(&output.stderr),
                String::from_utf8_lossy(&output.stdout),
            )));
        }
        let manifest = read_json(&campaign.coverage.build_manifest).map_err(Error::unknown)?;
        let callgraph = directory.join("callgraph.json");
        let cfg = directory.join("cfg.json");
        let calls = read_graph(&callgraph, &manifest)?;
        let cfgs = read_graph(&cfg, &manifest)?;
        let functions = calls
            .get("functions")
            .and_then(Value::as_array)
            .ok_or_else(|| Error::unknown("callgraph.json requires a functions array"))?;
        let edges = calls
            .get("edges")
            .and_then(Value::as_array)
            .ok_or_else(|| Error::unknown("callgraph.json requires an edges array"))?;
        let cfg_functions = cfgs
            .get("functions")
            .and_then(Value::as_object)
            .ok_or_else(|| Error::unknown("cfg.json requires a functions object"))?;
        log::info!(
            "coverage graph JSON ready: functions={} calls={} cfg_functions={} elapsed_ms={} callgraph={} cfg={}",
            functions.len(), edges.len(), cfg_functions.len(), started.elapsed().as_millis(),
            callgraph.display(), cfg.display(),
        );
        let seal = ArtifactSeal::capture(directory, &["callgraph.json", "cfg.json"])
            .map_err(Error::unknown)?;
        Ok(Self {
            callgraph,
            cfg,
            seal: Arc::new(seal),
        })
    }

    pub fn verify(&self) -> Result<(), String> {
        self.seal
            .verify(self.callgraph.parent().expect("graph directory"))
    }
}

fn read_graph(path: &Path, manifest: &Value) -> Result<Value, Error> {
    let graph = read_json(path).map_err(Error::unknown)?;
    if graph.get("version").and_then(Value::as_u64) != Some(1) {
        return Err(Error::unknown(format!(
            "{} must have version 1",
            path.display()
        )));
    }
    for field in ["source_sha256", "graphs_sha256"] {
        if graph.get(field).and_then(Value::as_str).is_none()
            || graph.get(field) != manifest.get(field)
        {
            return Err(Error::unknown(format!(
                "{} {field} does not match the build manifest",
                path.display()
            )));
        }
    }
    Ok(graph)
}
