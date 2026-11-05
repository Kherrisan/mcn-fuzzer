//! Opt-in campaign against the local `hc` binary.
//!
//! `cargo test` skips this. It starts one private herdr-core per phase, with a
//! fresh `HC_SESSION`, `HC_SOCKET`, and XDG directories under `/tmp`. It never
//! opens `/home/user/.config/herdr/herdr.sock` and never stops the shared
//! `herdr server`.
//!
//! ```text
//! MCN_HC_INTEGRATION=1 cargo test --offline --test real_hc_core -- --ignored --test-threads=1 --nocapture
//! ```

use std::{
    fs,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    thread,
    time::{Duration, Instant},
};

use mcn_fuzz::agents::HcClient;
use serde_json::Value;

const HC: &str = "/home/user/.local/bin/hc";
const SHARED_SERVER: i32 = 1_657_103;
const SHARED_SOCK: &str = "/home/user/.config/herdr/herdr.sock";

#[test]
#[ignore = "private herdr-core via sudo; set MCN_HC_INTEGRATION=1"]
fn private_hc_core_boot_launch_inspect_and_cleanup() {
    if std::env::var("MCN_HC_INTEGRATION").ok().as_deref() != Some("1") {
        panic!("refusing to start hc without MCN_HC_INTEGRATION=1");
    }
    let hc = fs::canonicalize(HC).expect("local hc");
    assert!(
        hc.ends_with("herdr-core"),
        "expected /home/user/.local/bin/hc to resolve to herdr-core, got {}",
        hc.display()
    );
    assert_ne!(hc, Path::new("/usr/local/bin/herdr"));

    let root = PathBuf::from(format!("/tmp/mcn-hc-itest-{}", std::process::id()));
    let _ = fs::remove_dir_all(&root);
    fs::create_dir_all(&root).unwrap();
    let stub = root.join("stub.py");
    fs::write(&stub, STUB).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&stub, fs::Permissions::from_mode(0o755)).unwrap();
    }

    let baseline = Baseline::capture();
    let dirs_before = hc_runtime_dirs();

    happy(&root, &stub);
    baseline.assert_same("after a normal private-core run");

    hold(&root, &stub);
    baseline.assert_same("after an initialization failure");

    terminated(&root, &stub);
    baseline.assert_same("after SIGTERM");

    let leaked: Vec<_> = hc_runtime_dirs()
        .into_iter()
        .filter(|path| !dirs_before.contains(path))
        .collect();
    assert!(
        leaked.is_empty(),
        "private runtime directories remained: {leaked:?}"
    );
    let _ = fs::remove_dir_all(&root);
}

fn happy(root: &Path, stub: &Path) {
    let dir = root.join("happy");
    write_campaign(&dir, stub, &[], 30);
    let mut child = spawn_fuzzer(&dir.join("campaign.toml"), &dir.join("run.log"));
    let (socket, session) = wait_private_socket(&dir.join("synthesis"), 40);
    let observed = wait_namespace_sample(&session, 20);
    eprintln!("happy namespace\n{observed}");
    let core_pid = session_pids(&session)
        .into_iter()
        .next()
        .expect("private core pid");
    let survived_cases = Arc::new(AtomicBool::new(false));
    let flag = Arc::clone(&survived_cases);
    let log_path = dir.join("run.log");
    thread::spawn(move || {
        for _ in 0..400 {
            let alive = Path::new(&format!("/proc/{core_pid}")).exists();
            let log = fs::read_to_string(&log_path).unwrap_or_default();
            if alive && log.contains("Testcase") {
                flag.store(true, Ordering::Relaxed);
                return;
            }
            thread::sleep(Duration::from_millis(25));
        }
    });
    assert!(
        observed.contains("pid1_netns_differs_from_host=1"),
        "{observed}"
    );
    assert!(observed.contains("session_uid_is_caller=1"), "{observed}");
    assert!(
        observed.contains("session_pidns_matches_pid1=1"),
        "{observed}"
    );
    assert!(
        observed.contains("session_netns_matches_host=1"),
        "{observed}"
    );

    let ready = dir.join("synthesis/init/ready.json");
    wait_file(&ready, 30);
    let ready_value: Value = serde_json::from_slice(&fs::read(&ready).unwrap()).unwrap();
    assert_eq!(ready_value["status"], "ready");
    assert_eq!(ready_value["session"], session);
    assert!(!dir.join("synthesis/ready.json").exists());

    let mut client = connect_private(&socket);
    let info = client
        .get("generator")
        .expect("terminal.get on the private core");
    assert_eq!(info.state, "live");
    let screen = client
        .read(&info.name, 40)
        .expect("terminal.read on the private core");
    assert!(
        screen.contains("ready.json") || screen.contains("SESSION="),
        "inspect did not show the init prompt: {screen}"
    );
    let sleeper = client
        .run_terminal(
            "sleeper",
            &["/bin/sleep".to_string(), "30".to_string()],
            &[],
            &dir.join("synthesis"),
        )
        .expect("launch a second private terminal");
    client
        .interrupt(&sleeper.name)
        .expect("interrupt stays on the private core");
    client
        .kill(&sleeper.name)
        .expect("kill stays on the private core");
    drop(client);

    let status = wait_child(&mut child, 90);
    let log = fs::read_to_string(dir.join("run.log")).unwrap_or_default();
    assert!(
        status.map(|code| code.success()).unwrap_or(false),
        "happy run failed: {status:?}\n{log}"
    );
    assert!(
        survived_cases.load(Ordering::Relaxed),
        "private core was gone before a fuzz case completed; case cleanup may have killed it\n{log}"
    );
    assert_correlation(&dir.join("synthesis"));
    assert!(
        corpus_uses_published_generator(&dir.join("corpus")),
        "corpus did not keep the published generator"
    );
    let pcaps = assert_marker_absent(&dir.join("corpus"));
    assert!(pcaps > 0, "SUT capture wrote no pcap");
    assert!(
        session_pids(&session).is_empty(),
        "session {session} still has processes"
    );
    assert!(
        !socket.exists(),
        "private socket {} still exists",
        socket.display()
    );
}

fn hold(root: &Path, stub: &Path) {
    let dir = root.join("hold");
    write_campaign(&dir, stub, &["--hold"], 3);
    let mut child = spawn_fuzzer(&dir.join("campaign.toml"), &dir.join("run.log"));
    let (_socket, session) = wait_private_socket(&dir.join("synthesis"), 40);
    let status = wait_child(&mut child, 40);
    let log = fs::read_to_string(dir.join("run.log")).unwrap_or_default();
    assert!(
        status.map(|code| !code.success()).unwrap_or(false),
        "hold run should fail before cases: {status:?}\n{log}"
    );
    assert!(
        log.contains("initialization") || log.contains("timed out") || log.contains("ready.json"),
        "{log}"
    );
    assert!(
        session_pids(&session).is_empty(),
        "failed init left session {session} processes"
    );
}

fn terminated(root: &Path, stub: &Path) {
    let dir = root.join("term");
    write_campaign(&dir, stub, &[], 30);
    let mut child = spawn_fuzzer(&dir.join("campaign.toml"), &dir.join("run.log"));
    let (_socket, session) = wait_private_socket(&dir.join("synthesis"), 40);
    let log_path = dir.join("run.log");
    let deadline = Instant::now() + Duration::from_secs(30);
    while Instant::now() < deadline {
        if fs::read_to_string(&log_path)
            .unwrap_or_default()
            .contains("pcguard forkserver")
        {
            break;
        }
        thread::sleep(Duration::from_millis(100));
    }
    // SIGTERM the sudo supervisor. SIGKILL would skip PID 1's handler.
    let _ = Command::new("sudo")
        .args(["-n", "kill", "-TERM", &child.id().to_string()])
        .status();
    let status = wait_child(&mut child, 20);
    let log = fs::read_to_string(&log_path).unwrap_or_default();
    assert!(
        log.contains("PID 1 received a termination signal"),
        "TERM did not reach PID 1 cleanly: {status:?}\n{log}"
    );
    assert!(
        !log.contains("Interrupted system call"),
        "supervisor treated SIGTERM as a poll error:\n{log}"
    );
    assert!(
        session_pids(&session).is_empty(),
        "SIGTERM left session {session} processes"
    );
}

fn write_campaign(dir: &Path, stub: &Path, stub_args: &[&str], init_timeout: u64) {
    for name in ["work", "corpus", "crashes", "seeds", "synthesis"] {
        fs::create_dir_all(dir.join(name)).unwrap();
    }
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let model = manifest.join("../rsc/runs/ftp-gpt-6-sol-high-20261104T031756Z/protocol.json");
    let target = manifest.join("targets/lightftp");
    let args = if stub_args.is_empty() {
        "[]".to_string()
    } else {
        serde_json::to_string(stub_args).unwrap()
    };
    let toml = format!(
        r#"[target]
binary = "{binary}"
workdir = "{dir}/work"
fixture = "{fixture}"
config_template = "{conf}"
host = "127.0.0.1"
port = 0
map_size = 65536
debug_child = false

[timeouts]
connect_ms = 2000
io_ms = 1000
case_ms = 10000
exit_grace_ms = 5000

[campaign]
seeds = "{dir}/seeds"
corpus = "{dir}/corpus"
crashes = "{dir}/crashes"
python = "{python}"
pythonpath = "{pythonpath}"
iterations = 1

[capture]
enabled = true
file_limit = 67108864

[source_coverage]
enabled = false

[protocol_generation]
enabled = true
model = "{model}"
profile = "stub"
output = "{dir}/synthesis"
init_timeout_secs = {init_timeout}
task_timeout_secs = 30
trace_len_min = 1
trace_len_max = 1
sample_attempts = 32
hc_executable = "{hc}"

[parties]
sut = ["server-PI", "server-DTP", "server-PI A", "server-PI B", "server-DTP A", "server-DTP B"]
generator = "*"

[agents.profiles.stub]
provider = "pi"
executable = "{stub}"
args = {args}
"#,
        binary = target.join("build/fftp").display(),
        dir = dir.display(),
        fixture = target.join("fixture").display(),
        conf = target.join("fftp.conf").display(),
        python = manifest.join(".venv/bin/python").display(),
        pythonpath = manifest.join("python").display(),
        model = model.display(),
        init_timeout = init_timeout,
        hc = HC,
        stub = stub.display(),
        args = args,
    );
    fs::write(dir.join("campaign.toml"), toml).unwrap();
}

fn spawn_fuzzer(campaign: &Path, log_path: &Path) -> std::process::Child {
    let log = fs::File::create(log_path).unwrap();
    let err = log.try_clone().unwrap();
    Command::new("sudo")
        .args([
            "-n",
            env!("CARGO_BIN_EXE_mcn-fuzz"),
            "run",
            "--campaign",
            campaign.to_str().unwrap(),
            "--iterations",
            "1",
        ])
        .stdin(Stdio::null())
        .stdout(log)
        .stderr(err)
        .spawn()
        .expect("sudo mcn-fuzz")
}

fn wait_child(child: &mut std::process::Child, secs: u64) -> Option<std::process::ExitStatus> {
    let deadline = Instant::now() + Duration::from_secs(secs);
    while Instant::now() < deadline {
        if let Some(status) = child.try_wait().unwrap() {
            return Some(status);
        }
        thread::sleep(Duration::from_millis(100));
    }
    let _ = Command::new("sudo")
        .args(["-n", "kill", "-TERM", &child.id().to_string()])
        .status();
    child.wait().ok()
}

fn wait_private_socket(output: &Path, secs: u64) -> (PathBuf, String) {
    let deadline = Instant::now() + Duration::from_secs(secs);
    while Instant::now() < deadline {
        if let Some(found) = socket_from_marker(output) {
            assert!(
                found.0.to_string_lossy().starts_with("/tmp/mcn-hc-"),
                "{}",
                found.0.display()
            );
            assert_ne!(found.1, "default");
            return found;
        }
        thread::sleep(Duration::from_millis(50));
    }
    panic!("private HC socket did not appear under {output:?}");
}

fn socket_from_marker(output: &Path) -> Option<(PathBuf, String)> {
    let text = fs::read_to_string(output.join("private-hc-runtime")).ok()?;
    let runtime = PathBuf::from(text.trim());
    if !runtime.to_string_lossy().starts_with("/tmp/mcn-hc-") {
        return None;
    }
    let entries = fs::read_dir(runtime.join("herdr-core")).ok()?;
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|ext| ext.to_str()) == Some("sock") {
            let session = path.file_stem()?.to_str()?.to_string();
            return Some((path, session));
        }
    }
    None
}

fn connect_private(socket: &Path) -> HcClient {
    let text = socket.to_string_lossy();
    assert!(text.starts_with("/tmp/mcn-hc-"), "{text}");
    assert!(!text.contains("herdr.sock"), "{text}");
    assert!(!text.contains("/run/user/"), "{text}");
    HcClient::connect(socket, Duration::from_secs(15)).expect("connect private socket")
}

fn wait_file(path: &Path, secs: u64) {
    let deadline = Instant::now() + Duration::from_secs(secs);
    while Instant::now() < deadline {
        if path.is_file() {
            return;
        }
        thread::sleep(Duration::from_millis(50));
    }
    panic!("missing {}", path.display());
}

fn wait_namespace_sample(session: &str, secs: u64) -> String {
    let deadline = Instant::now() + Duration::from_secs(secs);
    let mut last = String::new();
    while Instant::now() < deadline {
        last = namespace_sample(session);
        if last.contains("pid1_netns_differs_from_host=1")
            && last.contains("session_uid_is_caller=1")
            && last.contains("session_pidns_matches_pid1=1")
            && last.contains("session_netns_matches_host=1")
        {
            return last;
        }
        thread::sleep(Duration::from_millis(100));
    }
    last
}

fn namespace_sample(session: &str) -> String {
    let pids = session_pids(session);
    let mut lines = vec![format!("session_pids={}", pids.len())];
    let host_net = read_ns(std::process::id() as i32, "net");
    let mut pid1_net = None;
    let mut pid1_pidns = None;
    if let Some(pid1) = find_pid1() {
        pid1_net = read_ns(pid1, "net");
        pid1_pidns = read_ns(pid1, "pid");
        lines.push(format!(
            "pid1={pid1} nspid={} netns={} pidns={}",
            nspid(pid1),
            pid1_net.as_deref().unwrap_or("?"),
            pid1_pidns.as_deref().unwrap_or("?")
        ));
    }
    let mut uid_ok = !pids.is_empty();
    let mut pidns_ok = !pids.is_empty();
    let mut net_ok = !pids.is_empty();
    for pid in pids {
        let uid = uid_of(pid);
        let net = read_ns(pid, "net");
        let pidns = read_ns(pid, "pid");
        if uid != Some(1005) {
            uid_ok = false;
        }
        if pidns != pid1_pidns || pid1_pidns.is_none() {
            pidns_ok = false;
        }
        if net != host_net || host_net.is_none() || net == pid1_net {
            net_ok = false;
        }
        lines.push(format!(
            "session_pid={pid} uid={} nspid={} netns={} pidns={} comm={}",
            uid.map(|value| value.to_string())
                .unwrap_or_else(|| "?".into()),
            nspid(pid),
            net.as_deref().unwrap_or("?"),
            pidns.as_deref().unwrap_or("?"),
            comm(pid)
        ));
    }
    lines.push(format!(
        "pid1_netns_differs_from_host={}",
        match (&pid1_net, &host_net) {
            (Some(left), Some(right)) if left != right => 1,
            _ => 0,
        }
    ));
    lines.push(format!("session_uid_is_caller={}", uid_ok as u8));
    lines.push(format!("session_pidns_matches_pid1={}", pidns_ok as u8));
    lines.push(format!("session_netns_matches_host={}", net_ok as u8));
    lines.join("\n")
}

fn find_pid1() -> Option<i32> {
    let entries = fs::read_dir("/proc").ok()?;
    for entry in entries.flatten() {
        let Ok(pid) = entry.file_name().to_string_lossy().parse::<i32>() else {
            continue;
        };
        let status = fs::read_to_string(format!("/proc/{pid}/status")).unwrap_or_default();
        if status.lines().any(|line| {
            line.strip_prefix("NSpid:")
                .is_some_and(|value| value.split_whitespace().last() == Some("1"))
        }) && comm(pid).contains("mcn-fuzz")
        {
            return Some(pid);
        }
    }
    None
}

fn assert_correlation(output: &Path) {
    for name in [
        "ready.json",
        "receipt.json",
        "generator.py",
        "params.schema.json",
        "params.json",
        "result.json",
    ] {
        assert!(
            !output.join(name).exists(),
            "{name} was written into the fixed campaign workspace"
        );
    }
    let requests = output.join("requests");
    let mut seen = 0;
    for entry in fs::read_dir(&requests).unwrap().flatten() {
        let dir = entry.path();
        if !dir.is_dir() {
            continue;
        }
        let receipt: Value =
            serde_json::from_slice(&fs::read(dir.join("receipt.json")).unwrap()).unwrap();
        let result: Value =
            serde_json::from_slice(&fs::read(dir.join("result.json")).unwrap()).unwrap();
        let request = dir.file_name().unwrap().to_string_lossy();
        assert_eq!(result["request_id"], request.as_ref());
        assert_eq!(result["protocol_id"], receipt["protocol_id"]);
        assert_eq!(result["trace_id"], receipt["trace_id"]);
        assert_eq!(result["status"], "ok");
        assert!(result["added_steps"]
            .as_array()
            .is_some_and(|items| !items.is_empty()));
        assert!(dir.join("published/generator.py").is_file());
        seen += 1;
    }
    assert!(seen > 0, "no correlated request artifacts");
}

fn corpus_uses_published_generator(corpus: &Path) -> bool {
    let Ok(entries) = fs::read_dir(corpus) else {
        return false;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if !path.is_file() || path.extension().is_some() {
            continue;
        }
        let Ok(text) = fs::read_to_string(&path) else {
            continue;
        };
        if text.contains("published/generator.py") {
            return true;
        }
    }
    false
}

fn assert_marker_absent(corpus: &Path) -> usize {
    let Ok(entries) = fs::read_dir(corpus) else {
        return 0;
    };
    let mut pcaps = 0;
    for entry in entries.flatten() {
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if !name.ends_with(".pcap") {
            continue;
        }
        pcaps += 1;
        let bytes = fs::read(entry.path()).unwrap_or_default();
        assert!(
            !bytes.windows(16).any(|window| window == b"HC-AGENT-MARKER"),
            "SUT pcap contains the host-network agent marker"
        );
    }
    pcaps
}

fn session_pids(session: &str) -> Vec<i32> {
    assert!(session.starts_with('s') && session != "default" && session.len() > 2);
    let needle = format!("HC_SESSION={session}");
    let mut found = Vec::new();
    let Ok(entries) = fs::read_dir("/proc") else {
        return found;
    };
    for entry in entries.flatten() {
        let Ok(pid) = entry.file_name().to_string_lossy().parse::<i32>() else {
            continue;
        };
        let Ok(bytes) = fs::read(format!("/proc/{pid}/environ")) else {
            continue;
        };
        let text = String::from_utf8_lossy(&bytes);
        if text.split('\0').any(|item| item == needle) {
            found.push(pid);
        }
    }
    found
}

fn hc_runtime_dirs() -> Vec<PathBuf> {
    let mut found = Vec::new();
    let Ok(entries) = fs::read_dir("/tmp") else {
        return found;
    };
    for entry in entries.flatten() {
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if name.starts_with("mcn-hc-") && entry.path().is_dir() {
            found.push(entry.path());
        }
    }
    found.sort();
    found
}

struct Baseline {
    starttime: String,
    sock_inode: Option<u64>,
}

impl Baseline {
    fn capture() -> Self {
        Self {
            starttime: proc_starttime(SHARED_SERVER),
            sock_inode: fs::metadata(SHARED_SOCK).ok().and_then(|meta| {
                use std::os::unix::fs::MetadataExt;
                Some(meta.ino())
            }),
        }
    }

    fn assert_same(&self, when: &str) {
        assert_eq!(
            proc_starttime(SHARED_SERVER),
            self.starttime,
            "shared herdr server changed {when}"
        );
        let inode = fs::metadata(SHARED_SOCK).ok().and_then(|meta| {
            use std::os::unix::fs::MetadataExt;
            Some(meta.ino())
        });
        assert_eq!(inode, self.sock_inode, "shared herdr socket changed {when}");
    }
}

fn proc_starttime(pid: i32) -> String {
    let text = fs::read_to_string(format!("/proc/{pid}/stat")).unwrap_or_default();
    let Some(end) = text.rfind(')') else {
        return String::new();
    };
    text[end + 1..]
        .split_whitespace()
        .nth(19)
        .unwrap_or("")
        .to_string()
}

fn read_ns(pid: i32, kind: &str) -> Option<String> {
    if let Ok(path) = fs::read_link(format!("/proc/{pid}/ns/{kind}")) {
        return Some(path.display().to_string());
    }
    let output = Command::new("sudo")
        .args(["-n", "readlink", &format!("/proc/{pid}/ns/{kind}")])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if text.is_empty() {
        None
    } else {
        Some(text)
    }
}

fn nspid(pid: i32) -> String {
    fs::read_to_string(format!("/proc/{pid}/status"))
        .unwrap_or_default()
        .lines()
        .find_map(|line| line.strip_prefix("NSpid:"))
        .map(|value| value.split_whitespace().collect::<Vec<_>>().join(" "))
        .unwrap_or_default()
}

fn uid_of(pid: i32) -> Option<u32> {
    fs::read_to_string(format!("/proc/{pid}/status"))
        .ok()?
        .lines()
        .find_map(|line| line.strip_prefix("Uid:"))?
        .split_whitespace()
        .next()?
        .parse()
        .ok()
}

fn comm(pid: i32) -> String {
    fs::read_to_string(format!("/proc/{pid}/comm"))
        .unwrap_or_default()
        .trim()
        .to_string()
}

const STUB: &str = r#"#!/usr/bin/env python3
import json, os, select, socket, sys, time

def field(text, prefix):
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""

def atomic(path, payload):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
        handle.write("\n")
    os.replace(temporary, path)

def write_ready(text):
    if "--hold" in sys.argv or field(text, "HOLD=") == "1":
        return
    output_dir = field(text, "OUTPUT_DIR=")
    session = field(text, "SESSION=")
    if not output_dir or not session:
        return
    os.makedirs(output_dir, exist_ok=True)
    atomic(os.path.join(output_dir, "ready.json"), {"status": "ready", "session": session})

def write_result(text):
    if "--hold" in sys.argv:
        return
    output_dir = field(text, "OUTPUT_DIR=")
    request = field(text, "REQUEST=")
    protocol = field(text, "PROTOCOL=")
    trace = field(text, "TRACE=")
    if not (output_dir and request and protocol and trace):
        return
    os.makedirs(output_dir, exist_ok=True)
    script = (
        "def generate(ctx, params):\n"
        "    verb = str(params.get('verb', 'NOOP'))\n"
        "    arg = str(params.get('arg', ''))\n"
        "    line = verb if arg == '' else verb + ' ' + arg\n"
        "    ctx.send((line + '\\r\\n').encode())\n"
        "    ctx.recv(until=b'\\r\\n')\n"
    )
    with open(os.path.join(output_dir, "generator.py"), "w", encoding="utf-8") as handle:
        handle.write(script)
    with open(os.path.join(output_dir, "params.schema.json"), "w", encoding="utf-8") as handle:
        json.dump({
            "type": "object",
            "additionalProperties": False,
            "required": ["verb", "arg"],
            "properties": {"verb": {"type": "string"}, "arg": {"type": "string"}},
        }, handle)
    with open(os.path.join(output_dir, "params.json"), "w", encoding="utf-8") as handle:
        json.dump({"verb": "NOOP", "arg": ""}, handle)
    atomic(os.path.join(output_dir, "result.json"), {
        "request_id": request,
        "protocol_id": protocol,
        "trace_id": trace,
        "status": "ok",
        "error": None,
        "added_steps": ["open control connection"],
    })

marker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
buffer = ""
deadline = time.time() + 180
while time.time() < deadline:
    try:
        marker.sendto(b"HC-AGENT-MARKER", ("127.0.0.1", 9))
    except OSError:
        pass
    ready, _, _ = select.select([sys.stdin], [], [], 0.2)
    if not ready:
        continue
    chunk = os.read(sys.stdin.fileno(), 65536)
    if not chunk:
        time.sleep(0.2)
        continue
    buffer += chunk.decode("utf-8", "replace")
    if "WRITE ready.json" in buffer and "ready.json must include this session id." in buffer:
        write_ready(buffer)
        buffer = ""
    elif "REQUEST=" in buffer and "result.json in OUTPUT_DIR." in buffer:
        write_result(buffer)
        buffer = ""
"#;
