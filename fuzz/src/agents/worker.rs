//! One background HC terminal per agent. Stages exchange typed commands/outcomes;
//! protocol-specific preparation and artifact acceptance are supplied as traits.

use super::{AgentFailure, CommandLine, HcClient, TerminalFact};
use std::{
    io,
    path::{Path, PathBuf},
    sync::mpsc,
    thread,
    time::Duration,
};

pub(crate) trait AgentInit: Send + 'static {
    type Output: Send;
    fn output_dir(&self) -> &Path;
    fn prepare(&self) -> Result<(), String> {
        Ok(())
    }
    fn accept(&self) -> Result<Self::Output, String>;
}

pub(crate) trait AgentTask: Send + 'static {
    type Prepared;
    type Output: Send;
    fn id(&self) -> &str;
    fn output_dir(&self) -> &Path;
    fn prepare(&self) -> Result<Self::Prepared, String>;
    fn accept(&self, prepared: Self::Prepared) -> Result<Self::Output, String>;
    /// Extra wait after the task budget, only for a result published at the end.
    fn result_grace(&self) -> Duration {
        Duration::ZERO
    }
}

pub(crate) enum AgentCommand<I, T> {
    Init {
        context: I,
        prompt: String,
        launch: CommandLine,
    },
    Run {
        request: T,
        prompt: String,
    },
    Stop,
}

#[derive(Debug)]
pub(crate) enum AgentOutcome<R, A> {
    Launched(TerminalFact),
    InitTimedOut(AgentFailure),
    Ready(R, TerminalFact),
    Artifact {
        request: String,
        result: Result<A, String>,
        fact: TerminalFact,
    },
    Failed(AgentFailure),
}

type Outcome<I, T> = AgentOutcome<<I as AgentInit>::Output, <T as AgentTask>::Output>;

pub(crate) struct WorkerConfig {
    pub thread_name: &'static str,
    pub socket: PathBuf,
    pub terminal: String,
    pub init_timeout: Duration,
    pub task_timeout: Duration,
    pub secrets: Vec<String>,
}

/// Owns channels and requests terminal shutdown on drop; never joins on the fuzz thread.
pub(crate) struct AgentClient<I: AgentInit, T: AgentTask> {
    commands: mpsc::Sender<AgentCommand<I, T>>,
    outcomes: Option<mpsc::Receiver<Outcome<I, T>>>,
}

impl<I: AgentInit, T: AgentTask> AgentClient<I, T> {
    pub(crate) fn spawn(config: WorkerConfig) -> io::Result<Self> {
        let (commands, command_rx) = mpsc::channel();
        let (outcome_tx, outcomes) = mpsc::channel();
        let initial_terminal = config.terminal.clone();
        thread::Builder::new()
            .name(config.thread_name.into())
            .spawn(move || {
                Worker {
                    config,
                    initial_terminal,
                    commands: command_rx,
                    outcomes: outcome_tx,
                    stopping: false,
                }
                .run();
            })?;
        Ok(Self {
            commands,
            outcomes: Some(outcomes),
        })
    }

    pub(crate) fn send(&self, command: AgentCommand<I, T>) -> Result<(), String> {
        self.commands
            .send(command)
            .map_err(|_| "agent worker disconnected".into())
    }

    pub(crate) fn recv_timeout(&mut self, timeout: Duration) -> Option<Outcome<I, T>> {
        let receiver = self.outcomes.as_ref()?;
        match receiver.recv_timeout(timeout) {
            Ok(outcome) => Some(outcome),
            Err(mpsc::RecvTimeoutError::Timeout) => None,
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                self.outcomes = None;
                Some(AgentOutcome::Failed(AgentFailure::new(
                    "agent worker disconnected".into(),
                    TerminalFact::default(),
                )))
            }
        }
    }

    pub(crate) fn stop(&self) {
        let _ = self.commands.send(AgentCommand::Stop);
    }

    #[cfg(test)]
    pub(crate) fn from_channels(
        commands: mpsc::Sender<AgentCommand<I, T>>,
        outcomes: mpsc::Receiver<Outcome<I, T>>,
    ) -> Self {
        Self {
            commands,
            outcomes: Some(outcomes),
        }
    }
}

impl<I: AgentInit, T: AgentTask> Drop for AgentClient<I, T> {
    fn drop(&mut self) {
        self.stop();
    }
}

struct Worker<I: AgentInit, T: AgentTask> {
    config: WorkerConfig,
    initial_terminal: String,
    commands: mpsc::Receiver<AgentCommand<I, T>>,
    outcomes: mpsc::Sender<Outcome<I, T>>,
    stopping: bool,
}

impl<I: AgentInit, T: AgentTask> Worker<I, T> {
    fn run(mut self) {
        let mut client = match HcClient::connect(&self.config.socket, Duration::from_secs(30)) {
            Ok(client) => client,
            Err(error) => {
                let _ = self.outcomes.send(AgentOutcome::Failed(AgentFailure::new(
                    error.message,
                    TerminalFact::default(),
                )));
                return;
            }
        };
        while let Ok(command) = self.commands.recv() {
            let outcome = match command {
                AgentCommand::Init {
                    context,
                    prompt,
                    launch,
                } => self.initialize(&mut client, &context, &prompt, launch),
                AgentCommand::Run { request, prompt } => {
                    self.execute(&mut client, &request, &prompt)
                }
                AgentCommand::Stop => break,
            };
            if self.outcomes.send(outcome).is_err() || self.stopping {
                break;
            }
        }
        // Only the campaign runtime owns the shared HC core.
        let _ = client.kill(&self.config.terminal);
    }

    fn initialize(
        &mut self,
        client: &mut HcClient,
        context: &I,
        prompt: &str,
        launch: CommandLine,
    ) -> Outcome<I, T> {
        let outcome = self.initialize_attempt(client, context, prompt, launch);
        if matches!(outcome, AgentOutcome::Failed(_)) {
            let _ = client.kill(&self.config.terminal);
            self.config.terminal.clone_from(&self.initial_terminal);
        }
        outcome
    }

    fn initialize_attempt(
        &mut self,
        client: &mut HcClient,
        context: &I,
        prompt: &str,
        launch: CommandLine,
    ) -> Outcome<I, T> {
        if let Err(reason) = context.prepare() {
            return self.failure(reason, client);
        }
        self.config.secrets = launch.secrets();
        match client.run_terminal(
            &self.initial_terminal,
            &launch.argv,
            &launch.env,
            &launch.cwd,
        ) {
            Ok(info) => {
                self.config.terminal = info.name;
                let _ = self
                    .outcomes
                    .send(AgentOutcome::Launched(self.observe(client)));
            }
            Err(error) => return self.failure(error.message, client),
        }
        if launch.codex {
            // CLI 0.166.0 has no --name. `/rename <name>` calls thread/name/set.
            // The init prompt waits until the status line shows that name.
            // A dead terminal is an initialization failure.
            let ui_deadline =
                std::time::Instant::now() + self.config.init_timeout.min(Duration::from_secs(15));
            if let Err(reason) = self.wait_for_screen(
                client,
                ui_deadline,
                codex_composer_ready,
                "the Codex composer to accept input",
            ) {
                if codex_composer_lost(&reason) {
                    return self.failure(
                        format!(
                            "Codex composer unavailable on {}: {reason}",
                            self.config.terminal
                        ),
                        client,
                    );
                }
            }
            if let Some(reason) = self.codex_terminal_closed(client) {
                return self.failure(
                    format!(
                        "Codex composer unavailable on {}: {reason}",
                        self.config.terminal
                    ),
                    client,
                );
            }
            let rename = format!("/rename {}", self.initial_terminal);
            if let Err(error) = client.send(&self.config.terminal, &rename) {
                return self.failure(
                    format!(
                        "Codex composer unavailable on {}: {}",
                        self.config.terminal, error.message
                    ),
                    client,
                );
            }
            // Send the init prompt only after the status line shows the name.
            let terminal = self.initial_terminal.clone();
            if let Err(reason) = self.wait_for_screen(
                client,
                std::time::Instant::now() + Duration::from_secs(15),
                |screen| codex_thread_name_visible(screen, &terminal),
                "the renamed Codex thread",
            ) {
                return self.failure(reason, client);
            }
        }
        if let Err(error) = client.send(&self.config.terminal, prompt) {
            return self.failure(error.message, client);
        }
        let ready_path = context.output_dir().join("ready.json");
        let mut deadline = std::time::Instant::now() + self.config.init_timeout;
        loop {
            match self.commands.try_recv() {
                Ok(AgentCommand::Stop) | Err(mpsc::TryRecvError::Disconnected) => {
                    self.stopping = true;
                    return self.failure("agent initialization stopped".into(), client);
                }
                Ok(_) => {
                    return self.failure("unexpected command during initialization".into(), client)
                }
                Err(mpsc::TryRecvError::Empty) => {}
            }
            if ready_path.is_file() {
                break;
            }
            match client.get(&self.config.terminal) {
                Ok(info) if matches!(info.state.as_str(), "finished" | "exited" | "dead") => {
                    let detail = info
                        .error
                        .or(info.status)
                        .unwrap_or_else(|| "no terminal error was reported".to_string());
                    return self.failure(
                        format!(
                            "agent terminal {} exited during initialization ({}): {detail}",
                            info.name, info.state
                        ),
                        client,
                    );
                }
                Err(error) => {
                    return self.failure(
                        format!("checking agent during initialization: {}", error.message),
                        client,
                    );
                }
                _ => {}
            }
            if std::time::Instant::now() >= deadline {
                let failure = AgentFailure::new(
                    format!(
                        "{} was not published before the deadline",
                        ready_path.display()
                    ),
                    self.observe(client),
                );
                // Keep the same terminal and handshake alive. Each stage owns
                // its timeout count and decides when the campaign must fail.
                if self
                    .outcomes
                    .send(AgentOutcome::InitTimedOut(failure))
                    .is_err()
                {
                    return self.failure("agent initialization stopped".into(), client);
                }
                deadline = std::time::Instant::now() + self.config.init_timeout;
            }
            thread::sleep(Duration::from_millis(100));
        }
        match context.accept() {
            Ok(ready) => AgentOutcome::Ready(ready, self.observe(client)),
            Err(reason) => self.failure(reason, client),
        }
    }

    fn execute(&self, client: &mut HcClient, request: &T, prompt: &str) -> Outcome<I, T> {
        let prepared = match request.prepare() {
            Ok(prepared) => prepared,
            Err(reason) => return self.artifact(request, Err(reason), client),
        };
        if let Err(error) = client.send(&self.config.terminal, prompt) {
            return self.failure(error.message, client);
        }
        let result_path = request.output_dir().join("result.json");
        if let Err(reason) = super::artifacts::wait_for_result(
            &result_path,
            self.config.task_timeout,
            request.result_grace(),
        ) {
            return self.failure(
                format!("request {} timed out: {reason}", request.id()),
                client,
            );
        }
        self.artifact(request, request.accept(prepared), client)
    }

    fn artifact(
        &self,
        request: &T,
        result: Result<T::Output, String>,
        client: &mut HcClient,
    ) -> Outcome<I, T> {
        AgentOutcome::Artifact {
            request: request.id().into(),
            result,
            fact: self.observe(client),
        }
    }

    fn observe(&self, client: &mut HcClient) -> TerminalFact {
        TerminalFact::observe(client, &self.config.terminal, &self.config.secrets)
    }

    fn codex_terminal_closed(&self, client: &mut HcClient) -> Option<String> {
        match client.get(&self.config.terminal) {
            Ok(info) if matches!(info.state.as_str(), "finished" | "exited" | "dead") => {
                Some(format!("terminal {} {}", info.name, info.state))
            }
            _ => None,
        }
    }

    fn failure(&self, reason: String, client: &mut HcClient) -> Outcome<I, T> {
        AgentOutcome::Failed(AgentFailure::new(reason, self.observe(client)))
    }

    fn wait_for_screen(
        &self,
        client: &mut HcClient,
        deadline: std::time::Instant,
        mut ready: impl FnMut(&str) -> bool,
        waiting_for: &str,
    ) -> Result<(), String> {
        loop {
            if std::time::Instant::now() >= deadline {
                return Err(format!(
                    "timed out waiting for {waiting_for} on {}",
                    self.config.terminal
                ));
            }
            match client.get(&self.config.terminal) {
                Ok(info) if matches!(info.state.as_str(), "finished" | "exited" | "dead") => {
                    return Err(format!(
                        "agent terminal {} exited while waiting for {waiting_for} ({})",
                        info.name, info.state
                    ));
                }
                Ok(_) => {}
                Err(error) => return Err(error.message),
            }
            match client.read(&self.config.terminal, 40) {
                Ok(screen) if ready(&plain_terminal(&screen)) => return Ok(()),
                Ok(_) => {}
                Err(error) => return Err(error.message),
            }
            thread::sleep(Duration::from_millis(200));
        }
    }
}

/// Codex 0.166.0 draws this placeholder when the composer will submit a line.
fn codex_composer_ready(screen: &str) -> bool {
    screen.contains("Ask Codex to do anything")
}

fn codex_composer_lost(reason: &str) -> bool {
    reason.contains("exited") || reason.contains("finished") || reason.contains("dead")
}

/// The status line shows ` · <name>` after `/rename`. An unsubmitted
/// `/rename <name>` line does not.
fn codex_thread_name_visible(screen: &str, name: &str) -> bool {
    screen.contains(&format!(" · {name}"))
}

fn plain_terminal(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut chars = text.chars().peekable();
    while let Some(character) = chars.next() {
        if character != '\u{1b}' {
            out.push(character);
            continue;
        }
        match chars.peek().copied() {
            Some('[') => {
                chars.next();
                for next in chars.by_ref() {
                    if next.is_ascii_alphabetic() {
                        break;
                    }
                }
            }
            Some(']') => {
                chars.next();
                for next in chars.by_ref() {
                    if next == '\u{7}' || next == '\\' {
                        break;
                    }
                }
            }
            _ => {}
        }
    }
    out
}

/// Standard ready.json handshake, also reused by specialized initializers.
pub(crate) struct SessionInit {
    pub output_dir: PathBuf,
    pub session: String,
}

impl AgentInit for SessionInit {
    type Output = ();
    fn output_dir(&self) -> &Path {
        &self.output_dir
    }
    fn accept(&self) -> Result<(), String> {
        let ready = super::artifacts::read_json(&self.output_dir.join("ready.json"))?;
        super::artifacts::check_identity(&ready, &[("status", "ready"), ("session", &self.session)])
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::agents::{
        artifacts::{check_identity, read_json},
        MockCore,
    };
    use serde_json::Value;
    use std::{
        fs,
        sync::atomic::{AtomicU64, Ordering},
    };

    struct Request {
        id: &'static str,
        output: PathBuf,
        reject: bool,
    }

    impl AgentTask for Request {
        type Prepared = ();
        type Output = Value;
        fn id(&self) -> &str {
            self.id
        }
        fn output_dir(&self) -> &Path {
            &self.output
        }
        fn prepare(&self) -> Result<(), String> {
            assert_eq!(thread::current().name(), Some("shared-worker-test"));
            if self.reject {
                Err("task preparation rejected".into())
            } else {
                Ok(())
            }
        }
        fn accept(&self, (): ()) -> Result<Value, String> {
            let value = read_json(&self.output.join("result.json"))?;
            check_identity(&value, &[("request_id", self.id)])?;
            Ok(value)
        }
    }

    #[test]
    fn preparation_failure_preserves_the_session_and_drop_releases_the_connection() {
        static TEMP: AtomicU64 = AtomicU64::new(0);
        let directory = std::env::temp_dir().join(format!(
            "mcn-shared-agent-{}-{}",
            std::process::id(),
            TEMP.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir_all(&directory).unwrap();
        let socket = directory.join("hc.sock");
        let core = MockCore::spawn(socket.clone()).unwrap();
        let mut client = AgentClient::<SessionInit, Request>::spawn(WorkerConfig {
            thread_name: "shared-worker-test",
            socket: socket.clone(),
            terminal: "shared-test".into(),
            init_timeout: Duration::from_secs(2),
            task_timeout: Duration::from_secs(2),
            secrets: vec![],
        })
        .unwrap();
        client
            .send(AgentCommand::Init {
                context: SessionInit {
                    output_dir: directory.clone(),
                    session: "shared-session".into(),
                },
                prompt: format!(
                    "OUTPUT_DIR={}\nSESSION=shared-session\nWRITE ready.json",
                    directory.display()
                ),
                launch: CommandLine {
                    argv: vec!["/bin/true".into()],
                    env: vec![],
                    cwd: directory.clone(),
                    codex: false,
                },
            })
            .unwrap();
        assert!(matches!(
            client.recv_timeout(Duration::from_secs(3)),
            Some(AgentOutcome::Launched(_))
        ));
        assert!(matches!(
            client.recv_timeout(Duration::from_secs(3)),
            Some(AgentOutcome::Ready((), _))
        ));
        for reject in [true, false] {
            client
                .send(AgentCommand::Run {
                    request: Request {
                        id: "task",
                        output: directory.clone(),
                        reject,
                    },
                    prompt: format!("OUTPUT_DIR={}\nREQUEST=task", directory.display()),
                })
                .unwrap();
            match client.recv_timeout(Duration::from_secs(3)).unwrap() {
                AgentOutcome::Artifact {
                    request, result, ..
                } => {
                    assert_eq!(request, "task");
                    assert_eq!(result.is_err(), reject);
                }
                outcome => panic!("unexpected outcome: {outcome:?}"),
            }
        }
        drop(client);
        // MockCore serves one connection at a time, so this handshake also proves
        // that dropping the handle let its worker finish and release HC I/O.
        let mut next = HcClient::connect(&socket, Duration::from_secs(2)).unwrap();
        next.run_terminal("next-agent", &["/bin/true".into()], &[], &directory)
            .unwrap();
        drop(next);
        drop(core);
        let _ = fs::remove_dir_all(directory);
    }

    #[test]
    fn init_timeouts_keep_the_terminal_alive_until_ready_or_shutdown() {
        for recover in [true, false] {
            let directory = std::env::temp_dir().join(format!(
                "mcn-init-timeouts-{}-{recover}",
                std::process::id()
            ));
            let _ = fs::remove_dir_all(&directory);
            fs::create_dir_all(&directory).unwrap();
            let socket = directory.join("hc.sock");
            let core = MockCore::spawn(socket.clone()).unwrap();
            let mut client = AgentClient::<SessionInit, Request>::spawn(WorkerConfig {
                thread_name: "shared-worker-test",
                socket: socket.clone(),
                terminal: "timeout-test".into(),
                init_timeout: Duration::from_millis(100),
                task_timeout: Duration::from_secs(2),
                secrets: vec![],
            })
            .unwrap();
            client
                .send(AgentCommand::Init {
                    context: SessionInit {
                        output_dir: directory.clone(),
                        session: "timeout-session".into(),
                    },
                    prompt: format!(
                        "OUTPUT_DIR={}\nSESSION=timeout-session\nWRITE ready.json\nHOLD=1",
                        directory.display()
                    ),
                    launch: CommandLine {
                        argv: vec!["/bin/true".into()],
                        env: vec![],
                        cwd: directory.clone(),
                        codex: false,
                    },
                })
                .unwrap();
            assert!(matches!(
                client.recv_timeout(Duration::from_secs(3)),
                Some(AgentOutcome::Launched(_))
            ));
            for _ in 0..if recover { 2 } else { 3 } {
                assert!(matches!(
                    client.recv_timeout(Duration::from_secs(3)),
                    Some(AgentOutcome::InitTimedOut(_))
                ));
            }
            if recover {
                fs::write(
                    directory.join("ready.json"),
                    r#"{"status":"ready","session":"timeout-session"}"#,
                )
                .unwrap();
                assert!(matches!(
                    client.recv_timeout(Duration::from_secs(3)),
                    Some(AgentOutcome::Ready((), _))
                ));
            }
            let calls = fs::read_to_string(socket.with_extension("calls")).unwrap();
            assert_eq!(
                calls.lines().filter(|line| *line == "terminal.run").count(),
                1
            );
            assert_eq!(
                calls
                    .lines()
                    .filter(|line| *line == "terminal.send")
                    .count(),
                1
            );
            assert!(!calls.lines().any(|line| line == "terminal.kill"));
            drop(client);
            // The single-connection mock can answer only after the worker exits.
            drop(HcClient::connect(&socket, Duration::from_secs(3)).unwrap());
            let calls = fs::read_to_string(socket.with_extension("calls")).unwrap();
            assert!(calls.lines().any(|line| line == "terminal.kill"));
            drop(core);
            fs::remove_dir_all(directory).unwrap();
        }
    }

    #[test]
    fn codex_init_renames_the_thread_to_the_hc_terminal() {
        static TEMP: AtomicU64 = AtomicU64::new(0);
        let directory = std::env::temp_dir().join(format!(
            "mcn-codex-name-{}-{}",
            std::process::id(),
            TEMP.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir_all(&directory).unwrap();
        let socket = directory.join("hc.sock");
        let core = MockCore::spawn(socket.clone()).unwrap();
        let terminal =
            crate::agents::campaign_terminal_name("coverage", Path::new("/campaigns/lightftp"));
        let mut client = AgentClient::<SessionInit, Request>::spawn(WorkerConfig {
            thread_name: "shared-worker-test",
            socket: socket.clone(),
            terminal: terminal.clone(),
            init_timeout: Duration::from_secs(2),
            task_timeout: Duration::from_secs(2),
            secrets: vec![],
        })
        .unwrap();
        client
            .send(AgentCommand::Init {
                context: SessionInit {
                    output_dir: directory.clone(),
                    session: "shared-session".into(),
                },
                prompt: format!(
                    "OUTPUT_DIR={}\nSESSION=shared-session\nWRITE ready.json",
                    directory.display()
                ),
                launch: CommandLine {
                    argv: vec!["/usr/bin/codex".into(), "--no-daemon".into()],
                    env: vec![],
                    cwd: directory.clone(),
                    codex: true,
                },
            })
            .unwrap();
        assert!(matches!(
            client.recv_timeout(Duration::from_secs(3)),
            Some(AgentOutcome::Launched(_))
        ));
        assert!(matches!(
            client.recv_timeout(Duration::from_secs(3)),
            Some(AgentOutcome::Ready((), _))
        ));
        let sent = fs::read_to_string(socket.with_extension("sends")).unwrap();
        let rename = format!("/rename {terminal}");
        let prompt_at = sent.find("WRITE ready.json").expect("init prompt");
        let rename_at = sent.find(&rename).expect("rename command");
        assert!(rename_at < prompt_at);
        assert_eq!(terminal, "mcn-fuzzer-coverage-lightftp");
        assert!(sent.starts_with(&format!("{rename}\n")));
        drop(client);
        drop(core);
        let _ = fs::remove_dir_all(directory);
    }

    #[test]
    fn codex_status_line_is_not_the_unsubmitted_rename_command() {
        assert!(codex_composer_ready("Ask Codex to do anything"));
        assert!(codex_thread_name_visible(
            "GPT · /tmp/probe · mcn-fuzzer-synthesis-lightftp",
            "mcn-fuzzer-synthesis-lightftp",
        ));
        assert!(!codex_thread_name_visible(
            "› /rename mcn-fuzzer-synthesis-lightftp",
            "mcn-fuzzer-synthesis-lightftp",
        ));
        assert_eq!(
            plain_terminal("\u{1b}[32mAsk Codex to do anything\u{1b}[0m"),
            "Ask Codex to do anything"
        );
    }

    #[test]
    fn disconnected_worker_is_reported_once() {
        let (commands, _requests) = mpsc::channel();
        let (sender, outcomes) = mpsc::channel();
        let mut client = AgentClient::<SessionInit, Request>::from_channels(commands, outcomes);
        drop(sender);
        assert!(matches!(
            client.recv_timeout(Duration::ZERO),
            Some(AgentOutcome::Failed(_))
        ));
        assert!(client.recv_timeout(Duration::ZERO).is_none());
    }
}
