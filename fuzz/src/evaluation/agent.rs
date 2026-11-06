//! Direct Codex, Claude, and Pi sessions. No HC and no shared "last session".

use std::{fs, time::Duration};

use serde_json::Value;

use crate::app::config::AgentProvider;

use super::{
    process::{SpawnSpec, Supervisor},
    task::Task,
};

pub struct DirectAgentSession<'a> {
    pub task: &'a mut Task,
    pub provider: AgentProvider,
    pub deadline: Duration,
    env: Vec<(String, String)>,
    supervisor: Supervisor,
}

impl<'a> DirectAgentSession<'a> {
    pub fn new(
        task: &'a mut Task,
        provider: AgentProvider,
        deadline: Duration,
        env: Vec<(String, String)>,
    ) -> Self {
        let secrets = env
            .iter()
            .map(|(_, value)| value.clone())
            .filter(|value| !value.is_empty())
            .collect();
        Self {
            task,
            provider,
            deadline,
            env,
            supervisor: Supervisor::new(secrets),
        }
    }

    pub fn preflight(&mut self) -> Result<String, (String, String)> {
        let phase = "preflight";
        if !self.task.executable.is_file() {
            return Err(fail(
                self.provider,
                phase,
                "preflight",
                format!("executable {} is missing", self.task.executable.display()),
            ));
        }
        let stdout = self.task.instance_dir.join("agent/preflight.stdout.log");
        let stderr = self.task.instance_dir.join("agent/preflight.stderr.log");
        let pid = self
            .supervisor
            .spawn(&SpawnSpec {
                role: "agent-preflight".to_string(),
                argv: vec![
                    self.task.executable.display().to_string(),
                    "--version".to_string(),
                ],
                env: self.env.clone(),
                cwd: self.task.instance_dir.clone(),
                stdout_log: stdout.clone(),
                stderr_log: stderr.clone(),
                clear_afl: true,
            })
            .map_err(|error| fail(self.provider, phase, "preflight", error))?;
        // Version discovery should be cheap, and must obey a shorter configured
        // initialization budget as well as interruption and process-tree cleanup.
        let status = self.wait_agent(pid, phase, self.deadline.min(Duration::from_secs(5)))?;
        if !status.success() {
            return Err(fail(
                self.provider,
                phase,
                "preflight",
                format!(
                    "version check failed: {}",
                    fs::read_to_string(&stderr).unwrap_or_default()
                ),
            ));
        }
        let version = fs::read_to_string(&stdout)
            .unwrap_or_default()
            .trim()
            .to_string();
        if version.is_empty() {
            return Err(fail(
                self.provider,
                phase,
                "preflight",
                "version check returned an empty version",
            ));
        }
        self.task.cli_version = Some(version.clone());
        Ok(version)
    }

    pub fn initialize(&mut self, prompt: &str) -> Result<(), (String, String)> {
        self.preflight()?;
        let cwd = self.task.workspace();
        fs::create_dir_all(cwd.join("init"))
            .map_err(|error| fail(self.provider, "init", "infrastructure", error.to_string()))?;
        let argv = command_argv(self.task, self.provider, None, prompt);
        let stdout = self.task.instance_dir.join("agent/init.stdout.log");
        let stderr = self.task.instance_dir.join("agent/init.stderr.log");
        let pid = self
            .supervisor
            .spawn(&SpawnSpec {
                role: "agent-init".to_string(),
                argv,
                env: self.env.clone(),
                cwd,
                stdout_log: stdout.clone(),
                stderr_log: stderr.clone(),
                clear_afl: true,
            })
            .map_err(|error| fail(self.provider, "init", "infrastructure", error))?;
        let status = self.wait_agent(pid, "init", self.deadline)?;
        let out = fs::read_to_string(&stdout).unwrap_or_default();
        let err = fs::read_to_string(&stderr).unwrap_or_default();
        if !status.success() && auth_failure(&out, &err) {
            return Err(fail(self.provider, "init", "auth", format!("{err}{out}")));
        }
        if !status.success() {
            return Err(fail(
                self.provider,
                "init",
                "cli_exit",
                format!("exit {}: {err}", status.code().unwrap_or(-1)),
            ));
        }
        let provider_session = parse_session(self.provider, &out)
            .map_err(|error| fail(self.provider, "init", "cli_exit", error))?;
        let ready_path = self.task.workspace().join("init/ready.json");
        if !ready_path.is_file() {
            return Err(fail(
                self.provider,
                "init",
                "missing_ready",
                "ready.json was not published",
            ));
        }
        let ready: Value =
            serde_json::from_slice(&fs::read(&ready_path).map_err(|error| {
                fail(self.provider, "init", "missing_ready", error.to_string())
            })?)
            .map_err(|error| fail(self.provider, "init", "missing_ready", error.to_string()))?;
        if ready.get("status").and_then(Value::as_str) != Some("ready")
            || ready.get("session").and_then(Value::as_str)
                != Some(self.task.logical_session.as_str())
        {
            return Err(fail(
                self.provider,
                "init",
                "missing_ready",
                "ready.json is missing this instance session",
            ));
        }
        self.task.provider_session = Some(provider_session);
        Ok(())
    }

    pub fn generate(&mut self, prompt: &str) -> Result<(), (String, String)> {
        let session = self.task.provider_session.clone().ok_or_else(|| {
            fail(
                self.provider,
                "generate",
                "cli_exit",
                "generate requires the session from initialize",
            )
        })?;
        let request_dir = self.task.request_dir();
        if request_dir.join("result.json").exists() || request_dir.join("generator.py").exists() {
            return Err(fail(
                self.provider,
                "generate",
                "infrastructure",
                "instance already has synthesis artifacts; create a new instance",
            ));
        }
        fs::create_dir_all(&request_dir).map_err(|error| {
            fail(
                self.provider,
                "generate",
                "infrastructure",
                error.to_string(),
            )
        })?;
        fs::copy(
            self.task.prepared().join("proposal.json"),
            request_dir.join("proposal.json"),
        )
        .map_err(|error| {
            fail(
                self.provider,
                "generate",
                "infrastructure",
                error.to_string(),
            )
        })?;
        let argv = command_argv(self.task, self.provider, Some(&session), prompt);
        if argv.iter().any(|arg| arg == "--last") {
            return Err(fail(
                self.provider,
                "generate",
                "infrastructure",
                "refusing --last",
            ));
        }
        let stdout = self.task.instance_dir.join("agent/generate.stdout.log");
        let stderr = self.task.instance_dir.join("agent/generate.stderr.log");
        let pid = self
            .supervisor
            .spawn(&SpawnSpec {
                role: "agent-generate".to_string(),
                argv,
                env: self.env.clone(),
                cwd: self.task.workspace(),
                stdout_log: stdout.clone(),
                stderr_log: stderr.clone(),
                clear_afl: true,
            })
            .map_err(|error| fail(self.provider, "generate", "infrastructure", error))?;
        let status = self.wait_agent(pid, "generate", self.deadline)?;
        let out = fs::read_to_string(&stdout).unwrap_or_default();
        let err = fs::read_to_string(&stderr).unwrap_or_default();
        if !status.success() && auth_failure(&out, &err) {
            return Err(fail(
                self.provider,
                "generate",
                "auth",
                format!("{err}{out}"),
            ));
        }
        if !status.success() {
            return Err(fail(
                self.provider,
                "generate",
                "cli_exit",
                format!("exit {}: {err}", status.code().unwrap_or(-1)),
            ));
        }
        parse_completion(self.provider, &out)
            .map_err(|error| fail(self.provider, "generate", "cli_exit", error))?;
        if !request_dir.join("result.json").is_file() {
            return Err(fail(
                self.provider,
                "generate",
                "missing_result",
                "result.json was not published",
            ));
        }
        Ok(())
    }

    fn wait_agent(
        &mut self,
        pid: u32,
        phase: &str,
        budget: Duration,
    ) -> Result<std::process::ExitStatus, (String, String)> {
        let status = self.supervisor.wait_for(pid, budget).map_err(|error| {
            let kind = if error.contains("deadline") {
                "deadline"
            } else if error.contains("interrupted") {
                "interrupted"
            } else {
                "infrastructure"
            };
            fail(self.provider, phase, kind, error)
        })?;
        // Root exit is independent of the pipe readers. Do not parse session or
        // completion events until all buffered output has reached the log files.
        self.supervisor
            .finish_child_logs(pid, Duration::from_millis(500))
            .map_err(|error| fail(self.provider, phase, "infrastructure", error))?;
        Ok(status)
    }
}

pub fn provider_of(task: &Task) -> Result<AgentProvider, String> {
    match task.provider.as_str() {
        "codex" => Ok(AgentProvider::Codex),
        "claude" => Ok(AgentProvider::Claude),
        "pi" => Ok(AgentProvider::Pi),
        other => Err(format!("unknown provider {other}")),
    }
}

fn fail(
    provider: AgentProvider,
    phase: &str,
    kind: &str,
    message: impl AsRef<str>,
) -> (String, String) {
    let name = match provider {
        AgentProvider::Codex => "codex",
        AgentProvider::Claude => "claude",
        AgentProvider::Pi => "pi",
    };
    (
        kind.to_string(),
        format!("{name} {phase}: {}", message.as_ref()),
    )
}

fn command_argv(
    task: &Task,
    provider: AgentProvider,
    resume: Option<&str>,
    prompt: &str,
) -> Vec<String> {
    let mut argv = vec![task.executable.display().to_string()];
    match provider {
        AgentProvider::Codex => {
            argv.push("--no-daemon".to_string());
            argv.push("exec".to_string());
            argv.push("--json".to_string());
            argv.push("--skip-git-repo-check".to_string());
            argv.push("--sandbox".to_string());
            argv.push("workspace-write".to_string());
            argv.push("-C".to_string());
            argv.push(task.workspace().display().to_string());
            if let Some(model) = &task.model_name {
                argv.push("-m".to_string());
                argv.push(model.clone());
            }
            if let Some(effort) = &task.effort {
                argv.push("-c".to_string());
                argv.push(format!("model_reasoning_effort={effort:?}"));
            }
            push_filtered_args(&mut argv, &task.profile_args);
            if let Some(id) = resume {
                argv.push("resume".to_string());
                argv.push(id.to_string());
            }
        }
        AgentProvider::Claude => {
            argv.push("-p".to_string());
            argv.push("--output-format".to_string());
            argv.push("json".to_string());
            argv.push("--permission-mode".to_string());
            argv.push("dontAsk".to_string());
            if let Some(model) = &task.model_name {
                argv.push("--model".to_string());
                argv.push(model.clone());
            }
            if let Some(effort) = &task.effort {
                argv.push("--effort".to_string());
                argv.push(effort.clone());
            }
            push_filtered_args(&mut argv, &task.profile_args);
            if let Some(id) = resume {
                argv.push("--resume".to_string());
                argv.push(id.to_string());
            } else {
                argv.push("--session-id".to_string());
                argv.push(task.logical_session.clone());
            }
        }
        AgentProvider::Pi => {
            argv.push("--mode".to_string());
            argv.push("json".to_string());
            argv.push("--session".to_string());
            argv.push(
                task.instance_dir
                    .join("agent/pi-session.json")
                    .display()
                    .to_string(),
            );
            if let Some(model) = &task.model_name {
                argv.push("--model".to_string());
                argv.push(model.clone());
            }
            if let Some(effort) = &task.effort {
                argv.push("--thinking".to_string());
                argv.push(effort.clone());
            }
            push_filtered_args(&mut argv, &task.profile_args);
        }
    }
    argv.push(prompt.to_string());
    argv
}

fn push_filtered_args(argv: &mut Vec<String>, args: &[String]) {
    let blocked = [
        "--last",
        "--resume",
        "--session-id",
        "--session",
        "--print",
        "-p",
        "--output-format",
        "--json",
        "--mode",
        "--cd",
        "-C",
        "--no-daemon",
        "--model",
        "-m",
        "--effort",
        "--thinking",
        "--sandbox",
        "exec",
        "resume",
    ];
    let mut skip_value = false;
    for arg in args {
        if skip_value {
            skip_value = false;
            continue;
        }
        if blocked.contains(&arg.as_str()) {
            skip_value = !arg.starts_with("--last") && arg != "exec" && arg != "resume";
            continue;
        }
        argv.push(arg.clone());
    }
}

fn auth_failure(stdout: &str, stderr: &str) -> bool {
    let needles = [
        "authentication",
        "not logged",
        "unauthorized",
        "please log in",
        "401 ",
        "permission denied",
    ];
    let matches = |text: &str| {
        let lower = text.to_ascii_lowercase();
        needles.iter().any(|needle| lower.contains(needle))
    };
    // Tool output and assistant prose can contain RFC authentication terminology
    // and FTP 530 replies. Only CLI diagnostics and terminal error events count.
    matches(stderr)
        || json_values(stdout).any(|event| {
            let terminal_error = matches!(
                event.get("type").and_then(Value::as_str),
                Some("error") | Some("turn.failed")
            ) || (event.get("type").and_then(Value::as_str) == Some("result")
                && event.get("is_error").and_then(Value::as_bool) == Some(true));
            terminal_error && matches(&event.to_string())
        })
}

#[cfg(test)]
mod auth_tests {
    use super::auth_failure;

    #[test]
    fn rfc_and_tool_output_are_not_cli_authentication_failures() {
        let stdout = r#"{"type":"item.completed","item":{"type":"command_execution","aggregated_output":"authentication is required; 530 Not logged in; Permission denied"}}
{"type":"item.completed","item":{"type":"agent_message","text":"RFC access controls prevent unauthorized use"}}
{"type":"turn.completed"}"#;
        assert!(!auth_failure(
            stdout,
            "Reading additional input from stdin..."
        ));
        assert!(auth_failure("", "authentication failed"));
        assert!(auth_failure(
            r#"{"type":"turn.failed","error":{"message":"401 unauthorized"}}"#,
            ""
        ));
    }
}

fn parse_session(provider: AgentProvider, stdout: &str) -> Result<String, String> {
    match provider {
        AgentProvider::Codex => json_lines(stdout)
            .find_map(|value| {
                if value.get("type").and_then(Value::as_str) == Some("thread.started") {
                    value
                        .get("thread_id")
                        .and_then(Value::as_str)
                        .map(str::to_string)
                } else {
                    None
                }
            })
            .ok_or_else(|| "codex did not report a thread id".to_string()),
        AgentProvider::Claude => json_values(stdout)
            .find_map(|value| {
                value
                    .get("session_id")
                    .and_then(Value::as_str)
                    .map(str::to_string)
            })
            .ok_or_else(|| "claude did not report a session id".to_string()),
        AgentProvider::Pi => json_lines(stdout)
            .find_map(|value| {
                if value.get("type").and_then(Value::as_str) == Some("session") {
                    value.get("id").and_then(Value::as_str).map(str::to_string)
                } else {
                    None
                }
            })
            .ok_or_else(|| "pi did not report a session id".to_string()),
    }
}

fn parse_completion(provider: AgentProvider, stdout: &str) -> Result<(), String> {
    let done = match provider {
        AgentProvider::Codex => json_lines(stdout).any(|value| {
            matches!(
                value.get("type").and_then(Value::as_str),
                Some("turn.completed") | Some("thread.completed")
            )
        }),
        AgentProvider::Claude => json_values(stdout).any(|value| {
            value.get("is_error").and_then(Value::as_bool) == Some(false)
                || value.get("type").and_then(Value::as_str) == Some("result")
        }),
        AgentProvider::Pi => json_lines(stdout)
            .any(|value| value.get("type").and_then(Value::as_str) == Some("done")),
    };
    if done {
        Ok(())
    } else {
        Err("agent output did not include a completion marker".to_string())
    }
}

fn json_lines(text: &str) -> impl Iterator<Item = Value> + '_ {
    text.lines()
        .filter_map(|line| serde_json::from_str(line).ok())
}

fn json_values(text: &str) -> impl Iterator<Item = Value> + '_ {
    let whole = serde_json::from_str::<Value>(text).ok();
    json_lines(text).chain(whole.into_iter())
}
