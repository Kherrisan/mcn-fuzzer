//! Stateful HC interaction, confined to the background synthesis thread.

use std::{
    fs,
    path::{Path, PathBuf},
    sync::mpsc,
    time::Duration,
};

use super::{
    redact_text, wait_file, AgentCommand, AgentFailure, GeneratorRequest, Outcome, ReadyInput,
    TerminalFact,
};
use crate::agents::{shutdown_session, CommandLine, HcClient};

pub(super) struct AgentWorker {
    pub(super) commands: mpsc::Receiver<AgentCommand>,
    pub(super) outcomes: mpsc::Sender<Outcome>,
    pub(super) socket: PathBuf,
    pub(super) terminal: String,
    pub(super) init_timeout: Duration,
    pub(super) task_timeout: Duration,
    pub(super) session: String,
    pub(super) secrets: Vec<String>,
}

impl AgentWorker {
    pub(super) fn run(mut self) {
        let mut client = match HcClient::connect(&self.socket, Duration::from_secs(5)) {
            Ok(client) => client,
            Err(error) => {
                let _ = self
                    .outcomes
                    .send(Outcome::failure(error.message, TerminalFact::default()));
                return;
            }
        };
        while let Ok(command) = self.commands.recv() {
            let outcome = match command {
                AgentCommand::Init {
                    output_dir,
                    prompt,
                    launch,
                } => self.initialize(&mut client, &output_dir, &prompt, launch),
                AgentCommand::Generate { request, prompt } => {
                    self.generate(&mut client, &request, &prompt)
                }
                AgentCommand::Stop => {
                    self.shutdown(&mut client);
                    break;
                }
            };
            let _ = self.outcomes.send(outcome);
        }
    }

    fn initialize(
        &mut self,
        client: &mut HcClient,
        output_dir: &Path,
        prompt: &str,
        launch: CommandLine,
    ) -> Outcome {
        self.secrets = launch
            .env
            .iter()
            .filter(|(_, value)| !value.is_empty())
            .map(|(_, value)| value.clone())
            .collect();
        match client.run_terminal(&self.terminal, &launch.argv, &launch.env, &launch.cwd) {
            Ok(info) => {
                self.terminal = info.name;
                let _ = self.outcomes.send(Outcome::Launched(self.observe(client)));
            }
            Err(error) => return Outcome::failure(error.message, TerminalFact::default()),
        }
        if let Err(error) = client.send(&self.terminal, prompt) {
            return Outcome::failure(error.message, self.observe(client));
        }
        match wait_file(&output_dir.join("ready.json"), self.init_timeout) {
            Ok(()) => Outcome::Ready(self.observe(client)),
            Err(error) => Outcome::failure(error, self.observe(client)),
        }
    }

    fn generate(&self, client: &mut HcClient, request: &GeneratorRequest, prompt: &str) -> Outcome {
        if let Err(error) = client.send(&self.terminal, prompt) {
            return Outcome::failure(error.message, self.observe(client));
        }
        if wait_file(&request.output_dir.join("result.json"), self.task_timeout).is_err() {
            return Outcome::failure(
                format!("request {} timed out", request.id),
                self.observe(client),
            );
        }
        let result = request.accept().map(|validated| ReadyInput {
            request: request.id.clone(),
            input: validated.input,
            origin: validated.origin,
        });
        Outcome::Artifact(result, self.observe(client))
    }

    fn observe(&self, client: &mut HcClient) -> TerminalFact {
        match client.get(&self.terminal) {
            Ok(info) => TerminalFact {
                terminal: Some(redact_text(&info.name, &self.secrets)),
                state: Some(redact_text(&info.state, &self.secrets)),
                status: info.status.map(|value| redact_text(&value, &self.secrets)),
                error: info.error.map(|value| redact_text(&value, &self.secrets)),
            },
            Err(error) => TerminalFact {
                terminal: Some(self.terminal.clone()),
                error: Some(redact_text(&error.message, &self.secrets)),
                ..TerminalFact::default()
            },
        }
    }

    fn shutdown(&self, client: &mut HcClient) {
        shutdown_session(client, &self.session);
        if let Some(runtime) = self.socket.parent().and_then(|path| path.parent()) {
            let name = runtime
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or("");
            // A socket placed directly in a temp directory must not make this
            // delete the temp directory itself.
            if runtime != std::env::temp_dir() && name.starts_with("mcn-") {
                let _ = fs::remove_dir_all(runtime);
            }
        }
    }
}

impl Outcome {
    fn failure(reason: String, fact: TerminalFact) -> Self {
        let timeout = reason.contains("timed out") || reason.contains("was not published");
        Self::Failed(AgentFailure {
            reason,
            fact,
            timeout,
        })
    }
}
