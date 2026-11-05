//! One supervisor for agent, Python, and SUT children.
//!
//! Root-process exit is observed without waiting for inherited pipes to close.
//! Log drains, joins, and cleanup each have their own bound. The evaluation
//! binary is a subreaper; tests leave adoption off so other test children are
//! left alone.

use std::{
    collections::BTreeSet,
    fs::{self, OpenOptions},
    io::{Read, Write},
    os::fd::AsRawFd,
    path::Path,
    process::{Child, Command, ExitStatus, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    thread,
    time::{Duration, Instant},
};

use std::os::unix::process::CommandExt;

use crate::engine::synthesis::redact_text;

const SUBREAPER: i32 = 36;
const POLL_SLICE_MS: i32 = 50;

static CANCEL: AtomicBool = AtomicBool::new(false);
static ADOPT_ORPHANS: AtomicBool = AtomicBool::new(false);

/// AFL forkserver variables that must not leak into an ordinary SUT process.
const AFL_VARS: [&str; 6] = [
    "AFL_LLVM_ONLY_FSRV",
    "AFL_GCC_ONLY_FSRV",
    "__AFL_SHM_ID",
    "__AFL_SHM_ID_SIZE",
    "AFL_MAP_SIZE",
    "AFL_FORKSRV_FD",
];

#[derive(Clone, Debug)]
pub struct SpawnSpec {
    pub role: String,
    pub argv: Vec<String>,
    pub env: Vec<(String, String)>,
    pub cwd: std::path::PathBuf,
    pub stdout_log: std::path::PathBuf,
    pub stderr_log: std::path::PathBuf,
    /// Drop inherited AFL forkserver variables and file descriptors.
    pub clear_afl: bool,
}

struct Drain {
    stop: Arc<AtomicBool>,
    handle: Option<thread::JoinHandle<Result<(), String>>>,
}

struct Tracked {
    role: String,
    child: Child,
    pgid: i32,
    stdout: Option<Drain>,
    stderr: Option<Drain>,
}

pub struct Supervisor {
    children: Vec<Tracked>,
    pub launched: Vec<Vec<String>>,
    secrets: Vec<String>,
    cleaned: bool,
}

impl Supervisor {
    pub fn new(secrets: Vec<String>) -> Self {
        Self {
            children: Vec::new(),
            launched: Vec::new(),
            secrets,
            cleaned: false,
        }
    }

    /// Adopt grandchildren when their parent exits. Call this from the eval binary only.
    pub fn become_subreaper() {
        unsafe {
            libc::prctl(SUBREAPER, 1, 0, 0, 0);
        }
        ADOPT_ORPHANS.store(true, Ordering::SeqCst);
    }

    pub fn install_signal_handlers() {
        extern "C" fn handle(_: i32) {
            CANCEL.store(true, Ordering::SeqCst);
        }
        unsafe {
            libc::signal(libc::SIGTERM, handle as *const () as libc::sighandler_t);
            libc::signal(libc::SIGINT, handle as *const () as libc::sighandler_t);
        }
    }

    pub fn cancel_requested() -> bool {
        CANCEL.load(Ordering::SeqCst)
    }

    pub fn spawn(&mut self, spec: &SpawnSpec) -> Result<u32, String> {
        if spec.argv.is_empty() {
            return Err(format!("{} command is empty", spec.role));
        }
        if let Some(parent) = spec.stdout_log.parent() {
            fs::create_dir_all(parent).map_err(|error| error.to_string())?;
        }
        let mut child = None;
        let mut spawn_error = None;
        for _ in 0..5 {
            let mut command = Command::new(&spec.argv[0]);
            command
                .args(&spec.argv[1..])
                .current_dir(&spec.cwd)
                .stdin(Stdio::null())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .process_group(0);
            command.env_remove("MCN_HC_SOCKET");
            if spec.clear_afl {
                for key in AFL_VARS {
                    command.env_remove(key);
                }
            }
            for (key, value) in &spec.env {
                command.env(key, value);
            }
            if spec.clear_afl {
                unsafe {
                    command.pre_exec(|| {
                        for fd in 3..256 {
                            libc::close(fd);
                        }
                        Ok(())
                    });
                }
            }
            match command.spawn() {
                Ok(spawned) => {
                    child = Some(spawned);
                    break;
                }
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                    spawn_error = Some(error);
                    thread::sleep(Duration::from_millis(20));
                }
                Err(error) => {
                    return Err(format!(
                        "starting {} ({}): {error}",
                        spec.role, spec.argv[0]
                    ));
                }
            }
        }
        let mut child = child.ok_or_else(|| {
            format!(
                "starting {} ({}): {}",
                spec.role,
                spec.argv[0],
                spawn_error
                    .map(|error| error.to_string())
                    .unwrap_or_else(|| "not found".to_string())
            )
        })?;
        let pid = child.id();
        let pgid = pid as i32;
        let stdout = drain(child.stdout.take(), &spec.stdout_log, self.secrets.clone());
        let stderr = drain(child.stderr.take(), &spec.stderr_log, self.secrets.clone());
        self.launched.push(spec.argv.clone());
        self.cleaned = false;
        self.children.push(Tracked {
            role: spec.role.clone(),
            child,
            pgid,
            stdout,
            stderr,
        });
        Ok(pid)
    }

    /// Root exit only. Log threads keep reading and are not joined here.
    pub fn poll(&mut self, pid: u32) -> Result<Option<ExitStatus>, String> {
        let tracked = self
            .children
            .iter_mut()
            .find(|child| child.child.id() == pid)
            .ok_or_else(|| format!("pid {pid} is not supervised"))?;
        tracked.child.try_wait().map_err(|error| error.to_string())
    }

    pub fn wait_for(&mut self, pid: u32, budget: Duration) -> Result<ExitStatus, String> {
        let deadline = Instant::now() + budget;
        loop {
            if Self::cancel_requested() {
                let _ = self.cleanup(Duration::from_millis(200));
                return Err(format!("pid {pid} interrupted"));
            }
            if let Some(status) = self.poll(pid)? {
                return Ok(status);
            }
            if Instant::now() >= deadline {
                self.signal_pid(pid, libc::SIGKILL);
                let kill_deadline = Instant::now() + Duration::from_millis(200);
                while Instant::now() < kill_deadline {
                    if let Some(status) = self.poll(pid)? {
                        let _ = status;
                        break;
                    }
                    thread::sleep(Duration::from_millis(10));
                }
                return Err(format!("pid {pid} exceeded its deadline"));
            }
            thread::sleep(Duration::from_millis(10));
        }
    }

    pub fn running(&mut self, pid: u32) -> bool {
        matches!(self.poll(pid), Ok(None))
    }

    /// After root exit, drain bytes already in its pipes and join the readers.
    /// Inherited pipes do not have to reach EOF; continuously writing descendants
    /// produce a bounded failure instead of incomplete logs being parsed.
    pub fn finish_child_logs(&mut self, pid: u32, budget: Duration) -> Result<(), String> {
        let tracked = self
            .children
            .iter_mut()
            .find(|child| child.child.id() == pid)
            .ok_or_else(|| format!("pid {pid} is not supervised"))?;
        request_log_stop(&tracked.stdout);
        request_log_stop(&tracked.stderr);
        let deadline = Instant::now() + budget;
        stop_and_join(&mut tracked.stdout, deadline)?;
        stop_and_join(&mut tracked.stderr, deadline)
    }

    pub fn signal_pid(&self, pid: u32, signal: i32) {
        if let Some(tracked) = self.children.iter().find(|child| child.child.id() == pid) {
            let _ = signal_group(tracked.pgid, signal);
        }
    }

    /// Stop every process in this supervisor's groups, then reap them.
    ///
    /// A root that has already exited does not hide its descendants. TERM is
    /// followed by KILL when the grace expires. Adopted orphans are reaped
    /// only in the evaluation process, and only when they belong to a group
    /// this supervisor created or were reparented from one of its children.
    pub fn cleanup(&mut self, term_grace: Duration) -> Result<(), String> {
        let groups: BTreeSet<i32> = self.children.iter().map(|tracked| tracked.pgid).collect();
        let known: BTreeSet<i32> = self
            .children
            .iter()
            .map(|tracked| tracked.child.id() as i32)
            .collect();
        for pgid in &groups {
            let _ = signal_group(*pgid, libc::SIGTERM);
        }
        let deadline = Instant::now() + term_grace;
        while Instant::now() < deadline && !our_pids(&groups, &known).is_empty() {
            thread::sleep(Duration::from_millis(10));
        }
        for pgid in &groups {
            let _ = signal_group(*pgid, libc::SIGKILL);
        }
        for pid in our_pids(&groups, &known) {
            let _ = signal_one(pid, libc::SIGKILL);
        }
        let kill_deadline = Instant::now() + term_grace.max(Duration::from_millis(200));
        while Instant::now() < kill_deadline && !our_pids(&groups, &known).is_empty() {
            reap_known(&our_pids(&groups, &known));
            thread::sleep(Duration::from_millis(10));
        }
        let mut leftover = Vec::new();
        for tracked in &mut self.children {
            match tracked.child.try_wait() {
                Ok(None) => leftover.push(format!("{}:{}", tracked.role, tracked.child.id())),
                Ok(Some(_)) => {}
                Err(error) if error.raw_os_error() == Some(libc::ECHILD) => {}
                Err(error) => leftover.push(format!("{}:{error}", tracked.role)),
            }
        }
        for pid in our_pids(&groups, &known) {
            leftover.push(format!("descendant:{pid}"));
        }
        if let Err(error) = self.finish_logs(Duration::from_millis(500)) {
            leftover.push(error);
        }
        self.cleaned = leftover.is_empty();
        if leftover.is_empty() {
            Ok(())
        } else {
            Err(format!("cleanup_failed: {leftover:?}"))
        }
    }

    fn finish_logs(&mut self, budget: Duration) -> Result<(), String> {
        for tracked in &self.children {
            request_log_stop(&tracked.stdout);
            request_log_stop(&tracked.stderr);
        }
        let deadline = Instant::now() + budget;
        let mut errors = Vec::new();
        for tracked in &mut self.children {
            for drain in [&mut tracked.stdout, &mut tracked.stderr] {
                if let Err(error) = stop_and_join(drain, deadline) {
                    errors.push(error);
                }
            }
        }
        if errors.is_empty() {
            Ok(())
        } else {
            Err(errors.join("; "))
        }
    }
}

impl Drop for Supervisor {
    fn drop(&mut self) {
        if !self.cleaned {
            let _ = self.cleanup(Duration::from_millis(200));
        }
    }
}

fn signal_group(pgid: i32, signal: i32) -> bool {
    if pgid <= 1 {
        return false;
    }
    unsafe { libc::kill(-pgid, signal) == 0 }
}

fn signal_one(pid: i32, signal: i32) -> bool {
    if pid <= 1 {
        return false;
    }
    unsafe { libc::kill(pid, signal) == 0 }
}

fn our_pids(groups: &BTreeSet<i32>, known: &BTreeSet<i32>) -> Vec<i32> {
    let mut found = Vec::new();
    let Ok(entries) = fs::read_dir("/proc") else {
        return found;
    };
    let self_pid = std::process::id() as i32;
    for entry in entries.flatten() {
        let Ok(pid) = entry.file_name().to_string_lossy().parse::<i32>() else {
            continue;
        };
        if pid <= 1 {
            continue;
        }
        let Some((ppid, pgrp, state)) = proc_identity(pid) else {
            continue;
        };
        let in_group = groups.contains(&pgrp);
        // Only the standalone evaluation binary enables adoption; all of its
        // direct children are evaluation children. A setsid orphan keeps this
        // ownership even though its process group has changed.
        let adopted = ADOPT_ORPHANS.load(Ordering::SeqCst) && ppid == self_pid;
        if state == 'Z' {
            // Direct roots remain owned by Child::try_wait. Adopted zombies
            // must also be reaped when setsid moved them out of our groups.
            if !known.contains(&pid) && (in_group || adopted) {
                reap_one(pid);
                if Path::new(&format!("/proc/{pid}")).exists() {
                    found.push(pid);
                }
            }
            continue;
        }
        if in_group || known.contains(&pid) || adopted {
            if Path::new(&format!("/proc/{pid}")).exists() {
                found.push(pid);
            }
        }
    }
    found
}

fn proc_identity(pid: i32) -> Option<(i32, i32, char)> {
    let text = fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let rest = text.rsplit_once(')')?.1.trim_start();
    let mut fields = rest.split_whitespace();
    let state = fields.next()?.chars().next()?;
    let ppid: i32 = fields.next()?.parse().ok()?;
    let pgrp: i32 = fields.next()?.parse().ok()?;
    Some((ppid, pgrp, state))
}

fn reap_known(pids: &[i32]) {
    for pid in pids {
        reap_one(*pid);
    }
}

fn reap_one(pid: i32) {
    let mut status = 0;
    unsafe {
        libc::waitpid(pid, &mut status, libc::WNOHANG);
    }
}

fn request_log_stop(drain: &Option<Drain>) {
    if let Some(drain) = drain {
        drain.stop.store(true, Ordering::SeqCst);
    }
}

fn stop_and_join(drain: &mut Option<Drain>, deadline: Instant) -> Result<(), String> {
    let Some(reader) = drain.as_mut() else {
        return Ok(());
    };
    reader.stop.store(true, Ordering::SeqCst);
    let Some(handle) = reader.handle.as_ref() else {
        return Ok(());
    };
    while !handle.is_finished() && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(10));
    }
    if !handle.is_finished() {
        return Err("log reader did not stop before its deadline".to_string());
    }
    let mut reader = drain.take().expect("reader exists");
    reader
        .handle
        .take()
        .expect("reader handle exists")
        .join()
        .map_err(|_| "log reader panicked".to_string())?
}

fn drain(
    pipe: Option<impl Read + AsRawFd + Send + 'static>,
    path: &Path,
    secrets: Vec<String>,
) -> Option<Drain> {
    let mut pipe = pipe?;
    let path = path.to_path_buf();
    let stop = Arc::new(AtomicBool::new(false));
    let stop_thread = Arc::clone(&stop);
    let handle = thread::spawn(move || {
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)
            .ok();
        let fd = pipe.as_raw_fd();
        let mut buf = [0_u8; 8192];
        loop {
            if stop_thread.load(Ordering::SeqCst) {
                set_nonblocking(fd)?;
                let deadline = Instant::now() + Duration::from_millis(200);
                while write_read(&mut pipe, &mut file, &mut buf, &secrets) {
                    if Instant::now() >= deadline {
                        return Err("log draining exceeded its deadline".to_string());
                    }
                }
                break;
            }
            let mut pollfd = libc::pollfd {
                fd,
                events: libc::POLLIN,
                revents: 0,
            };
            let ready = unsafe { libc::poll(&mut pollfd, 1, POLL_SLICE_MS) };
            if ready > 0 {
                if !write_read(&mut pipe, &mut file, &mut buf, &secrets) {
                    break;
                }
                continue;
            }
            if ready < 0 {
                break;
            }
        }
        Ok(())
    });
    Some(Drain {
        stop,
        handle: Some(handle),
    })
}

fn set_nonblocking(fd: i32) -> Result<(), String> {
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        return Err(format!(
            "setting log pipe nonblocking: {}",
            std::io::Error::last_os_error()
        ));
    }
    Ok(())
}

fn write_read(
    pipe: &mut impl Read,
    file: &mut Option<std::fs::File>,
    buf: &mut [u8],
    secrets: &[String],
) -> bool {
    match pipe.read(buf) {
        Ok(0) => false,
        Ok(size) => {
            if let Some(file) = file.as_mut() {
                let text = String::from_utf8_lossy(&buf[..size]);
                let redacted = redact_text(&text, secrets);
                let _ = file.write_all(redacted.as_bytes());
                let _ = file.flush();
            }
            true
        }
        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => false,
        Err(_) => false,
    }
}

/// True when `pid` has a TCP listen socket on `port`. This does not connect.
pub fn listens_on(pid: u32, port: u16) -> bool {
    let Ok(inodes) = listen_inodes(port) else {
        return false;
    };
    if inodes.is_empty() {
        return false;
    }
    let Ok(entries) = fs::read_dir(format!("/proc/{pid}/fd")) else {
        return false;
    };
    for entry in entries.flatten() {
        let Ok(target) = fs::read_link(entry.path()) else {
            continue;
        };
        let text = target.to_string_lossy();
        if let Some(inode) = text
            .strip_prefix("socket:[")
            .and_then(|rest| rest.strip_suffix(']'))
        {
            if inodes.iter().any(|candidate| candidate == inode) {
                return true;
            }
        }
    }
    false
}

fn listen_inodes(port: u16) -> Result<Vec<String>, ()> {
    let mut inodes = Vec::new();
    for table in ["/proc/net/tcp", "/proc/net/tcp6"] {
        let Ok(text) = fs::read_to_string(table) else {
            continue;
        };
        for line in text.lines().skip(1) {
            let fields: Vec<&str> = line.split_whitespace().collect();
            if fields.len() < 10 || fields[3] != "0A" {
                continue;
            }
            let Some((_, port_hex)) = fields[1].rsplit_once(':') else {
                continue;
            };
            if u16::from_str_radix(port_hex, 16).ok() == Some(port) {
                inodes.push(fields[9].to_string());
            }
        }
    }
    Ok(inodes)
}
