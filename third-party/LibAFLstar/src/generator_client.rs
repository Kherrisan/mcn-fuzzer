//! Lazy iceoryx2 client for the resident Python generator server.
//!
//! The server process is started the first time a [`crate::input::StarInput::Generator`]
//! is executed. Byte-only campaigns never open the service.

use std::{
    env, io,
    process::{Child, Command, Stdio},
    thread::sleep,
    time::{Duration, Instant},
};

use iceoryx2::port::client::Client;
use iceoryx2::prelude::*;
use libafl::Error;

use crate::input::{ControlRequest, GeneratorInput, GeneratorResponse};

const SERVICE_NAME: &str = "libaflstar/generator";
const MAX_MESSAGE: usize = 1 << 20;

/// Client plus the Python child it owns.
pub struct GeneratorClient {
    child: Child,
    node: Node<ipc::Service>,
    client: Client<ipc::Service, [u8], (), [u8], ()>,
    next_id: u64,
    client_id: u64,
}

impl GeneratorClient {
    /// Spawn `python -m generator_server` and wait until `ping` succeeds.
    pub fn spawn() -> Result<Self, Error> {
        let python = env::var("LIBAFLSTAR_PYTHON").unwrap_or_else(|_| "python3".to_string());
        let pythonpath =
            env::var("LIBAFLSTAR_PYTHONPATH").unwrap_or_else(|_| env!("CARGO_MANIFEST_DIR").to_string());
        let mut child = Command::new(&python)
            .arg("-m")
            .arg("generator_server")
            .arg("--service")
            .arg(SERVICE_NAME)
            .env("PYTHONPATH", &pythonpath)
            .env("PYTHONUNBUFFERED", "1")
            .stdin(Stdio::null())
            .spawn()
            .map_err(|error| {
                Error::unknown(format!(
                    "failed to start generator server with {python} (PYTHONPATH={pythonpath}): {error}"
                ))
            })?;

        let node = NodeBuilder::new().create::<ipc::Service>().map_err(|error| {
            let _ = child.kill();
            Error::unknown(format!("iceoryx2 node: {error}"))
        })?;
        let service_name: ServiceName = SERVICE_NAME.try_into().map_err(|error| {
            Error::illegal_argument(format!("service name: {error:?}"))
        })?;
        let mut opened = None;
        let deadline = Instant::now() + Duration::from_secs(10);
        let mut last_error = String::from("service not created");
        while Instant::now() < deadline {
            match node
                .service_builder(&service_name)
                .request_response::<[u8], [u8]>()
                .open()
            {
                Ok(service) => {
                    opened = Some(service);
                    break;
                }
                Err(error) => {
                    last_error = format!("{error}");
                    if let Some(status) = child.try_wait().map_err(|error| {
                Error::os_error(error, "waiting for generator server")
            })? {
                        return Err(Error::unknown(format!(
                            "generator server exited early: {status}"
                        )));
                    }
                    sleep(Duration::from_millis(50));
                }
            }
        }
        let service = opened.ok_or_else(|| {
            Error::unknown(format!("generator server did not open {SERVICE_NAME}: {last_error}"))
        })?;
        let client = service
            .client_builder()
            .initial_max_slice_len(65536)
            .allocation_strategy(AllocationStrategy::PowerOfTwo)
            .create()
            .map_err(|error| Error::unknown(format!("iceoryx2 client: {error}")))?;
        let mut this = Self {
            child,
            node,
            client,
            next_id: 1,
            client_id: std::process::id() as u64,
        };
        let response = this.roundtrip(&ControlRequest {
            op: "ping",
            client: this.client_id,
            id: 0,
        }.encode()?, Duration::from_secs(5))?;
        if response.status != "ok" {
            return Err(Error::unknown(format!(
                "generator ping failed: {} {}",
                response.status,
                response.error.unwrap_or_default()
            )));
        }
        Ok(this)
    }

    /// Drop the Python session's sockets for this fuzzer process.
    pub fn reset(&mut self) -> Result<(), Error> {
        let body = ControlRequest {
            op: "reset",
            client: self.client_id,
            id: self.allocate_id(),
        }
        .encode()?;
        let response = self.roundtrip(&body, Duration::from_secs(2))?;
        if response.status != "ok" {
            return Err(Error::unknown(format!(
                "generator reset failed: {}",
                response.error.unwrap_or(response.status)
            )));
        }
        Ok(())
    }

    /// Run one generator script and wait until the interaction finishes.
    pub fn run(
        &mut self,
        input: &GeneratorInput,
        host: &str,
        port: u16,
        timeout: Duration,
    ) -> Result<GeneratorResponse, Error> {
        let timeout_ms = timeout.as_millis().min(u128::from(u64::MAX)) as u64;
        let body = input.run_request(self.client_id, self.allocate_id(), host, port, timeout_ms)?;
        if body.len() > MAX_MESSAGE {
            return Err(Error::illegal_argument("generator request exceeds 1 MiB"));
        }
        // The script may recv several times. Give the RPC the execution timeout
        // plus a small margin for connect and import.
        let wait = timeout.saturating_add(Duration::from_secs(2));
        self.roundtrip(&body, wait)
    }

    fn allocate_id(&mut self) -> u64 {
        let id = self.next_id;
        self.next_id = self.next_id.wrapping_add(1);
        id
    }

    fn roundtrip(&mut self, body: &[u8], wait: Duration) -> Result<GeneratorResponse, Error> {
        let sample = self
            .client
            .loan_slice_uninit(body.len())
            .map_err(|error| Error::unknown(format!("iceoryx2 loan: {error}")))?;
        let sample = sample.write_from_fn(|index| body[index]);
        let pending = sample
            .send()
            .map_err(|error| Error::unknown(format!("iceoryx2 send: {error}")))?;
        let deadline = Instant::now() + wait;
        while Instant::now() < deadline {
            if let Some(response) = pending
                .receive()
                .map_err(|error| Error::unknown(format!("iceoryx2 receive: {error}")))?
            {
                let payload = response.payload();
                let bytes: Vec<u8> = payload.iter().copied().collect();
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
}

impl Drop for GeneratorClient {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}
