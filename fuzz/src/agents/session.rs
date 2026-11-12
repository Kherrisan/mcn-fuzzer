//! Redacted worker observations.

use super::HcClient;

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
