//! Shared HC session state and redacted worker observations.
use super::HcClient;
use serde::{Deserialize, Serialize};
use std::{fs::OpenOptions, io::Write, path::Path};

/// Coarse session phase. Completion still follows `ready.json` and `result.json`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum SessionPhase {
    Initializing,
    Ready,
    Busy,
    Paused,
    Exited,
}

impl Default for SessionPhase {
    fn default() -> Self {
        Self::Initializing
    }
}

/// Latest agent session. Environment values are never stored here.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AgentSession {
    pub phase: SessionPhase,
    /// `launch`, `ready`, `send`, `result`, `blocked`, `timeout`, or `stop`.
    pub transition: String,
    pub terminal: Option<String>,
    /// Raw HC terminal `state`, such as `live`.
    pub hc_state: Option<String>,
    /// Raw HC terminal `status`. Non-strings are compact JSON.
    pub hc_status: Option<String>,
    /// Raw HC error or the campaign failure that accompanied it.
    pub hc_error: Option<String>,
}

impl Default for AgentSession {
    fn default() -> Self {
        Self {
            phase: SessionPhase::Initializing,
            transition: String::new(),
            terminal: None,
            hc_state: None,
            hc_status: None,
            hc_error: None,
        }
    }
}

/// One `terminal.get` observation. Values are redacted before this is stored.
#[derive(Clone, Debug, Default)]
pub(crate) struct TerminalFact {
    pub(crate) terminal: Option<String>,
    pub(crate) state: Option<String>,
    pub(crate) status: Option<String>,
    pub(crate) error: Option<String>,
}

#[derive(Debug)]
pub(crate) struct AgentFailure {
    pub(crate) reason: String,
    pub(crate) fact: TerminalFact,
    pub(crate) timeout: bool,
}

pub(crate) fn redact_text(text: &str, secrets: &[String]) -> String {
    let mut out = text.to_string();
    for secret in secrets {
        if !secret.is_empty() {
            out = out.replace(secret, "[redacted]");
        }
    }
    out
}

impl TerminalFact {
    pub(crate) fn observe(client: &mut HcClient, terminal: &str, secrets: &[String]) -> Self {
        match client.get(terminal) {
            Ok(info) => Self {
                terminal: Some(redact_text(&info.name, secrets)),
                state: Some(redact_text(&info.state, secrets)),
                status: info.status.map(|value| redact_text(&value, secrets)),
                error: info.error.map(|value| redact_text(&value, secrets)),
            },
            Err(error) => Self {
                terminal: Some(terminal.to_string()),
                error: Some(redact_text(&error.message, secrets)),
                ..Self::default()
            },
        }
    }
}

impl AgentFailure {
    pub(crate) fn new(reason: String, fact: TerminalFact) -> Self {
        let timeout = reason.contains("timed out") || reason.contains("was not published");
        Self {
            reason,
            fact,
            timeout,
        }
    }
}

impl AgentSession {
    pub(crate) fn record(
        &mut self,
        transition: &str,
        phase: SessionPhase,
        fact: &TerminalFact,
        error: Option<String>,
        secrets: &[String],
    ) {
        self.phase = phase;
        self.transition = transition.into();
        for (target, source) in [
            (&mut self.terminal, &fact.terminal),
            (&mut self.hc_state, &fact.state),
            (&mut self.hc_status, &fact.status),
        ] {
            if let Some(value) = source {
                *target = Some(redact_text(value, secrets));
            }
        }
        if let Some(error) = error.or(fact.error.clone()) {
            self.hc_error = Some(redact_text(&error, secrets));
        } else if matches!(transition, "ready" | "result") {
            self.hc_error = None;
        }
    }

    pub(crate) fn append_log(&self, path: &Path) {
        match OpenOptions::new().create(true).append(true).open(path) {
            Ok(mut file) => {
                if let Err(error) = serde_json::to_writer(&mut file, self)
                    .and_then(|_| writeln!(file).map_err(serde_json::Error::io))
                {
                    log::error!("writing agent session {}: {error}", path.display());
                }
            }
            Err(error) => log::error!("opening agent session {}: {error}", path.display()),
        }
    }
}
