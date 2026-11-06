//! File-based protocol shared by coding agents and their background workers.

use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs,
    path::Path,
    thread,
    time::{Duration, Instant},
};

pub(crate) fn read_json(path: &Path) -> Result<Value, String> {
    let bytes = fs::read(path).map_err(|error| format!("reading {}: {error}", path.display()))?;
    serde_json::from_slice(&bytes)
        .map_err(|error| format!("{} is not JSON: {error}", path.display()))
}

pub(crate) fn check_identity(value: &Value, fields: &[(&str, &str)]) -> Result<(), String> {
    for &(name, expected) in fields {
        if value.get(name).and_then(Value::as_str) != Some(expected) {
            return Err(format!(
                "result.json {name} does not match the dispatched request"
            ));
        }
    }
    Ok(())
}

/// Protect tool-generated inputs from agent edits while a request is running.
#[derive(Clone, Debug, Default)]
pub(crate) struct ArtifactSeal(BTreeMap<String, String>);

impl ArtifactSeal {
    pub(crate) fn capture(directory: &Path, names: &[&str]) -> Result<Self, String> {
        names
            .iter()
            .map(|name| Ok(((*name).to_owned(), digest(&directory.join(name))?)))
            .collect::<Result<_, _>>()
            .map(Self)
    }

    pub(crate) fn verify(&self, directory: &Path) -> Result<(), String> {
        for (name, expected) in &self.0 {
            if digest(&directory.join(name))? != *expected {
                return Err(format!("{name} no longer matches the tool output"));
            }
        }
        Ok(())
    }
}

fn digest(path: &Path) -> Result<String, String> {
    let bytes = fs::read(path).map_err(|error| format!("reading {}: {error}", path.display()))?;
    Ok(format!("{:x}", Sha256::digest(bytes)))
}

/// Wait for `path` during `budget`. If it is still absent, wait `grace` more.
///
/// `grace` covers an agent that publishes its final file immediately after the
/// task budget. It is not a larger analysis budget: an agent that has not
/// started publishing still has only this short window.
pub(crate) fn wait_for_result(
    path: &Path,
    budget: Duration,
    grace: Duration,
) -> Result<(), String> {
    if wait_file(path, budget).is_ok() || path.is_file() {
        return Ok(());
    }
    if grace.is_zero() {
        return Err(format!(
            "{} was not published before the deadline",
            path.display()
        ));
    }
    wait_file(path, grace)
}

pub(crate) fn wait_file(path: &Path, budget: Duration) -> Result<(), String> {
    let deadline = Instant::now() + budget;
    while Instant::now() < deadline {
        if path.is_file() {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(20));
    }
    Err(format!(
        "{} was not published before the deadline",
        path.display()
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    #[test]
    fn grace_accepts_a_file_published_after_the_budget() {
        static TEMP: AtomicU64 = AtomicU64::new(0);
        let dir = std::env::temp_dir().join(format!(
            "mcn-wait-grace-{}-{}",
            std::process::id(),
            TEMP.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("result.json");
        let delayed = path.clone();
        thread::spawn(move || {
            thread::sleep(Duration::from_millis(120));
            fs::write(delayed, b"{}").unwrap();
        });
        wait_for_result(&path, Duration::from_millis(40), Duration::from_millis(500)).unwrap();
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn zero_grace_fails_when_the_file_stays_absent() {
        static TEMP: AtomicU64 = AtomicU64::new(0);
        let dir = std::env::temp_dir().join(format!(
            "mcn-wait-nograce-{}-{}",
            std::process::id(),
            TEMP.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("result.json");
        let error = wait_for_result(&path, Duration::from_millis(40), Duration::ZERO).unwrap_err();
        assert!(error.contains("was not published"), "{error}");
        let _ = fs::remove_dir_all(&dir);
    }
}
