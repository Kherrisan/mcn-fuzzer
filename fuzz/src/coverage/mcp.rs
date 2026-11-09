//! Campaign-private coverage MCP service: lifecycle and JSON-line client.
//!
//! The supervisor starts the Python service before entering the SUT network
//! namespace, exactly like the private HC core. Agents reach it through a
//! per-session `stdio` bridge; Rust control calls use the same Unix socket.

use std::{
    fs,
    io::{BufRead, BufReader, Write},
    os::unix::{fs::chown, net::UnixStream, process::CommandExt},
    path::{Path, PathBuf},
    process::{Child, Command},
    sync::{
        atomic::{AtomicU64, Ordering},
        Mutex,
    },
    time::{Duration, Instant},
};

use serde_json::{json, Value};

use crate::runtime::{drop_privileges, prepare_owned_dir, TargetIdentity};

type Result<T> = std::result::Result<T, libafl::Error>;

/// Facts the supervisor needs before it unshares the network namespace.
#[derive(Clone, Debug)]
pub struct ServiceRequest {
    pub python: PathBuf,
    pub pythonpath: PathBuf,
    /// Directory that receives the runtime record and service state.
    pub output: PathBuf,
    /// Supervisor-provided endpoint. `None` starts a private service.
    pub socket: Option<PathBuf>,
    pub source_dir: Option<PathBuf>,
    pub ready_timeout: Duration,
}

static REQUEST: Mutex<Option<ServiceRequest>> = Mutex::new(None);
static SERVICE: Mutex<Option<ServiceRecord>> = Mutex::new(None);

struct ServiceRecord {
    socket: PathBuf,
    owned: bool,
    child: Option<Child>,
}

pub fn install_request(request: ServiceRequest) {
    *REQUEST.lock().unwrap_or_else(|error| error.into_inner()) = Some(request);
}

pub fn requested() -> bool {
    REQUEST
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .is_some()
}

pub fn socket_path() -> Option<PathBuf> {
    SERVICE
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .as_ref()
        .map(|record| record.socket.clone())
}

/// Start or connect the service while PID 1 still owns the host network.
pub fn boot_if_requested(identity: &TargetIdentity) -> Result<()> {
    let request = REQUEST
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .clone();
    let Some(request) = request else {
        return Ok(());
    };
    if let Some(socket) = &request.socket {
        connect_ready(socket, request.ready_timeout)?;
        *SERVICE.lock().unwrap_or_else(|error| error.into_inner()) = Some(ServiceRecord {
            socket: socket.clone(),
            owned: false,
            child: None,
        });
        write_runtime(&request.output, socket, None, false)?;
        return Ok(());
    }
    let nonce = format!(
        "{}{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|value| value.subsec_nanos())
            .unwrap_or(1)
    );
    let runtime = PathBuf::from(format!("/tmp/mcn-mcp-{nonce}"));
    prepare_owned_dir(&runtime, identity)?;
    let socket = runtime.join("coverage.sock");
    // Keep parsed state and the call log with the campaign, not in /tmp.
    let state = request.output.join("mcp-state");
    prepare_owned_dir(&state, identity)?;
    // The service runs as the campaign account. Its cwd is the runtime
    // directory, which exists even before the first snapshot.
    prepare_owned_dir(&request.output, identity)?;
    let gid = identity.gid;
    let uid = identity.uid;
    let log_path = runtime.join("service.log");
    let log = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .map_err(|error| {
            libafl::Error::unknown(format!("creating {}: {error}", log_path.display()))
        })?;
    chown(&log_path, Some(uid), Some(gid))
        .map_err(|error| libafl::Error::unknown(format!("chowning service log: {error}")))?;
    let log_err = log
        .try_clone()
        .map_err(|error| libafl::Error::unknown(format!("duplicating service log: {error}")))?;
    let mut command = Command::new(&request.python);
    command
        .arg("-m")
        .arg("coverage_analysis.server")
        .arg("serve")
        .arg("--socket")
        .arg(&socket)
        .arg("--state-dir")
        .arg(&state);
    if let Some(source) = &request.source_dir {
        command.arg("--source-root").arg(source);
    }
    command
        .env_clear()
        .env(
            "PATH",
            std::env::var("PATH").unwrap_or_else(|_| "/usr/bin:/bin".into()),
        )
        .env("PYTHONPATH", &request.pythonpath)
        .env("PYTHONUNBUFFERED", "1")
        .current_dir(&runtime)
        .stdout(std::process::Stdio::from(log))
        .stderr(std::process::Stdio::from(log_err));
    let identity = identity.clone();
    unsafe {
        command.pre_exec(move || {
            drop_privileges(&identity).map_err(|error| std::io::Error::other(error.to_string()))
        });
    }
    let child = command.spawn().map_err(|error| {
        libafl::Error::unknown(format!(
            "starting coverage MCP service {}: {error}",
            request.python.display()
        ))
    })?;
    let pid = child.id();
    *SERVICE.lock().unwrap_or_else(|error| error.into_inner()) = Some(ServiceRecord {
        socket: socket.clone(),
        owned: true,
        child: Some(child),
    });
    connect_ready(&socket, request.ready_timeout).inspect_err(|_| {
        let _ = stop_service();
    })?;
    write_runtime(&request.output, &socket, Some(pid), true)?;
    Ok(())
}

fn connect_ready(socket: &Path, timeout: Duration) -> Result<()> {
    let deadline = Instant::now() + timeout;
    let mut last = String::new();
    while Instant::now() < deadline {
        match McpClient::at(socket.to_path_buf()).health() {
            Ok(()) => return Ok(()),
            Err(error) => last = error.to_string(),
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    Err(libafl::Error::unknown(format!(
        "coverage MCP service at {} did not become ready: {last}",
        socket.display()
    )))
}

fn write_runtime(output: &Path, socket: &Path, pid: Option<u32>, owned: bool) -> Result<()> {
    let path = output.join("mcp-runtime.json");
    let body = json!({"socket": socket, "pid": pid, "owned": owned});
    fs::write(&path, format!("{body}\n"))
        .map_err(|error| libafl::Error::unknown(format!("writing {}: {error}", path.display())))
}

/// Stop the private service and wait for it to exit. External services only disconnect.
pub fn shutdown() {
    let record = SERVICE
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .take();
    let Some(mut record) = record else {
        return;
    };
    if !record.owned {
        return;
    }
    let client = McpClient::at(record.socket.clone());
    let _ = client.call(json!({"op": "shutdown"}));
    if let Some(child) = record.child.as_mut() {
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            match child.try_wait() {
                Ok(Some(_)) => break,
                Ok(None) if Instant::now() < deadline => {
                    std::thread::sleep(Duration::from_millis(50))
                }
                _ => {
                    let _ = child.kill();
                    let _ = child.wait();
                    break;
                }
            }
        }
    }
    if let Some(runtime) = record.socket.parent() {
        let _ = fs::remove_dir_all(runtime);
    }
}

fn stop_service() {
    shutdown();
}

/// One connection per control call. Loads can take a while; the call timeout
/// covers parsing plus MCP-side validation.
pub struct McpClient {
    socket: PathBuf,
    timeout: Duration,
}

static NEXT_ID: AtomicU64 = AtomicU64::new(1);

impl McpClient {
    pub fn at(socket: PathBuf) -> Self {
        Self {
            socket,
            timeout: Duration::from_secs(300),
        }
    }

    pub fn connect() -> Result<Self> {
        let socket = socket_path().ok_or_else(|| {
            libafl::Error::unknown("the coverage MCP service was not started".to_string())
        })?;
        Ok(Self::at(socket))
    }

    pub fn call(&self, mut payload: Value) -> Result<Value> {
        let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
        if let Some(object) = payload.as_object_mut() {
            object.insert("id".into(), json!(id));
        }
        let mut stream = UnixStream::connect(&self.socket).map_err(|error| {
            libafl::Error::unknown(format!(
                "connecting to coverage MCP service {}: {error}",
                self.socket.display()
            ))
        })?;
        stream
            .set_read_timeout(Some(self.timeout))
            .and_then(|()| stream.set_write_timeout(Some(self.timeout)))
            .map_err(|error| libafl::Error::unknown(error.to_string()))?;
        let body = serde_json::to_vec(&payload)
            .map_err(|error| libafl::Error::unknown(error.to_string()))?;
        stream
            .write_all(&[body.as_slice(), b"\n"].concat())
            .map_err(|error| libafl::Error::unknown(format!("writing MCP request: {error}")))?;
        let mut reader = BufReader::new(stream);
        let mut line = String::new();
        reader
            .read_line(&mut line)
            .map_err(|error| libafl::Error::unknown(format!("reading MCP reply: {error}")))?;
        if line.trim().is_empty() {
            return Err(libafl::Error::unknown(
                "coverage MCP service closed the connection".to_string(),
            ));
        }
        let reply: Value = serde_json::from_str(line.trim())
            .map_err(|error| libafl::Error::unknown(format!("invalid MCP reply: {error}")))?;
        if reply.get("ok").and_then(Value::as_bool) != Some(true) {
            let error = reply.get("error").cloned().unwrap_or(Value::Null);
            return Err(libafl::Error::unknown(format!(
                "coverage MCP call failed: {error}"
            )));
        }
        Ok(reply.get("result").cloned().unwrap_or(Value::Null))
    }

    pub fn health(&self) -> Result<()> {
        self.call(json!({"op": "health"})).map(|_| ())
    }

    pub fn register(&self, request: &Value) -> Result<Value> {
        self.call(json!({"op": "register_request", "request": request}))
    }

    pub fn begin(&self, request_id: &str) -> Result<Value> {
        self.call(json!({"op": "begin_analysis", "request_id": request_id}))
    }

    pub fn commit(&self, request_id: &str, sha256: &str) -> Result<Value> {
        self.call(json!({
            "op": "commit_analysis",
            "request_id": request_id,
            "proposal_sha256": sha256,
        }))
    }

    pub fn abort(&self, request_id: &str) -> Result<Value> {
        self.call(json!({"op": "abort_analysis", "request_id": request_id}))
    }

    pub fn tool(&self, name: &str, arguments: Value) -> Result<Value> {
        self.call(json!({"op": "tool", "name": name, "arguments": arguments}))
    }
}
