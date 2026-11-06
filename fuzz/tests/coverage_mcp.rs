//! End-to-end control-plane test: Rust client -> Python coverage MCP service.
//!
//! Skips when the campaign virtualenv or the `mcp` package is unavailable.

use std::{
    fs,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};

use mcn_fuzz::coverage::mcp::McpClient;
use mcn_fuzz::coverage::proposal::{sha256_file, Proposal, ProposalIdentity};
use mcn_fuzz::protocol::{load_model, write_catalog, Binding, PartyRole};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn root() -> PathBuf {
    std::env::temp_dir().join(format!(
        "mcn-mcp-it-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .subsec_nanos()
    ))
}

fn sha256(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn graphs_digest(root: &Path, paths: &[&str]) -> String {
    let mut sorted = paths.to_vec();
    sorted.sort();
    let mut hasher = Sha256::new();
    for relative in sorted {
        let raw = relative.as_bytes();
        hasher.update((raw.len() as u64).to_be_bytes());
        hasher.update(raw);
        let digest = sha256(&fs::read(root.join(relative)).unwrap());
        hasher.update((digest.len() as u64).to_be_bytes());
        hasher.update(digest.as_bytes());
    }
    format!("{:x}", hasher.finalize())
}

fn fixture(dir: &Path) -> Value {
    let source = dir.join("source");
    fs::create_dir_all(&source).unwrap();
    fs::write(source.join("a.c"), "int foo(int x) { return x ? 1 : 0; }\n").unwrap();
    let model_body = json!({
        "meta": {"schema_version": "7.0.0", "protocol": "demo", "complete": true, "documents": []},
        "parties": [{"name": "client", "description": "c"}, {"name": "server", "description": "s"}],
        "messages": [{"name": "hello", "associated_fields": []}],
        "channels": [{"name": "control", "description": "c", "transport": "TCP",
                      "connection": "connection_oriented",
                      "endpoint_party_names": ["client", "server"],
                      "initiator_party_name": "client", "acceptor_party_name": "server",
                      "endpoint_sources": []}],
        "steps": [{"id": "step-001", "name": "hello", "message_name": "hello",
                   "channel_name": "control", "sender_name": "client",
                   "receiver_name": "server", "condition": "start"}],
        "step_dependencies": [],
        "unresolved": []
    });
    let protocol = dir.join("protocol.json");
    fs::write(&protocol, model_body.to_string()).unwrap();
    let model = load_model(&protocol).unwrap();
    let binding = Binding::new(
        [
            ("client".to_string(), PartyRole::Generator),
            ("server".to_string(), PartyRole::Sut),
        ]
        .into_iter()
        .collect(),
    );
    let catalog = dir.join("catalog.json");
    write_catalog(&model, &binding, &catalog).unwrap();
    let handlers = dir.join("handlers.json");
    fs::write(
        &handlers,
        json!({
            "version": 1, "session": "s",
            "mappings": [{"message": "hello",
                          "handlers": [{"symbol": "foo", "location": "a.c:10",
                                        "evidence": "dispatch", "uncertain": false}],
                          "step_ids": [model.steps[0].id().0], "reason": null}]
        })
        .to_string(),
    )
    .unwrap();
    let cfg = dir.join("cfg");
    fs::create_dir_all(&cfg).unwrap();
    fs::write(
        cfg.join("source-map.json"),
        json!({"version": 1, "functions": {}}).to_string(),
    )
    .unwrap();
    fs::write(cfg.join(".foo.dot"),
        "digraph \"CFG for 'foo' function\" {\n\tNodeA [shape=record,label=\"{entry:\\l|{<s0>T|<s1>F}}\"];\n\tNodeA:s0 -> NodeB;\n\tNodeA:s1 -> NodeC;\n\tNodeB [shape=record,label=\"{yes:\\l|}\"];\n\tNodeC [shape=record,label=\"{no:\\l|}\"];\n}\n").unwrap();
    let callgraph = dir.join("callgraph.dot");
    fs::write(&callgraph,
        "digraph \"Call graph\" {\n\tNode1 [shape=record,label=\"{foo}\"];\n\tNode2 [shape=record,label=\"{bar}\"];\n\tNode1 -> Node2;\n}\n").unwrap();
    let binary = dir.join("fftp");
    fs::write(&binary, b"binary").unwrap();
    let manifest = dir.join("manifest.json");
    fs::write(&manifest, json!({
        "version": 1,
        "binary_sha256": sha256(b"binary"),
        "source_sha256": sha256(b"source"),
        "graphs_sha256": graphs_digest(dir, &["callgraph.dot", "cfg/.foo.dot", "cfg/source-map.json"]),
    }).to_string()).unwrap();
    let snapshot = dir.join("snapshots/000001");
    fs::create_dir_all(&snapshot).unwrap();
    fs::write(
        snapshot.join("metadata.json"),
        json!({
            "fingerprint": "build1", "binary": binary, "binary_sha256": sha256(b"binary")
        })
        .to_string(),
    )
    .unwrap();
    let branch = json!([10, 3, 10, 9, 1, 0, 0, 0, 4]);
    fs::write(
        snapshot.join("coverage.json"),
        json!({
            "data": [{"functions": [{"name": "foo", "count": 3,
                                     "filenames": [source.join("a.c")], "branches": [branch],
                                     "regions": []}],
                      "files": [{"filename": source.join("a.c"), "branches": [branch]}],
                      "totals": {"branches": {"count": 2, "covered": 1}}}],
            "type": "llvm.coverage.json.export", "version": "2.0.1"
        })
        .to_string(),
    )
    .unwrap();
    fs::write(snapshot.join("coverage.profdata.zlib"), b"not-zlib").unwrap();
    json!({
        "protocol": protocol, "catalog": catalog, "handlers": handlers, "cfg": cfg,
        "callgraph": callgraph, "manifest": manifest, "snapshot": snapshot,
        "source": source, "binary": binary,
        "protocol_id": model.protocol_id,
        "step_id": model.steps[0].id().0,
    })
}

fn wait_ready(socket: &Path) -> Option<McpClient> {
    let deadline = Instant::now() + Duration::from_secs(20);
    while Instant::now() < deadline {
        let client = McpClient::at(socket.to_path_buf());
        if client.health().is_ok() {
            return Some(client);
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    None
}

#[test]
fn rust_client_drives_the_python_service_and_validates_the_proposal() {
    let python = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(".venv/bin/python");
    if !python.is_file() {
        eprintln!("skipping: {} is missing", python.display());
        return;
    }
    let pythonpath = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("python");
    if !Command::new(&python)
        .arg("-c")
        .arg("import mcp, coverage_analysis.server")
        .env("PYTHONPATH", &pythonpath)
        .status()
        .map(|status| status.success())
        .unwrap_or(false)
    {
        eprintln!("skipping: mcp package or coverage_analysis is unavailable");
        return;
    }
    let dir = root();
    fs::create_dir_all(&dir).unwrap();
    let files = fixture(&dir);
    let socket = dir.join("mcp.sock");
    let state = dir.join("state");
    let mut child: Child = Command::new(&python)
        .arg("-m")
        .arg("coverage_analysis.server")
        .arg("serve")
        .arg("--socket")
        .arg(&socket)
        .arg("--state-dir")
        .arg(&state)
        .arg("--source-root")
        .arg(files["source"].as_str().unwrap())
        .current_dir(&pythonpath)
        .env("PYTHONPATH", &pythonpath)
        .env("PYTHONUNBUFFERED", "1")
        .stdout(Stdio::null())
        .stderr(Stdio::inherit())
        .spawn()
        .unwrap();
    let result = std::panic::catch_unwind(|| {
        let client = wait_ready(&socket).expect("service becomes ready");
        let request = json!({
            "request_id": "req-1", "session": "s", "build": "build1",
            "snapshot": "000001", "protocol_id": files["protocol_id"],
            "output_dir": dir.join("out"),
            "snapshot_dir": files["snapshot"], "manifest": files["manifest"],
            "model": files["protocol"], "catalog": files["catalog"],
            "handlers": files["handlers"], "source_dir": files["source"],
        });
        fs::create_dir_all(dir.join("out")).unwrap();
        client.register(&request).unwrap();
        client
            .tool(
                "load_protocol",
                json!({
            "protocol_path": files["protocol"], "catalog_path": files["catalog"]}),
            )
            .unwrap();
        client
            .tool("load_handlers", json!({"handlers_path": files["handlers"]}))
            .unwrap();
        client
            .tool(
                "load_cfg",
                json!({
            "cfg_dir": files["cfg"], "manifest_path": files["manifest"]}),
            )
            .unwrap();
        client
            .tool(
                "load_callgraph",
                json!({
            "callgraph_dot": files["callgraph"], "manifest_path": files["manifest"]}),
            )
            .unwrap();
        client
            .tool(
                "load_profdata",
                json!({
            "request_id": "req-1", "snapshot_dir": files["snapshot"],
            "manifest_path": files["manifest"]}),
            )
            .unwrap();
        let overview = client
            .tool("show_overview_code_coverage", json!({}))
            .unwrap();
        assert_eq!(overview["total_branches"], 2, "{overview}");
        assert_eq!(overview["covered_branches"], 1, "{overview}");
        client.begin("req-1").unwrap();
        let handlers = client
            .tool("show_handler_code_coverage", json!({}))
            .unwrap();
        let handler = handlers["handlers"]["items"][0]["handler"]["id"]
            .as_str()
            .unwrap()
            .to_string();
        let submitted = client
            .tool(
                "submit_proposal",
                json!({
                    "request_id": "req-1", "origin": "coverage",
                    "goal": {"handler_ids": [handler], "reason": "reach foo"},
                    "trace": [{"step_id": files["step_id"], "handler_ids": [handler],
                               "reason": "target", "constraints": []}],
                }),
            )
            .unwrap();
        let proposal_path = dir.join("out").join("proposal.json");
        let model = load_model(Path::new(files["protocol"].as_str().unwrap())).unwrap();
        let binding = Binding::new(
            [
                ("client".to_string(), PartyRole::Generator),
                ("server".to_string(), PartyRole::Sut),
            ]
            .into_iter()
            .collect(),
        );
        let proposal = Proposal::read(&proposal_path).unwrap();
        proposal
            .validate(
                &model,
                &binding,
                &ProposalIdentity {
                    session: "s".into(),
                    request_id: "req-1".into(),
                    build: "build1".into(),
                    snapshot: "000001".into(),
                    protocol_id: model.protocol_id.clone(),
                },
            )
            .unwrap();
        let digest = sha256_file(&proposal_path).unwrap();
        let committed = client
            .commit("req-1", submitted["proposal_id"].as_str().unwrap(), &digest)
            .unwrap();
        assert_eq!(committed["graph_revision"], 1);
        client.call(json!({"op": "shutdown"})).unwrap();
    });
    let _ = child.wait();
    let _ = fs::remove_dir_all(&dir);
    if let Err(panic) = result {
        std::panic::resume_unwind(panic);
    }
}
