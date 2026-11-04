//! Outer supervisor and the Fuzzer PID 1.
//!
//! The supervisor creates a PID namespace and forks. The child is PID 1. It
//! then creates the network and mount namespaces, mounts `/proc`, and brings
//! up `lo` before dropping capabilities. Reaping uses pidfd `waitid` only for
//! orphans this process has adopted. `waitpid(-1)` is not used.

use std::{
    collections::HashSet,
    fs::{self, File},
    io::Read,
    os::fd::{AsFd, AsRawFd, FromRawFd, IntoRawFd, OwnedFd, RawFd},
    path::{Path, PathBuf},
    process::exit,
    sync::atomic::{AtomicBool, Ordering},
    thread::sleep,
    time::{Duration, Instant},
};

use libafl::Error;
use nix::sched::CloneFlags;
use nix::sys::wait::{waitid, Id, WaitPidFlag, WaitStatus};

const CLEANUP_BUDGET: Duration = Duration::from_secs(2);
const TERM_GRACE: Duration = Duration::from_millis(200);
const SUPERVISOR_KILL_DEADLINE: Duration = Duration::from_secs(5);

static STOP: AtomicBool = AtomicBool::new(false);
static CHILD_SOCK: std::sync::atomic::AtomicI32 = std::sync::atomic::AtomicI32::new(-1);
static INSTALLED_WORKER: std::sync::Mutex<Option<WorkerId>> = std::sync::Mutex::new(None);

/// Remember the PID 1 worker id for metadata, IPC, and the AFL input file.
pub fn install_worker(worker: WorkerId) {
    *INSTALLED_WORKER
        .lock()
        .unwrap_or_else(|error| error.into_inner()) = Some(worker);
}

/// The installed id, or one fresh id shared by this process when no supervisor ran.
pub fn worker_id() -> Result<WorkerId, Error> {
    let mut slot = INSTALLED_WORKER
        .lock()
        .unwrap_or_else(|error| error.into_inner());
    if let Some(worker) = *slot {
        return Ok(worker);
    }
    let worker = fresh_worker_id(0)?;
    *slot = Some(worker);
    Ok(worker)
}

/// PID 1 has been asked to stop. Case loops check this between waits.
pub fn shutdown_requested() -> bool {
    STOP.load(Ordering::Relaxed)
}

/// OS random nonce plus the orchestrator job number.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct WorkerId {
    /// Non-zero value from `/dev/urandom`.
    pub nonce: u64,
    /// Job sequence chosen by the orchestrator.
    pub job: u64,
}

impl WorkerId {
    /// Service, log, and file prefix. Not a PID.
    pub fn label(self) -> String {
        format!("{:016x}-{:016x}", self.nonce, self.job)
    }
}

/// Read a fresh id. Fails when `/dev/urandom` cannot supply 8 bytes.
pub fn fresh_worker_id(job: u64) -> Result<WorkerId, Error> {
    let mut bytes = [0u8; 8];
    File::open("/dev/urandom")
        .and_then(|mut file| file.read_exact(&mut bytes))
        .map_err(|error| Error::unknown(format!("reading /dev/urandom for WorkerId: {error}")))?;
    let nonce = u64::from_ne_bytes(bytes);
    if nonce == 0 {
        return Err(Error::unknown(
            "WorkerId nonce from /dev/urandom was zero".to_string(),
        ));
    }
    Ok(WorkerId { nonce, job })
}

/// Account the fuzzer and its outputs must use. Never uid 0.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TargetIdentity {
    /// Numeric user id.
    pub uid: u32,
    /// Primary group id.
    pub gid: u32,
    /// Supplementary groups, including the primary group.
    pub groups: Vec<u32>,
    /// Login name.
    pub name: String,
}

/// Resolve the target account before `fork`.
///
/// A sudo launch uses `SUDO_UID`/`SUDO_GID` when `--run-as` is omitted.
/// A root launch without those values fails. UID 0 is rejected.
pub fn resolve_identity(run_as: Option<&str>) -> Result<TargetIdentity, Error> {
    let sudo_uid = std::env::var("SUDO_UID")
        .ok()
        .and_then(|value| value.parse().ok());
    let sudo_gid = std::env::var("SUDO_GID")
        .ok()
        .and_then(|value| value.parse().ok());
    resolve_identity_with(unsafe { libc::geteuid() }, sudo_uid, sudo_gid, run_as)
}

pub(crate) fn resolve_identity_with(
    euid: u32,
    sudo_uid: Option<u32>,
    sudo_gid: Option<u32>,
    run_as: Option<&str>,
) -> Result<TargetIdentity, Error> {
    if let Some(spec) = run_as {
        let identity = lookup_spec(spec)?;
        if euid != 0 && euid != identity.uid {
            return Err(Error::unknown(format!(
                "--run-as {} requires root; the current euid is {euid}",
                identity.name
            )));
        }
        return Ok(identity);
    }
    if euid == 0 {
        let uid = sudo_uid.ok_or_else(|| {
            Error::unknown("root startup has no SUDO_UID; pass --run-as USER".to_string())
        })?;
        let gid = sudo_gid.ok_or_else(|| {
            Error::unknown("root startup has no SUDO_GID; pass --run-as USER".to_string())
        })?;
        return lookup_uid_with_gid(uid, gid);
    }
    lookup_uid(euid)
}

struct AccountRecord {
    uid: u32,
    gid: u32,
    name: String,
}

fn lookup_spec(spec: &str) -> Result<TargetIdentity, Error> {
    if let Ok(uid) = spec.parse::<u32>() {
        return lookup_uid(uid);
    }
    if spec.as_bytes().contains(&0) {
        return Err(Error::illegal_argument(format!(
            "--run-as {spec} contains an interior nul"
        )));
    }
    let account = account_by_name(spec)?;
    identity_from_account(&account, account.gid)
}

fn lookup_uid(uid: u32) -> Result<TargetIdentity, Error> {
    let account = account_by_uid(uid)?;
    identity_from_account(&account, account.gid)
}

/// Account `uid` with an explicit primary group.
///
/// sudo's default identity passes `SUDO_GID` here. `getgrouplist` includes that
/// gid, so `setgroups` matches the group set `verify_identity` checks.
fn lookup_uid_with_gid(uid: u32, gid: u32) -> Result<TargetIdentity, Error> {
    let account = account_by_uid(uid)?;
    identity_from_account(&account, gid)
}

fn account_by_uid(uid: u32) -> Result<AccountRecord, Error> {
    if uid == 0 {
        return Err(Error::illegal_argument(
            "--run-as rejects uid 0".to_string(),
        ));
    }
    let mut pwd = unsafe { std::mem::zeroed::<libc::passwd>() };
    let mut buf = vec![0u8; 4096];
    let mut result = std::ptr::null_mut();
    loop {
        let rc = unsafe {
            libc::getpwuid_r(
                uid,
                &mut pwd,
                buf.as_mut_ptr().cast(),
                buf.len(),
                &mut result,
            )
        };
        if rc == libc::ERANGE {
            buf.resize(buf.len().saturating_mul(2).max(8192), 0);
            continue;
        }
        if rc != 0 {
            return Err(Error::unknown(format!(
                "getpwuid_r({uid}) failed: {}",
                std::io::Error::from_raw_os_error(rc)
            )));
        }
        break;
    }
    if result.is_null() {
        return Err(Error::unknown(format!(
            "uid {uid} does not map to an existing account"
        )));
    }
    account_from_passwd(&pwd)
}

fn account_by_name(spec: &str) -> Result<AccountRecord, Error> {
    let name = std::ffi::CString::new(spec).map_err(|_| {
        Error::illegal_argument(format!("--run-as {spec} contains an interior nul"))
    })?;
    let mut pwd = unsafe { std::mem::zeroed::<libc::passwd>() };
    let mut buf = vec![0u8; 4096];
    let mut result = std::ptr::null_mut();
    loop {
        let rc = unsafe {
            libc::getpwnam_r(
                name.as_ptr(),
                &mut pwd,
                buf.as_mut_ptr().cast(),
                buf.len(),
                &mut result,
            )
        };
        if rc == libc::ERANGE {
            buf.resize(buf.len().saturating_mul(2).max(8192), 0);
            continue;
        }
        if rc != 0 {
            return Err(Error::unknown(format!(
                "getpwnam_r({spec}) failed: {}",
                std::io::Error::from_raw_os_error(rc)
            )));
        }
        break;
    }
    if result.is_null() {
        return Err(Error::unknown(format!(
            "--run-as {spec} does not name an existing account"
        )));
    }
    account_from_passwd(&pwd)
}

fn account_from_passwd(pwd: &libc::passwd) -> Result<AccountRecord, Error> {
    if pwd.pw_uid == 0 {
        return Err(Error::illegal_argument(
            "--run-as rejects uid 0".to_string(),
        ));
    }
    let name = unsafe { std::ffi::CStr::from_ptr(pwd.pw_name) }
        .to_string_lossy()
        .into_owned();
    Ok(AccountRecord {
        uid: pwd.pw_uid,
        gid: pwd.pw_gid,
        name,
    })
}

fn identity_from_account(account: &AccountRecord, gid: u32) -> Result<TargetIdentity, Error> {
    let c_name = std::ffi::CString::new(account.name.clone()).map_err(|_| {
        Error::unknown(format!(
            "account {} has an interior nul in its name",
            account.name
        ))
    })?;
    let mut size = 64i32;
    let mut groups = vec![0u32; size as usize];
    let rc = unsafe { libc::getgrouplist(c_name.as_ptr(), gid, groups.as_mut_ptr(), &mut size) };
    if rc < 0 {
        groups.resize(size.max(1) as usize, 0);
        let rc =
            unsafe { libc::getgrouplist(c_name.as_ptr(), gid, groups.as_mut_ptr(), &mut size) };
        if rc < 0 {
            return Err(Error::unknown(format!(
                "getgrouplist failed for {}",
                account.name
            )));
        }
    }
    groups.truncate(size.max(0) as usize);
    if !groups.contains(&gid) {
        groups.push(gid);
    }
    Ok(TargetIdentity {
        uid: account.uid,
        gid,
        groups,
        name: account.name.clone(),
    })
}

/// True when `uid` can create entries in an existing directory. Does not run as that user.
pub fn user_can_write(path: &Path, identity: &TargetIdentity) -> Result<bool, Error> {
    use std::os::unix::fs::MetadataExt;
    let meta = std::fs::metadata(path)
        .map_err(|error| Error::unknown(format!("stat {}: {error}", path.display())))?;
    if !meta.is_dir() {
        return Ok(false);
    }
    let mode = meta.mode();
    if meta.uid() == identity.uid && mode & 0o200 != 0 {
        return Ok(true);
    }
    if identity.groups.contains(&meta.gid()) && mode & 0o020 != 0 {
        return Ok(true);
    }
    Ok(mode & 0o002 != 0)
}

/// Fail before fork when an existing output path is not writable by the target account.
pub fn ensure_output_accessible(path: &Path, identity: &TargetIdentity) -> Result<(), Error> {
    let check = if path.exists() {
        path.to_path_buf()
    } else {
        path.parent()
            .filter(|parent| !parent.as_os_str().is_empty())
            .unwrap_or_else(|| Path::new("."))
            .to_path_buf()
    };
    if !check.exists() {
        return Err(Error::unknown(format!(
            "output parent {} does not exist; it is not created as root",
            check.display()
        )));
    }
    if !user_can_write(&check, identity)? {
        return Err(Error::unknown(format!(
            "output {} is not writable by uid {} ({})",
            check.display(),
            identity.uid,
            identity.name
        )));
    }
    Ok(())
}

/// Which side of the PID-namespace fork this process is.
pub enum Role {
    /// Outer supervisor. The PID 1 child has already exited.
    Parent(i32),
    /// Fuzzer PID 1. Namespaces are set up and the process runs as `identity`.
    Child(WorkerId),
}

/// Create the PID namespace, fork, and finish the startup handshake.
///
/// The caller must still be single-threaded. After this returns `Parent`, the
/// supervisor has no business threads of its own.
pub fn supervise(job: u64, capture: bool, identity: &TargetIdentity) -> Result<Role, Error> {
    let worker = fresh_worker_id(job)?;
    let mut pair = [0; 2];
    let rc = unsafe {
        libc::socketpair(
            libc::AF_UNIX,
            libc::SOCK_STREAM | libc::SOCK_CLOEXEC,
            0,
            pair.as_mut_ptr(),
        )
    };
    if rc != 0 {
        return Err(Error::unknown(format!(
            "supervisor socketpair failed: {}",
            std::io::Error::last_os_error()
        )));
    }
    let parent_fd = unsafe { OwnedFd::from_raw_fd(pair[0]) };
    let child_fd = unsafe { OwnedFd::from_raw_fd(pair[1]) };
    let parent_pidfd = pidfd_open(std::process::id() as i32)?;
    match nix::sched::unshare(CloneFlags::CLONE_NEWPID) {
        Ok(()) => {}
        Err(error) => {
            return Err(Error::unknown(format!(
                "unshare(CLONE_NEWPID) failed before any case as uid {}: {error}. CAP_SYS_ADMIN in the current user namespace is required; this process has no effective capabilities and a user namespace was not used",
                unsafe { libc::geteuid() }
            )));
        }
    }
    let pid = unsafe { libc::fork() };
    if pid < 0 {
        return Err(Error::unknown(format!(
            "fork after CLONE_NEWPID failed: {}",
            std::io::Error::last_os_error()
        )));
    }
    if pid > 0 {
        drop(child_fd);
        let child_pidfd = pidfd_open(pid)?;
        set_nonblocking(parent_fd.as_raw_fd())?;
        install_supervisor_signals()?;
        let ready = wait_child_ready(&parent_fd, &child_pidfd, Duration::from_secs(30));
        if let Err(error) = ready {
            let _ = pidfd_send(child_pidfd.as_raw_fd(), libc::SIGKILL);
            let _ = wait_pidfd(&child_pidfd, true);
            return Err(error);
        }
        if let Err(error) = drop_to_user(identity) {
            let _ = pidfd_send(child_pidfd.as_raw_fd(), libc::SIGKILL);
            let _ = wait_pidfd(&child_pidfd, true);
            return Err(error);
        }
        return Ok(Role::Parent(supervise_parent(parent_fd, child_pidfd)?));
    }
    drop(parent_fd);
    if let Err(error) = child_setup(parent_pidfd, &child_fd, capture, identity) {
        let _ = write_line(&child_fd, &format!("child-fail {error}"));
        exit(1);
    }
    write_line(&child_fd, "child-ready")?;
    CHILD_SOCK.store(child_fd.into_raw_fd(), Ordering::SeqCst);
    install_pid1_signals()?;
    Ok(Role::Child(worker))
}

fn wait_child_ready(sock: &OwnedFd, child: &OwnedFd, budget: Duration) -> Result<(), Error> {
    let deadline = Instant::now() + budget;
    let mut pending = String::new();
    while Instant::now() < deadline {
        if STOP.swap(false, Ordering::Relaxed) {
            let _ = pidfd_send(child.as_raw_fd(), libc::SIGKILL);
            let _ = wait_pidfd(child, true);
            return Err(Error::unknown(
                "startup interrupted by SIGINT or SIGTERM before the child dropped privileges"
                    .to_string(),
            ));
        }
        if let Some(line) = poll_line(sock, &mut pending, 200)? {
            if line == "child-ready" {
                return Ok(());
            }
            if let Some(error) = line.strip_prefix("child-fail ") {
                return Err(Error::unknown(format!(
                    "PID 1 failed during startup: {error}"
                )));
            }
            return Err(Error::unknown(format!(
                "PID 1 startup handshake returned {line:?}"
            )));
        }
        if wait_pidfd(child, false)?.is_some() {
            return Err(Error::unknown(
                "PID 1 exited before reporting that privileges were dropped".to_string(),
            ));
        }
    }
    Err(Error::unknown(
        "PID 1 startup handshake exceeded 30s".to_string(),
    ))
}

fn supervise_parent(sock: OwnedFd, child: OwnedFd) -> Result<i32, Error> {
    let mut pending = String::new();
    let mut kill_at: Option<Instant> = None;
    loop {
        if STOP.swap(false, Ordering::Relaxed) && kill_at.is_none() {
            let _ = pidfd_send(child.as_raw_fd(), libc::SIGTERM);
            kill_at = Some(Instant::now() + SUPERVISOR_KILL_DEADLINE);
        }
        if let Some(deadline) = kill_at {
            if Instant::now() >= deadline {
                let _ = pidfd_send(child.as_raw_fd(), libc::SIGKILL);
            }
        }
        if let Some(line) = poll_line(&sock, &mut pending, 50)? {
            if let Some(code) = line.strip_prefix("child-exit ") {
                let code = code.trim().parse::<i32>().unwrap_or(1);
                let _ = wait_pidfd(&child, true);
                return Ok(code);
            }
            if let Some(error) = line.strip_prefix("child-fail ") {
                let _ = pidfd_send(child.as_raw_fd(), libc::SIGKILL);
                let _ = wait_pidfd(&child, true);
                return Err(Error::unknown(format!("PID 1 reported: {error}")));
            }
        }
        if let Some(status) = wait_pidfd(&child, false)? {
            return Ok(status_code(status));
        }
    }
}

fn child_setup(
    parent_pidfd: OwnedFd,
    sock: &OwnedFd,
    capture: bool,
    identity: &TargetIdentity,
) -> Result<(), Error> {
    unsafe {
        libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL);
    }
    if pidfd_exited(parent_pidfd.as_raw_fd())? {
        exit(1);
    }
    nix::sched::unshare(CloneFlags::CLONE_NEWNET | CloneFlags::CLONE_NEWNS).map_err(|error| {
        Error::unknown(format!(
            "PID 1 unshare(CLONE_NEWNET|CLONE_NEWNS) failed: {error}"
        ))
    })?;
    nix::mount::mount(
        Some("none"),
        "/",
        None::<&str>,
        nix::mount::MsFlags::MS_REC | nix::mount::MsFlags::MS_PRIVATE,
        None::<&str>,
    )
    .map_err(|error| Error::unknown(format!("making / private failed: {error}")))?;
    nix::mount::mount(
        Some("proc"),
        "/proc",
        Some("proc"),
        nix::mount::MsFlags::MS_NOSUID
            | nix::mount::MsFlags::MS_NODEV
            | nix::mount::MsFlags::MS_NOEXEC,
        None::<&str>,
    )
    .map_err(|error| Error::unknown(format!("mounting PID-namespace /proc failed: {error}")))?;
    bring_up_loopback()?;
    mount_private_ipc(identity)?;
    if capture {
        crate::capture::prepare_live_handle()?;
    }
    drop_to_user(identity)?;
    if pidfd_exited(parent_pidfd.as_raw_fd())? {
        return Err(Error::unknown(
            "parent exited before PID 1 finished dropping privileges".to_string(),
        ));
    }
    let _ = sock;
    Ok(())
}

fn mount_private_ipc(identity: &TargetIdentity) -> Result<(), Error> {
    mount_tmpfs(
        Path::new("/dev/shm"),
        &format!("uid={},gid={},mode=1777", identity.uid, identity.gid),
    )?;
    mount_tmpfs(
        Path::new("/tmp/iceoryx2"),
        &format!("uid={},gid={},mode=0700", identity.uid, identity.gid),
    )?;
    Ok(())
}

fn mount_tmpfs(path: &Path, options: &str) -> Result<(), Error> {
    if !path.exists() {
        std::fs::create_dir_all(path).map_err(|error| {
            Error::unknown(format!(
                "creating missing mountpoint {} failed: {error}",
                path.display()
            ))
        })?;
    }
    nix::mount::mount(
        Some("tmpfs"),
        path,
        Some("tmpfs"),
        nix::mount::MsFlags::MS_NOSUID | nix::mount::MsFlags::MS_NODEV,
        Some(options),
    )
    .map_err(|error| {
        Error::unknown(format!(
            "mounting private tmpfs on {} failed: {error}",
            path.display()
        ))
    })
}

fn drop_to_user(identity: &TargetIdentity) -> Result<(), Error> {
    if identity.uid == 0 {
        return Err(Error::illegal_argument(
            "refusing to keep uid 0".to_string(),
        ));
    }
    let groups: Vec<libc::gid_t> = identity.groups.clone();
    let rc = unsafe { libc::setgroups(groups.len(), groups.as_ptr()) };
    if rc != 0 {
        return Err(Error::unknown(format!(
            "setgroups for {} failed: {}",
            identity.name,
            std::io::Error::last_os_error()
        )));
    }
    let rc = unsafe { libc::setresgid(identity.gid, identity.gid, identity.gid) };
    if rc != 0 {
        return Err(Error::unknown(format!(
            "setresgid({}, {}) failed: {}",
            identity.gid,
            identity.name,
            std::io::Error::last_os_error()
        )));
    }
    let rc = unsafe { libc::setresuid(identity.uid, identity.uid, identity.uid) };
    if rc != 0 {
        return Err(Error::unknown(format!(
            "setresuid({}, {}) failed: {}",
            identity.uid,
            identity.name,
            std::io::Error::last_os_error()
        )));
    }
    drop_capabilities(true)?;
    verify_identity(identity)?;
    let reset = unsafe { libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL) };
    if reset != 0 {
        return Err(Error::unknown(format!(
            "resetting PDEATHSIG after setuid failed: {}",
            std::io::Error::last_os_error()
        )));
    }
    Ok(())
}

fn verify_identity(identity: &TargetIdentity) -> Result<(), Error> {
    let uid = unsafe { libc::getuid() };
    let euid = unsafe { libc::geteuid() };
    let gid = unsafe { libc::getgid() };
    let egid = unsafe { libc::getegid() };
    if uid != identity.uid || euid != identity.uid || gid != identity.gid || egid != identity.gid {
        return Err(Error::unknown(format!(
            "privilege drop left uid={uid}/{euid} gid={gid}/{egid}, expected {}/{}",
            identity.uid, identity.gid
        )));
    }
    let mut current = vec![0u32; identity.groups.len().max(1) + 8];
    let count = unsafe { libc::getgroups(current.len() as i32, current.as_mut_ptr()) };
    if count < 0 {
        return Err(Error::unknown(format!(
            "getgroups after setgroups failed: {}",
            std::io::Error::last_os_error()
        )));
    }
    current.truncate(count as usize);
    let mut expected = identity.groups.clone();
    current.sort_unstable();
    expected.sort_unstable();
    current.dedup();
    expected.dedup();
    if current != expected {
        return Err(Error::unknown(format!(
            "supplementary groups are {current:?}, expected {expected:?}"
        )));
    }
    let status = std::fs::read_to_string("/proc/self/status").map_err(|error| {
        Error::unknown(format!("reading /proc/self/status after setuid: {error}"))
    })?;
    if let Err(error) = credential_status_cleared(&status) {
        return Err(Error::unknown(error));
    }
    Ok(())
}

/// `NoNewPrivs` is 1 and effective, permitted, inheritable, and ambient capabilities are empty.
fn credential_status_cleared(status: &str) -> Result<(), String> {
    let nnp = status_field(status, "NoNewPrivs:");
    if nnp != Some("1") {
        return Err(format!(
            "expected NoNewPrivs 1 after drop, status says nnp={nnp:?}"
        ));
    }
    for name in ["CapEff:", "CapPrm:", "CapInh:", "CapAmb:"] {
        let value = status_field(status, name);
        if !value.is_some_and(cap_word_is_empty) {
            return Err(format!(
                "expected empty {name} after drop, status says {value:?}"
            ));
        }
    }
    Ok(())
}

fn status_field<'a>(status: &'a str, prefix: &str) -> Option<&'a str> {
    status
        .lines()
        .find_map(|line| line.strip_prefix(prefix))
        .map(str::trim)
}

fn cap_word_is_empty(value: &str) -> bool {
    !value.is_empty() && value.bytes().all(|byte| byte == b'0')
}

fn set_nonblocking(fd: RawFd) -> Result<(), Error> {
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 {
        return Err(Error::unknown(format!(
            "F_GETFL failed: {}",
            std::io::Error::last_os_error()
        )));
    }
    let rc = unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) };
    if rc < 0 {
        return Err(Error::unknown(format!(
            "F_SETFL failed: {}",
            std::io::Error::last_os_error()
        )));
    }
    Ok(())
}

/// Clear effective, permitted, inheritable, and ambient capabilities.
/// `no_new_privs` is set when `lock` is true.
pub fn drop_capabilities(lock: bool) -> Result<(), Error> {
    let clear = unsafe {
        libc::prctl(
            libc::PR_CAP_AMBIENT,
            libc::PR_CAP_AMBIENT_CLEAR_ALL,
            0,
            0,
            0,
        )
    };
    if clear != 0 {
        let error = std::io::Error::last_os_error();
        if error.raw_os_error() != Some(libc::EINVAL) && error.raw_os_error() != Some(libc::EPERM) {
            return Err(Error::unknown(format!(
                "clearing ambient capabilities failed: {error}"
            )));
        }
    }
    #[repr(C)]
    struct CapHeader {
        version: u32,
        pid: i32,
    }
    #[repr(C)]
    struct CapData {
        effective: u32,
        permitted: u32,
        inheritable: u32,
    }
    let header = CapHeader {
        version: 0x2008_0522,
        pid: 0,
    };
    let data = [
        CapData {
            effective: 0,
            permitted: 0,
            inheritable: 0,
        },
        CapData {
            effective: 0,
            permitted: 0,
            inheritable: 0,
        },
    ];
    let rc = unsafe { libc::syscall(libc::SYS_capset, &header, data.as_ptr()) };
    if rc != 0 {
        return Err(Error::unknown(format!(
            "capset clearing all capabilities failed: {}",
            std::io::Error::last_os_error()
        )));
    }
    if lock {
        let nnp = unsafe { libc::prctl(libc::PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) };
        if nnp != 0 {
            return Err(Error::unknown(format!(
                "PR_SET_NO_NEW_PRIVS failed: {}",
                std::io::Error::last_os_error()
            )));
        }
    }
    Ok(())
}

fn install_supervisor_signals() -> Result<(), Error> {
    install_signals(&[libc::SIGINT, libc::SIGTERM])
}

fn install_pid1_signals() -> Result<(), Error> {
    // Keep SIGCHLD at its default disposition. Asynchronous SIGCHLD delivery
    // interrupted libafl's forkserver pipe and iceoryx2's IPC polling, which
    // turned ordinary worker exits into spurious EINTR failures. PID 1 reaps
    // adopted children explicitly after each case.
    install_signals(&[libc::SIGINT, libc::SIGTERM])
}

fn install_signals(signals: &[i32]) -> Result<(), Error> {
    unsafe {
        let mut action: libc::sigaction = std::mem::zeroed();
        action.sa_sigaction = signal_handler as *const () as usize;
        action.sa_flags = libc::SA_SIGINFO;
        for signal in signals {
            if libc::sigaction(*signal, &action, std::ptr::null_mut()) != 0 {
                return Err(Error::unknown(format!(
                    "installing PID 1 handler for signal {signal} failed: {}",
                    std::io::Error::last_os_error()
                )));
            }
        }
    }
    Ok(())
}

unsafe extern "C" fn signal_handler(
    _signal: libc::c_int,
    _: *mut libc::siginfo_t,
    _: *mut libc::c_void,
) {
    STOP.store(true, Ordering::Relaxed);
}

/// One process row from a `/proc` tree.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProcInfo {
    /// Thread group id.
    pub pid: i32,
    /// Parent pid.
    pub ppid: i32,
    /// `starttime` field from `stat`.
    pub starttime: u64,
    /// State character, such as `R` or `Z`.
    pub state: char,
}

/// Read TGID rows. Non-numeric directory names are skipped.
pub fn list_procs(proc_root: &Path) -> Vec<ProcInfo> {
    let mut rows = Vec::new();
    let Ok(entries) = fs::read_dir(proc_root) else {
        return rows;
    };
    for entry in entries.flatten() {
        let Ok(pid) = entry.file_name().to_string_lossy().parse::<i32>() else {
            continue;
        };
        let status = fs::read_to_string(entry.path().join("status")).unwrap_or_default();
        let stat = fs::read_to_string(entry.path().join("stat")).unwrap_or_default();
        let ppid = status
            .lines()
            .find_map(|line| line.strip_prefix("PPid:"))
            .and_then(|value| value.trim().parse().ok())
            .unwrap_or(0);
        let state = status
            .lines()
            .find_map(|line| line.strip_prefix("State:"))
            .and_then(|value| value.trim().chars().next())
            .unwrap_or('?');
        let starttime = stat_starttime(&stat);
        rows.push(ProcInfo {
            pid,
            ppid,
            starttime,
            state,
        });
    }
    rows
}

fn stat_starttime(stat: &str) -> u64 {
    let Some(end) = stat.rfind(')') else {
        return 0;
    };
    stat[end + 1..]
        .split_whitespace()
        .nth(19)
        .and_then(|field| field.parse().ok())
        .unwrap_or(0)
}

/// Processes that may be signalled. Residents are excluded.
pub fn signal_targets(procs: &[ProcInfo], residents: &HashSet<i32>) -> Vec<ProcInfo> {
    procs
        .iter()
        .filter(|proc| !residents.contains(&proc.pid))
        .cloned()
        .collect()
}

/// Adopted non-resident children of `self_pid`. Only these may be `waitid`'d.
pub fn adopted(procs: &[ProcInfo], residents: &HashSet<i32>, self_pid: i32) -> Vec<ProcInfo> {
    procs
        .iter()
        .filter(|proc| proc.ppid == self_pid && !residents.contains(&proc.pid))
        .cloned()
        .collect()
}

/// Signal every non-resident TGID, then `waitid` only adopted children.
///
/// Runs only when this process is PID 1. Otherwise it refuses, so a unit test
/// cannot sweep the host process table.
pub fn cleanup_namespace(residents: &HashSet<i32>) -> Result<(), Error> {
    if std::process::id() != 1 {
        return Err(Error::unknown(
            "PID 1 cleanup refused because this process is not PID 1".to_string(),
        ));
    }
    let deadline = Instant::now() + CLEANUP_BUDGET;
    let self_pid = std::process::id() as i32;
    loop {
        let procs = list_procs(Path::new("/proc"));
        let targets = signal_targets(&procs, residents);
        let live: Vec<_> = targets.iter().filter(|proc| proc.state != 'Z').collect();
        if live.is_empty()
            && adopted(&procs, residents, self_pid)
                .iter()
                .all(|proc| proc.state != 'Z')
            && targets
                .iter()
                .all(|proc| proc.state == 'Z' || !pid_alive(proc.pid))
        {
            reap_adopted(&adopted(&procs, residents, self_pid))?;
            let again = list_procs(Path::new("/proc"));
            if signal_targets(&again, residents).is_empty() {
                return Ok(());
            }
        }
        if Instant::now() >= deadline {
            return Err(Error::unknown(
                "PID 1 cleanup exceeded 2s; the instance must stop".to_string(),
            ));
        }
        for proc in &targets {
            let _ = kill_pid(proc.pid, libc::SIGTERM);
        }
        sleep(TERM_GRACE.min(deadline.saturating_duration_since(Instant::now())));
        for proc in signal_targets(&list_procs(Path::new("/proc")), residents) {
            let _ = kill_pid(proc.pid, libc::SIGKILL);
        }
        reap_adopted(&adopted(
            &list_procs(Path::new("/proc")),
            residents,
            self_pid,
        ))?;
    }
}

fn pid_alive(pid: i32) -> bool {
    Path::new(&format!("/proc/{pid}")).exists()
}

fn kill_pid(pid: i32, signal: i32) -> Result<(), Error> {
    let fd = pidfd_open(pid)?;
    pidfd_send(fd.as_raw_fd(), signal)
}

fn reap_adopted(procs: &[ProcInfo]) -> Result<(), Error> {
    for proc in procs {
        let Ok(fd) = pidfd_open(proc.pid) else {
            continue;
        };
        let _ = wait_pidfd(&fd, false)?;
    }
    Ok(())
}

fn pidfd_open(pid: i32) -> Result<OwnedFd, Error> {
    let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid, libc::PIDFD_NONBLOCK) };
    if fd < 0 {
        return Err(Error::unknown(format!(
            "pidfd_open({pid}) failed: {}",
            std::io::Error::last_os_error()
        )));
    }
    Ok(unsafe { OwnedFd::from_raw_fd(fd as RawFd) })
}

fn pidfd_send(fd: RawFd, signal: i32) -> Result<(), Error> {
    let rc = unsafe {
        libc::syscall(
            libc::SYS_pidfd_send_signal,
            fd,
            signal,
            std::ptr::null::<()>(),
            0,
        )
    };
    if rc != 0 {
        return Err(Error::unknown(format!(
            "pidfd_send_signal failed: {}",
            std::io::Error::last_os_error()
        )));
    }
    Ok(())
}

/// `pidfd_send_signal` result. `AlreadyGone` is an exit race, not a new failure.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SignalDelivery {
    /// The signal was queued.
    Delivered,
    /// The pidfd target had already exited.
    AlreadyGone,
}

/// Open a nonblocking pidfd.
pub(crate) fn open_pidfd(pid: i32) -> Result<OwnedFd, Error> {
    pidfd_open(pid)
}

/// Signal through a pidfd. `ESRCH` means the process is already gone.
pub(crate) fn signal_pidfd(fd: RawFd, signal: i32) -> Result<SignalDelivery, Error> {
    let rc = unsafe {
        libc::syscall(
            libc::SYS_pidfd_send_signal,
            fd,
            signal,
            std::ptr::null::<()>(),
            0,
        )
    };
    if rc == 0 {
        return Ok(SignalDelivery::Delivered);
    }
    let error = std::io::Error::last_os_error();
    if error.raw_os_error() == Some(libc::ESRCH) {
        return Ok(SignalDelivery::AlreadyGone);
    }
    Err(Error::unknown(format!("pidfd_send_signal failed: {error}")))
}

/// `true` when the pidfd is readable, which means the process has exited.
pub(crate) fn pidfd_has_exited(fd: RawFd) -> Result<bool, Error> {
    pidfd_exited(fd)
}

/// Parent and starttime of a live pid. `None` when `/proc` is already gone.
pub(crate) fn process_birth(pid: i32) -> Option<(i32, u64)> {
    let status = fs::read_to_string(format!("/proc/{pid}/status")).ok()?;
    let stat = fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let ppid = status
        .lines()
        .find_map(|line| line.strip_prefix("PPid:"))
        .and_then(|value| value.trim().parse().ok())?;
    Some((ppid, stat_starttime(&stat)))
}

fn pidfd_exited(fd: RawFd) -> Result<bool, Error> {
    let mut pollfd = libc::pollfd {
        fd,
        events: libc::POLLIN,
        revents: 0,
    };
    let rc = unsafe { libc::poll(&mut pollfd, 1, 0) };
    if rc < 0 {
        return Err(Error::unknown(format!(
            "poll parent pidfd failed: {}",
            std::io::Error::last_os_error()
        )));
    }
    Ok(rc > 0)
}

fn wait_pidfd(fd: &OwnedFd, block: bool) -> Result<Option<WaitStatus>, Error> {
    let mut flags = WaitPidFlag::WEXITED;
    if !block {
        flags |= WaitPidFlag::WNOHANG;
    }
    match waitid(Id::PIDFd(fd.as_fd()), flags) {
        Ok(WaitStatus::StillAlive) => Ok(None),
        Ok(status) => Ok(Some(status)),
        Err(nix::errno::Errno::ECHILD) => Ok(None),
        Err(error) => Err(Error::unknown(format!("waitid(pidfd) failed: {error}"))),
    }
}

fn status_code(status: WaitStatus) -> i32 {
    match status {
        WaitStatus::Exited(_, code) => code,
        WaitStatus::Signaled(_, signal, _) => 128 + signal as i32,
        _ => 1,
    }
}

fn bring_up_loopback() -> Result<(), Error> {
    let socket = unsafe { libc::socket(libc::AF_INET, libc::SOCK_DGRAM | libc::SOCK_CLOEXEC, 0) };
    if socket < 0 {
        return Err(Error::unknown(format!(
            "opening a datagram socket for lo failed: {}",
            std::io::Error::last_os_error()
        )));
    }
    let mut req = libc::ifreq {
        ifr_name: [0; libc::IFNAMSIZ],
        ifr_ifru: libc::__c_anonymous_ifr_ifru { ifru_flags: 0 },
    };
    let name = b"lo";
    for (index, byte) in name.iter().enumerate() {
        req.ifr_name[index] = *byte as libc::c_char;
    }
    let get = unsafe { libc::ioctl(socket, libc::SIOCGIFFLAGS, &mut req) };
    if get != 0 {
        unsafe { libc::close(socket) };
        return Err(Error::unknown(format!(
            "SIOCGIFFLAGS lo failed: {}",
            std::io::Error::last_os_error()
        )));
    }
    unsafe {
        req.ifr_ifru.ifru_flags |= libc::IFF_UP as libc::c_short;
    }
    let set = unsafe { libc::ioctl(socket, libc::SIOCSIFFLAGS, &req) };
    unsafe { libc::close(socket) };
    if set != 0 {
        return Err(Error::unknown(format!(
            "SIOCSIFFLAGS lo failed: {}",
            std::io::Error::last_os_error()
        )));
    }
    Ok(())
}

fn write_line(fd: &OwnedFd, text: &str) -> Result<(), Error> {
    let mut line = text.to_string();
    line.push('\n');
    let bytes = line.as_bytes();
    let mut offset = 0;
    while offset < bytes.len() {
        let wrote = unsafe {
            libc::write(
                fd.as_raw_fd(),
                bytes[offset..].as_ptr().cast(),
                bytes.len() - offset,
            )
        };
        if wrote < 0 {
            return Err(Error::unknown(format!(
                "supervisor write failed: {}",
                std::io::Error::last_os_error()
            )));
        }
        offset += wrote as usize;
    }
    Ok(())
}

#[allow(dead_code)]
fn read_line(fd: &OwnedFd, timeout: Duration) -> Result<String, Error> {
    let deadline = Instant::now() + timeout;
    let mut buf = Vec::new();
    let mut byte = [0u8; 1];
    while Instant::now() < deadline {
        let n = unsafe { libc::read(fd.as_raw_fd(), byte.as_mut_ptr().cast(), 1) };
        if n == 0 {
            break;
        }
        if n < 0 {
            let error = std::io::Error::last_os_error();
            if error.kind() == std::io::ErrorKind::WouldBlock {
                sleep(Duration::from_millis(5));
                continue;
            }
            return Err(Error::unknown(format!("supervisor read failed: {error}")));
        }
        if byte[0] == b'\n' {
            break;
        }
        buf.push(byte[0]);
    }
    String::from_utf8(buf).map_err(|error| Error::unknown(format!("supervisor message: {error}")))
}

/// Private AFL input path for this worker. Avoids `.cur_input_<pid>`.
pub fn private_input_path(dir: &Path, worker: WorkerId) -> PathBuf {
    dir.join(format!(".cur_input_{}", worker.label()))
}

/// Tell the outer supervisor the job's exit code, then the process can exit.
pub fn report_child_exit(code: i32) {
    let fd = CHILD_SOCK.swap(-1, Ordering::SeqCst);
    if fd < 0 {
        return;
    }
    let line = format!("child-exit {code}\n");
    let bytes = line.as_bytes();
    unsafe {
        libc::write(fd, bytes.as_ptr().cast(), bytes.len());
        libc::close(fd);
    }
}

fn poll_line(fd: &OwnedFd, pending: &mut String, timeout_ms: i32) -> Result<Option<String>, Error> {
    let mut pollfd = libc::pollfd {
        fd: fd.as_raw_fd(),
        events: libc::POLLIN,
        revents: 0,
    };
    let rc = unsafe { libc::poll(&mut pollfd, 1, timeout_ms) };
    if rc < 0 {
        return Err(Error::unknown(format!(
            "supervisor poll failed: {}",
            std::io::Error::last_os_error()
        )));
    }
    if rc == 0 {
        return Ok(None);
    }
    let mut byte = [0u8; 1];
    loop {
        let n = unsafe { libc::read(fd.as_raw_fd(), byte.as_mut_ptr().cast(), 1) };
        if n == 0 {
            break;
        }
        if n < 0 {
            let error = std::io::Error::last_os_error();
            if error.kind() == std::io::ErrorKind::WouldBlock {
                break;
            }
            return Err(Error::unknown(format!("supervisor read failed: {error}")));
        }
        if byte[0] == b'\n' {
            let line = std::mem::take(pending);
            return Ok(Some(line));
        }
        pending.push(byte[0] as char);
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn run_as_rejects_root_and_missing_sudo_identity() {
        let err = resolve_identity_with(0, None, None, None).unwrap_err();
        assert!(format!("{err:?}").contains("SUDO_UID"), "{err:?}");
        let err = resolve_identity_with(0, Some(0), Some(0), None).unwrap_err();
        assert!(format!("{err:?}").contains("uid 0"), "{err:?}");
        let err = resolve_identity_with(0, None, None, Some("0")).unwrap_err();
        assert!(format!("{err:?}").contains("uid 0"), "{err:?}");
        let err = resolve_identity_with(1005, None, None, Some("0")).unwrap_err();
        assert!(format!("{err:?}").contains("uid 0"), "{err:?}");
    }

    #[test]
    fn run_as_uid_resolves_without_staying_root() {
        let self_uid = unsafe { libc::geteuid() };
        if self_uid == 0 {
            return;
        }
        let identity = resolve_identity_with(self_uid, None, None, None).unwrap();
        assert_eq!(identity.uid, self_uid);
        assert_ne!(identity.uid, 0);
        assert!(!identity.name.is_empty());
        assert!(identity.groups.contains(&identity.gid));
        let by_uid = resolve_identity_with(0, None, None, Some(&self_uid.to_string())).unwrap();
        assert_eq!(by_uid.uid, identity.uid);
        assert_eq!(by_uid.name, identity.name);
        let by_name = resolve_identity_with(0, None, None, Some(&identity.name)).unwrap();
        assert_eq!(by_name.uid, identity.uid);
        let err = resolve_identity_with(self_uid, None, None, Some("root")).unwrap_err();
        assert!(format!("{err:?}").contains("uid 0"), "{err:?}");
        if lookup_uid(1).is_ok() {
            let err = resolve_identity_with(self_uid, None, None, Some("1")).unwrap_err();
            assert!(format!("{err:?}").contains("requires root"), "{err:?}");
        }
    }

    #[test]
    fn sudo_gid_is_the_primary_group_and_run_as_keeps_the_account_group() {
        let self_uid = unsafe { libc::geteuid() };
        if self_uid == 0 {
            return;
        }
        let account = lookup_uid(self_uid).unwrap();
        let err = resolve_identity_with(0, Some(self_uid), None, None).unwrap_err();
        assert!(format!("{err:?}").contains("SUDO_GID"), "{err:?}");
        let mut sudo_gid = account.gid.wrapping_add(97);
        if sudo_gid == 0 || sudo_gid == account.gid {
            sudo_gid = account.gid.wrapping_add(98);
        }
        let identity = resolve_identity_with(0, Some(self_uid), Some(sudo_gid), None).unwrap();
        assert_eq!(identity.uid, self_uid);
        assert_eq!(identity.gid, sudo_gid);
        assert_ne!(identity.gid, account.gid);
        assert!(identity.groups.contains(&sudo_gid));
        let explicit = resolve_identity_with(
            0,
            Some(self_uid),
            Some(sudo_gid),
            Some(&self_uid.to_string()),
        )
        .unwrap();
        assert_eq!(explicit.uid, account.uid);
        assert_eq!(explicit.gid, account.gid);
        assert!(explicit.groups.contains(&account.gid));
    }

    #[test]
    fn dropped_status_requires_empty_caps_and_no_new_privs() {
        let cleared = "\
Name:\tmcn-fuzz
CapInh:\t0000000000000000
CapPrm:\t0000000000000000
CapEff:\t0000000000000000
CapBnd:\t000001ffffffffff
CapAmb:\t0000000000000000
NoNewPrivs:\t1
";
        credential_status_cleared(cleared).unwrap();
        for (label, broken) in [
            (
                "CapPrm",
                cleared.replace("CapPrm:\t0000000000000000", "CapPrm:\t0000000000000004"),
            ),
            (
                "CapInh",
                cleared.replace("CapInh:\t0000000000000000", "CapInh:\t0000000000000001"),
            ),
            (
                "CapAmb",
                cleared.replace("CapAmb:\t0000000000000000", "CapAmb:\t00000000000000a0"),
            ),
            (
                "CapEff",
                cleared.replace("CapEff:\t0000000000000000", "CapEff:\t0000000000000008"),
            ),
            (
                "NoNewPrivs",
                cleared.replace("NoNewPrivs:\t1", "NoNewPrivs:\t0"),
            ),
        ] {
            let err = credential_status_cleared(&broken).unwrap_err();
            assert!(err.contains(label), "{label}: {err}");
        }
    }

    #[test]
    fn installed_worker_is_the_one_later_callers_see() {
        let worker = fresh_worker_id(9).unwrap();
        install_worker(worker);
        let again = worker_id().unwrap();
        assert_eq!(again, worker);
        let path = private_input_path(std::path::Path::new("/tmp/mcn-work"), again);
        assert!(path.ends_with(format!(".cur_input_{}", worker.label())));
    }

    #[test]
    fn worker_id_is_nonzero_and_unique() {
        let left = fresh_worker_id(1).unwrap();
        let right = fresh_worker_id(1).unwrap();
        assert_ne!(left.nonce, 0);
        assert_ne!(left.nonce, right.nonce);
        assert!(left.label().contains("-0000000000000001"));
    }

    #[test]
    fn signal_and_wait_sets_stay_distinct() {
        let procs = vec![
            ProcInfo {
                pid: 1,
                ppid: 0,
                starttime: 1,
                state: 'S',
            },
            ProcInfo {
                pid: 20,
                ppid: 1,
                starttime: 2,
                state: 'S',
            },
            ProcInfo {
                pid: 21,
                ppid: 1,
                starttime: 3,
                state: 'S',
            },
            ProcInfo {
                pid: 30,
                ppid: 20,
                starttime: 4,
                state: 'S',
            },
            ProcInfo {
                pid: 31,
                ppid: 1,
                starttime: 5,
                state: 'Z',
            },
        ];
        let mut residents = HashSet::new();
        residents.insert(1);
        residents.insert(20);
        residents.insert(21);
        let signals = signal_targets(&procs, &residents);
        let waits = adopted(&procs, &residents, 1);
        assert!(signals.iter().any(|proc| proc.pid == 30));
        assert!(signals.iter().any(|proc| proc.pid == 31));
        assert!(!signals.iter().any(|proc| proc.pid == 20));
        assert!(waits.iter().any(|proc| proc.pid == 31));
        assert!(!waits.iter().any(|proc| proc.pid == 30));
    }

    #[test]
    fn cleanup_keeps_main_and_coverage_residents_and_refuses_outside_pid_1() {
        let procs = vec![
            ProcInfo { pid: 1, ppid: 0, starttime: 1, state: 'S' },
            ProcInfo { pid: 2, ppid: 1, starttime: 2, state: 'S' },
            ProcInfo { pid: 3, ppid: 1, starttime: 3, state: 'S' },
            ProcInfo { pid: 4, ppid: 1, starttime: 4, state: 'S' },
            ProcInfo { pid: 5, ppid: 1, starttime: 5, state: 'S' },
            ProcInfo { pid: 6, ppid: 4, starttime: 6, state: 'S' },
            ProcInfo { pid: 7, ppid: 5, starttime: 7, state: 'R' },
            ProcInfo { pid: 8, ppid: 1, starttime: 8, state: 'Z' },
        ];
        let mut residents = HashSet::new();
        for pid in [1, 2, 3, 4, 5] {
            residents.insert(pid);
        }
        let signals = signal_targets(&procs, &residents);
        let signal_pids: Vec<i32> = signals.iter().map(|proc| proc.pid).collect();
        assert_eq!(signal_pids, vec![6, 7, 8]);
        let waits = adopted(&procs, &residents, 1);
        assert_eq!(waits.iter().map(|proc| proc.pid).collect::<Vec<_>>(), vec![8]);
        if std::process::id() != 1 {
            let error = cleanup_namespace(&residents).unwrap_err();
            assert!(
                format!("{error:?}").contains("not PID 1"),
                "{error:?}"
            );
        }
    }

    #[test]
    fn directed_pidfd_wait_collects_our_child() {
        let pid = unsafe { libc::fork() };
        assert!(pid >= 0);
        if pid == 0 {
            unsafe { libc::_exit(42) };
        }
        let fd = pidfd_open(pid).unwrap();
        let mut status = None;
        for _ in 0..50 {
            status = wait_pidfd(&fd, false).unwrap();
            if status.is_some() {
                break;
            }
            sleep(Duration::from_millis(10));
        }
        match status {
            Some(WaitStatus::Exited(child, code)) => {
                assert_eq!(child.as_raw(), pid);
                assert_eq!(code, 42);
            }
            other => panic!("missing directed wait status: {other:?}"),
        }
    }

    #[test]
    fn single_threaded_child_cannot_silently_enter_pid_namespace() {
        let pid = unsafe { libc::fork() };
        assert!(pid >= 0);
        if pid == 0 {
            let rc = unsafe { libc::unshare(libc::CLONE_NEWPID) };
            let code = if rc == 0 {
                0
            } else {
                let err = std::io::Error::last_os_error();
                if err.raw_os_error() == Some(libc::EPERM) {
                    2
                } else {
                    3
                }
            };
            unsafe { libc::_exit(code) };
        }
        let fd = pidfd_open(pid).unwrap();
        let mut status = None;
        for _ in 0..50 {
            status = wait_pidfd(&fd, false).unwrap();
            if status.is_some() {
                break;
            }
            sleep(Duration::from_millis(10));
        }
        match status {
            Some(WaitStatus::Exited(_, code)) => assert!(
                code == 0 || code == 2,
                "unshare(CLONE_NEWPID) returned an unexpected child exit {code}"
            ),
            other => panic!("namespace probe child did not exit cleanly: {other:?}"),
        }
    }
}
