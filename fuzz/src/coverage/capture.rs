//! Resident loopback capture records for one case.
//!
//! The live `pcap` crate handle is not compiled into this build: the system
//! has no libpcap. Capture-on preflight therefore fails before the first case.
//! Record layout, the 64 MiB whole-record limit, drain timing, metadata, and
//! the sidecar export are still implemented and tested here.

use std::{
    io::{self, Write},
    os::fd::{AsRawFd, FromRawFd, OwnedFd},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc, Mutex,
    },
    thread::JoinHandle,
    time::{Duration, Instant},
};

use pcap::{Capture, Direction, Packet};
use pcap_file::{
    pcap::{PcapHeader, PcapPacket, PcapWriter},
    DataLink, Endianness, TsResolution,
};

use flate2::{write::ZlibEncoder, Compression};
use libafl::{
    corpus::Testcase,
    executors::ExitKind,
    feedbacks::{Feedback, StateInitializer},
    observers::Observer,
    Error, HasMetadata,
};
use libafl_bolts::{impl_serdeany, Named};
use serde::{Deserialize, Serialize};

use crate::{
    app::config::CaptureConfig,
    engine::input::{GeneratorInput, PendingCapture},
    runtime::WorkerId,
};

/// zlib level used for stored captures.
pub const COMPRESSION_LEVEL: u32 = 1;
/// Default snaplen.
pub const SNAPLEN: u32 = 262_144;
/// One direction of the loopback preflight payload. 17 bytes.
pub const PREFLIGHT_FORWARD: &[u8] = b"mcn-preflight-a2b";
/// The opposite direction. 17 bytes.
pub const PREFLIGHT_BACK: &[u8] = b"mcn-preflight-b2a";

/// True when `haystack` contains `marker` at exactly `marker.len()` bytes.
pub fn contains_marker(haystack: &[u8], marker: &[u8]) -> bool {
    !marker.is_empty()
        && haystack
            .windows(marker.len())
            .any(|window| window == marker)
}

/// Both loopback directions are present.
pub fn both_preflight_directions(haystack: &[u8]) -> bool {
    contains_marker(haystack, PREFLIGHT_FORWARD) && contains_marker(haystack, PREFLIGHT_BACK)
}
#[cfg(test)]
const LINKTYPE_ETHERNET: u32 = 1;

/// Phase costs for one case. Persistence is filled later by `to_file`.
/// Present only with the `stats` feature.
#[cfg(feature = "stats")]
#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
pub struct PhaseSnapshot {
    /// Restore plus forkserver prepare.
    pub target_setup_us: u128,
    /// Capture Begin until ACK, or zero when capture is off.
    pub capture_ready_us: u128,
    /// SUT request through generator and target cleanup.
    pub interaction_us: u128,
    /// End drain.
    pub tail_drain_us: u128,
    /// Orphan reaping.
    pub stop_reap_us: u128,
    /// Unused by the libpcap path. Kept so older bench rows still parse.
    pub pcap_read_us: u128,
    /// zlib compression.
    pub zlib_us: u128,
    /// Sidecar decompression and write. Zero until a testcase is saved.
    pub persist_us: u128,
    /// Uncompressed bytes retained.
    pub uncompressed_len: u64,
    /// Compressed bytes retained.
    pub compressed_len: u64,
    /// Packets observed.
    pub packets_captured: u64,
    /// Packets saved into the PCAP prefix.
    pub packets_received: u64,
    /// Packets not saved.
    pub packets_dropped: u64,
    /// `complete`, `incomplete`, or `disabled`.
    pub status: String,
    /// Why the capture is incomplete, when it is.
    pub note: String,
    /// Diagnostic pids. Not used as a filter.
    pub pids: Vec<i32>,
}

#[cfg(feature = "stats")]
struct PhaseState {
    target_setup_us: u128,
    capture_ready_us: u128,
    interaction_us: u128,
    tail_drain_us: u128,
    stop_reap_us: u128,
    pcap_read_us: u128,
    zlib_us: u128,
    persist_us: Arc<Mutex<u128>>,
    uncompressed_len: u64,
    compressed_len: u64,
    packets_captured: u64,
    packets_received: u64,
    packets_dropped: u64,
    status: String,
    note: String,
    pids: Vec<i32>,
}

#[cfg(feature = "stats")]
impl PhaseState {
    fn new() -> Self {
        Self {
            target_setup_us: 0,
            capture_ready_us: 0,
            interaction_us: 0,
            tail_drain_us: 0,
            stop_reap_us: 0,
            pcap_read_us: 0,
            zlib_us: 0,
            persist_us: Arc::new(Mutex::new(0)),
            uncompressed_len: 0,
            compressed_len: 0,
            packets_captured: 0,
            packets_received: 0,
            packets_dropped: 0,
            status: "disabled".to_string(),
            note: String::new(),
            pids: Vec::new(),
        }
    }

    fn snapshot(&self) -> PhaseSnapshot {
        PhaseSnapshot {
            target_setup_us: self.target_setup_us,
            capture_ready_us: self.capture_ready_us,
            interaction_us: self.interaction_us,
            tail_drain_us: self.tail_drain_us,
            stop_reap_us: self.stop_reap_us,
            pcap_read_us: self.pcap_read_us,
            zlib_us: self.zlib_us,
            persist_us: *self
                .persist_us
                .lock()
                .unwrap_or_else(|error| error.into_inner()),
            uncompressed_len: self.uncompressed_len,
            compressed_len: self.compressed_len,
            packets_captured: self.packets_captured,
            packets_received: self.packets_received,
            packets_dropped: self.packets_dropped,
            status: self.status.clone(),
            note: self.note.clone(),
            pids: self.pids.clone(),
        }
    }
}

/// zlib capture stored on a saved testcase. The bytes stay zlib; JSON writes them as base64.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct PcapMetadata {
    /// zlib level-1 bytes.
    #[serde(with = "b64bytes")]
    pub zlib: Vec<u8>,
    /// Byte length before compression.
    pub uncompressed_len: u64,
    /// `complete` or `incomplete`.
    pub status: String,
    /// Diagnostic pids. Not a capture filter.
    pub pids: Vec<i32>,
    /// Packets observed during the case.
    pub packets_captured: u64,
    /// Packets written into the retained prefix.
    pub packets_received: u64,
    /// Packets dropped by libpcap stats, when known.
    pub packets_dropped: u64,
    /// Spawn until ready, only with the stats feature. Not the new Begin time.
    #[cfg(feature = "stats")]
    #[serde(default)]
    pub ready_us: u64,
    /// Old helper tail time. Not the new drain time.
    #[cfg(feature = "stats")]
    #[serde(default)]
    pub tail_us: u64,
    /// Old helper stop time.
    #[cfg(feature = "stats")]
    #[serde(default)]
    pub stop_us: u64,
    /// Old helper read time.
    #[cfg(feature = "stats")]
    #[serde(default)]
    pub read_us: u64,
    /// Old helper compress time. New compress time is `compress_us` below when stats is off too.
    #[cfg(feature = "stats")]
    #[serde(default)]
    pub compress_us: u64,
    /// Capture backend name.
    #[serde(default)]
    pub backend: String,
    /// Capture backend version.
    #[serde(default)]
    pub backend_version: String,
    /// WorkerId label.
    #[serde(default)]
    pub worker_id: String,
    /// Case id inside the worker.
    #[serde(default)]
    pub case_id: u64,
    /// Packets observed.
    #[serde(default)]
    pub observed: u64,
    /// Packets saved.
    #[serde(default)]
    pub saved: u64,
    /// Packets omitted because the 64 MiB prefix was full.
    #[serde(default)]
    pub capacity_dropped: u64,
    /// Incremental libpcap drop counter. Not derived from received-captured.
    #[serde(default)]
    pub stats_dropped: u64,
    /// Begin until ACK, microseconds.
    #[serde(default)]
    pub begin_us: u64,
    /// End drain, microseconds.
    #[serde(default)]
    pub drain_us: u64,
    /// zlib compression, microseconds.
    #[serde(default)]
    pub zlib_us: u64,
    /// Incomplete or fatal reason.
    #[serde(default)]
    pub reason: String,
    /// Joined incomplete reasons. Empty when complete.
    pub note: String,
}

impl_serdeany!(PcapMetadata);

/// One finished case, shared with the feedback that annotates a saved testcase.
#[derive(Clone, Debug)]
pub struct FinishedCapture {
    /// Metadata stored on the testcase.
    pub metadata: PcapMetadata,
    /// Sidecar payload. Only the cloned testcase input receives this.
    pub attachment: PendingCapture,
}

struct SharedBuf(Arc<Mutex<Vec<u8>>>);

impl Write for SharedBuf {
    fn write(&mut self, data: &[u8]) -> io::Result<usize> {
        self.0
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .extend_from_slice(data);
        Ok(data.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

/// In-memory PCAP prefix written by `pcap-file`. Oversized input keeps only whole records.
pub struct PcapBuffer {
    bytes: Arc<Mutex<Vec<u8>>>,
    writer: PcapWriter<SharedBuf>,
    limit: usize,
    observed: u64,
    saved: u64,
    capacity_dropped: u64,
    snap_truncated: u64,
    stats_dropped: u64,
    case_id: u64,
    begin_us: u128,
    incomplete: Vec<String>,
}

impl PcapBuffer {
    /// Start a PCAP with the global header counted against `limit`.
    pub fn new(limit: u64, snaplen: u32, linktype: u32) -> Self {
        let bytes = Arc::new(Mutex::new(Vec::new()));
        let header = PcapHeader {
            version_major: 2,
            version_minor: 4,
            ts_correction: 0,
            ts_accuracy: 0,
            snaplen,
            datalink: DataLink::from(linktype),
            ts_resolution: TsResolution::MicroSecond,
            endianness: Endianness::Little,
        };
        let writer = PcapWriter::with_header(SharedBuf(Arc::clone(&bytes)), header)
            .expect("pcap header write");
        Self {
            bytes,
            writer,
            limit: usize::try_from(limit).unwrap_or(usize::MAX),
            observed: 0,
            saved: 0,
            capacity_dropped: 0,
            snap_truncated: 0,
            stats_dropped: 0,
            case_id: 0,
            begin_us: 0,
            incomplete: Vec::new(),
        }
    }

    /// Bytes retained so far, including the global header.
    pub fn bytes(&self) -> Vec<u8> {
        self.bytes
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .clone()
    }

    /// Append one record, or count it as a capacity drop when it would not fit whole.
    pub fn push(&mut self, ts_sec: u32, ts_usec: u32, original_len: u32, packet: &[u8]) {
        self.observed += 1;
        let caplen = packet.len() as u32;
        if original_len > caplen {
            self.snap_truncated += 1;
            if !self
                .incomplete
                .iter()
                .any(|item| item == "snaplen truncated")
            {
                self.incomplete.push("snaplen truncated".to_string());
            }
        }
        let record_len = 16 + packet.len();
        let used = self
            .bytes
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .len();
        if used.saturating_add(record_len) > self.limit {
            self.capacity_dropped += 1;
            if !self
                .incomplete
                .iter()
                .any(|item| item == "capacity truncated")
            {
                self.incomplete.push("capacity truncated".to_string());
            }
            return;
        }
        let timestamp = Duration::new(u64::from(ts_sec), ts_usec.saturating_mul(1_000));
        let record = PcapPacket::new(timestamp, original_len, packet);
        match self.writer.write_packet(&record) {
            Ok(_) => self.saved += 1,
            Err(error) => self.incomplete.push(format!("pcap-file write: {error}")),
        }
    }

    fn status(&self) -> &'static str {
        if self.incomplete.is_empty()
            && self.capacity_dropped == 0
            && self.snap_truncated == 0
            && self.stats_dropped == 0
        {
            "complete"
        } else {
            "incomplete"
        }
    }
}

/// True when the queue is empty and the quiet window has elapsed.
pub fn linked_pcap_version() -> String {
    unsafe {
        let text = pcap_lib_version();
        if text.is_null() {
            return "libpcap".to_string();
        }
        std::ffi::CStr::from_ptr(text)
            .to_string_lossy()
            .into_owned()
    }
}

unsafe extern "C" {
    fn pcap_lib_version() -> *const libc::c_char;
}

fn spawn_live(
    mut cap: Capture<pcap::Active>,
    limit: u64,
    quiet: Duration,
    drain_max: Duration,
) -> Result<LiveCapture, Error> {
    let (tx, rx) = mpsc::channel::<Cmd>();
    let wake_raw = unsafe { libc::eventfd(0, libc::EFD_NONBLOCK | libc::EFD_CLOEXEC) };
    if wake_raw < 0 {
        return Err(Error::unknown(format!(
            "capture eventfd failed before any case: {}",
            std::io::Error::last_os_error()
        )));
    }
    let wake = unsafe { OwnedFd::from_raw_fd(wake_raw) };
    let thread_wake = wake_raw;
    let thread = std::thread::Builder::new()
        .name("mcn-pcap".to_string())
        .spawn(move || live_loop(&mut cap, rx, thread_wake, limit, quiet, drain_max))
        .map_err(|error| {
            Error::unknown(format!("capture thread failed before any case: {error}"))
        })?;
    Ok(LiveCapture {
        tx,
        wake,
        thread: Some(thread),
    })
}

struct LiveState {
    mode: LiveMode,
    linktype: u32,
    baseline_dropped: u32,
    fatal: Option<String>,
}

enum LiveMode {
    Idle,
    Capturing {
        buffer: PcapBuffer,
        last_packet: Option<Instant>,
        payloads: Vec<u8>,
    },
    Draining {
        buffer: PcapBuffer,
        last_packet: Option<Instant>,
        deadline: Instant,
        reply: mpsc::Sender<Result<PcapBuffer, String>>,
    },
}

fn live_loop(
    cap: &mut Capture<pcap::Active>,
    rx: mpsc::Receiver<Cmd>,
    wake: i32,
    limit: u64,
    quiet: Duration,
    drain_max: Duration,
) {
    let linktype = cap.get_datalink().0 as u32;
    let mut state = LiveState {
        mode: LiveMode::Idle,
        linktype,
        baseline_dropped: 0,
        fatal: None,
    };
    let pcap_fd = cap.as_raw_fd();
    loop {
        let timeout = match &state.mode {
            LiveMode::Draining { deadline, .. } => deadline
                .saturating_duration_since(Instant::now())
                .as_millis()
                .min(50) as i32,
            _ => 50,
        };
        let mut polls = [
            libc::pollfd {
                fd: pcap_fd,
                events: libc::POLLIN,
                revents: 0,
            },
            libc::pollfd {
                fd: wake,
                events: libc::POLLIN,
                revents: 0,
            },
        ];
        unsafe { libc::poll(polls.as_mut_ptr(), 2, timeout) };
        if polls[0].revents & libc::POLLIN != 0 {
            read_batch(cap, &mut state);
        }
        if polls[1].revents & libc::POLLIN != 0 {
            drain_fd(wake);
            while let Ok(cmd) = rx.try_recv() {
                if !handle_cmd(cap, &mut state, cmd, limit, drain_max) {
                    return;
                }
            }
        }
        if let LiveMode::Draining {
            deadline,
            last_packet,
            ..
        } = &state.mode
        {
            if Instant::now() >= *deadline || drain_is_quiet(*last_packet, quiet, Instant::now()) {
                finish_drain(&mut state);
            }
        }
    }
}

fn read_batch(cap: &mut Capture<pcap::Active>, state: &mut LiveState) {
    for _ in 0..64 {
        match cap.next_packet() {
            Ok(packet) => accept_packet(state, &packet),
            Err(pcap::Error::TimeoutExpired) | Err(pcap::Error::NoMorePackets) => break,
            Err(error) => {
                state.fatal = Some(format!("pcap read: {error}"));
                break;
            }
        }
    }
}

fn accept_packet(state: &mut LiveState, packet: &Packet) {
    let ts_sec = packet.header.ts.tv_sec as u32;
    let ts_usec = packet.header.ts.tv_usec as u32;
    match &mut state.mode {
        LiveMode::Idle => {}
        LiveMode::Capturing {
            buffer,
            last_packet,
            payloads,
        } => {
            buffer.push(ts_sec, ts_usec, packet.header.len, packet.data);
            if payloads.len() < 65_536 {
                payloads.extend_from_slice(packet.data);
            }
            *last_packet = Some(Instant::now());
        }
        LiveMode::Draining {
            buffer,
            last_packet,
            ..
        } => {
            buffer.push(ts_sec, ts_usec, packet.header.len, packet.data);
            *last_packet = Some(Instant::now());
        }
    }
}

fn handle_cmd(
    cap: &mut Capture<pcap::Active>,
    state: &mut LiveState,
    cmd: Cmd,
    limit: u64,
    drain_max: Duration,
) -> bool {
    if let Some(error) = &state.fatal {
        match cmd {
            Cmd::Begin { reply, .. } => {
                let _ = reply.send(Err(error.clone()));
            }
            Cmd::End { reply } => {
                let _ = reply.send(Err(error.clone()));
            }
            Cmd::Payloads { reply } => {
                let _ = reply.send(Vec::new());
            }
            Cmd::Shutdown => return false,
        }
        return true;
    }
    match cmd {
        Cmd::Shutdown => return false,
        Cmd::Payloads { reply } => {
            let payloads = match &state.mode {
                LiveMode::Capturing { payloads, .. } => payloads.clone(),
                _ => Vec::new(),
            };
            let _ = reply.send(payloads);
        }
        Cmd::Begin { id, reply } => {
            let started = Instant::now();
            while cap.next_packet().is_ok() {}
            state.baseline_dropped = cap.stats().map(|stat| stat.dropped).unwrap_or(0);
            let mut buffer = PcapBuffer::new(limit, SNAPLEN, state.linktype);
            buffer.case_id = id;
            buffer.begin_us = started.elapsed().as_micros();
            state.mode = LiveMode::Capturing {
                buffer,
                last_packet: None,
                payloads: Vec::new(),
            };
            let _ = reply.send(Ok(started.elapsed().as_micros()));
        }
        Cmd::End { reply } => {
            let LiveMode::Capturing {
                buffer,
                last_packet,
                payloads: _,
            } = std::mem::replace(&mut state.mode, LiveMode::Idle)
            else {
                let _ = reply.send(Err("capture end without begin".to_string()));
                return true;
            };
            let dropped = cap
                .stats()
                .map(|stat| stat.dropped.saturating_sub(state.baseline_dropped))
                .unwrap_or(0);
            let mut buffer = buffer;
            buffer.stats_dropped = u64::from(dropped);
            if dropped > 0 {
                buffer
                    .incomplete
                    .push(format!("libpcap dropped {dropped} packets"));
            }
            state.mode = LiveMode::Draining {
                buffer,
                last_packet,
                deadline: Instant::now() + drain_max,
                reply,
            };
        }
    }
    true
}

fn finish_drain(state: &mut LiveState) {
    let LiveMode::Draining { buffer, reply, .. } =
        std::mem::replace(&mut state.mode, LiveMode::Idle)
    else {
        return;
    };
    let _ = reply.send(Ok(buffer));
}

fn drain_is_quiet(last_packet: Option<Instant>, quiet: Duration, now: Instant) -> bool {
    match last_packet {
        None => true,
        Some(at) => now.saturating_duration_since(at) >= quiet,
    }
}

/// Shared capture state for the executor and the feedback.
pub struct CaptureEngine {
    enabled: bool,
    file_limit: u64,
    quiet: Duration,
    drain_max: Duration,
    worker: Mutex<Option<WorkerId>>,
    next_case: Mutex<u64>,
    live: Mutex<Option<LiveCapture>>,
    last: Mutex<Option<FinishedCapture>>,
    fatal: Mutex<Option<String>>,
    case_open: AtomicBool,
    last_begin_us: Mutex<u128>,
    #[cfg(feature = "stats")]
    phases: Mutex<PhaseState>,
    #[cfg(feature = "stats")]
    preflight_us: Mutex<u128>,
}

enum Cmd {
    Begin {
        id: u64,
        reply: mpsc::Sender<Result<u128, String>>,
    },
    End {
        reply: mpsc::Sender<Result<PcapBuffer, String>>,
    },
    Payloads {
        reply: mpsc::Sender<Vec<u8>>,
    },
    Shutdown,
}

struct LiveCapture {
    tx: mpsc::Sender<Cmd>,
    wake: OwnedFd,
    thread: Option<JoinHandle<()>>,
}

impl Drop for LiveCapture {
    fn drop(&mut self) {
        let _ = self.tx.send(Cmd::Shutdown);
        wake_fd(self.wake.as_raw_fd());
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

thread_local! {
    static PREPARED: std::cell::RefCell<Option<Capture<pcap::Active>>> = const { std::cell::RefCell::new(None) };
}

/// Open `lo` while CAP_NET_RAW is still held. `preflight` takes this handle.
pub fn prepare_live_handle() -> Result<(), Error> {
    let handle = open_lo()?;
    PREPARED.with(|slot| *slot.borrow_mut() = Some(handle));
    Ok(())
}

fn open_lo() -> Result<Capture<pcap::Active>, Error> {
    let opened = Capture::from_device("lo")
        .map_err(|error| Error::unknown(format!("pcap from_device lo: {error}")))?
        .promisc(false)
        .snaplen(SNAPLEN as i32)
        .buffer_size(4 * 1024 * 1024)
        .immediate_mode(true)
        .timeout(1)
        .open()
        .map_err(|error| Error::unknown(format!("pcap activate lo: {error}")))?;
    opened
        .direction(Direction::InOut)
        .map_err(|error| Error::unknown(format!("pcap direction inout on lo: {error}")))?;
    opened
        .setnonblock()
        .map_err(|error| Error::unknown(format!("pcap setnonblock lo: {error}")))
}

fn take_prepared() -> Option<Capture<pcap::Active>> {
    PREPARED.with(|slot| slot.borrow_mut().take())
}

fn wake_fd(fd: i32) {
    let one = 1u64.to_ne_bytes();
    unsafe {
        libc::write(fd, one.as_ptr().cast(), one.len());
    }
}

fn drain_fd(fd: i32) {
    let mut buf = [0u8; 16];
    unsafe {
        libc::read(fd, buf.as_mut_ptr().cast(), buf.len());
    }
}

impl CaptureEngine {
    /// Build an engine. Disabled campaigns never open a capture handle.
    pub fn new(config: CaptureConfig) -> Self {
        Self {
            enabled: config.enabled,
            file_limit: config.file_limit,
            quiet: Duration::from_millis(config.drain_quiet_ms),
            drain_max: Duration::from_millis(config.drain_timeout_ms),
            worker: Mutex::new(None),
            next_case: Mutex::new(1),
            live: Mutex::new(None),
            last: Mutex::new(None),
            fatal: Mutex::new(None),
            case_open: AtomicBool::new(false),
            last_begin_us: Mutex::new(0),
            #[cfg(feature = "stats")]
            phases: Mutex::new(PhaseState::new()),
            #[cfg(feature = "stats")]
            preflight_us: Mutex::new(0),
        }
    }

    /// Capture is configured on.
    pub fn enabled(&self) -> bool {
        self.enabled
    }

    /// Sticky failure that must stop the next case.
    pub fn fatal(&self) -> Option<String> {
        self.fatal
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .clone()
    }

    /// Startup preflight cost, in microseconds.
    #[cfg(feature = "stats")]
    pub fn preflight_us(&self) -> u128 {
        *self
            .preflight_us
            .lock()
            .unwrap_or_else(|error| error.into_inner())
    }

    /// Latest phase record.
    #[cfg(feature = "stats")]
    pub fn phase_snapshot(&self) -> PhaseSnapshot {
        self.phases
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .snapshot()
    }

    /// Remember the worker label used in metadata.
    pub fn set_worker(&self, worker: WorkerId) {
        *self
            .worker
            .lock()
            .unwrap_or_else(|error| error.into_inner()) = Some(worker);
    }

    /// Fail before the first case when capture cannot be opened.
    pub fn preflight(&self) -> Result<(), Error> {
        #[cfg(feature = "stats")]
        let started = Instant::now();
        let result = self.preflight_inner();
        #[cfg(feature = "stats")]
        {
            *self
                .preflight_us
                .lock()
                .unwrap_or_else(|error| error.into_inner()) = started.elapsed().as_micros();
        }
        result
    }

    fn preflight_inner(&self) -> Result<(), Error> {
        if !self.enabled {
            return Ok(());
        }
        if self
            .live
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .is_some()
        {
            return Ok(());
        }
        let handle = match take_prepared() {
            Some(handle) => handle,
            None => open_lo()?,
        };
        let live = spawn_live(handle, self.file_limit, self.quiet, self.drain_max)?;
        *self.live.lock().unwrap_or_else(|error| error.into_inner()) = Some(live);
        if let Err(error) = self.prove_loopback() {
            self.fail(error.to_string());
            return Err(error);
        }
        Ok(())
    }

    fn prove_loopback(&self) -> Result<(), Error> {
        let left = std::net::UdpSocket::bind("127.0.0.1:0").map_err(|error| {
            Error::unknown(format!(
                "capture preflight socket failed before any case: {error}"
            ))
        })?;
        let right = std::net::UdpSocket::bind("127.0.0.1:0").map_err(|error| {
            Error::unknown(format!(
                "capture preflight socket failed before any case: {error}"
            ))
        })?;
        left.set_nonblocking(true).map_err(|error| {
            Error::unknown(format!(
                "capture preflight set_nonblocking failed before any case: {error}"
            ))
        })?;
        right.set_nonblocking(true).map_err(|error| {
            Error::unknown(format!(
                "capture preflight set_nonblocking failed before any case: {error}"
            ))
        })?;
        let left_addr = left
            .local_addr()
            .map_err(|error| Error::unknown(format!("capture preflight local address: {error}")))?;
        let right_addr = right
            .local_addr()
            .map_err(|error| Error::unknown(format!("capture preflight local address: {error}")))?;
        let deadline = Instant::now() + Duration::from_secs(5);
        let id = self.allocate_case();
        self.begin_case_until(id, deadline)?;
        let mut saw_forward = false;
        let mut saw_back = false;
        let mut scratch = [0u8; 64];
        while Instant::now() < deadline && !(saw_forward && saw_back) {
            left.send_to(PREFLIGHT_FORWARD, right_addr)
                .map_err(|error| {
                    Error::unknown(format!(
                        "capture preflight send failed before any case: {error}"
                    ))
                })?;
            right.send_to(PREFLIGHT_BACK, left_addr).map_err(|error| {
                Error::unknown(format!(
                    "capture preflight send failed before any case: {error}"
                ))
            })?;
            for socket in [&left, &right] {
                match socket.recv_from(&mut scratch) {
                    Ok(_) => {}
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {}
                    Err(error) => {
                        return Err(Error::unknown(format!(
                            "capture preflight read failed before any case: {error}"
                        )));
                    }
                }
            }
            let captured = self.payloads_until(deadline)?;
            saw_forward = contains_marker(&captured, PREFLIGHT_FORWARD);
            saw_back = contains_marker(&captured, PREFLIGHT_BACK);
            let pause =
                Duration::from_millis(20).min(deadline.saturating_duration_since(Instant::now()));
            if !pause.is_zero() {
                std::thread::sleep(pause);
            }
        }
        self.end_case_until(deadline)?;
        *self.last.lock().unwrap_or_else(|error| error.into_inner()) = None;
        if saw_forward && saw_back {
            return Ok(());
        }
        Err(Error::unknown(
            "capture preflight failed before any case: libpcap on lo did not deliver both loopback directions within 5s"
                .to_string(),
        ))
    }

    fn payloads_until(&self, deadline: Instant) -> Result<Vec<u8>, Error> {
        let (tx, rx) = mpsc::channel();
        self.command(Cmd::Payloads { reply: tx })?;
        self.recv_until(&rx, deadline, "payloads")
    }

    fn recv_until<T>(
        &self,
        rx: &mpsc::Receiver<T>,
        deadline: Instant,
        phase: &str,
    ) -> Result<T, Error> {
        match recv_until(rx, deadline) {
            Ok(value) => Ok(value),
            Err(mpsc::RecvTimeoutError::Timeout) => Err(self.sticky_preflight_timeout(phase)),
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                let message = format!("capture preflight {phase} disconnected before any case");
                self.fail(message.clone());
                Err(Error::unknown(message))
            }
        }
    }

    fn sticky_preflight_timeout(&self, phase: &str) -> Error {
        let message = format!("capture preflight timed out waiting for {phase} before any case");
        self.fail(message.clone());
        Error::unknown(message)
    }

    fn command(&self, cmd: Cmd) -> Result<(), Error> {
        let live = self.live.lock().unwrap_or_else(|error| error.into_inner());
        let Some(live) = live.as_ref() else {
            return Err(Error::unknown(
                "capture thread is not ready before any case".to_string(),
            ));
        };
        live.tx
            .send(cmd)
            .map_err(|error| Error::unknown(format!("capture command: {error}")))?;
        wake_fd(live.wake.as_raw_fd());
        Ok(())
    }

    fn fail(&self, message: String) {
        *self.fatal.lock().unwrap_or_else(|error| error.into_inner()) = Some(message);
    }

    /// Begin one case. Capture-off does nothing.
    pub fn begin_case(&self, case_id: u64) -> Result<(), Error> {
        self.begin_case_wait(case_id, None)
    }

    fn begin_case_until(&self, case_id: u64, deadline: Instant) -> Result<(), Error> {
        self.begin_case_wait(case_id, Some(deadline))
    }

    fn begin_case_wait(&self, case_id: u64, deadline: Option<Instant>) -> Result<(), Error> {
        if let Some(fatal) = self.fatal() {
            return Err(Error::unknown(format!("capture is fatal: {fatal}")));
        }
        if !self.enabled {
            return Ok(());
        }
        *self.last.lock().unwrap_or_else(|error| error.into_inner()) = None;
        let (tx, rx) = mpsc::channel();
        self.command(Cmd::Begin {
            id: case_id,
            reply: tx,
        })?;
        let reply = match deadline {
            Some(deadline) => self.recv_until(&rx, deadline, "begin")?,
            None => rx
                .recv()
                .map_err(|error| Error::unknown(format!("capture begin: {error}")))?,
        };
        let begin_us = reply.map_err(Error::unknown)?;
        *self
            .last_begin_us
            .lock()
            .unwrap_or_else(|error| error.into_inner()) = begin_us;
        self.case_open.store(true, Ordering::Relaxed);
        #[cfg(feature = "stats")]
        self.set_capture_ready(begin_us);
        Ok(())
    }

    /// Allocate the next case id.
    pub fn allocate_case(&self) -> u64 {
        let mut next = self
            .next_case
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let id = *next;
        *next = next.wrapping_add(1);
        id
    }

    /// Finish the case after the process tree is clean. Compresses on this thread.
    pub fn end_case(&self) -> Result<(), Error> {
        self.finish_case(self.drain_max + Duration::from_secs(1), false)
    }

    fn end_case_until(&self, deadline: Instant) -> Result<(), Error> {
        self.finish_case(deadline.saturating_duration_since(Instant::now()), true)
    }

    fn finish_case(&self, ack_wait: Duration, sticky_preflight: bool) -> Result<(), Error> {
        if !self.enabled {
            #[cfg(feature = "stats")]
            {
                let mut phases = self
                    .phases
                    .lock()
                    .unwrap_or_else(|error| error.into_inner());
                phases.status = "disabled".to_string();
            }
            return Ok(());
        }
        if !self.case_open.swap(false, Ordering::Relaxed) {
            return Ok(());
        }
        let (tx, rx) = mpsc::channel();
        let drain_at = Instant::now();
        self.command(Cmd::End { reply: tx })?;
        let buffer = match rx.recv_timeout(ack_wait) {
            Ok(value) => value.map_err(|error| {
                self.fail(error.clone());
                Error::unknown(error)
            })?,
            Err(mpsc::RecvTimeoutError::Timeout) if sticky_preflight => {
                return Err(self.sticky_preflight_timeout("end"));
            }
            Err(mpsc::RecvTimeoutError::Disconnected) if sticky_preflight => {
                let message = "capture preflight end disconnected before any case".to_string();
                self.fail(message.clone());
                return Err(Error::unknown(message));
            }
            Err(error) => return Err(Error::unknown(format!("capture end: {error}"))),
        };
        let drain_us = drain_at.elapsed().as_micros();
        let raw = buffer.bytes();
        let compress_at = Instant::now();
        let compressed = zlib_compress(&raw)?;
        let zlib_us = compress_at.elapsed().as_micros();
        let status = buffer.status();
        let note = buffer.incomplete.join("; ");
        #[cfg(feature = "stats")]
        let persist_us = Arc::new(Mutex::new(0u128));
        let installed = *self
            .worker
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let worker = installed.map(|item| item.label()).unwrap_or_default();
        let session = installed.map(|item| item.nonce).unwrap_or(0);
        let begin_us = *self
            .last_begin_us
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let metadata = PcapMetadata {
            zlib: compressed.clone(),
            uncompressed_len: raw.len() as u64,
            status: status.to_string(),
            pids: vec![std::process::id() as i32],
            packets_captured: buffer.observed,
            packets_received: buffer.saved,
            packets_dropped: buffer.stats_dropped,
            #[cfg(feature = "stats")]
            ready_us: u64_us(begin_us),
            #[cfg(feature = "stats")]
            tail_us: u64_us(drain_us),
            #[cfg(feature = "stats")]
            stop_us: 0,
            #[cfg(feature = "stats")]
            read_us: 0,
            #[cfg(feature = "stats")]
            compress_us: u64_us(zlib_us),
            backend: "libpcap".to_string(),
            backend_version: linked_pcap_version(),
            worker_id: worker,
            case_id: buffer.case_id,
            observed: buffer.observed,
            saved: buffer.saved,
            capacity_dropped: buffer.capacity_dropped,
            stats_dropped: buffer.stats_dropped,
            begin_us: u64_us(begin_us),
            drain_us: u64_us(drain_us),
            zlib_us: u64_us(zlib_us),
            reason: note.clone(),
            note,
        };
        #[cfg(feature = "stats")]
        {
            let mut phases = self
                .phases
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            phases.capture_ready_us = begin_us;
            phases.tail_drain_us = drain_us;
            phases.zlib_us = zlib_us;
            phases.persist_us = Arc::clone(&persist_us);
            phases.uncompressed_len = metadata.uncompressed_len;
            phases.compressed_len = compressed.len() as u64;
            phases.packets_captured = metadata.observed;
            phases.packets_received = metadata.saved;
            phases.packets_dropped = metadata.capacity_dropped;
            phases.status = metadata.status.clone();
            phases.note = metadata.note.clone();
        }
        *self.last.lock().unwrap_or_else(|error| error.into_inner()) = Some(FinishedCapture {
            metadata,
            attachment: PendingCapture {
                session,
                seq: buffer.case_id,
                compressed,
                #[cfg(feature = "stats")]
                persist_us,
            },
        });
        Ok(())
    }

    /// `post_exec`: end a case the executor did not finish.
    pub fn finish_post_exec(&self) {
        if self.enabled
            && self
                .live
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .is_some()
            && self
                .last
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .is_none()
        {
            let _ = self.end_case();
        }
    }

    fn last_finished(&self) -> Option<FinishedCapture> {
        self.last
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .clone()
    }

    /// Record work-directory restore and forkserver prepare.
    #[cfg(feature = "stats")]
    pub fn set_target_setup(&self, micros: u128) {
        self.phases
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .target_setup_us = micros;
    }

    /// Record Begin cost.
    #[cfg(feature = "stats")]
    pub fn set_capture_ready(&self, micros: u128) {
        self.phases
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .capture_ready_us = micros;
    }

    /// Record generator and SUT time through cleanup.
    #[cfg(feature = "stats")]
    pub fn set_interaction(&self, micros: u128) {
        self.phases
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .interaction_us = micros;
    }
}

fn u64_us(value: u128) -> u64 {
    u64::try_from(value).unwrap_or(u64::MAX)
}

fn zlib_compress(bytes: &[u8]) -> Result<Vec<u8>, Error> {
    let mut encoder = ZlibEncoder::new(Vec::new(), Compression::new(COMPRESSION_LEVEL));
    encoder
        .write_all(bytes)
        .map_err(|error| Error::unknown(format!("zlib compress failed: {error}")))?;
    encoder
        .finish()
        .map_err(|error| Error::unknown(format!("zlib finish failed: {error}")))
}

/// LibAFL observer. `post_exec` finalizes a case the executor has not ended.
#[derive(Clone)]
pub struct PcapObserver {
    name: std::borrow::Cow<'static, str>,
    engine: Arc<CaptureEngine>,
}

impl PcapObserver {
    /// Observe the shared engine.
    pub fn new(engine: Arc<CaptureEngine>) -> Self {
        Self {
            name: std::borrow::Cow::Borrowed("pcap"),
            engine,
        }
    }
}

impl Serialize for PcapObserver {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        serializer.serialize_unit()
    }
}

impl<'de> Deserialize<'de> for PcapObserver {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        struct Visitor;
        impl serde::de::Visitor<'_> for Visitor {
            type Value = ();
            fn expecting(&self, formatter: &mut std::fmt::Formatter) -> std::fmt::Result {
                formatter.write_str("unit")
            }
            fn visit_unit<E>(self) -> Result<(), E> {
                Ok(())
            }
        }
        deserializer.deserialize_unit(Visitor)?;
        Ok(Self::new(Arc::new(CaptureEngine::new(
            CaptureConfig::default(),
        ))))
    }
}

impl Named for PcapObserver {
    fn name(&self) -> &std::borrow::Cow<'static, str> {
        &self.name
    }
}

impl<S> Observer<GeneratorInput, S> for PcapObserver {
    fn pre_exec(&mut self, _state: &mut S, _input: &GeneratorInput) -> Result<(), Error> {
        Ok(())
    }

    fn post_exec(
        &mut self,
        _state: &mut S,
        _input: &GeneratorInput,
        _exit_kind: &ExitKind,
    ) -> Result<(), Error> {
        self.engine.finish_post_exec();
        Ok(())
    }
}

/// Attaches capture metadata only when another feedback accepts the testcase.
pub struct PcapToMetadataFeedback {
    name: std::borrow::Cow<'static, str>,
    engine: Arc<CaptureEngine>,
}

impl PcapToMetadataFeedback {
    /// Feedback that never marks a testcase interesting by itself.
    pub fn new(engine: Arc<CaptureEngine>) -> Self {
        Self {
            name: std::borrow::Cow::Borrowed("pcap_metadata"),
            engine,
        }
    }
}

impl Named for PcapToMetadataFeedback {
    fn name(&self) -> &std::borrow::Cow<'static, str> {
        &self.name
    }
}

impl<S> StateInitializer<S> for PcapToMetadataFeedback {}

impl<EM, OT, S> Feedback<EM, GeneratorInput, OT, S> for PcapToMetadataFeedback {
    fn is_interesting(
        &mut self,
        _state: &mut S,
        _manager: &mut EM,
        _input: &GeneratorInput,
        _observers: &OT,
        _exit_kind: &ExitKind,
    ) -> Result<bool, Error> {
        Ok(false)
    }

    fn append_metadata(
        &mut self,
        _state: &mut S,
        _manager: &mut EM,
        _observers: &OT,
        testcase: &mut Testcase<GeneratorInput>,
    ) -> Result<(), Error> {
        let Some(finished) = self.engine.last_finished() else {
            return Ok(());
        };
        if let Some(input) = testcase.input_mut().as_mut() {
            input.attach_pending(finished.attachment);
        }
        testcase.metadata_map_mut().insert(finished.metadata);
        Ok(())
    }
}

/// Drop guard kept so a failed `run_target` still ends the case.
pub struct PostExecHandoff {
    engine: Arc<CaptureEngine>,
    armed: bool,
}

impl PostExecHandoff {
    /// The success path ends the case itself.
    pub fn disarm(&mut self) {
        self.armed = false;
    }
}

impl Drop for PostExecHandoff {
    fn drop(&mut self) {
        if self.armed {
            self.engine.finish_post_exec();
        }
    }
}

impl CaptureEngine {
    /// Arm drop-finalize until the caller disarms and `post_exec` runs.
    pub fn handoff(self: &Arc<Self>) -> PostExecHandoff {
        PostExecHandoff {
            engine: Arc::clone(self),
            armed: self.enabled,
        }
    }
}

mod b64bytes {
    use super::base64_shim::{decode, encode};
    use serde::{Deserialize, Deserializer, Serializer};

    pub fn serialize<S>(bytes: &Vec<u8>, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        serializer.serialize_str(&encode(bytes))
    }

    pub fn deserialize<'de, D>(deserializer: D) -> Result<Vec<u8>, D::Error>
    where
        D: Deserializer<'de>,
    {
        let text = String::deserialize(deserializer)?;
        decode(&text).map_err(serde::de::Error::custom)
    }
}

mod base64_shim {
    pub fn encode(bytes: &[u8]) -> String {
        const TABLE: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
        let mut out = String::new();
        let mut index = 0;
        while index + 3 <= bytes.len() {
            let triple = ((bytes[index] as u32) << 16)
                | ((bytes[index + 1] as u32) << 8)
                | bytes[index + 2] as u32;
            for shift in [18, 12, 6, 0] {
                out.push(TABLE[((triple >> shift) & 0x3f) as usize] as char);
            }
            index += 3;
        }
        if index < bytes.len() {
            let remain = bytes.len() - index;
            let mut triple = (bytes[index] as u32) << 16;
            if remain == 2 {
                triple |= (bytes[index + 1] as u32) << 8;
            }
            out.push(TABLE[((triple >> 18) & 0x3f) as usize] as char);
            out.push(TABLE[((triple >> 12) & 0x3f) as usize] as char);
            if remain == 2 {
                out.push(TABLE[((triple >> 6) & 0x3f) as usize] as char);
                out.push('=');
            } else {
                out.push('=');
                out.push('=');
            }
        }
        out
    }

    pub fn decode(text: &str) -> Result<Vec<u8>, String> {
        fn value(byte: u8) -> Result<u8, String> {
            match byte {
                b'A'..=b'Z' => Ok(byte - b'A'),
                b'a'..=b'z' => Ok(byte - b'a' + 26),
                b'0'..=b'9' => Ok(byte - b'0' + 52),
                b'+' => Ok(62),
                b'/' => Ok(63),
                _ => Err(format!("bad base64 byte {byte}")),
            }
        }
        let bytes = text.as_bytes();
        if bytes.len() % 4 != 0 {
            return Err("base64 length".to_string());
        }
        let mut out = Vec::new();
        for chunk in bytes.chunks(4) {
            let pad = chunk.iter().filter(|byte| **byte == b'=').count();
            let mut triple = 0u32;
            for (index, byte) in chunk.iter().enumerate() {
                if *byte == b'=' {
                    continue;
                }
                triple |= u32::from(value(*byte)?) << (18 - 6 * index);
            }
            out.push((triple >> 16) as u8);
            if pad < 2 {
                out.push((triple >> 8) as u8);
            }
            if pad < 1 {
                out.push(triple as u8);
            }
        }
        Ok(out)
    }
}

fn recv_until<T>(rx: &mpsc::Receiver<T>, deadline: Instant) -> Result<T, mpsc::RecvTimeoutError> {
    rx.recv_timeout(deadline.saturating_duration_since(Instant::now()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use libafl::{
        corpus::{Corpus, OnDiskCorpus},
        inputs::Input,
    };

    #[test]
    fn capacity_limit_keeps_only_whole_records() {
        let header = 24;
        let packet = 8;
        let record = 16 + packet;
        let mut buffer = PcapBuffer::new((header + record) as u64, 64, LINKTYPE_ETHERNET);
        buffer.push(1, 0, packet as u32, &[1, 2, 3, 4, 5, 6, 7, 8]);
        buffer.push(2, 0, packet as u32, &[9, 9, 9, 9, 9, 9, 9, 9]);
        assert_eq!(buffer.saved, 1);
        assert_eq!(buffer.capacity_dropped, 1);
        assert_eq!(buffer.bytes().len(), header + record);
        assert_eq!(buffer.status(), "incomplete");
        assert!(!buffer.bytes().ends_with(&[9, 9]));
    }

    #[test]
    fn empty_queue_is_already_quiet() {
        let now = Instant::now();
        assert!(drain_is_quiet(None, Duration::from_millis(5), now));
        assert!(!drain_is_quiet(Some(now), Duration::from_millis(5), now));
        assert!(drain_is_quiet(
            Some(now - Duration::from_millis(5)),
            Duration::from_millis(5),
            now
        ));
    }

    #[test]
    fn markers_match_at_seventeen_bytes_and_require_both_directions() {
        assert_eq!(PREFLIGHT_FORWARD.len(), 17);
        assert_eq!(PREFLIGHT_BACK.len(), 17);
        assert!(contains_marker(PREFLIGHT_FORWARD, PREFLIGHT_FORWARD));
        assert!(contains_marker(PREFLIGHT_BACK, PREFLIGHT_BACK));
        assert!(!contains_marker(PREFLIGHT_FORWARD, PREFLIGHT_BACK));
        assert!(!both_preflight_directions(PREFLIGHT_FORWARD));
        let mut frame = b"ethernet-ip-udp".to_vec();
        frame.extend_from_slice(PREFLIGHT_FORWARD);
        frame.extend_from_slice(b"pad");
        frame.extend_from_slice(PREFLIGHT_BACK);
        assert!(both_preflight_directions(&frame));
        assert!(PREFLIGHT_FORWARD
            .windows(18)
            .all(|window| window != PREFLIGHT_FORWARD));
        assert!(!contains_marker(
            &frame[..PREFLIGHT_FORWARD.len() - 1],
            PREFLIGHT_FORWARD
        ));
    }

    #[test]
    fn preflight_replies_use_the_remaining_deadline_and_stick() {
        let (tx, rx) = mpsc::channel::<Vec<u8>>();
        let deadline = Instant::now() + Duration::from_millis(150);
        let started = Instant::now();
        let timed_out = recv_until(&rx, deadline).unwrap_err();
        assert!(matches!(timed_out, mpsc::RecvTimeoutError::Timeout));
        let elapsed = started.elapsed();
        assert!(
            elapsed < Duration::from_millis(1000),
            "empty preflight reply waited {elapsed:?}"
        );
        let due = Instant::now();
        assert!(matches!(
            recv_until(&rx, due).unwrap_err(),
            mpsc::RecvTimeoutError::Timeout
        ));
        assert!(due.elapsed() < Duration::from_millis(250));
        tx.send(vec![7]).unwrap();
        let ready = Instant::now();
        assert_eq!(
            recv_until(&rx, Instant::now() + Duration::from_secs(5)).unwrap(),
            vec![7]
        );
        assert!(ready.elapsed() < Duration::from_millis(250));

        let (engine, hold) = parked_capture();
        let (done_tx, done_rx) = mpsc::channel();
        let started = Instant::now();
        std::thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_millis(120);
            let payloads = engine.payloads_until(deadline);
            let fatal = engine.fatal();
            let begin = engine.begin_case(1);
            let _ = done_tx.send((
                format!("{payloads:?}"),
                fatal,
                format!("{begin:?}"),
                started.elapsed(),
            ));
        });
        let (payloads, fatal, begin, elapsed) = done_rx
            .recv_timeout(Duration::from_secs(2))
            .expect("payloads recv was not bounded by the preflight deadline");
        assert!(elapsed < Duration::from_secs(1), "{elapsed:?}");
        assert!(payloads.contains("timed out"), "{payloads}");
        assert!(payloads.contains("payloads"), "{payloads}");
        let fatal = fatal.expect("payload timeout is sticky");
        assert!(fatal.contains("payloads"), "{fatal}");
        assert!(begin.contains("fatal"), "{begin}");
        drop(hold);

        let (engine, hold) = parked_capture();
        let (done_tx, done_rx) = mpsc::channel();
        std::thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_millis(80);
            let begin = engine.begin_case_until(3, deadline);
            let fatal = engine.fatal();
            engine.case_open.store(true, Ordering::Relaxed);
            let end_deadline = Instant::now() + Duration::from_millis(80);
            let end = engine.end_case_until(end_deadline);
            let _ = done_tx.send((format!("{begin:?}"), fatal, format!("{end:?}")));
        });
        let (begin, begin_fatal, end) = done_rx
            .recv_timeout(Duration::from_secs(2))
            .expect("begin/end ack was not bounded by the preflight deadline");
        assert!(
            begin.contains("timed out") && begin.contains("begin"),
            "{begin}"
        );
        assert!(
            begin_fatal
                .as_deref()
                .is_some_and(|text| text.contains("begin")),
            "{begin_fatal:?}"
        );
        assert!(end.contains("timed out") && end.contains("end"), "{end}");
        drop(hold);
    }

    fn parked_capture() -> (CaptureEngine, mpsc::Receiver<Cmd>) {
        let engine = CaptureEngine::new(CaptureConfig {
            enabled: true,
            ..CaptureConfig::default()
        });
        let (tx, rx) = mpsc::channel();
        let wake_raw = unsafe { libc::eventfd(0, libc::EFD_NONBLOCK | libc::EFD_CLOEXEC) };
        assert!(
            wake_raw >= 0,
            "eventfd: {}",
            std::io::Error::last_os_error()
        );
        let wake = unsafe { OwnedFd::from_raw_fd(wake_raw) };
        *engine
            .live
            .lock()
            .unwrap_or_else(|error| error.into_inner()) = Some(LiveCapture {
            tx,
            wake,
            thread: None,
        });
        (engine, rx)
    }

    #[test]
    fn enabled_preflight_uses_libpcap_and_does_not_stub() {
        let engine = CaptureEngine::new(CaptureConfig {
            enabled: true,
            ..CaptureConfig::default()
        });
        match engine.preflight() {
            Ok(()) => {
                engine.begin_case(1).unwrap();
                engine.end_case().unwrap();
            }
            Err(error) => {
                let message = format!("{error:?}");
                assert!(
                    message.contains("pcap activate")
                        || message.contains("pcap direction")
                        || message.contains("pcap setnonblock")
                        || message.contains("both loopback")
                        || message.contains("capture thread")
                        || message.contains("eventfd")
                        || message.contains("preflight"),
                    "{message}"
                );
                assert!(engine.begin_case(1).is_err(), "{message}");
            }
        }
    }

    #[test]
    fn saved_corpus_inputs_get_metadata_and_a_sidecar() {
        let dir = std::env::temp_dir().join(format!("mcn-pcap-save-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let corpus_dir = dir.join("corpus");
        std::fs::create_dir_all(&corpus_dir).unwrap();
        let script = dir.join("gen.py");
        std::fs::write(&script, "def generate(ctx, params):\n    pass\n").unwrap();
        let seed = dir.join("seed.json");
        std::fs::write(&seed, format!("{{\"generator\":\"{}\"}}", script.display())).unwrap();
        let base = GeneratorInput::from_file(&seed).unwrap();
        let engine = Arc::new(CaptureEngine::new(CaptureConfig::default()));
        let mut feedback = PcapToMetadataFeedback::new(Arc::clone(&engine));
        let compressed = zlib_compress(b"pcap-bytes").unwrap();
        *engine.last.lock().unwrap() = Some(FinishedCapture {
            metadata: PcapMetadata {
                zlib: compressed.clone(),
                uncompressed_len: 10,
                status: "complete".to_string(),
                pids: vec![1],
                packets_captured: 1,
                packets_received: 1,
                packets_dropped: 0,
                #[cfg(feature = "stats")]
                ready_us: 0,
                #[cfg(feature = "stats")]
                tail_us: 0,
                #[cfg(feature = "stats")]
                stop_us: 0,
                #[cfg(feature = "stats")]
                read_us: 0,
                #[cfg(feature = "stats")]
                compress_us: 0,
                backend: "test".to_string(),
                backend_version: String::new(),
                worker_id: "w".to_string(),
                case_id: 7,
                observed: 1,
                saved: 1,
                capacity_dropped: 0,
                stats_dropped: 0,
                begin_us: 0,
                drain_us: 0,
                zlib_us: 0,
                reason: String::new(),
                note: String::new(),
            },
            attachment: PendingCapture {
                session: 0x1111,
                seq: 7,
                compressed,
                #[cfg(feature = "stats")]
                persist_us: Arc::new(Mutex::new(0)),
            },
        });
        let mut testcase = Testcase::from(base);
        feedback
            .append_metadata(&mut (), &mut (), &(), &mut testcase)
            .unwrap();
        assert!(testcase.metadata_map().get::<PcapMetadata>().is_some());
        let mut corpus = OnDiskCorpus::new(&corpus_dir).unwrap();
        corpus.add(testcase).unwrap();
        let names = std::fs::read_dir(&corpus_dir)
            .unwrap()
            .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
            .collect::<Vec<_>>();
        assert!(
            names.iter().any(|name| name.ends_with(".pcap")),
            "{names:?}"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn disabled_engine_does_not_invent_a_capture() {
        let engine = CaptureEngine::new(CaptureConfig::default());
        engine.preflight().unwrap();
        engine.begin_case(1).unwrap();
        engine.end_case().unwrap();
        assert!(engine.last_finished().is_none());
    }
}
