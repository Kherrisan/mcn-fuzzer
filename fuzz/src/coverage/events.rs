//! Append-only JSONL events for coverage analysis and proposal handoff.

use std::{
    fs,
    io::Write,
    path::PathBuf,
    time::{SystemTime, UNIX_EPOCH},
};

#[derive(Clone)]
pub struct SharedLog(std::sync::Arc<std::sync::Mutex<EventLog>>);

pub struct EventLog {
    path: PathBuf,
    seq: u64,
    session: String,
    build: String,
}

impl Default for SharedLog {
    fn default() -> Self {
        Self(std::sync::Arc::new(std::sync::Mutex::new(EventLog {
            path: PathBuf::new(),
            seq: 0,
            session: String::new(),
            build: String::new(),
        })))
    }
}

impl SharedLog {
    pub fn open(path: PathBuf, session: impl Into<String>, build: impl Into<String>) -> Self {
        Self(std::sync::Arc::new(std::sync::Mutex::new(EventLog {
            path,
            seq: 0,
            session: session.into(),
            build: build.into(),
        })))
    }

    pub fn append(&self, kind: &str, body: serde_json::Value) {
        let mut log = self.0.lock().unwrap_or_else(|error| error.into_inner());
        if let Err(error) = log.append(kind, body) {
            log::error!("coverage analysis event log failed: {error}");
        }
    }
}

impl EventLog {
    fn append(&mut self, kind: &str, body: serde_json::Value) -> Result<(), String> {
        self.seq += 1;
        let event = serde_json::json!({
            "time_ms": SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis()).unwrap_or(0),
            "seq": self.seq,
            "kind": kind,
            "session": self.session,
            "build": self.build,
            "body": body,
        });
        if let Some(parent) = self.path.parent() {
            fs::create_dir_all(parent).map_err(|error| error.to_string())?;
        }
        let mut file = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
            .map_err(|error| error.to_string())?;
        writeln!(file, "{event}").map_err(|error| error.to_string())
    }
}
