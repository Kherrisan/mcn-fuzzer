//! Aggregate `report.json` and `report.md` for one evaluation directory.

use std::{collections::BTreeMap, fs, path::Path};

use serde_json::{json, Value};

use super::task::Task;

pub fn write_report(out: &Path, tasks: &[Task]) -> Result<(), String> {
    let planned = tasks.len();
    let mut by_kind: BTreeMap<String, usize> = BTreeMap::new();
    let mut infrastructure = 0_usize;
    let mut artifact_pass = 0_usize;
    let mut entered_replay = 0_usize;
    let mut runtime_pass = 0_usize;
    let mut passed = 0_usize;
    let mut stage_pass = BTreeMap::new();
    for name in ["prepare", "generate", "validate", "replay"] {
        stage_pass.insert(name, 0_usize);
    }
    let mut rows = Vec::new();
    for task in tasks {
        let artifact = stage_passed(task, "validate");
        let replay_status = task
            .stages
            .get("replay")
            .map(|stage| stage.status.as_str())
            .unwrap_or("not_run");
        let runtime = replay_status == "passed";
        if artifact {
            artifact_pass += 1;
        }
        if replay_status != "not_run" {
            entered_replay += 1;
        }
        if runtime {
            runtime_pass += 1;
        }
        let fully_passed = instance_passed(task);
        if fully_passed {
            passed += 1;
        }
        for (name, count) in &mut stage_pass {
            if stage_passed(task, name) {
                *count += 1;
            }
        }
        for stage in task.stages.values() {
            if stage.status == "failed" {
                let kind = stage
                    .failure_kind
                    .clone()
                    .unwrap_or_else(|| "failed".to_string());
                *by_kind.entry(kind.clone()).or_default() += 1;
                if matches!(
                    kind.as_str(),
                    "infrastructure" | "preflight" | "source_changed" | "cleanup_failed"
                ) {
                    infrastructure += 1;
                }
            }
        }
        rows.push(json!({
            "profile": task.profile,
            "provider": task.provider,
            "case": task.case_name,
            "repeat": task.repeat,
            "model": task.model_name,
            "effort": task.effort,
            "cli_version": task.cli_version,
            "protocol_id": task.protocol_id,
            "trace_id": task.trace_id,
            "artifact_pass": artifact,
            "runtime_pass": runtime,
            "passed": fully_passed,
            "schema_validation": "production_subset",
            "stages": task.stages,
        }));
    }
    let body = json!({
        "planned": planned,
        "stage_pass": stage_pass,
        "artifact_pass": artifact_pass,
        "runtime_pass": runtime_pass,
        "runtime_denominator": entered_replay,
        "passed": passed,
        "infrastructure_failures": infrastructure,
        "failure_kinds": by_kind,
        "schema_validation": "production_subset",
        "byte_counter_note": "Ctx byte counters can miss native sockets and Python modules; zero is not a failure",
        "instances": rows,
    });
    fs::create_dir_all(out).map_err(|error| error.to_string())?;
    fs::write(
        out.join("report.json"),
        serde_json::to_vec_pretty(&body).map_err(|error| error.to_string())?,
    )
    .map_err(|error| error.to_string())?;
    fs::write(out.join("report.md"), markdown(&body)).map_err(|error| error.to_string())?;
    Ok(())
}

pub(crate) fn instance_passed(task: &Task) -> bool {
    ["prepare", "generate", "validate", "replay"]
        .iter()
        .all(|name| stage_passed(task, name))
}

fn stage_passed(task: &Task, name: &str) -> bool {
    task.stages
        .get(name)
        .map(|stage| stage.status == "passed")
        .unwrap_or(false)
}

fn markdown(body: &Value) -> String {
    let mut text = String::from("# Synthesis evaluation\n\n");
    text.push_str(&format!(
        "Planned instances: {}. artifact_pass {}/{}. runtime_pass {}/{}. passed {}/{}. Infrastructure failures: {}.\n\n",
        body["planned"],
        body["artifact_pass"],
        body["planned"],
        body["runtime_pass"],
        body["runtime_denominator"],
        body["passed"],
        body["planned"],
        body["infrastructure_failures"],
    ));
    text.push_str("Schema checks use the production subset.\n\n");
    text.push_str(&body["byte_counter_note"].as_str().unwrap_or("").to_string());
    text.push_str("\n\n| Profile | Case | Repeat | Artifact | Runtime | Passed |\n");
    text.push_str("| --- | --- | --- | --- | --- | --- |\n");
    for instance in body["instances"].as_array().into_iter().flatten() {
        text.push_str(&format!(
            "| {} | {} | {} | {} | {} | {} |\n",
            instance["profile"].as_str().unwrap_or(""),
            instance["case"].as_str().unwrap_or(""),
            instance["repeat"],
            instance["artifact_pass"],
            instance["runtime_pass"],
            instance["passed"],
        ));
    }
    text.push_str("\n## Failure kinds\n\n");
    if let Some(kinds) = body["failure_kinds"].as_object() {
        for (kind, count) in kinds {
            text.push_str(&format!("- {kind}: {count}\n"));
        }
    }
    text
}
