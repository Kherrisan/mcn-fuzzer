//! Line-delimited JSON-RPC over the campaign Unix socket.
//!
//! The client never starts a core. `hello`, terminal run/send/read/get,
//! interrupt, and kill are the operations the synthesis stage uses.

use std::{
    io::{BufRead, BufReader, Write},
    os::unix::net::UnixStream,
    path::Path,
    time::Duration,
};

use serde_json::{json, Value};

#[derive(Debug)]
pub struct HcError {
    pub message: String,
}

impl HcError {
    fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl From<HcError> for libafl::Error {
    fn from(error: HcError) -> Self {
        libafl::Error::unknown(error.message)
    }
}

#[derive(Clone, Debug)]
pub struct TerminalInfo {
    pub name: String,
    pub state: String,
    /// Latest raw HC status. Objects are stored as their JSON text.
    pub status: Option<String>,
    /// Latest raw HC error, when the terminal result carries one.
    pub error: Option<String>,
}

pub struct HcClient {
    reader: BufReader<UnixStream>,
    writer: UnixStream,
    next_id: u64,
    io_timeout: Duration,
}

impl HcClient {
    pub fn connect(path: &Path, io_timeout: Duration) -> Result<Self, HcError> {
        let stream = UnixStream::connect(path).map_err(|error| {
            HcError::new(format!(
                "connecting to HC socket {}: {error}",
                path.display()
            ))
        })?;
        stream
            .set_read_timeout(Some(io_timeout))
            .map_err(|error| HcError::new(format!("HC read timeout: {error}")))?;
        stream
            .set_write_timeout(Some(io_timeout))
            .map_err(|error| HcError::new(format!("HC write timeout: {error}")))?;
        let writer = stream
            .try_clone()
            .map_err(|error| HcError::new(format!("HC socket clone: {error}")))?;
        let mut client = Self {
            reader: BufReader::new(stream),
            writer,
            next_id: 1,
            io_timeout,
        };
        let _ = client.call("hello", json!({"generation": 1, "framed": false}));
        Ok(client)
    }

    pub fn call(&mut self, method: &str, params: Value) -> Result<Value, HcError> {
        let id = self.next_id;
        self.next_id = self.next_id.wrapping_add(1);
        let body = json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params});
        let line = serde_json::to_string(&body).map_err(|error| HcError::new(error.to_string()))?;
        self.writer
            .write_all(line.as_bytes())
            .and_then(|_| self.writer.write_all(b"\n"))
            .and_then(|_| self.writer.flush())
            .map_err(|error| HcError::new(format!("writing HC {method}: {error}")))?;
        let deadline = std::time::Instant::now() + self.io_timeout;
        loop {
            if std::time::Instant::now() >= deadline {
                return Err(HcError::new(format!("HC {method} timed out")));
            }
            let mut response = String::new();
            let read = match self.reader.read_line(&mut response) {
                Ok(read) => read,
                // Linux reports SO_RCVTIMEO as EAGAIN. That is a poll interval,
                // not a dead socket, so keep waiting until the call deadline.
                Err(error) if io_wait(&error) => {
                    std::thread::sleep(Duration::from_millis(50));
                    continue;
                }
                Err(error) => {
                    return Err(HcError::new(format!("reading HC {method}: {error}")));
                }
            };
            if read == 0 {
                return Err(HcError::new(format!("HC socket closed during {method}")));
            }
            let value: Value = serde_json::from_str(response.trim())
                .map_err(|error| HcError::new(format!("HC response is not JSON: {error}")))?;
            if value.get("id").and_then(Value::as_u64) != Some(id) {
                continue;
            }
            if let Some(error) = value.get("error") {
                return Err(HcError::new(format!("HC {method} failed: {error}")));
            }
            return Ok(value.get("result").cloned().unwrap_or(Value::Null));
        }
    }

    pub fn run_terminal(
        &mut self,
        name: &str,
        argv: &[String],
        env: &[(String, String)],
        cwd: &Path,
    ) -> Result<TerminalInfo, HcError> {
        let env_map: serde_json::Map<String, Value> = env
            .iter()
            .map(|(key, value)| (key.clone(), Value::String(value.clone())))
            .collect();
        let result = self.call(
            "terminal.run",
            json!({
                "name": name,
                "argv": argv,
                "cwd": cwd,
                "env": env_map,
                "no_parent": true,
                "wait": false
            }),
        )?;
        Ok(info_from(&result, name))
    }

    pub fn send(&mut self, terminal: &str, text: &str) -> Result<(), HcError> {
        self.call("terminal.send", json!({"terminal": terminal, "text": text}))?;
        Ok(())
    }

    pub fn read(&mut self, terminal: &str, lines: usize) -> Result<String, HcError> {
        let result = self.call(
            "terminal.read",
            json!({"terminal": terminal, "lines": lines}),
        )?;
        Ok(result
            .get("text")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string())
    }

    pub fn get(&mut self, terminal: &str) -> Result<TerminalInfo, HcError> {
        let result = self.call("terminal.get", json!({"terminal": terminal}))?;
        Ok(info_from(&result, terminal))
    }

    pub fn interrupt(&mut self, terminal: &str) -> Result<(), HcError> {
        self.call("terminal.interrupt", json!({"terminal": terminal}))?;
        Ok(())
    }

    pub fn kill(&mut self, terminal: &str) -> Result<(), HcError> {
        self.call("terminal.kill", json!({"terminal": terminal}))?;
        Ok(())
    }
}

fn info_from(result: &Value, fallback: &str) -> TerminalInfo {
    TerminalInfo {
        name: result
            .get("address")
            .or_else(|| result.get("terminal"))
            .or_else(|| result.get("name"))
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .unwrap_or(fallback)
            .to_string(),
        state: result
            .get("state")
            .and_then(Value::as_str)
            .unwrap_or("unknown")
            .to_string(),
        status: json_text(result.get("status")),
        error: json_text(result.get("error")),
    }
}

/// Keep a string as itself and any other JSON value as compact JSON.
fn json_text(value: Option<&Value>) -> Option<String> {
    match value {
        None | Some(Value::Null) => None,
        Some(Value::String(text)) => Some(text.clone()),
        Some(other) => Some(other.to_string()),
    }
}

/// In-process core used by tests. It speaks the same line protocol.
pub struct MockCore {
    pub path: std::path::PathBuf,
    thread: Option<std::thread::JoinHandle<()>>,
    stop: std::sync::Arc<std::sync::atomic::AtomicBool>,
}

impl MockCore {
    pub fn spawn(path: std::path::PathBuf) -> Result<Self, String> {
        let _ = std::fs::remove_file(&path);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|error| error.to_string())?;
        }
        let listener =
            std::os::unix::net::UnixListener::bind(&path).map_err(|error| error.to_string())?;
        let stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let flag = stop.clone();
        let sends = path.with_extension("sends");
        let _ = std::fs::remove_file(&sends);
        let thread = std::thread::spawn(move || mock_loop(listener, flag, sends));
        Ok(Self {
            path,
            thread: Some(thread),
            stop,
        })
    }
}

impl Drop for MockCore {
    fn drop(&mut self) {
        self.stop.store(true, std::sync::atomic::Ordering::Relaxed);
        let _ = UnixStream::connect(&self.path);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
        let _ = std::fs::remove_file(&self.path);
    }
}

fn mock_loop(
    listener: std::os::unix::net::UnixListener,
    stop: std::sync::Arc<std::sync::atomic::AtomicBool>,
    sends: std::path::PathBuf,
) {
    let _ = listener.set_nonblocking(false);
    while !stop.load(std::sync::atomic::Ordering::Relaxed) {
        let Ok((stream, _)) = listener.accept() else {
            break;
        };
        let _ = stream.set_read_timeout(Some(Duration::from_millis(200)));
        let mut reader = BufReader::new(stream.try_clone().unwrap());
        let mut writer = stream;
        let mut line = String::new();
        // Codex 0.166.0 shows this placeholder once the composer accepts input.
        // `/rename` then puts ` · <name>` on the status line.
        let mut screen = String::from("OpenAI Codex\nAsk Codex to do anything\n");
        loop {
            line.clear();
            if stop.load(std::sync::atomic::Ordering::Relaxed) {
                return;
            }
            match reader.read_line(&mut line) {
                Ok(0) => break,
                Err(error)
                    if error.kind() == std::io::ErrorKind::TimedOut
                        || error.kind() == std::io::ErrorKind::WouldBlock =>
                {
                    continue;
                }
                Err(_) => break,
                Ok(_) => {}
            }
            let Ok(value) = serde_json::from_str::<Value>(line.trim()) else {
                continue;
            };
            let id = value.get("id").cloned().unwrap_or(Value::from(0));
            let method = value.get("method").and_then(Value::as_str).unwrap_or("");
            let params = value.get("params").cloned().unwrap_or(Value::Null);
            let result = match method {
                "hello" => json!({"protocol": 1}),
                "terminal.run" => {
                    let name = params
                        .get("name")
                        .and_then(Value::as_str)
                        .unwrap_or("agent");
                    json!({"address": name, "name": name, "terminal": name, "state": "running", "status": null_status()})
                }
                "terminal.send" => {
                    let text = params.get("text").and_then(Value::as_str).unwrap_or("");
                    if let Ok(mut record) = std::fs::OpenOptions::new()
                        .create(true)
                        .append(true)
                        .open(&sends)
                    {
                        let _ = writeln!(record, "{text}");
                    }
                    if let Some(name) = text.strip_prefix("/rename ") {
                        let name = name.trim();
                        if !name.is_empty() {
                            screen = format!("OpenAI Codex\n · {name}\n");
                        }
                    }
                    let _ = apply_prompt(text);
                    json!({"ok": true})
                }
                "terminal.read" => json!({"text": screen.clone()}),
                "terminal.get" => {
                    json!({"terminal": params.get("terminal").and_then(Value::as_str).unwrap_or("agent"), "state": "live", "status": "idle"})
                }
                "terminal.interrupt" | "terminal.kill" | "server.stop" | "core.shutdown" => {
                    json!({"ok": true, "stopped": true})
                }
                _ => json!({"ok": false}),
            };
            let response = json!({"jsonrpc": "2.0", "id": id, "result": result});
            let _ = writeln!(writer, "{response}");
        }
    }
}

fn io_wait(error: &std::io::Error) -> bool {
    matches!(
        error.kind(),
        std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock
    ) || error.raw_os_error() == Some(libc::EAGAIN)
}

fn null_status() -> Value {
    Value::Null
}

fn apply_prompt(text: &str) -> Result<(), String> {
    let output_dir = text
        .lines()
        .find_map(|line| line.strip_prefix("OUTPUT_DIR="))
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .ok_or("missing output directory")?;
    let root = std::path::PathBuf::from(output_dir);
    std::fs::create_dir_all(&root).map_err(|error| error.to_string())?;
    if text.contains("WRITE ready.json") {
        if field(text, "HOLD=") == "1" {
            return Ok(());
        }
        if text.contains("# Coverage analysis session") {
            let model = std::path::PathBuf::from(field(text, "MODEL="));
            let fixture = model
                .parent()
                .ok_or("model path missing parent")?
                .join("handlers.fixture.json");
            let mapping: Value =
                serde_json::from_slice(&std::fs::read(fixture).map_err(|error| error.to_string())?)
                    .map_err(|error| error.to_string())?;
            atomic_write(&root.join("handlers.json"), &mapping)?;
        }
        atomic_write(
            &root.join("ready.json"),
            &json!({"status": "ready", "session": field(text, "SESSION=")}),
        )?;
    }
    let Some(request) = text.lines().find_map(|line| line.strip_prefix("REQUEST=")) else {
        return Ok(());
    };
    let request = request.trim();
    if text.contains("# Coverage analysis request") {
        // Mock requests only need a terminal result; the MCP service is not
        // available in this in-process test double.
        let request: Value = serde_json::from_slice(
            &std::fs::read(root.join("request.json")).map_err(|error| error.to_string())?,
        )
        .map_err(|error| error.to_string())?;
        let identity = request.get("identity").cloned().unwrap_or_default();
        let result = json!({
            "request_id": identity.get("request_id").cloned().unwrap_or_default(),
            "session": identity.get("session").cloned().unwrap_or_default(),
            "build": identity.get("build").cloned().unwrap_or_default(),
            "snapshot": identity.get("snapshot").cloned().unwrap_or_default(),
            "status": "no_proposal",
            "error": "mock core does not call MCP tools",
        });
        return atomic_write(&root.join("result.json"), &result);
    }
    let mode = text
        .lines()
        .find_map(|line| line.strip_prefix("MODE="))
        .unwrap_or("ok")
        .trim();
    if mode == "delay" {
        std::thread::sleep(Duration::from_millis(400));
    }
    if mode == "silent" {
        return Ok(());
    }
    let request_id = if mode == "stale" {
        "stale-request"
    } else {
        request
    };
    if mode == "fail" {
        return atomic_write(
            &root.join("result.json"),
            &json!({
                "request_id": request_id,
                "status": "error",
                "error": "agent failed",
                "added_steps": ["connect"],
            }),
        );
    }
    if mode == "schema" {
        std::fs::write(
            root.join("generator.py"),
            b"def generate(ctx, params):\n    return None\n",
        )
        .ok();
        std::fs::write(
            root.join("params.schema.json"),
            br#"{"type":"object","properties":{"verb":{"type":"string"}},"$ref":"https://example.invalid/schema"}"#,
        )
        .ok();
        std::fs::write(root.join("params.json"), br#"{"verb":"NOOP"}"#).ok();
    } else if mode != "nostatus" {
        std::fs::write(
            root.join("generator.py"),
            b"def generate(ctx, params):\n    ctx.send(str(params.get('verb', 'NOOP')).encode() + b'\\r\\n')\n    ctx.recv(until=b'\\r\\n')\n",
        )
        .ok();
        std::fs::write(
            root.join("params.schema.json"),
            br#"{"type":"object","additionalProperties":false,"required":["verb"],"properties":{"verb":{"type":"string"}}}"#,
        )
        .ok();
        std::fs::write(root.join("params.json"), br#"{"verb":"NOOP"}"#).ok();
    } else {
        std::fs::write(
            root.join("generator.py"),
            b"def generate(ctx, params):\n    return None\n",
        )
        .ok();
        std::fs::write(
            root.join("params.schema.json"),
            br#"{"type":"object","additionalProperties":false,"required":["verb"],"properties":{"verb":{"type":"string"}}}"#,
        )
        .ok();
        std::fs::write(root.join("params.json"), br#"{"verb":"NOOP"}"#).ok();
    }
    let mut body = json!({
        "request_id": request_id,
        "added_steps": ["open control connection"],
    });
    if mode != "nostatus" {
        body["status"] = json!("ok");
        body["error"] = Value::Null;
    }
    atomic_write(&root.join("result.json"), &body)
}

fn field(text: &str, prefix: &str) -> String {
    text.lines()
        .find_map(|line| line.strip_prefix(prefix))
        .unwrap_or("")
        .trim()
        .to_string()
}

fn atomic_write(path: &std::path::Path, value: &Value) -> Result<(), String> {
    let tmp = path.with_extension("json.tmp");
    std::fs::write(&tmp, serde_json::to_vec(value).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())?;
    std::fs::rename(tmp, path).map_err(|e| e.to_string())
}
