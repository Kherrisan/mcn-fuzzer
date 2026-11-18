use std::{
    collections::VecDeque,
    io::{BufRead, BufReader, Write},
    os::unix::process::CommandExt,
    path::{Path, PathBuf},
    process::{ChildStdin, Command, Stdio},
    sync::mpsc::{self, Receiver},
    thread,
    time::{Duration, Instant, SystemTime},
};

use serde_json::{json, Value};

use crate::agents::CommandLine;

use super::{error, unix_ms, Service, Stage};

pub(super) struct Source {
    pub thread: String,
    pub target: String,
    previous: String,
    pub prompt: String,
    pub started_at_ms: u64,
}

pub(super) fn find(root: &Path, request: &Path, id: &str, stage: Stage) -> Result<Source, String> {
    let home = std::env::var_os("CODEX_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".codex")))
        .ok_or("cannot locate the Codex session store")?;
    let mut matches = Vec::new();
    for directory in [home.join("sessions"), home.join("archived_sessions")] {
        scan_directory(&directory, root, request, id, stage, &mut matches)?;
    }
    match matches.len() {
        1 => Ok(matches.remove(0)),
        0 => Err(format!(
            "no recorded Codex turn found for request {id} in {}",
            root.display()
        )),
        _ => Err(format!("multiple recorded Codex turns match request {id}")),
    }
}

fn scan_directory(
    directory: &Path,
    root: &Path,
    request: &Path,
    id: &str,
    stage: Stage,
    matches: &mut Vec<Source>,
) -> Result<(), String> {
    let entries = match std::fs::read_dir(directory) {
        Ok(entries) => entries,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(e) => return Err(error(e)),
    };
    for entry in entries {
        let entry = entry.map_err(error)?;
        let path = entry.path();
        if entry.file_type().map_err(error)?.is_dir() {
            scan_directory(&path, root, request, id, stage, matches)?;
        } else if path.extension().is_some_and(|ext| ext == "jsonl") {
            if let Some(source) = read_source(&path, root, request, id, stage)? {
                matches.push(source);
            }
        }
    }
    Ok(())
}

fn read_source(
    path: &Path,
    root: &Path,
    request: &Path,
    id: &str,
    stage: Stage,
) -> Result<Option<Source>, String> {
    let mut lines = BufReader::new(std::fs::File::open(path).map_err(error)?).lines();
    let Some(line) = lines.next() else {
        return Ok(None);
    };
    let Ok(header) = serde_json::from_str::<Value>(&line.map_err(error)?) else {
        return Ok(None);
    };
    let meta = &header["payload"];
    let expected_root = root.canonicalize().map_err(error)?;
    let cwd = meta["cwd"]
        .as_str()
        .map(Path::new)
        .and_then(|path| path.canonicalize().ok());
    if header["type"] != "session_meta" || cwd.as_ref() != Some(&expected_root) {
        return Ok(None);
    }
    let thread = meta["id"]
        .as_str()
        .ok_or("Codex session metadata lacks id")?;
    let mut previous = String::new();
    let mut active = String::new();
    let mut started_at_ms = 0;
    for line in lines {
        let line = line.map_err(error)?;
        let Ok(event) = serde_json::from_str::<Value>(&line) else {
            continue;
        };
        let payload = &event["payload"];
        if event["type"] == "event_msg" && payload["type"] == "task_started" {
            previous = std::mem::take(&mut active);
            active = payload["turn_id"].as_str().unwrap_or("").into();
            let millis = event["timestamp"]
                .as_str()
                .and_then(|timestamp| {
                    let (_, fraction) = timestamp.split_once('.')?;
                    fraction.get(..3)?.parse::<u64>().ok()
                })
                .unwrap_or(0);
            started_at_ms = payload["started_at"].as_u64().unwrap_or(0) * 1000 + millis;
        }
        if event["type"] != "response_item"
            || payload["type"] != "message"
            || payload["role"] != "user"
        {
            continue;
        }
        let prompt = payload["content"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|item| item["text"].as_str())
            .collect::<Vec<_>>()
            .join("\n");
        let matched = match stage {
            Stage::Analysis => {
                super::field(&prompt, "REQUEST") == Some(id)
                    && prompt.starts_with("# Coverage analysis request")
            }
            Stage::Synthesis => {
                super::field(&prompt, "REQUEST") == Some(id)
                    && prompt.starts_with("# Assignment")
                    && prompt.contains(request.to_str().ok_or("invalid request path")?)
            }
        };
        if matched {
            if previous.is_empty() || active.is_empty() {
                return Err("request has no preceding Codex turn boundary".into());
            }
            return Ok(Some(Source {
                thread: thread.into(),
                target: active,
                previous,
                prompt,
                started_at_ms,
            }));
        }
    }
    Ok(None)
}

struct Rpc {
    _service: Service,
    stdin: ChildStdin,
    stdout: Receiver<Value>,
    id: u64,
    notifications: VecDeque<Value>,
}

impl Rpc {
    fn launch(launch: &CommandLine, out: &Path) -> Result<Self, String> {
        let (program, args) = launch.argv.split_first().ok_or("empty Codex command")?;
        let mut arguments = Vec::new();
        let mut args = args.iter();
        while let Some(arg) = args.next() {
            match arg.as_str() {
                "--no-daemon" => {}
                "--model" | "-m" => {
                    let model = args.next().ok_or("missing model value")?;
                    arguments.extend(["-c".into(), format!("model={}", json!(model))]);
                }
                _ => arguments.push(arg.clone()),
            }
        }
        let mut child = Service(
            Command::new(program)
                .args(arguments)
                .args(["app-server", "--listen", "stdio://"])
                .envs(launch.env.iter().map(|(k, v)| (k, v)))
                .current_dir(&launch.cwd)
                .process_group(0)
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::from(
                    std::fs::File::create(out.join("codex.stderr.log")).map_err(error)?,
                ))
                .spawn()
                .map_err(error)?,
        );
        let stdin = child.0.stdin.take().ok_or("Codex stdout setup failed")?;
        let stdout = child.0.stdout.take().ok_or("Codex stdout setup failed")?;
        let (send, receive) = mpsc::channel();
        thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if let Ok(value) = serde_json::from_str::<Value>(&line) {
                    // Keep protocol replies and observable metadata, not model reasoning text.
                    let method = value["method"].as_str().unwrap_or("");
                    if !method.starts_with("item/reasoning/") && send.send(value).is_err() {
                        break;
                    }
                }
            }
        });
        Ok(Self {
            _service: child,
            stdin,
            stdout: receive,
            id: 0,
            notifications: VecDeque::new(),
        })
    }

    fn send(&mut self, value: &Value) -> Result<(), String> {
        writeln!(self.stdin, "{value}").map_err(error)?;
        self.stdin.flush().map_err(error)
    }

    fn receive(&self, deadline: Instant) -> Result<Value, String> {
        self.stdout
            .recv_timeout(deadline.saturating_duration_since(Instant::now()))
            .map_err(|e| match e {
                mpsc::RecvTimeoutError::Timeout => "Codex request timed out".into(),
                mpsc::RecvTimeoutError::Disconnected => {
                    "Codex app-server closed the connection".into()
                }
            })
    }

    fn call(&mut self, deadline: Instant, method: &str, params: Value) -> Result<Value, String> {
        self.id += 1;
        self.send(&json!({"id": self.id, "method":method, "params":params}))?;
        loop {
            let value = self.receive(deadline)?;
            if value["id"].as_u64() == Some(self.id) && value.get("method").is_none() {
                if let Some(e) = value.get("error") {
                    return Err(format!("{method}: {e}"));
                }
                return Ok(value["result"].clone());
            }
            if value.get("id").is_some() && value.get("method").is_some() {
                return Err(format!(
                    "Codex requested interactive input for {}",
                    value["method"]
                ));
            }
            self.notifications.push_back(value);
        }
    }
}

fn turn_ids(response: &Value) -> Result<Vec<&str>, String> {
    response["thread"]["turns"]
        .as_array()
        .ok_or("app-server did not return thread turns")?
        .iter()
        .map(|turn| turn["id"].as_str().ok_or("thread turn lacks id".into()))
        .collect()
}

pub(super) fn run(
    launch: &CommandLine,
    source: &Source,
    prompt: &str,
    timeout: Duration,
    out: &Path,
) -> Result<Value, String> {
    let mut rpc = Rpc::launch(launch, out)?;
    let prep_deadline = Instant::now() + Duration::from_secs(60);
    rpc.call(
        prep_deadline,
        "initialize",
        json!({"clientInfo":{"name":"mcn-fuzz","version":env!("CARGO_PKG_VERSION")}}),
    )?;
    rpc.send(&json!({"method":"initialized"}))?;
    let original = rpc.call(
        prep_deadline,
        "thread/read",
        json!({"threadId":source.thread,"includeTurns":true}),
    )?;
    let original_ids = turn_ids(&original)?;
    let target = original_ids
        .iter()
        .position(|id| *id == source.target)
        .ok_or("target turn not found by app-server")?;
    if target == 0 || original_ids[target - 1] != source.previous {
        return Err("previous turn does not match the app-server history".into());
    }
    let fork = rpc.call(
        prep_deadline,
        "thread/fork",
        json!({
            "threadId":source.thread, "lastTurnId":source.previous, "cwd":out,
            "excludeTurns": true,
        }),
    )?;
    let fork_id = fork["thread"]["id"]
        .as_str()
        .ok_or("fork lacks thread id")?;
    let fork_history = rpc.call(
        prep_deadline,
        "thread/read",
        json!({"threadId":fork_id,"includeTurns":true}),
    )?;
    let prefix = original["thread"]["turns"]
        .as_array()
        .ok_or("missing source turns")?;
    if fork_id == source.thread
        || fork_history["thread"]["turns"]
            .as_array()
            .map(Vec::as_slice)
            != Some(&prefix[..target])
    {
        return Err("forked thread has a different turn prefix".into());
    }
    rpc.notifications.clear();
    let started = SystemTime::now();
    let deadline = Instant::now() + timeout;
    let reply = rpc.call(
        deadline,
        "turn/start",
        json!({
            "threadId":fork_id,"input":[{"type":"text","text":prompt,"text_elements":[]}],
            "model":fork["model"], "effort":fork["reasoningEffort"],
        }),
    )?;
    let id = reply["turn"]["id"].as_str().ok_or("turn/start lacks id")?;
    super::write_json(
        &out.join("turn-started.json"),
        &json!({
            "thread_id":fork_id, "turn_id":id, "sent_at_unix_ms":unix_ms(started),
            "model":fork["model"], "reasoning_effort":fork["reasoningEffort"],
        }),
    )?;
    let mut usage = Value::Null;
    let mut settings = Value::Null;
    let mut tool_count = 0;
    let mut tool_errors = 0;
    let mut tool_log = std::fs::File::create(out.join("agent-tools.jsonl")).map_err(error)?;
    loop {
        let message = if let Some(message) = rpc.notifications.pop_front() {
            message
        } else {
            rpc.receive(deadline)?
        };
        let params = &message["params"];
        if message.get("id").is_some() {
            return Err(format!(
                "target turn requested interactive input for {}",
                message["method"]
            ));
        }
        if params["threadId"] != fork_id {
            continue;
        }
        match message["method"].as_str().unwrap_or("") {
            "thread/tokenUsage/updated" if params["turnId"] == id => {
                usage = params["tokenUsage"].clone()
            }
            "thread/settings/updated" => settings = params["threadSettings"].clone(),
            "item/completed" if params["item"]["type"] == "mcpToolCall" => {
                tool_count += 1;
                if params["item"]["error"].is_object() {
                    tool_errors += 1;
                }
                let item = &params["item"];
                writeln!(
                    tool_log,
                    "{}",
                    json!({
                        "time_ms": unix_ms(SystemTime::now()),
                        "server": item["server"], "tool": item["tool"],
                        "status": item["status"], "duration_ms": item["durationMs"],
                    })
                )
                .map_err(error)?;
            }
            "turn/completed" if params["turn"]["id"] == id => {
                return Ok(json!({
                    "thread_id":fork_id, "turn_id":id, "status":params["turn"]["status"],
                    "model":fork["model"], "reasoning_effort":fork["reasoningEffort"],
                    "settings":settings, "elapsed_ms":unix_ms(SystemTime::now()).saturating_sub(unix_ms(started)),
                    "sent_at_unix_ms":unix_ms(started),
                    "token_usage":usage,"tool_count":tool_count,"tool_errors":tool_errors,
                }));
            }
            _ => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sends_one_turn_after_verified_fork_with_current_profile() {
        let out = std::env::temp_dir().join(format!("mcn-eval-rpc-{}", std::process::id()));
        std::fs::create_dir_all(&out).unwrap();
        let script = out.join("app_server.py");
        std::fs::write(&script, r#"
import json, sys
assert 'model="new-model"' in sys.argv
assert 'model_reasoning_effort=high' in sys.argv
def send(o):
    print(json.dumps(o), flush=True)
prefix = [{"id": "before", "items": [{"type": "userMessage"}]}]
for line in sys.stdin:
    req = json.loads(line)
    if "id" not in req:
        continue
    method, args = req["method"], req["params"]
    if method == "initialize":
        result = {}
    elif method == "thread/read":
        t = args["threadId"]
        result = {"thread": {"id": t, "turns": prefix if t == "fork" else prefix + [{"id": "target"}, {"id": "later"}]}}
    elif method == "thread/fork":
        assert args["lastTurnId"] == "before"
        result = {"thread": {"id": "fork"}, "model": "new-model", "reasoningEffort": "high"}
    elif method == "turn/start":
        assert args["threadId"] == "fork" and args["model"] == "new-model" and args["effort"] == "high"
        assert [item["text"] for item in args["input"]] == ["edited prompt"]
        send({"id": req["id"], "result": {"turn": {"id": "evaluated"}}})
        send({"method": "turn/completed", "params": {"threadId": "fork", "turn": {"id": "evaluated", "status": "completed"}}})
        continue
    else:
        raise AssertionError(method)
    send({"id": req["id"], "result": result})
"#).unwrap();
        let launch = CommandLine {
            argv: vec![
                "python3".into(),
                script.display().to_string(),
                "--model".into(),
                "new-model".into(),
                "-c".into(),
                "model_reasoning_effort=high".into(),
            ],
            env: Vec::new(),
            cwd: out.clone(),
            codex: true,
        };
        let source = Source {
            thread: "source".into(),
            target: "target".into(),
            previous: "before".into(),
            prompt: "original prompt".into(),
            started_at_ms: 1,
        };
        let result = run(
            &launch,
            &source,
            "edited prompt",
            Duration::from_secs(2),
            &out,
        )
        .unwrap();
        assert_eq!(result["thread_id"], "fork");
        assert_eq!(result["turn_id"], "evaluated");
        assert_eq!(result["reasoning_effort"], "high");
        std::fs::remove_dir_all(out).unwrap();
    }

    #[test]
    fn identifies_previous_turn_and_separates_stages_sharing_a_request_id() {
        let root = std::env::temp_dir().join(format!("mcn-eval-session-{}", std::process::id()));
        std::fs::create_dir_all(&root).unwrap();
        let root = root.canonicalize().unwrap();
        let request = root.join("requests/000002-one");
        let path = root.join("rollout.jsonl");
        let log = [
            json!({"type":"session_meta","payload":{"cwd":root,"id":"source"}}),
            json!({"type":"event_msg","payload":{"type":"task_started","turn_id":"init","started_at":1}}),
            json!({"type":"event_msg","payload":{"type":"task_started","turn_id":"analysis","started_at":2}}),
            json!({"type":"response_item","payload":{"type":"message","role":"user","content":[
                {"text":"# Coverage analysis request\n\nREQUEST=000002-one\n"}
            ]}}),
            json!({"type":"event_msg","payload":{"type":"task_started","turn_id":"synthesis","started_at":3}}),
            json!({"type":"response_item","payload":{"type":"message","role":"user","content":[
                {"text":format!("# Assignment\n\nREQUEST=000002-one\nOUTPUT_DIR={}",request.display())}
            ]}}),
        ];
        std::fs::write(
            &path,
            log.iter()
                .map(Value::to_string)
                .collect::<Vec<_>>()
                .join("\n"),
        )
        .unwrap();
        let analysis = read_source(&path, &root, &request, "000002-one", Stage::Analysis)
            .unwrap()
            .unwrap();
        let synthesis = read_source(&path, &root, &request, "000002-one", Stage::Synthesis)
            .unwrap()
            .unwrap();
        assert_eq!(
            (analysis.previous.as_str(), analysis.target.as_str()),
            ("init", "analysis")
        );
        assert_eq!(
            (synthesis.previous.as_str(), synthesis.target.as_str()),
            ("analysis", "synthesis")
        );
        std::fs::remove_dir_all(root).unwrap();
    }
}
