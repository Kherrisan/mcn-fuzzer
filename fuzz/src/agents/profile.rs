//! Provider profiles become argv, env, and a working directory.
//!
//! API backends stay in each tool's own flags and environment. There is no
//! shared backend enum beyond the executable each profile already names.

use std::path::Path;

use crate::app::config::{AgentProfile, AgentProvider};

/// One concrete command. Environment values are not logged by this function.
#[derive(Clone, Debug)]
pub struct CommandLine {
    pub argv: Vec<String>,
    pub env: Vec<(String, String)>,
    pub cwd: std::path::PathBuf,
    /// Interactive Codex. Its thread is named with `/rename`, which calls
    /// app-server `thread/name/set`. CLI 0.166.0 rejects `--name`.
    pub codex: bool,
}

impl CommandLine {
    /// Values used only to redact observations, never written to session logs.
    pub(crate) fn secrets(&self) -> Vec<String> {
        self.env
            .iter()
            .filter(|(_, value)| !value.is_empty())
            .map(|(_, value)| value.clone())
            .collect()
    }
}

/// Build the interactive command for a profile. Print/exec one-shot modes are not used.
pub fn command_line(profile: &AgentProfile, cwd: &Path) -> Result<CommandLine, libafl::Error> {
    if profile.executable.as_os_str().is_empty() {
        return Err(libafl::Error::illegal_argument(
            "agent profile executable is empty".to_string(),
        ));
    }
    let mut argv = vec![profile.executable.display().to_string()];
    let codex = matches!(profile.provider, AgentProvider::Codex);
    match profile.provider {
        AgentProvider::Codex => {
            argv.push("--no-daemon".to_string());
            // Fuzzer workers run with the campaign user's permissions. A
            // startup self-update can try to replace a system-wide npm install
            // and exit with EACCES before the agent publishes ready.json.
            argv.extend([
                "-c".to_string(),
                "check_for_update_on_startup=false".to_string(),
            ]);
            if let Some(model) = profile.model.as_deref() {
                argv.push("--model".to_string());
                argv.push(model.to_string());
            }
        }
        AgentProvider::Claude | AgentProvider::Pi => {
            if let Some(model) = profile.model.as_deref() {
                argv.push("--model".to_string());
                argv.push(model.to_string());
            }
        }
    }
    if let Some(effort) = profile.effort.as_deref() {
        match profile.provider {
            AgentProvider::Codex => {
                argv.push("-c".to_string());
                argv.push(format!("model_reasoning_effort={effort}"));
            }
            AgentProvider::Claude => {
                argv.push("--effort".to_string());
                argv.push(effort.to_string());
            }
            AgentProvider::Pi => {}
        }
    }
    argv.extend(profile.args.iter().cloned());
    Ok(CommandLine {
        argv,
        env: profile.env.clone(),
        cwd: cwd.to_path_buf(),
        codex,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::{Path, PathBuf};

    fn profile(provider: AgentProvider, effort: Option<&str>) -> AgentProfile {
        AgentProfile {
            provider,
            executable: PathBuf::from("/usr/bin/agent"),
            model: Some("m".to_string()),
            effort: effort.map(str::to_string),
            args: vec!["--extra".to_string()],
            env: vec![("TOKEN".to_string(), "secret".to_string())],
        }
    }

    #[test]
    fn providers_keep_their_own_flags_and_do_not_print_env() {
        let codex = command_line(
            &profile(AgentProvider::Codex, Some("high")),
            Path::new("/work"),
        )
        .unwrap();
        assert_eq!(
            codex.argv,
            vec![
                "/usr/bin/agent",
                "--no-daemon",
                "-c",
                "check_for_update_on_startup=false",
                "--model",
                "m",
                "-c",
                "model_reasoning_effort=high",
                "--extra"
            ]
            .into_iter()
            .map(str::to_string)
            .collect::<Vec<_>>()
        );
        let claude = command_line(
            &profile(AgentProvider::Claude, Some("low")),
            Path::new("/work"),
        )
        .unwrap();
        assert_eq!(
            claude.argv,
            vec![
                "/usr/bin/agent",
                "--model",
                "m",
                "--effort",
                "low",
                "--extra"
            ]
            .into_iter()
            .map(str::to_string)
            .collect::<Vec<_>>()
        );
        let pi = command_line(
            &profile(AgentProvider::Pi, Some("high")),
            Path::new("/work"),
        )
        .unwrap();
        assert_eq!(
            pi.argv,
            vec!["/usr/bin/agent", "--model", "m", "--extra"]
                .into_iter()
                .map(str::to_string)
                .collect::<Vec<_>>()
        );
        assert!(codex.codex);
        assert!(!claude.codex);
        assert!(!pi.codex);
        assert!(codex.env.iter().any(|(key, _)| key == "TOKEN"));
        let rendered = format!("{:?}", codex.argv);
        assert!(!rendered.contains("secret"));
        for argv in [&codex.argv, &claude.argv, &pi.argv] {
            assert!(!argv.iter().any(|arg| arg == "--print" || arg == "exec"));
        }
    }
}
