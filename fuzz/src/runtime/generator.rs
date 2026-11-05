//! iceoryx2 client for the resident Python generator server.
//!
//! One run request stays open until its `Result`. `Started` and `Result` are
//! delivered separately. A timeout tombs the request so a late response cannot
//! be applied to a later case. This client never restarts the server after a
//! timeout or an IPC error.

use std::{
    io,
    os::unix::process::CommandExt,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    thread::sleep,
    time::{Duration, Instant},
};

use iceoryx2::pending_response::PendingResponse;
use iceoryx2::port::client::Client;
use iceoryx2::prelude::*;
use libafl::Error;
use serde::Deserialize;

use crate::{
    engine::input::{ControlRequest, GeneratorInput, GeneratorResponse},
    runtime::lifecycle::{response_disposition, ResponseDisposition, ResponseId},
};

/// One incremental message from an open run request.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum GeneratorUpdate {
    /// The worker pid is known.
    Started {
        /// Worker pid reported by the server.
        pid: i32,
    },
    /// `waitpid` outcome. This does not include a pid.
    Result {
        /// Exit code when the worker called `_exit`.
        code: Option<i32>,
        /// Signal when the worker was signalled.
        signal: Option<i32>,
    },
}

struct Inflight {
    id: ResponseId,
    pending: IoxPending,
    saw_started: bool,
}

const MAX_MESSAGE: usize = 1 << 20;

type IoxClient = Client<ipc::Service, [u8], (), [u8], ()>;
type IoxPending = PendingResponse<ipc::Service, [u8], (), [u8], ()>;

/// Client plus the Python process group it owns.
pub struct GeneratorClient {
    child: Child,
    node: Node<ipc::Service>,
    client: IoxClient,
    inflight: Option<Inflight>,
    tombstones: Vec<(ResponseId, IoxPending)>,
    dead: bool,
    next_id: u64,
    client_id: u64,
    service_serial: u64,
    python: String,
    pythonpath: String,
    log_path: PathBuf,
    /// Distinguishes a coverage replay server from the main fuzz server.
    ipc_suffix: String,
}

impl GeneratorClient {
    /// Spawn `python -m generator_server` and wait until `ping` succeeds.
    pub fn spawn(python: &Path, pythonpath: &Path, log_path: &Path) -> Result<Self, Error> {
        Self::spawn_tagged(python, pythonpath, log_path, "")
    }

    /// Spawn a server whose iceoryx2 service name does not collide with the main fuzzer.
    pub fn spawn_tagged(
        python: &Path,
        pythonpath: &Path,
        log_path: &Path,
        ipc_suffix: &str,
    ) -> Result<Self, Error> {
        let node = node_builder()
            .create::<ipc::Service>()
            .map_err(|error| Error::unknown(format!("iceoryx2 node: {error}")))?;
        let mut client = Self {
            child: Command::new("true")
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .map_err(|error| Error::unknown(format!("placeholder spawn: {error}")))?,
            node,
            client: open_bootstrap_client()?,
            inflight: None,
            tombstones: Vec::new(),
            dead: false,
            next_id: 1,
            client_id: crate::runtime::worker_id()?.nonce,
            service_serial: 0,
            python: python.display().to_string(),
            pythonpath: pythonpath.display().to_string(),
            log_path: log_path.to_path_buf(),
            ipc_suffix: ipc_suffix.to_string(),
        };
        client.respawn()?;
        Ok(client)
    }

    /// Send one run request. Responses arrive through [`Self::poll_update`].
    pub fn submit(
        &mut self,
        input: &GeneratorInput,
        host: &str,
        port: u16,
        io_timeout: Duration,
        connect_timeout: Duration,
        worker_id: u64,
        case_id: u64,
    ) -> Result<(), Error> {
        if self.dead {
            return Err(Error::unknown(
                "generator server was stopped and is not restarted".to_string(),
            ));
        }
        if self.inflight.is_some() {
            return Err(Error::illegal_state(
                "a generator request is already in flight".to_string(),
            ));
        }
        self.drain_tombstones()?;
        self.ensure_alive()?;
        let request = self.allocate_id();
        let body = input.run_request(
            self.client_id,
            request,
            worker_id,
            case_id,
            host,
            port,
            as_millis(io_timeout),
            as_millis(connect_timeout),
        )?;
        if body.len() > MAX_MESSAGE {
            return Err(Error::illegal_argument(
                "generator request exceeds 1 MiB".to_string(),
            ));
        }
        self.inflight = Some(Inflight {
            id: ResponseId {
                request,
                worker: worker_id,
                case: case_id,
            },
            pending: self.send_body(&body)?,
            saw_started: false,
        });
        Ok(())
    }

    /// Non-blocking read of the next `Started` or `Result` for the open request.
    pub fn poll_update(&mut self) -> Result<Option<GeneratorUpdate>, Error> {
        let Some(flight) = self.inflight.as_mut() else {
            return Ok(None);
        };
        let received = flight
            .pending
            .receive()
            .map_err(|error| Error::unknown(format!("iceoryx2 receive: {error}")))?;
        if received.is_none() {
            let _ = self.node.wait(Duration::from_millis(1));
            if let Some(status) = self
                .child
                .try_wait()
                .map_err(|error| Error::os_error(error, "waiting for generator server"))?
            {
                self.inflight = None;
                self.dead = true;
                return Err(Error::unknown(format!(
                    "generator server exited while a case was running: {status}"
                )));
            }
            return Ok(None);
        }
        let response = received.expect("checked");
        let bytes: Vec<u8> = response.payload().iter().copied().collect();
        let wire = decode_wire(&bytes)?;
        let got = ResponseId {
            request: wire.id,
            worker: wire.worker_id,
            case: wire.case_id,
        };
        let open = self.inflight.as_ref().map(|item| item.id);
        match response_disposition(open, got, false, &[]) {
            ResponseDisposition::Accept => {}
            ResponseDisposition::Drain | ResponseDisposition::Reject => {
                return Err(Error::unknown(format!(
                    "generator response {} does not match the open request",
                    wire.kind.as_deref().unwrap_or("unknown")
                )));
            }
        }
        match wire.kind.as_deref() {
            Some("started") => {
                let flight = self.inflight.as_mut().expect("open request");
                if flight.saw_started {
                    return Err(Error::unknown(
                        "duplicate generator Started for the open request".to_string(),
                    ));
                }
                let Some(pid) = wire.pid else {
                    return Err(Error::unknown(
                        "generator Started did not include a pid".to_string(),
                    ));
                };
                if pid <= 0 || pid > i64::from(i32::MAX) {
                    return Err(Error::unknown(format!(
                        "generator Started pid {pid} is not a process id"
                    )));
                }
                flight.saw_started = true;
                Ok(Some(GeneratorUpdate::Started { pid: pid as i32 }))
            }
            Some("result") => {
                let flight = self.inflight.as_ref().expect("open request");
                if !flight.saw_started {
                    return Err(Error::unknown(
                        "generator Result arrived before Started".to_string(),
                    ));
                }
                if wire.code.is_some() && wire.signal.is_some() {
                    return Err(Error::unknown(
                        "generator Result included both an exit code and a signal".to_string(),
                    ));
                }
                if wire.code.is_none() && wire.signal.is_none() {
                    return Err(Error::unknown(
                        "generator Result included neither an exit code nor a signal".to_string(),
                    ));
                }
                self.inflight = None;
                Ok(Some(GeneratorUpdate::Result {
                    code: wire.code,
                    signal: wire.signal,
                }))
            }
            other => Err(Error::unknown(format!(
                "generator run response kind {other:?} is not Started or Result"
            ))),
        }
    }

    /// Keep the open request only as a drain target. A late Result cannot change a closed case.
    pub fn tombstone_inflight(&mut self) {
        if let Some(flight) = self.inflight.take() {
            self.tombstones.push((flight.id, flight.pending));
        }
    }

    /// Read and discard responses whose ids were already closed.
    pub fn drain_tombstones(&mut self) -> Result<(), Error> {
        let mut kept = Vec::new();
        for (id, pending) in self.tombstones.drain(..) {
            let received = pending
                .receive()
                .map_err(|error| Error::unknown(format!("iceoryx2 receive: {error}")))?;
            let Some(response) = received else {
                kept.push((id, pending));
                continue;
            };
            let bytes: Vec<u8> = response.payload().iter().copied().collect();
            let wire = decode_wire(&bytes)?;
            let got = ResponseId {
                request: wire.id,
                worker: wire.worker_id,
                case: wire.case_id,
            };
            match response_disposition(None, got, false, &[id]) {
                ResponseDisposition::Drain => {
                    if wire.kind.as_deref() != Some("result") {
                        kept.push((id, pending));
                    }
                }
                ResponseDisposition::Accept | ResponseDisposition::Reject => {
                    return Err(Error::unknown(format!(
                        "late generator response {} does not match a tombstone",
                        wire.kind.as_deref().unwrap_or("unknown")
                    )));
                }
            }
        }
        self.tombstones = kept;
        Ok(())
    }

    /// Stop the Python server. It is not started again.
    pub fn retire(&mut self) {
        self.inflight = None;
        self.tombstones.clear();
        self.kill_group();
        self.dead = true;
    }

    fn respawn(&mut self) -> Result<(), Error> {
        self.kill_group();
        self.service_serial = self.service_serial.wrapping_add(1);
        let service = format!(
            "mcn-fuzz/w{}{}n{}",
            crate::runtime::worker_id()?.label(),
            self.ipc_suffix,
            self.service_serial
        );
        if let Some(parent) = self.log_path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        let log = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.log_path)
            .map_err(|error| Error::unknown(format!("generator log: {error}")))?;
        let log_err = log
            .try_clone()
            .map_err(|error| Error::unknown(format!("generator log: {error}")))?;
        let mut command = Command::new(&self.python);
        command
            .arg("-m")
            .arg("generator_server")
            .arg("--service")
            .arg(&service)
            .env("PYTHONPATH", &self.pythonpath)
            .env("PYTHONUNBUFFERED", "1")
            .stdin(Stdio::null())
            .stdout(Stdio::from(log))
            .stderr(Stdio::from(log_err))
            .process_group(0);
        self.child = command.spawn().map_err(|error| {
            Error::unknown(format!(
                "failed to start generator server with {} (PYTHONPATH={}): {error}",
                self.python, self.pythonpath
            ))
        })?;
        self.client = self.open_service(&service)?;
        let response = self.roundtrip(
            &ControlRequest {
                op: "ping",
                client: self.client_id,
                id: 0,
            }
            .encode()?,
            Duration::from_secs(5),
        )?;
        if response.status != "ok" {
            return Err(Error::unknown(format!(
                "generator ping failed: {} {}",
                response.status,
                response.error.unwrap_or_default()
            )));
        }
        Ok(())
    }

    fn open_service(&mut self, service: &str) -> Result<IoxClient, Error> {
        let service_name: ServiceName = service
            .try_into()
            .map_err(|error| Error::illegal_argument(format!("service name: {error:?}")))?;
        let deadline = Instant::now() + Duration::from_secs(10);
        let mut last_error = String::from("service not created");
        while Instant::now() < deadline {
            match self
                .node
                .service_builder(&service_name)
                .request_response::<[u8], [u8]>()
                .max_active_requests_per_client(4)
                .max_response_buffer_size(2)
                .enable_safe_overflow_for_requests(false)
                .enable_safe_overflow_for_responses(false)
                .open()
            {
                Ok(service) => {
                    return service
                        .client_builder()
                        .initial_max_slice_len(65536)
                        .allocation_strategy(AllocationStrategy::PowerOfTwo)
                        .create()
                        .map_err(|error| Error::unknown(format!("iceoryx2 client: {error}")));
                }
                Err(error) => {
                    last_error = format!("{error}");
                    if let Some(status) = self
                        .child
                        .try_wait()
                        .map_err(|error| Error::os_error(error, "waiting for generator server"))?
                    {
                        return Err(Error::unknown(format!(
                            "generator server exited early: {status}"
                        )));
                    }
                    sleep(Duration::from_millis(50));
                }
            }
        }
        Err(Error::unknown(format!(
            "generator server did not open {service}: {last_error}"
        )))
    }

    fn roundtrip(&mut self, body: &[u8], wait: Duration) -> Result<GeneratorResponse, Error> {
        let pending = self.send_body(body)?;
        let deadline = Instant::now() + wait;
        while Instant::now() < deadline {
            if let Some(response) = pending
                .receive()
                .map_err(|error| Error::unknown(format!("iceoryx2 receive: {error}")))?
            {
                let bytes: Vec<u8> = response.payload().iter().copied().collect();
                return GeneratorResponse::decode(&bytes);
            }
            let _ = self.node.wait(Duration::from_millis(1));
        }
        Err(Error::os_error(
            io::Error::new(
                io::ErrorKind::TimedOut,
                "generator server produced no response",
            ),
            "generator server produced no response",
        ))
    }

    fn send_body(&mut self, body: &[u8]) -> Result<IoxPending, Error> {
        let sample = self
            .client
            .loan_slice_uninit(body.len())
            .map_err(|error| Error::unknown(format!("iceoryx2 loan: {error}")))?;
        let sample = sample.write_from_fn(|index| body[index]);
        sample
            .send()
            .map_err(|error| Error::unknown(format!("iceoryx2 send: {error}")))
    }

    fn ensure_alive(&mut self) -> Result<(), Error> {
        if self.dead {
            return Err(Error::unknown(
                "generator server was stopped and is not restarted".to_string(),
            ));
        }
        if let Some(status) = self
            .child
            .try_wait()
            .map_err(|error| Error::os_error(error, "waiting for generator server"))?
        {
            return Err(Error::unknown(format!(
                "generator server is not running: {status}"
            )));
        }
        Ok(())
    }

    /// Process id of the resident `generator_server`. The template is a child of this pid.
    pub fn server_pid(&self) -> u32 {
        self.child.id()
    }

    fn allocate_id(&mut self) -> u64 {
        let id = self.next_id;
        self.next_id = self.next_id.wrapping_add(1);
        id
    }

    fn kill_group(&mut self) {
        let pid = self.child.id();
        if pid != 0 {
            unsafe {
                libc::kill(-(pid as i32), libc::SIGKILL);
            }
        }
        let _ = self.child.wait();
    }
}

impl Drop for GeneratorClient {
    fn drop(&mut self) {
        self.inflight = None;
        self.tombstones.clear();
        self.kill_group();
    }
}

#[derive(Deserialize)]
struct WireMessage {
    id: u64,
    #[serde(default)]
    kind: Option<String>,
    #[serde(default)]
    worker_id: u64,
    #[serde(default)]
    case_id: u64,
    #[serde(default)]
    pid: Option<i64>,
    #[serde(default)]
    code: Option<i32>,
    #[serde(default)]
    signal: Option<i32>,
}

fn decode_wire(bytes: &[u8]) -> Result<WireMessage, Error> {
    ciborium::de::from_reader(bytes)
        .map_err(|error| Error::serialize(format!("generator response cbor: {error}")))
}

fn node_builder() -> NodeBuilder {
    NodeBuilder::new().signal_handling_mode(SignalHandlingMode::Disabled)
}

fn as_millis(duration: Duration) -> u64 {
    duration.as_millis().min(u128::from(u64::MAX)) as u64
}

/// A client that is replaced by [`GeneratorClient::respawn`] before any request.
fn open_bootstrap_client() -> Result<IoxClient, Error> {
    // The real client is assigned in `respawn`. Constructing `GeneratorClient`
    // still needs a value, so open a private unused service in this process.
    let node = node_builder()
        .create::<ipc::Service>()
        .map_err(|error| Error::unknown(format!("iceoryx2 bootstrap node: {error}")))?;
    let name = format!("mcn-fuzz/boot{}", std::process::id());
    let service_name: ServiceName = name
        .as_str()
        .try_into()
        .map_err(|error| Error::illegal_argument(format!("service name: {error:?}")))?;
    let service = node
        .service_builder(&service_name)
        .request_response::<[u8], [u8]>()
        .create()
        .map_err(|error| Error::unknown(format!("iceoryx2 bootstrap service: {error}")))?;
    // Leak the node so the service outlives this function. It is never used.
    let _node = Box::leak(Box::new(node));
    service
        .client_builder()
        .initial_max_slice_len(64)
        .allocation_strategy(AllocationStrategy::PowerOfTwo)
        .create()
        .map_err(|error| Error::unknown(format!("iceoryx2 bootstrap client: {error}")))
}
