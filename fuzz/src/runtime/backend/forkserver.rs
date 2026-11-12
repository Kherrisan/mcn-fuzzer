//! One ordinary forkserver child per case.
//!
//! The child writes the PCGUARD counters into the shared map created here.
//! `ForkserverExecutor::run_target` is not used. That method would request a
//! second child and try to feed it bytes.

use std::{
    fs::{self, File},
    path::Path,
    time::Duration,
};

use libafl::{
    executors::{forkserver::ForkserverExecutor, StdChildArgs},
    observers::{StdErrObserver, StdOutObserver},
    Error,
};
use libafl_bolts::{
    core_affinity::CoreId,
    shmem::{ShMem, ShMemProvider, UnixShMem, UnixShMemProvider},
    tuples::{tuple_list, Handled},
    StdTargetArgs,
};
use nix::{
    sys::{
        signal::{kill, Signal},
        time::TimeSpec,
    },
    unistd::Pid,
};

use super::{AbortReason, Aborted, TargetBackend};
use crate::{
    app::config::{reject_reserved_target_env, Campaign},
    runtime::report::TargetStop,
};

/// Variables that disable PCGUARD counters. They are removed, never set to zero.
const FSRV_ONLY_VARS: [&str; 2] = ["AFL_LLVM_ONLY_FSRV", "AFL_GCC_ONLY_FSRV"];

/// Observers attached to every forkserver executor. Coverage replay binds them
/// to append-only SUT logs; ordinary fuzzing leaves them unbound.
type OutputObservers = (StdOutObserver, (StdErrObserver, ()));

/// LibAFL forkserver plus the shared map the target updates.
pub struct ForkserverBackend {
    executor: ForkserverExecutor<(), OutputObservers, (), UnixShMem>,
    /// Coverage bitmap. The target attaches to it through `__AFL_SHM_ID`.
    shmem: UnixShMem,
    _shmem_provider: UnixShMemProvider,
    /// Bytes the handshake said the target uses. Never larger than `shmem`.
    map_len: usize,
    child_pid: Option<i32>,
    forkserver_pid: i32,
    cleanup_signal: Option<i32>,
    start_pending: bool,
    status_owed: bool,
    alive: bool,
}

impl ForkserverBackend {
    /// Spawn the forkserver and complete its handshake.
    pub fn spawn(campaign: &Campaign, port: u16) -> Result<Self, Error> {
        let binary = &campaign.target.binary;
        if !binary.is_file() {
            return Err(Error::illegal_argument(format!(
                "target binary {} is missing. Build the campaign target first.",
                binary.display()
            )));
        }
        if campaign.target.args.is_empty() {
            return Err(Error::illegal_argument(
                "target.args is empty; provide the target's arguments in target.args.".to_string(),
            ));
        }
        let allocated = campaign.target.map_size;
        if allocated < 64 || allocated % 64 != 0 {
            return Err(Error::illegal_argument(
                "target.map_size must be a multiple of 64 and at least 64. The default is 65536."
                    .to_string(),
            ));
        }
        reject_reserved_target_env(&campaign.target.extra_env)?;
        scrub_forkserver_only_env();
        fs::create_dir_all(&campaign.target.workdir).map_err(|error| {
            Error::unknown(format!(
                "creating workdir {}: {error}",
                campaign.target.workdir.display()
            ))
        })?;
        let mut provider = UnixShMemProvider::new()?;
        let shmem = provider.new_shmem(allocated)?;
        // Safety: this process is single-threaded while the forkserver starts.
        unsafe {
            shmem.write_to_env("__AFL_SHM_ID")?;
        }

        let placeholders = campaign.placeholders(port);
        let rendered: Vec<String> = campaign
            .target
            .args
            .iter()
            .map(|arg| placeholders.apply(arg))
            .collect();
        let mut builder = ForkserverExecutor::builder()
            .program(binary)
            .args(rendered.iter().map(String::as_str))
            .coverage_map_size(allocated)
            .is_persistent(false)
            .timeout(Duration::from_millis(campaign.timeouts.case_ms))
            .debug_child(campaign.target.debug_child)
            .current_dir(campaign.target.workdir.clone());
        let worker = crate::runtime::worker_id()?;
        let path = crate::runtime::private_input_path(&campaign.target.workdir, worker);
        let file = libafl_bolts::fs::InputFile::create(&path).map_err(|error| {
            Error::unknown(format!("AFL input file {}: {error}", path.display()))
        })?;
        {
            use libafl_bolts::target_args::StdTargetArgs;
            StdTargetArgs::inner_mut(&mut builder).input_location =
                libafl_bolts::InputLocation::StdIn {
                    input_file: Some(file),
                };
        }
        if let Some(core) = campaign.campaign.core {
            builder = builder.core(CoreId(core));
        }
        for (key, value) in &campaign.target.extra_env {
            builder = builder.env(key, placeholders.apply(value));
        }
        let executor = builder
            .build(tuple_list!(
                StdOutObserver::new("sut-stdout".into())?,
                StdErrObserver::new("sut-stderr".into())?
            ))
            .map_err(|error| {
            let text = format!("{error:?}");
            if text.contains("allocated map size") || text.contains("map size") {
                Error::illegal_argument(format!(
                    "forkserver handshake with {} failed: {text}. Increase target.map_size in the campaign; the shared map is not truncated.",
                    binary.display()
                ))
            } else {
                Error::unknown(format!(
                    "forkserver handshake with {} failed: {text}",
                    binary.display()
                ))
            }
        })?;
        let reported = executor.coverage_map_size().unwrap_or(allocated);
        if reported == 0 || reported > allocated {
            return Err(Error::illegal_argument(format!(
                "target {} reported coverage map size {reported}, allocation is {allocated}. Set target.map_size to at least the reported size.",
                binary.display()
            )));
        }
        let forkserver_pid = find_child_pid(binary).unwrap_or(0);
        log::info!(
            "pcguard forkserver pid {forkserver_pid}, map {reported}/{allocated}, control port {port}"
        );
        Ok(Self {
            executor,
            shmem,
            _shmem_provider: provider,
            map_len: reported,
            child_pid: None,
            forkserver_pid,
            cleanup_signal: None,
            start_pending: false,
            status_owed: false,
            alive: true,
        })
    }

    /// Start a forkserver-only coverage binary.
    ///
    /// The compatibility map is a new shared memory passed only on the child
    /// command. The process-global `__AFL_SHM_ID` that the PCGUARD forkserver
    /// uses is left unchanged.
    pub fn spawn_coverage(
        campaign: &Campaign,
        port: u16,
        profile_file: &str,
    ) -> Result<Self, Error> {
        Self::spawn_coverage_with_output(campaign, port, profile_file, None, None)
    }

    /// Start a forkserver-only coverage binary with optional stdout/stderr FDs.
    ///
    /// The descriptors are bound through `StdOutObserver::new_file` /
    /// `StdErrObserver::new_file`; the manual forkserver path does not call the
    /// observer hooks, so the children inherit the append-mode files directly.
    pub fn spawn_coverage_with_output(
        campaign: &Campaign,
        port: u16,
        profile_file: &str,
        stdout: Option<File>,
        stderr: Option<File>,
    ) -> Result<Self, Error> {
        let binary = &campaign.target.binary;
        if !binary.is_file() {
            return Err(Error::illegal_argument(format!(
                "coverage binary {} is missing",
                binary.display()
            )));
        }
        let allocated = campaign.target.map_size;
        if allocated < 64 || allocated % 64 != 0 {
            return Err(Error::illegal_argument(
                "target.map_size must be a multiple of 64 and at least 64".to_string(),
            ));
        }
        reject_reserved_target_env(&campaign.target.extra_env)?;
        fs::create_dir_all(&campaign.target.workdir).map_err(|error| {
            Error::unknown(format!(
                "creating workdir {}: {error}",
                campaign.target.workdir.display()
            ))
        })?;
        // The forkserver builder reads the process-global id even when this
        // command has no main fuzz executor. A sentinel keeps that id distinct
        // from the coverage map passed only to the child.
        if std::env::var("__AFL_SHM_ID").unwrap_or_default().is_empty() {
            std::env::set_var("__AFL_SHM_ID", "mcn-coverage-standalone");
        }
        let main_shm = std::env::var("__AFL_SHM_ID").unwrap_or_default();
        let mut provider = UnixShMemProvider::new()?;
        let shmem = provider.new_shmem(allocated)?;
        let coverage_shm = shmem.id().to_string();
        if !main_shm.is_empty() && coverage_shm == main_shm {
            return Err(Error::unknown(
                "coverage compatibility map reused the main PCGUARD __AFL_SHM_ID".to_string(),
            ));
        }
        let placeholders = campaign.placeholders(port);
        let rendered: Vec<String> = campaign
            .target
            .args
            .iter()
            .map(|arg| placeholders.apply(arg))
            .collect();
        let mut builder = ForkserverExecutor::builder()
            .program(binary)
            .args(rendered.iter().map(String::as_str))
            .coverage_map_size(allocated)
            .is_persistent(false)
            .fsrv_only(true)
            .timeout(Duration::from_millis(campaign.timeouts.case_ms))
            .debug_child(false)
            .current_dir(campaign.target.workdir.clone())
            .env("__AFL_SHM_ID", &coverage_shm)
            .env("__AFL_SHM_ID_SIZE", shmem.len().to_string())
            .env("LLVM_PROFILE_FILE", profile_file);
        let input_path = campaign.target.workdir.join(".cur_input_coverage");
        let file = libafl_bolts::fs::InputFile::create(&input_path)
            .map_err(|error| Error::unknown(format!("coverage AFL input file: {error}")))?;
        {
            use libafl_bolts::target_args::StdTargetArgs;
            StdTargetArgs::inner_mut(&mut builder).input_location =
                libafl_bolts::InputLocation::StdIn {
                    input_file: Some(file),
                };
        }
        for (key, value) in &campaign.target.extra_env {
            if key == "AFL_LLVM_ONLY_FSRV"
                || key == "AFL_GCC_ONLY_FSRV"
                || key == "__AFL_SHM_ID"
                || key == "__AFL_SHM_ID_SIZE"
                || key == "AFL_MAP_SIZE"
            {
                continue;
            }
            builder = builder.env(key, placeholders.apply(value));
        }
        let captured = stdout.is_some() || stderr.is_some();
        let stdout_observer = match stdout {
            Some(file) => StdOutObserver::new_file("sut-stdout".into(), file),
            None => StdOutObserver::new("sut-stdout".into())?,
        };
        let stderr_observer = match stderr {
            Some(file) => StdErrObserver::new_file("sut-stderr".into(), file),
            None => StdErrObserver::new("sut-stderr".into())?,
        };
        let observers = tuple_list!(stdout_observer, stderr_observer);
        let stdout_handle = observers.0.handle();
        let stderr_handle = observers.1 .0.handle();
        if captured {
            builder = builder
                .stdout_observer(stdout_handle)
                .stderr_observer(stderr_handle);
        }
        let executor = builder.build(observers).map_err(|error| {
            Error::unknown(format!(
                "coverage forkserver handshake with {} failed: {error:?}",
                binary.display()
            ))
        })?;
        let reported = executor.coverage_map_size().unwrap_or(allocated);
        if reported == 0 || reported > allocated {
            return Err(Error::illegal_argument(format!(
                "coverage target {} reported map {reported}, allocation is {allocated}",
                binary.display()
            )));
        }
        let forkserver_pid = find_child_pid(binary).unwrap_or(0);
        log::info!(
            "coverage forkserver pid {forkserver_pid}, compatibility map {reported}/{allocated}, port {port}"
        );
        Ok(Self {
            executor,
            shmem,
            _shmem_provider: provider,
            map_len: reported,
            child_pid: None,
            forkserver_pid,
            cleanup_signal: None,
            start_pending: false,
            status_owed: false,
            alive: true,
        })
    }

    /// Send the fork request without waiting for the child pid.
    pub fn begin_start(&mut self) -> Result<(), Error> {
        if !self.alive {
            return Err(Error::unknown(
                "forkserver was stopped and is not restarted".to_string(),
            ));
        }
        if self.start_pending || self.status_owed || self.child_pid.is_some() {
            return Err(Error::illegal_state(
                "forkserver still owes a pid or a status from an earlier case".to_string(),
            ));
        }
        let timed_out = self.executor.forkserver().last_run_timed_out_raw();
        self.executor.forkserver_mut().write_ctl(timed_out)?;
        self.start_pending = true;
        self.cleanup_signal = None;
        Ok(())
    }

    /// Read the child pid if the forkserver has written it.
    pub fn poll_start(&mut self, timeout: Duration) -> Result<Option<i32>, Error> {
        if !self.start_pending {
            return Ok(self.child_pid);
        }
        let spec = TimeSpec::from_duration(timeout);
        match self.executor.forkserver_mut().read_st_timed(&spec)? {
            None => Ok(None),
            Some(pid) if pid <= 0 => Err(Error::unknown(
                "forkserver returned a non-positive child pid".to_string(),
            )),
            Some(pid) => {
                self.executor
                    .forkserver_mut()
                    .set_child_pid(Pid::from_raw(pid));
                self.executor.forkserver_mut().set_last_run_timed_out(false);
                self.child_pid = Some(pid);
                self.start_pending = false;
                if self.forkserver_pid == 0 {
                    self.forkserver_pid = parent_of(pid).unwrap_or(0);
                }
                Ok(Some(pid))
            }
        }
    }

    /// SIGKILL the current child and do not wait for its status.
    pub fn kill_child(&mut self, case_deadline: bool) -> Result<(), Error> {
        if case_deadline {
            self.executor.forkserver_mut().set_last_run_timed_out(true);
        }
        self.signal_child(Signal::SIGKILL)?;
        Ok(())
    }

    /// The previous case's status has not been read. The map must not be reused yet.
    pub fn status_owed(&self) -> bool {
        self.status_owed
    }

    /// Remember that a child exited but its forkserver status missed the deadline.
    pub fn mark_status_owed(&mut self) {
        if self.child_pid.is_some() || self.start_pending {
            self.status_owed = true;
        }
    }

    /// Discard one owed status. `Ok(false)` means it is still missing.
    pub fn discard_status(&mut self, timeout: Duration) -> Result<bool, Error> {
        if !self.status_owed && self.child_pid.is_none() && !self.start_pending {
            return Ok(true);
        }
        if self.start_pending {
            return Ok(false);
        }
        match self.poll_status(timeout)? {
            Some(status) => {
                let _ = self.note_status(status);
                self.status_owed = false;
                Ok(true)
            }
            None => Ok(false),
        }
    }

    /// Stop the forkserver process. A later start fails instead of spawning a new one.
    pub fn retire(&mut self) {
        self.alive = false;
        self.start_pending = false;
        self.status_owed = false;
        if let Some(pid) = self.child_pid.take() {
            if pid > 0 {
                let _ = kill(Pid::from_raw(pid), Signal::SIGKILL);
            }
        }
        if self.forkserver_pid > 0 {
            let _ = kill(Pid::from_raw(self.forkserver_pid), Signal::SIGKILL);
        }
    }

    /// Child pid once the forkserver has reported it.
    pub fn child_pid(&self) -> Option<i32> {
        self.child_pid
    }

    /// Signal recorded for the current child.
    pub fn cleanup_signal(&self) -> Option<i32> {
        self.cleanup_signal
    }

    /// Handshake length of the shared map.
    pub fn map_len(&self) -> usize {
        self.map_len
    }

    /// Raw counters for this case. Index 0 may be the runtime marker.
    pub fn map(&self) -> &[u8] {
        &self.shmem[..self.map_len]
    }

    /// Pointer passed to the map observer. Valid while this backend is alive.
    pub fn map_ptr(&mut self) -> *mut u8 {
        self.shmem.as_mut_ptr()
    }

    fn request_child(&mut self) -> Result<i32, Error> {
        self.begin_start()?;
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        loop {
            if let Some(pid) = self.poll_start(Duration::from_millis(20))? {
                return Ok(pid);
            }
            if std::time::Instant::now() >= deadline {
                self.retire();
                return Err(Error::unknown(
                    "forkserver did not return a child pid and was not restarted".to_string(),
                ));
            }
        }
    }

    fn poll_status(&mut self, timeout: Duration) -> Result<Option<i32>, Error> {
        let spec = TimeSpec::from_duration(timeout);
        self.executor.forkserver_mut().read_st_timed(&spec)
    }

    fn note_status(&mut self, status: i32) -> TargetStop {
        self.executor.forkserver_mut().set_status(status);
        self.executor.forkserver_mut().reset_child_pid();
        self.child_pid = None;
        self.status_owed = false;
        self.start_pending = false;
        TargetStop::from_raw(status)
    }

    fn signal_child(&mut self, signal: Signal) -> Result<(), Error> {
        let Some(pid) = self.child_pid else {
            return Ok(());
        };
        match kill(Pid::from_raw(pid), signal) {
            Ok(()) => {
                self.cleanup_signal = Some(signal as i32);
                Ok(())
            }
            Err(nix::errno::Errno::ESRCH) => Ok(()),
            Err(error) => Err(Error::unknown(format!("kill {pid}: {error}"))),
        }
    }
}

impl Drop for ForkserverBackend {
    fn drop(&mut self) {
        if let Some(pid) = self.child_pid.take() {
            if pid > 0 {
                let _ = kill(Pid::from_raw(pid), Signal::SIGKILL);
            }
        }
        if self.forkserver_pid > 0 {
            let _ = kill(Pid::from_raw(self.forkserver_pid), Signal::SIGKILL);
        }
    }
}

impl TargetBackend for ForkserverBackend {
    fn prepare_case(&mut self) -> Result<(), Error> {
        self.shmem[..self.map_len].fill(0);
        Ok(())
    }

    fn start_case(&mut self) -> Result<i32, Error> {
        self.request_child()
    }

    fn poll_target(&mut self, timeout: Duration) -> Result<Option<TargetStop>, Error> {
        match self.poll_status(timeout)? {
            Some(status) => Ok(Some(self.note_status(status))),
            None => Ok(None),
        }
    }

    fn finish_case(&mut self) -> Result<(), Error> {
        if self.child_pid.is_some() {
            return Err(Error::illegal_state(
                "finish_case called while the child is still unreaped".to_string(),
            ));
        }
        Ok(())
    }

    fn abort_case(&mut self, reason: AbortReason) -> Result<Aborted, Error> {
        if matches!(reason, AbortReason::Deadline | AbortReason::ExitGrace) {
            self.executor.forkserver_mut().set_last_run_timed_out(true);
        }
        if self.child_pid.is_none() {
            return Err(Error::illegal_state(
                "abort_case called without a live child".to_string(),
            ));
        }
        self.signal_child(Signal::SIGTERM)?;
        let stop = if let Some(status) = self.poll_status(Duration::from_millis(200))? {
            self.note_status(status)
        } else {
            self.signal_child(Signal::SIGKILL)?;
            let status = self.poll_status(Duration::from_secs(2))?.ok_or_else(|| {
                Error::unknown("forkserver did not report the aborted child".to_string())
            })?;
            self.note_status(status)
        };
        let cleanup_signal = self.cleanup_signal.unwrap_or(libc::SIGKILL);
        Ok(Aborted {
            stop,
            cleanup_signal,
        })
    }

    fn forkserver_pid(&self) -> i32 {
        self.forkserver_pid
    }
}

fn scrub_forkserver_only_env() {
    for key in FSRV_ONLY_VARS {
        if std::env::var_os(key).is_some() {
            log::info!("removing {key} so PCGUARD counters stay enabled");
            std::env::remove_var(key);
        }
    }
}

fn find_child_pid(binary: &Path) -> Option<i32> {
    let parent = std::process::id();
    let dir = fs::read_dir("/proc").ok()?;
    for entry in dir.flatten() {
        let name = entry.file_name();
        let Ok(pid) = name.to_string_lossy().parse::<i32>() else {
            continue;
        };
        let Ok(status) = fs::read_to_string(format!("/proc/{pid}/status")) else {
            continue;
        };
        let Some(ppid) = status_value(&status, "PPid") else {
            continue;
        };
        if ppid != parent.to_string() {
            continue;
        }
        let Ok(cmdline) = fs::read(format!("/proc/{pid}/cmdline")) else {
            continue;
        };
        let text = String::from_utf8_lossy(&cmdline);
        if text.contains(&*binary.display().to_string()) {
            return Some(pid);
        }
    }
    None
}

fn parent_of(pid: i32) -> Option<i32> {
    let status = fs::read_to_string(format!("/proc/{pid}/status")).ok()?;
    status_value(&status, "PPid")?.parse().ok()
}

fn status_value(status: &str, key: &str) -> Option<String> {
    status.lines().find_map(|line| {
        let (name, value) = line.split_once(':')?;
        if name == key {
            Some(value.trim().to_string())
        } else {
            None
        }
    })
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use super::*;
    use crate::app::config::{Campaign, CampaignPaths, TargetConfig, Timeouts};

    fn campaign_with(key: &str, value: &str) -> Campaign {
        Campaign {
            target: TargetConfig {
                binary: PathBuf::from("/bin/true"),
                source_dir: None,
                workdir: PathBuf::from("/tmp"),
                cleanup_script: None,
                args: vec!["fftp.conf".to_string()],
                host: "127.0.0.1".to_string(),
                port: 0,
                extra_env: vec![(key.to_string(), value.to_string())],
                map_size: 65_536,
                debug_child: false,
            },
            timeouts: Timeouts::default(),
            campaign: CampaignPaths {
                seeds: PathBuf::from("/tmp"),
                corpus: PathBuf::from("/tmp"),
                crashes: PathBuf::from("/tmp"),
                python: PathBuf::from("/usr/bin/python3"),
                pythonpath: PathBuf::from("/tmp"),
                core: None,
                iterations: 1,
                timeout_secs: None,
                bitmap_stagnation_limit: crate::engine::stagnation::DEFAULT_BITMAP_STAGNATION_LIMIT,
            },
            capture: crate::app::config::CaptureConfig::default(),
            coverage: crate::app::config::CoverageConfig::default(),
            _legacy_source_coverage: (),
            _legacy_generator_synthesis: (),
            generator_synthesis: crate::app::config::GeneratorSynthesisConfig::default(),
            agents: crate::app::config::AgentCatalog::default(),
            parties: None,
            base: PathBuf::from("/tmp"),
            output_root: None,
        }
    }

    #[test]
    fn spawn_rejects_reserved_env_before_starting_a_target() {
        for (key, value) in [
            ("AFL_LLVM_ONLY_FSRV", "0"),
            ("AFL_GCC_ONLY_FSRV", "1"),
            ("__AFL_SHM_ID", "99"),
            ("AFL_MAP_SIZE", "4096"),
        ] {
            let error = match ForkserverBackend::spawn(&campaign_with(key, value), 0) {
                Err(error) => error,
                Ok(_) => panic!("{key} started a target"),
            };
            let message = format!("{error:?}");
            assert!(message.contains(key), "{message}");
        }
    }
}
