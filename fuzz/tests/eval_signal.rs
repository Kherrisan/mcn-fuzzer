//! TERM/INT against a real evaluation process. No HC, forkserver, or namespace.

use std::{
    fs,
    path::Path,
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};

#[test]
fn term_and_int_reap_the_probe_process() {
    let bin = std::path::Path::new(env!("CARGO_BIN_EXE_mcn-synthesis-eval"));
    assert!(
        bin.is_file(),
        "build mcn-synthesis-eval before this test: {}",
        bin.display()
    );
    for signal in [libc::SIGTERM, libc::SIGINT] {
        let dir =
            std::env::temp_dir().join(format!("mcn-eval-signal-{}-{}", signal, std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let mut child = Command::new(&bin)
            .env("MCN_EVAL_SIGNAL_PROBE", "1")
            .env("MCN_EVAL_PROBE_DIR", &dir)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap_or_else(|error| {
                if error.kind() == std::io::ErrorKind::PermissionDenied {
                    panic!("ENVIRONMENT: cannot spawn evaluation binary: {error}");
                }
                panic!("spawn probe: {error}");
            });
        let ready = dir.join("ready");
        let deadline = Instant::now() + Duration::from_secs(5);
        while !ready.exists() && Instant::now() < deadline {
            if let Some(status) = child.try_wait().unwrap() {
                let err = fs::read_to_string(dir.join("result.txt")).unwrap_or_default();
                panic!("probe exited early ({status}): {err}");
            }
            thread::sleep(Duration::from_millis(20));
        }
        assert!(ready.exists(), "probe did not become ready");
        let sleep_pid = read_pid(&dir.join("sleep.pid"));
        let orphan_pid = read_pid(&dir.join("orphan.pid"));
        assert!(
            proc_exists(sleep_pid),
            "sleep pid {sleep_pid} was not alive"
        );
        assert!(
            proc_exists(orphan_pid),
            "orphan pid {orphan_pid} was not alive"
        );
        let rc = unsafe { libc::kill(child.id() as i32, signal) };
        if rc != 0 {
            let error = std::io::Error::last_os_error();
            if error.kind() == std::io::ErrorKind::PermissionDenied
                || error.raw_os_error() == Some(1)
            {
                panic!("ENVIRONMENT: signal not permitted: {error}");
            }
            panic!("signal failed: {error}");
        }
        let status = child.wait().unwrap();
        let result = fs::read_to_string(dir.join("result.txt")).unwrap_or_default();
        assert!(status.success(), "{status:?} {result}");
        assert_eq!(result, "cleaned");
        assert!(!residue(sleep_pid), "sleep leftover pid {sleep_pid}");
        assert!(!residue(orphan_pid), "orphan leftover pid {orphan_pid}");
        let _ = fs::remove_dir_all(&dir);
    }
}

#[test]
fn term_and_int_interrupt_real_version_preflight() {
    use std::os::unix::fs::PermissionsExt;

    let bin = Path::new(env!("CARGO_BIN_EXE_mcn-synthesis-eval"));
    let root = Path::new(env!("CARGO_MANIFEST_DIR"));
    for signal in [libc::SIGTERM, libc::SIGINT] {
        let dir = std::env::temp_dir().join(format!(
            "mcn-eval-preflight-signal-{signal}-{}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let prepared = Command::new(bin)
            .args(["prepare", "--suite"])
            .arg(root.join("evaluation/lightftp.toml"))
            .args(["--profile", "codex", "--case", "login-quit", "--out"])
            .arg(dir.join("out"))
            .output()
            .unwrap();
        assert!(
            prepared.status.success(),
            "{}",
            String::from_utf8_lossy(&prepared.stderr)
        );
        let taskpath = dir.join("out/codex/login-quit/1/task.json");
        let mut task = mcn_fuzz::evaluation::Task::load(&taskpath).unwrap();
        let pidfile = dir.join("version.pid");
        let stub = dir.join("version.py");
        fs::write(&stub, format!(
            "#!/usr/bin/env python3\nimport os,time\nopen({:?}, 'w').write(str(os.getpid()))\ntime.sleep(30)\n",
            pidfile.display().to_string(),
        )).unwrap();
        fs::set_permissions(&stub, fs::Permissions::from_mode(0o755)).unwrap();
        task.executable = stub;
        task.save().unwrap();
        let mut child = Command::new(bin)
            .args(["generate", "--task"])
            .arg(&taskpath)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let ready_deadline = Instant::now() + Duration::from_secs(3);
        while !pidfile.exists() && Instant::now() < ready_deadline {
            assert!(
                child.try_wait().unwrap().is_none(),
                "generate exited before version check"
            );
            thread::sleep(Duration::from_millis(10));
        }
        if !pidfile.exists() {
            let _ = child.kill();
            let _ = child.wait();
            panic!("preflight did not start");
        }
        let version_pid = read_pid(&pidfile);
        assert_eq!(unsafe { libc::kill(child.id() as i32, signal) }, 0);
        let deadline = Instant::now() + Duration::from_secs(3);
        let status = loop {
            if let Some(status) = child.try_wait().unwrap() {
                break status;
            }
            if Instant::now() >= deadline {
                // Release a stuck preflight before failing, including on old code.
                unsafe {
                    libc::kill(version_pid, libc::SIGKILL);
                }
                let _ = child.kill();
                let _ = child.wait();
                panic!("preflight ignored signal {signal}");
            }
            thread::sleep(Duration::from_millis(10));
        };
        assert!(!status.success());
        assert!(!residue(version_pid), "version subprocess was not reaped");
        let saved = mcn_fuzz::evaluation::Task::load(&taskpath).unwrap();
        assert_eq!(
            saved.stages["generate"].failure_kind.as_deref(),
            Some("interrupted")
        );
        fs::remove_dir_all(dir).unwrap();
    }
}

fn read_pid(path: &Path) -> i32 {
    fs::read_to_string(path)
        .unwrap_or_else(|error| panic!("reading {}: {error}", path.display()))
        .trim()
        .parse()
        .unwrap()
}

fn proc_exists(pid: i32) -> bool {
    Path::new(&format!("/proc/{pid}")).exists()
}

fn residue(pid: i32) -> bool {
    let path = format!("/proc/{pid}/stat");
    if !Path::new(&path).exists() {
        return false;
    }
    let text = fs::read_to_string(&path).unwrap_or_default();
    let state = text
        .rsplit_once(')')
        .and_then(|(_, rest)| rest.trim_start().chars().next())
        .unwrap_or('?');
    // Alive or zombie both count as residue.
    let _ = state;
    true
}
