//! Cumulative SUT stdout/stderr capture for source coverage replay.
//!
//! One pair of append-only files lives at the coverage root so cleanup scripts
//! cannot remove them. Case markers make failed attempts and successful snapshot
//! cases distinguishable after the fact; snapshot copies are ordinary copies,
//! never links to the live append files.

use std::{
    fs::{self, File, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
};

use serde_json::{Value, json};

pub(crate) const STDOUT_LOG: &str = "sut.stdout.log";
pub(crate) const STDERR_LOG: &str = "sut.stderr.log";

pub(crate) struct SutLog {
    root: PathBuf,
    stdout: File,
    stderr: File,
    batch: String,
    case_seq: u64,
}

pub(crate) struct SutLogCopy {
    pub(crate) stdout_bytes: u64,
    pub(crate) stderr_bytes: u64,
}

impl SutLog {
    /// Open the cumulative files and append a batch marker before any child starts.
    pub(crate) fn open(root: &Path, batch: impl Into<String>) -> Result<Self, libafl::Error> {
        fs::create_dir_all(root).map_err(|error| {
            libafl::Error::unknown(format!(
                "creating SUT log directory {}: {error}",
                root.display()
            ))
        })?;
        let stdout = append_file(&root.join(STDOUT_LOG))?;
        let stderr = append_file(&root.join(STDERR_LOG))?;
        Ok(Self {
            root: root.to_path_buf(),
            stdout,
            stderr,
            batch: batch.into(),
            case_seq: 0,
        })
    }

    /// Clone descriptors for `StdOutObserver::new_file` / `StdErrObserver::new_file`.
    pub(crate) fn observer_files(&self) -> Result<(File, File), libafl::Error> {
        let stdout = self
            .stdout
            .try_clone()
            .map_err(|error| libafl::Error::unknown(format!("cloning SUT stdout log: {error}")))?;
        let stderr = self
            .stderr
            .try_clone()
            .map_err(|error| libafl::Error::unknown(format!("cloning SUT stderr log: {error}")))?;
        Ok((stdout, stderr))
    }

    pub(crate) fn batch_start(&mut self) -> Result<(), libafl::Error> {
        self.marker(json!({"event": "batch_start"}))
    }

    /// `case` is one-based within this batch. `filename` may be absent.
    pub(crate) fn case_start(
        &mut self,
        case_id: &str,
        filename: Option<&str>,
    ) -> Result<u64, libafl::Error> {
        self.case_seq = self.case_seq.saturating_add(1);
        let case = self.case_seq;
        self.marker(json!({
            "event": "case_start",
            "case": case,
            "case_id": case_id,
            "filename": filename.unwrap_or(""),
        }))?;
        Ok(case)
    }

    /// Write the end marker after the case's SUT and descendants have been reaped.
    pub(crate) fn case_end(
        &mut self,
        case: u64,
        case_id: &str,
        outcome: &str,
    ) -> Result<(), libafl::Error> {
        self.marker(json!({
            "event": "case_end",
            "case": case,
            "case_id": case_id,
            "outcome": outcome,
        }))
    }

    pub(crate) fn batch_end(&mut self, outcome: &str) -> Result<(), libafl::Error> {
        self.marker(json!({"event": "batch_end", "outcome": outcome}))
    }

    /// Copy the cumulative bytes into a snapshot staging directory.
    pub(crate) fn copy_to(&self, directory: &Path) -> Result<SutLogCopy, libafl::Error> {
        fs::create_dir_all(directory).map_err(|error| {
            libafl::Error::unknown(format!(
                "creating snapshot log directory {}: {error}",
                directory.display()
            ))
        })?;
        let mut copied = SutLogCopy {
            stdout_bytes: 0,
            stderr_bytes: 0,
        };
        for (name, bytes) in [
            (STDOUT_LOG, &mut copied.stdout_bytes),
            (STDERR_LOG, &mut copied.stderr_bytes),
        ] {
            let source = self.root.join(name);
            let target = directory.join(name);
            fs::copy(&source, &target).map_err(|error| {
                libafl::Error::unknown(format!(
                    "copying SUT log {} to {}: {error}",
                    source.display(),
                    target.display()
                ))
            })?;
            *bytes = fs::metadata(&target)
                .map_err(|error| {
                    libafl::Error::unknown(format!(
                        "reading snapshot SUT log {}: {error}",
                        target.display()
                    ))
                })?
                .len();
        }
        Ok(copied)
    }

    fn marker(&mut self, fields: Value) -> Result<(), libafl::Error> {
        let mut event = fields;
        if let Some(object) = event.as_object_mut() {
            object.insert("batch".into(), json!(self.batch));
        }
        let line = format!(
            "\n=== MCN_REPLAY {} ===\n",
            serde_json::to_string(&event).map_err(|error| libafl::Error::unknown(format!(
                "serializing SUT marker: {error}"
            )))?
        );
        self.stdout
            .write_all(line.as_bytes())
            .and_then(|()| self.stderr.write_all(line.as_bytes()))
            .map_err(|error| {
                libafl::Error::unknown(format!("writing SUT replay marker: {error}"))
            })?;
        self.stdout
            .flush()
            .and_then(|()| self.stderr.flush())
            .map_err(|error| libafl::Error::unknown(format!("flushing SUT replay marker: {error}")))
    }
}

fn append_file(path: &Path) -> Result<File, libafl::Error> {
    OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|error| {
            libafl::Error::unknown(format!("opening SUT log {}: {error}", path.display()))
        })
}
