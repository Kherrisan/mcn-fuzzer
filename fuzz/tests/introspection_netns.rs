//! Regression for the introspection listener across network namespaces.
//!
//! Direct `CLONE_NEWNET` needs `CAP_SYS_ADMIN` in the current user namespace.
//! When that is missing, only the test child is started with
//! `unshare --user --map-root-user`. Inside that user namespace the child is
//! uid 0, so after exec it can still bind `127.0.0.1:0` and then call
//! `CLONE_NEWNET`. `--map-current-user` keeps a non-zero uid and can drop that
//! capability. The parent stays in the original user and network namespaces,
//! checks that the netns inode changed and the bound port did not, and
//! completes MCP `initialize` and `list_proposals`. This test does not call
//! sudo and does not start HC. A real HC client belongs to the opt-in
//! private-core test (`MCN_HC_INTEGRATION=1`).

use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    net::{SocketAddr, TcpListener, TcpStream},
    path::Path,
    process::{Child, Command, Stdio},
    time::Duration,
};

use mcn_fuzz::coverage::introspection::{
    bind_host_listener, http::IntrospectionHttp, take_host_listener, CampaignIdentity,
    IntrospectionHandle,
};

fn main() {
    match std::env::args().nth(1).as_deref() {
        Some("check-newnet") => check_newnet(),
        Some("serve") => serve_prebound(),
        Some("isolated") => bind_after_isolation(),
        _ => parent(),
    }
}

fn check_newnet() {
    match nix::sched::unshare(nix::sched::CloneFlags::CLONE_NEWNET) {
        Ok(()) => println!("newnet-ok"),
        Err(error) => println!("newnet-failed {error}"),
    }
}

fn serve_prebound() {
    bind_host_listener().expect("bind host listener");
    let listener = take_host_listener().expect("prepared listener");
    let port = listener.local_addr().expect("port").port();
    println!("bound {port}");
    let _ = std::io::stdout().flush();
    if let Err(error) = nix::sched::unshare(nix::sched::CloneFlags::CLONE_NEWNET) {
        println!("unshare-failed {error}");
        return;
    }
    println!("netns {}", current_netns());
    let _ = std::io::stdout().flush();
    serve_listener(listener);
}

fn serve_listener(listener: TcpListener) {
    let port = listener.local_addr().expect("port").port();
    let handle = IntrospectionHandle::open(
        CampaignIdentity {
            run_id: "netns".into(),
            started_at_unix_ms: 1,
        },
        None,
    );
    handle
        .register_coverage("alpha", "s", "000001")
        .expect("register");
    let http = IntrospectionHttp::start(handle.store(), listener).expect("adopt listener");
    assert!(
        http.url().ends_with(&format!(":{port}/mcp")),
        "adopting the listener changed its port: {}",
        http.url()
    );
    println!("ready {port}");
    let _ = std::io::stdout().flush();
    let mut line = String::new();
    let _ = std::io::stdin().read_line(&mut line);
    http.shutdown();
}

fn bind_after_isolation() {
    if let Err(error) = nix::sched::unshare(nix::sched::CloneFlags::CLONE_NEWNET) {
        println!("unshare-failed {error}");
        return;
    }
    println!("netns {}", current_netns());
    let _ = std::io::stdout().flush();
    match TcpListener::bind("127.0.0.1:0") {
        Ok(listener) => {
            let port = listener.local_addr().expect("port").port();
            println!("isolated-bound {port}");
            let _ = std::io::stdout().flush();
            let mut line = String::new();
            let _ = std::io::stdin().read_line(&mut line);
            drop(listener);
        }
        Err(error) => println!("isolated-bind-failed {error}"),
    }
}

fn current_netns() -> String {
    fs::read_link("/proc/self/ns/net")
        .map(|path| path.display().to_string())
        .unwrap_or_else(|error| format!("unreadable:{error}"))
}

fn parent() {
    let exe = std::env::current_exe().expect("test executable");
    let user_ns = !direct_newnet(&exe);
    if user_ns {
        eprintln!(
            "direct CLONE_NEWNET is not permitted; only the test child uses unshare --user --map-root-user"
        );
    }
    if !probe_isolated(&exe, user_ns) {
        eprintln!(
            "netns limitation: could not create a network namespace; verified pre-bound listener adoption in this namespace only"
        );
        takeover_in_this_namespace();
        return;
    }
    let host_netns = current_netns();
    let mut child = spawn_probe(&exe, "serve", user_ns);
    let stdout = child.stdout.take().expect("child stdout");
    let mut lines = BufReader::new(stdout).lines();
    let bound = lines.next().expect("bound line").expect("read bound");
    if bound.starts_with("unshare-failed") {
        eprintln!(
            "netns limitation: {bound}; verified pre-bound listener adoption in this namespace only"
        );
        let _ = child.wait();
        takeover_in_this_namespace();
        return;
    }
    let netns = lines.next().expect("netns line").expect("read netns");
    if netns.starts_with("unshare-failed") {
        eprintln!(
            "netns limitation: {netns}; verified pre-bound listener adoption in this namespace only"
        );
        let _ = child.wait();
        takeover_in_this_namespace();
        return;
    }
    let ready = lines.next().expect("ready line").expect("read ready");
    let bound_port = line_value(&bound, "bound").expect("bound port");
    let ready_port = line_value(&ready, "ready").expect("ready port");
    assert_eq!(
        bound_port, ready_port,
        "listener port changed from {bound_port} to {ready_port}"
    );
    let child_netns = netns
        .strip_prefix("netns ")
        .expect("netns line")
        .trim()
        .to_string();
    assert_ne!(
        child_netns, host_netns,
        "server child stayed in the parent netns {host_netns}"
    );
    let body = mcp_list(ready_port);
    assert!(
        body.contains("coverage:alpha"),
        "original-netns MCP client did not see the pre-bound service: {body}"
    );
    assert!(
        body.contains("netns"),
        "list_proposals response did not carry this run: {body}"
    );
    let mut stdin = child.stdin.take().expect("child stdin");
    let _ = writeln!(stdin, "done");
    let _ = child.wait();
}

fn direct_newnet(exe: &Path) -> bool {
    let output = Command::new(exe)
        .arg("check-newnet")
        .output()
        .expect("spawn newnet check");
    String::from_utf8_lossy(&output.stdout).contains("newnet-ok")
}

/// A listener created after `CLONE_NEWNET` must not answer on the original
/// `127.0.0.1`. Returns false when this process cannot enter a new netns.
fn probe_isolated(exe: &Path, user_ns: bool) -> bool {
    let mut child = spawn_probe(exe, "isolated", user_ns);
    let stdout = child.stdout.take().expect("isolated stdout");
    let mut lines = BufReader::new(stdout).lines();
    let first = lines.next().and_then(Result::ok).unwrap_or_default();
    if first.starts_with("unshare-failed") || first.is_empty() {
        let _ = child.wait();
        return false;
    }
    let line = if first.starts_with("netns ") {
        lines.next().and_then(Result::ok).unwrap_or_default()
    } else {
        first
    };
    if let Some(port) = line_value(&line, "isolated-bound") {
        let refused = TcpStream::connect_timeout(
            &SocketAddr::from(([127, 0, 0, 1], port)),
            Duration::from_millis(200),
        )
        .is_err();
        assert!(
            refused,
            "a listener bound after CLONE_NEWNET was reachable on the original 127.0.0.1:{port}"
        );
    } else if line.starts_with("isolated-bind-failed") {
        let _ = child.wait();
        return false;
    }
    drop(child.stdin.take());
    let _ = child.wait();
    true
}

fn spawn_probe(exe: &Path, mode: &str, user_ns: bool) -> Child {
    let mut command = if user_ns {
        let mut command = Command::new("unshare");
        command.args([
            "--user",
            "--map-root-user",
            "--",
            exe.to_str().expect("test executable path is utf-8"),
            mode,
        ]);
        command
    } else {
        let mut command = Command::new(exe);
        command.arg(mode);
        command
    };
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .unwrap_or_else(|error| panic!("spawn {mode} probe: {error}"))
}

fn takeover_in_this_namespace() {
    bind_host_listener().expect("bind");
    let listener = take_host_listener().expect("take");
    let port = listener.local_addr().expect("port").port();
    let handle = IntrospectionHandle::open(
        CampaignIdentity {
            run_id: "local".into(),
            started_at_unix_ms: 1,
        },
        None,
    );
    handle
        .register_coverage("alpha", "s", "000001")
        .expect("register");
    let http = IntrospectionHttp::start(handle.store(), listener).expect("start");
    assert!(
        http.url().ends_with(&format!(":{port}/mcp")),
        "{}",
        http.url()
    );
    let body = mcp_list(port);
    assert!(body.contains("coverage:alpha"), "{body}");
    http.shutdown();
}

fn line_value(text: &str, key: &str) -> Option<u16> {
    text.lines().find_map(|line| {
        let rest = line.trim().strip_prefix(key)?.trim();
        rest.parse().ok()
    })
}

fn mcp_list(port: u16) -> String {
    let init = mcp_exchange(
        port,
        None,
        r#"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"netns-probe","version":"0"}}}"#,
    );
    let session = init
        .lines()
        .find_map(|line| {
            let (name, value) = line.split_once(':')?;
            name.eq_ignore_ascii_case("mcp-session-id")
                .then(|| value.trim().to_string())
        })
        .unwrap_or_else(|| panic!("initialize did not return a session: {init}"));
    let _ = mcp_exchange(
        port,
        Some(&session),
        r#"{"jsonrpc":"2.0","method":"notifications/initialized"}"#,
    );
    mcp_exchange(
        port,
        Some(&session),
        r#"{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_proposals","arguments":{}}}"#,
    )
}

fn mcp_exchange(port: u16, session: Option<&str>, body: &str) -> String {
    let mut stream = TcpStream::connect_timeout(
        &SocketAddr::from(([127, 0, 0, 1], port)),
        Duration::from_secs(2),
    )
    .unwrap_or_else(|error| panic!("connect 127.0.0.1:{port}: {error}"));
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    let mut head = format!(
        "POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\nAccept: application/json, text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n",
        body.len()
    );
    if let Some(session) = session {
        head.push_str(&format!("Mcp-Session-Id: {session}\r\n"));
    }
    head.push_str("\r\n");
    stream.write_all(head.as_bytes()).unwrap();
    stream.write_all(body.as_bytes()).unwrap();
    let mut buf = Vec::new();
    let mut tmp = [0u8; 4096];
    loop {
        match stream.read(&mut tmp) {
            Ok(0) => break,
            Ok(count) => buf.extend_from_slice(&tmp[..count]),
            Err(error)
                if error.kind() == std::io::ErrorKind::WouldBlock
                    || error.kind() == std::io::ErrorKind::TimedOut =>
            {
                break;
            }
            Err(error) => panic!("{error}"),
        }
    }
    String::from_utf8_lossy(&buf).into_owned()
}
