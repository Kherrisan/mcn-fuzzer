use std::{
    fs,
    path::{Path, PathBuf},
    time::Duration,
};

use super::{agent::DirectAgentSession, process::Supervisor, task::trace_from_names, Options};
use crate::{
    app::config::Campaign,
    engine::synthesis::{ExecutionMode, SynthesisPromptContext},
    protocol::{load_model, StepTrace},
};

fn write_model(dir: &Path, steps: &str) -> PathBuf {
    let path = dir.join("protocol.json");
    fs::write(
        &path,
        format!(
            r#"{{"meta":{{"schema_version":"7.0.0","protocol":"demo","complete":false,"documents":[{{"doc_id":"d","rfc":"RFC 1","input_sha256":"aa","document_sha256":"bb","path":"doc.json"}}]}},"parties":[{{"name":"user","description":"u"}},{{"name":"server","description":"s"}}],"messages":[{{"name":"m","associated_fields":[]}}],"channels":[{{"name":"control","description":"c","transport":"TCP","connection":"connection_oriented","endpoint_party_names":["user","server"],"initiator_party_name":"user","acceptor_party_name":"server","endpoint_sources":[]}}],"steps":[{steps}],"step_dependencies":[],"unresolved":[]}}"#
        ),
    )
    .unwrap();
    path
}

fn step(name: &str, sender: &str, receiver: &str) -> String {
    format!(
        r#"{{"id":"{name}","name":"{name}","message_name":"m","channel_name":"control","sender_name":"{sender}","receiver_name":"{receiver}","condition":"c"}}"#
    )
}

fn campaign_text(dir: &Path, executable: &Path, provider: &str) -> String {
    format!(
        r#"
[target]
binary = "{bin}"
workdir = "{dir}/w"
fixture = "{dir}/fixture"
config_template = "{dir}/template.conf"
port = 0
[timeouts]
connect_ms = 200
io_ms = 200
case_ms = 800
exit_grace_ms = 200
[campaign]
seeds = "{dir}/s"
corpus = "{dir}/c"
crashes = "{dir}/cr"
python = "{python}"
pythonpath = "{pythonpath}"
[parties]
sut = ["server"]
generator = ["user"]
[protocol_generation]
enabled = false
model = "{dir}/protocol.json"
profile = "stub"
init_timeout_secs = 2
task_timeout_secs = 2
[agents.profiles.stub]
provider = "{provider}"
executable = "{exe}"
model = "stub-model"
effort = "low"
args = ["--last", "--model", "stolen"]
env = [["STUB_MODE", "ok"], ["TOKEN", "secret-token"]]
"#,
        bin = dir.join("sut.py").display(),
        dir = dir.display(),
        python = python_bin().display(),
        pythonpath = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("python")
            .display(),
        exe = executable.display(),
        provider = provider,
    )
}

fn python_bin() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(".venv/bin/python")
}

fn stub(dir: &Path) -> PathBuf {
    let path = dir.join("stub.py");
    fs::write(
        &path,
        r#"#!/usr/bin/env python3
import os, sys, time
args = sys.argv[1:]
open("argv.txt", "a").write("\n".join(args) + "\n---\n")
if args == ["--version"]:
    print("stub-1.0")
    raise SystemExit(0)
mode = os.environ.get("STUB_MODE", "ok")
if mode == "deny-login":
    print("authentication failed", file=sys.stderr)
    raise SystemExit(1)
if mode == "early":
    raise SystemExit(3)
if mode == "flood":
    sys.stdout.write("x" * (512 * 1024))
    sys.stdout.write("\n")
    sys.stdout.flush()
if mode == "sleep":
    time.sleep(30)
prompt = args[-1]
def field(name):
    for line in prompt.splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    return ""
session = field("SESSION") or "missing"
out = field("OUTPUT_DIR")
if "WRITE ready.json" in prompt:
    if "resume" in args:
        print("session mismatch", file=sys.stderr)
        raise SystemExit(4)
    os.makedirs(out, exist_ok=True)
    if mode != "no-ready":
        open(os.path.join(out, "ready.json"), "w").write(
            '{"status":"ready","session":"%s"}\n' % session
        )
    open("thread-id", "w").write(session)
    print('{"type":"thread.started","thread_id":"%s"}' % session)
    print('{"type":"turn.completed"}')
    print('{"session_id":"%s","type":"result","is_error":false}' % session)
    print('{"type":"session","id":"%s"}' % session)
    print('{"type":"done"}')
    raise SystemExit(0)
if "REQUEST=" in prompt:
    expected = open("thread-id").read().strip()
    if "resume" in args and expected not in args:
        print("session mismatch", file=sys.stderr)
        raise SystemExit(4)
    os.makedirs(out, exist_ok=True)
    open(os.path.join(out, "generator.py"), "w").write("def generate(ctx, params):\n    return None\n")
    open(os.path.join(out, "params.schema.json"), "w").write(
        '{"type":"object","additionalProperties":false,"required":["scenario"],"properties":{"scenario":{"type":"string"}}}'
    )
    open(os.path.join(out, "params.json"), "w").write('{"scenario":"login-quit"}')
    open(os.path.join(out, "result.json"), "w").write(
        '{"request_id":"%s","protocol_id":"%s","trace_id":"%s","status":"ok","added_steps":[]}'
        % (field("REQUEST"), field("PROTOCOL"), field("TRACE"))
    )
    print('{"type":"turn.completed"}')
    print('{"session_id":"%s","type":"result","is_error":false}' % expected)
    print('{"type":"done"}')
    raise SystemExit(0)
raise SystemExit(5)
"#,
    )
    .unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
    }
    path
}

fn layout(dir: &Path, provider: &str) {
    fs::create_dir_all(dir.join("fixture")).unwrap();
    fs::write(dir.join("fixture/hello.txt"), b"hello").unwrap();
    fs::write(dir.join("template.conf"), b"port=@PORT@\nroot=@ROOT@\n").unwrap();
    fs::write(dir.join("sut.py"), b"import sys, time\ntime.sleep(30)\n").unwrap();
    let steps = [
        step("greet", "server", "user"),
        step("ask", "user", "server"),
    ]
    .join(",");
    write_model(dir, &steps);
    fs::write(
        dir.join("suite.toml"),
        r#"
campaign = "campaign.toml"
ready_ms = 400
sut_exit_ms = 100
term_grace_ms = 100
prefix = ["greet"]
suffix = ["ask"]
[binding]
sut = ["server"]
generator = ["user"]
[[cases]]
name = "only"
steps = []
[[cases]]
name = "again"
steps = ["greet"]
"#,
    )
    .unwrap();
    fs::write(
        dir.join("campaign.toml"),
        campaign_text(dir, &stub(dir), provider),
    )
    .unwrap();
}

#[test]
fn trace_keeps_suite_order_and_rejects_bad_names() {
    let dir = std::env::temp_dir().join(format!("mcn-eval-trace-{}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(&dir).unwrap();
    let steps = [
        step("ask", "user", "server"),
        step("greet", "server", "user"),
    ]
    .join(",");
    let model = load_model(&write_model(&dir, &steps)).unwrap();
    let parties = crate::app::config::PartiesConfig {
        sut: crate::app::config::PartySelector::Names(vec!["server".into()]),
        generator: crate::app::config::PartySelector::Names(vec!["user".into()]),
    };
    let binding = crate::protocol::Binding::new(parties.resolve(&model.party_names()).unwrap());
    let trace = trace_from_names(
        &model,
        &binding,
        &["greet".into(), "ask".into(), "greet".into()],
    )
    .unwrap();
    assert_eq!(
        trace
            .steps
            .iter()
            .map(|step| step.name.as_str())
            .collect::<Vec<_>>(),
        ["greet", "ask", "greet"]
    );
    let again = trace_from_names(&model, &binding, &["greet".into(), "ask".into()]).unwrap();
    assert_eq!(again.steps[0].id(), trace.steps[0].id());
    assert_ne!(trace.id, again.id);
    assert!(trace_from_names(&model, &binding, &["missing".into()])
        .unwrap_err()
        .contains("not found"));
    let dup = [
        step("same", "user", "server"),
        step("same", "server", "user"),
    ]
    .join(",");
    let dup_error = load_model(&write_model(&dir, &dup)).unwrap_err();
    assert!(
        dup_error.message().contains("duplicate step name"),
        "{}",
        dup_error.message()
    );
    let sut_only = load_model(&write_model(&dir, &step("loop", "server", "server"))).unwrap();
    assert!(
        trace_from_names(&sut_only, &binding, &["loop".into()])
            .unwrap_err()
            .contains("not executable")
            || trace_from_names(&sut_only, &binding, &["loop".into()])
                .unwrap_err()
                .contains("does not cover")
    );
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn prompts_share_a_template_and_name_the_sut_owner() {
    let dir = std::env::temp_dir().join(format!("mcn-eval-prompt-{}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    layout(&dir, "codex");
    let path = dir.join("campaign.toml");
    let original = fs::read_to_string(&path).unwrap();
    for source_dir in [
        None,
        Some(PathBuf::from("private sut source tree")),
        Some(dir.join("private-sut-source")),
    ] {
        let text = match &source_dir {
            Some(source) => original.replace(
                "[target]",
                &format!("[target]\nsource_dir = {:?}", source.display().to_string()),
            ),
            None => original.clone(),
        };
        fs::write(&path, text).unwrap();
        let campaign = Campaign::load(&path).unwrap();
        let resolved = source_dir.as_ref().map(|source| dir.join(source));
        assert_eq!(campaign.target.source_dir, resolved);
        for (mode, owner) in [
            (ExecutionMode::Fuzzer, "fuzzer-managed SUT"),
            (ExecutionMode::Evaluation, "evaluation-managed SUT"),
        ] {
            let context = SynthesisPromptContext::from_campaign(&campaign)
                .unwrap()
                .with_execution(mode);
            let init = context
                .initialization_prompt("sess", false, &dir.join("init"))
                .unwrap();
            let trace = StepTrace::new(context.model().steps.clone());
            assert_eq!(context.model().protocol_id.len(), 8);
            assert_eq!(trace.id.0.len(), 8);
            assert!(trace.steps.iter().all(|step| step.id().0.len() == 8));
            let generation = context
                .generation_prompt(&dir.join("request"), "req", "sess", "", None, false)
                .unwrap();
            let skill = context.generator_skill();
            assert!(skill.is_absolute());
            let skill_text = fs::read_to_string(skill).unwrap();
            assert!(skill_text.starts_with("---\nname: protocol-modules\n"));
            assert!(init.contains("WRITE ready.json"));
            for prompt in [&init, &generation] {
                assert!(prompt.contains(owner));
                assert!(!prompt.contains("SUT source directory"));
                if let Some(source) = &resolved {
                    assert!(!prompt.contains(&source.display().to_string()));
                }
                assert!(prompt.contains("Do not read or search the SUT source code"));
                assert!(prompt.contains("Generate protocol behavior solely from the proposal"));
                assert!(prompt.contains("Do not assess which features the SUT implements or omits"));
                assert!(prompt.contains("not for predicted SUT rejection"));
                assert!(prompt.contains(&format!(
                    "Generator module skill (read-only): {}",
                    skill.display()
                )));
                assert!(!prompt.contains("secret-token"));
            }
        }
    }
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn changing_a_skill_reference_invalidates_a_prepared_task() {
    let dir = std::env::temp_dir().join(format!("mcn-eval-skill-{}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    layout(&dir, "codex");
    let tasks = super::prepare(&Options {
        suite: dir.join("suite.toml"),
        profiles: vec!["stub".into()],
        cases: vec!["only".into()],
        repeat: 1,
        out: Some(dir.join("out")),
    })
    .unwrap();
    let mut task = super::Task::load(&tasks[0]).unwrap();
    assert!(task.sources.generator_skill.as_ref().unwrap().is_file());
    task.ensure_digest().unwrap();

    // Use a private skill copy to avoid changing repository files or other tests.
    let skill = dir.join("skill/SKILL.md");
    fs::create_dir_all(dir.join("skill/references")).unwrap();
    fs::copy(task.sources.generator_skill.as_ref().unwrap(), &skill).unwrap();
    let reference = dir.join("skill/references/modules.md");
    fs::write(&reference, "available modules").unwrap();
    task.sources.generator_skill = Some(skill);
    task.digest = super::task::source_digest(&task.sources).unwrap();
    task.ensure_digest().unwrap();
    fs::write(reference, "updated module APIs").unwrap();
    assert!(task
        .ensure_digest()
        .unwrap_err()
        .contains("sources changed"));
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn stub_session_is_isolated_and_records_failures() {
    let dir = std::env::temp_dir().join(format!("mcn-eval-agent-{}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    for provider in ["codex", "claude", "pi"] {
        let root = dir.join(provider);
        layout(&root, provider);
        let tasks = super::prepare(&Options {
            suite: root.join("suite.toml"),
            profiles: vec!["stub".into()],
            cases: vec!["only".into()],
            repeat: 1,
            out: Some(root.join("out")),
        })
        .unwrap();
        let campaign = Campaign::load(&root.join("campaign.toml")).unwrap();
        assert!(!campaign.protocol_generation.enabled);
        let first = super::generate(&tasks[0]).unwrap();
        assert_eq!(first.stages["generate"].status, "passed");
        assert!(first.provider_session.is_some());
        let argv = fs::read_to_string(first.workspace().join("argv.txt")).unwrap();
        assert!(!argv.contains("--last"));
        assert!(!argv.contains("stolen"));
        assert!(!argv.contains("secret-token"));
        let other = dir.join(format!("{provider}-b"));
        layout(&other, provider);
        let other_tasks = super::prepare(&Options {
            suite: other.join("suite.toml"),
            profiles: vec!["stub".into()],
            cases: vec!["only".into()],
            repeat: 1,
            out: Some(other.join("out")),
        })
        .unwrap();
        let other_task = super::generate(&other_tasks[0]).unwrap();
        assert_ne!(first.provider_session, other_task.provider_session);
        assert_ne!(first.workspace(), other_task.workspace());
    }
    let tamper_root = dir.join("tamper");
    layout(&tamper_root, "codex");
    let tamper_tasks = super::prepare(&Options {
        suite: tamper_root.join("suite.toml"),
        profiles: vec!["stub".into()],
        cases: vec!["only".into()],
        repeat: 1,
        out: Some(tamper_root.join("out")),
    })
    .unwrap();
    let mut tamper = super::generate(&tamper_tasks[0]).unwrap();
    tamper.provider_session = Some("wrong-session".into());
    let campaign = Campaign::load(&tamper_root.join("campaign.toml")).unwrap();
    let prompt = fs::read_to_string(tamper.prepared().join("prompts/generation.md")).unwrap();
    let _ = fs::remove_dir_all(tamper.request_dir());
    let mut session = DirectAgentSession::new(
        &mut tamper,
        crate::app::config::AgentProvider::Codex,
        Duration::from_secs(2),
        campaign.agents.profiles["stub"].env.clone(),
    );
    let error = session.generate(&prompt).unwrap_err().1;
    assert!(
        error.contains("session mismatch") || error.contains("cli_exit"),
        "{error}"
    );
    let root = dir.join("failures");
    layout(&root, "codex");
    for (mode, needle) in [
        ("deny-login", "auth"),
        ("early", "cli_exit"),
        ("no-ready", "missing_ready"),
        ("sleep", "deadline"),
        ("flood", "passed"),
    ] {
        let case_dir = root.join(mode);
        layout(&case_dir, "codex");
        let text = fs::read_to_string(case_dir.join("campaign.toml")).unwrap();
        fs::write(
            case_dir.join("campaign.toml"),
            text.replace(
                "[\"STUB_MODE\", \"ok\"]",
                &format!("[\"STUB_MODE\", \"{mode}\"]"),
            ),
        )
        .unwrap();
        if mode == "sleep" {
            let text = fs::read_to_string(case_dir.join("campaign.toml")).unwrap();
            fs::write(
                case_dir.join("campaign.toml"),
                text.replace("init_timeout_secs = 2", "init_timeout_secs = 1"),
            )
            .unwrap();
        }
        let tasks = super::prepare(&Options {
            suite: case_dir.join("suite.toml"),
            profiles: vec!["stub".into()],
            cases: vec!["only".into()],
            repeat: 1,
            out: Some(case_dir.join("out")),
        })
        .unwrap();
        let result = super::generate(&tasks[0]);
        if needle == "passed" {
            assert!(result.is_ok(), "{mode}: {result:?}");
        } else {
            let error = result.unwrap_err();
            assert!(error.contains(needle), "{mode}: {error}");
        }
    }
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn validator_rejects_bad_artifacts_and_accepts_the_production_subset() {
    let dir = std::env::temp_dir().join(format!("mcn-eval-valid-{}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(&dir).unwrap();
    fs::write(
        dir.join("generator.py"),
        b"def generate(ctx, params):\n    return None\n",
    )
    .unwrap();
    fs::write(
        dir.join("params.schema.json"),
        br#"{"type":"object","additionalProperties":false,"required":["scenario"],"properties":{"scenario":{"type":"string"}},"$ref":"https://example.invalid/schema"}"#,
    )
    .unwrap();
    fs::write(dir.join("params.json"), br#"{"scenario":"login-quit"}"#).unwrap();
    fs::write(
        dir.join("result.json"),
        br#"{"request_id":"r","protocol_id":"p","trace_id":"t","status":"ok","added_steps":[]}"#,
    )
    .unwrap();
    let error =
        crate::engine::synthesis::validate_generator_artifacts(&dir, "r", "p", "t").unwrap_err();
    assert!(error.contains("$ref"), "{error}");
    fs::write(
        dir.join("params.schema.json"),
        br#"{"type":"object","additionalProperties":false,"required":["scenario"],"properties":{"scenario":{"type":"string"}}}"#,
    )
    .unwrap();
    fs::write(dir.join("result.json"), br#"{"request_id":"other","protocol_id":"p","trace_id":"t","status":"ok","added_steps":[]}"#).unwrap();
    let error =
        crate::engine::synthesis::validate_generator_artifacts(&dir, "r", "p", "t").unwrap_err();
    assert!(error.contains("does not match"), "{error}");
    fs::write(
        dir.join("result.json"),
        br#"{"request_id":"r","protocol_id":"p","trace_id":"t","status":"ok","added_steps":[]}"#,
    )
    .unwrap();
    fs::write(dir.join("params.json"), br#"{"scenario":1}"#).unwrap();
    assert!(crate::engine::synthesis::validate_generator_artifacts(&dir, "r", "p", "t").is_err());
    fs::write(dir.join("params.json"), br#"{"scenario":"login-quit"}"#).unwrap();
    let validated =
        crate::engine::synthesis::validate_generator_artifacts(&dir, "r", "p", "t").unwrap();
    assert!(validated.input.generator.ends_with("generator.py"));
    assert_eq!(validated.origin.protocol_id, "p");
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn supervisor_reaps_a_process_group_without_a_new_namespace() {
    let dir = std::env::temp_dir().join(format!("mcn-eval-proc-{}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(&dir).unwrap();
    let mut supervisor = Supervisor::new(vec!["secret-token".into()]);
    let pid = supervisor
        .spawn(&super::process::SpawnSpec {
            role: "sleeper".into(),
            argv: vec!["/bin/sleep".into(), "30".into()],
            env: vec![("TOKEN".into(), "secret-token".into())],
            cwd: dir.clone(),
            stdout_log: dir.join("out.log"),
            stderr_log: dir.join("err.log"),
            clear_afl: true,
        })
        .unwrap();
    let own = fs::read_link("/proc/self/ns/pid").unwrap();
    let child = fs::read_link(format!("/proc/{pid}/ns/pid")).unwrap();
    assert_eq!(own, child);
    supervisor.cleanup(Duration::from_millis(200)).unwrap();
    assert!(!Path::new(&format!("/proc/{pid}")).exists());
    let joined = supervisor
        .launched
        .iter()
        .map(|argv| argv.join(" "))
        .collect::<Vec<_>>()
        .join("\n");
    assert!(!joined.contains("forkserver"));
    assert!(!joined.contains("iceoryx"));
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn replay_classifies_sut_and_python_failures() {
    let dir = std::env::temp_dir().join(format!("mcn-eval-replay-{}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    layout(&dir, "codex");
    let tasks = super::prepare(&Options {
        suite: dir.join("suite.toml"),
        profiles: vec!["stub".into()],
        cases: vec!["only".into()],
        repeat: 1,
        out: Some(dir.join("out")),
    })
    .unwrap();
    let task_path = &tasks[0];
    super::generate(task_path).unwrap();
    super::validate(task_path).unwrap();
    let mut task = super::Task::load(task_path).unwrap();
    let exit_script = dir.join("exit-sut.py");
    fs::write(&exit_script, b"raise SystemExit(1)\n").unwrap();
    task.target.binary = python_bin();
    task.target.args = vec![exit_script.display().to_string()];
    task.save().unwrap();
    let error = super::replay(task_path).unwrap_err();
    assert!(
        error.contains("sut_start_failed") || error.contains("ready_timeout"),
        "{error}"
    );
    let saved = super::Task::load(task_path).unwrap();
    assert_eq!(saved.stages["replay"].status, "failed");
    assert!(saved.stages["replay"].duration_ms.unwrap_or(0) > 0);

    let sleep_script = dir.join("sleep-sut.py");
    fs::write(&sleep_script, b"import time\ntime.sleep(30)\n").unwrap();
    task.target.args = vec![sleep_script.display().to_string()];
    task.stages
        .insert("replay".into(), super::task::StageRecord::not_run());
    task.save().unwrap();
    let error = super::replay(task_path).unwrap_err();
    assert!(error.contains("ready_timeout"), "{error}");

    let listener = dir.join("listen-sut.py");
    fs::write(
        &listener,
        b"import socket, sys, time\ntext=open(sys.argv[1]).read()\nport=int([line.split('=',1)[1] for line in text.splitlines() if line.startswith('port=')][0])\ns=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(('127.0.0.1', port)); s.listen(1); time.sleep(30)\n",
    )
    .unwrap();
    task.target.binary = python_bin();
    task.target.args = vec![listener.display().to_string(), "@CONFIG@".into()];
    task.target.case_ms = 400;
    let request = task.request_dir();
    let raising = request.join("raising.py");
    fs::write(
        &raising,
        b"def generate(ctx, params):\n    raise RuntimeError('boom')\n",
    )
    .unwrap();
    publish_replacement(&mut task, task_path, &raising);
    let error = super::replay(task_path).unwrap_err();
    assert!(error.contains("python_exception"), "{error}");

    let hanging = request.join("hang.py");
    fs::write(
        &hanging,
        b"import time\ntime.sleep(30)\ndef generate(ctx, params):\n    return None\n",
    )
    .unwrap();
    task = super::Task::load(task_path).unwrap();
    publish_replacement(&mut task, task_path, &hanging);
    let error = super::replay(task_path).unwrap_err();
    assert!(error.contains("case_timeout"), "{error}");

    let missing = request.join("missing.py");
    fs::write(
        &missing,
        b"def generate(ctx, params):\n    import os\n    os._exit(0)\n",
    )
    .unwrap();
    task = super::Task::load(task_path).unwrap();
    publish_replacement(&mut task, task_path, &missing);
    let error = super::replay(task_path).unwrap_err();
    assert!(error.contains("missing_result"), "{error}");
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn replay_uses_campaign_environment_and_rendered_paths() {
    require_loopback();
    let dir = std::env::temp_dir().join(format!("mcn-eval-env-{}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    layout(&dir, "codex");
    let campaign = dir.join("campaign.toml");
    let text = fs::read_to_string(&campaign).unwrap().replace(
        "[target]",
        "[target]\nextra_env = [[\"EVAL_CONFIG\", \"@CONFIG@\"], [\"EVAL_PORT\", \"@PORT@\"]]",
    );
    fs::write(&campaign, text).unwrap();
    let tasks = super::prepare(&Options {
        suite: dir.join("suite.toml"),
        profiles: vec!["stub".into()],
        cases: vec!["only".into()],
        repeat: 1,
        out: Some(dir.join("out")),
    })
    .unwrap();
    let task_path = &tasks[0];
    super::generate(task_path).unwrap();
    let mut task = super::Task::load(task_path).unwrap();
    assert_eq!(task.target.extra_env.len(), 2);
    let sut = dir.join("env-sut.py");
    fs::write(
        &sut,
        b"import os,pathlib,socket,sys\nassert os.environ['EVAL_CONFIG'] == sys.argv[1]\nconfig=pathlib.Path(sys.argv[1]); assert config.is_file()\nport=int(os.environ['EVAL_PORT']); assert f'port={port}' in config.read_text()\ns=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(('127.0.0.1',port)); s.listen(1)\nconn,_=s.accept(); assert conn.recv(1)==b'x'; conn.close(); s.close()\nconfig.with_name('env-seen').write_text('ok')\n",
    )
    .unwrap();
    task.target.binary = python_bin();
    task.target.args = vec![sut.display().to_string(), "@CONFIG@".into()];
    task.target.ready_ms = 2000;
    task.target.sut_exit_ms = 1000;
    let generator = task.request_dir().join("connect.py");
    fs::write(
        &generator,
        b"def generate(ctx, params):\n    ctx.send(b'x')\n",
    )
    .unwrap();
    publish_replacement(&mut task, task_path, &generator);
    super::replay(task_path).unwrap();
    assert_eq!(
        fs::read_to_string(task.instance_dir.join("replays/1/work/env-seen")).unwrap(),
        "ok"
    );
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn lightftp_suite_prepares_every_case_without_rewriting_the_campaign() {
    let suite = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("evaluation/lightftp.toml");
    let campaign_path =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("targets/lightftp/campaign.toml");
    let before = fs::read(&campaign_path).unwrap();
    let out = std::env::temp_dir().join(format!("mcn-eval-ftp-{}", std::process::id()));
    let _ = fs::remove_dir_all(&out);
    let tasks = super::prepare(&Options {
        suite,
        profiles: vec!["codex".into()],
        cases: vec![],
        repeat: 1,
        out: Some(out.clone()),
    })
    .unwrap();
    assert_eq!(tasks.len(), 5);
    assert_eq!(fs::read(&campaign_path).unwrap(), before);
    let names = [
        "login-quit",
        "control-commands",
        "passive-list",
        "passive-retr",
        "active-stor",
    ];
    for (task_path, name) in tasks.iter().zip(names) {
        let task = super::Task::load(task_path).unwrap();
        assert_eq!(task.case_name, name);
        assert!(task.step_names.first().unwrap() == "service greeting");
        assert_eq!(task.step_names.last().unwrap(), "ordinary command reply");
        let trace: serde_json::Value =
            serde_json::from_slice(&fs::read(task.instance_dir.join("trace.json")).unwrap())
                .unwrap();
        let listed: Vec<&str> = trace["steps"]
            .as_array()
            .unwrap()
            .iter()
            .map(|step| step["name"].as_str().unwrap())
            .collect();
        assert_eq!(listed, task.step_names);
        assert!(!task.workspace().join("requests").exists());
        assert!(task.prepared().join("proposal.json").is_file());
        let prompt = fs::read_to_string(task.prepared().join("prompts/generation.md")).unwrap();
        assert!(prompt.contains("evaluation-managed SUT"));
        assert!(!prompt.contains("secret"));
    }
    let _ = fs::remove_dir_all(&out);
}

#[test]
fn handwritten_lightftp_generators_replay() {
    let suite = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("evaluation/lightftp.toml");
    let out = std::env::temp_dir().join(format!("mcn-eval-ftp-run-{}", std::process::id()));
    let _ = fs::remove_dir_all(&out);
    let tasks = super::prepare(&Options {
        suite,
        profiles: vec!["codex".into()],
        cases: vec![],
        repeat: 1,
        out: Some(out.clone()),
    })
    .unwrap();
    let generator = fs::read_to_string(
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("evaluation/fixtures/lightftp_generator.py"),
    )
    .unwrap();
    for task_path in &tasks {
        let task = super::Task::load(task_path).unwrap();
        let dir = task.request_dir();
        fs::create_dir_all(&dir).unwrap();
        fs::copy(
            task.prepared().join("proposal.json"),
            dir.join("proposal.json"),
        )
        .unwrap();
        fs::write(dir.join("generator.py"), &generator).unwrap();
        fs::write(
            dir.join("params.schema.json"),
            br#"{"type":"object","additionalProperties":false,"required":["scenario"],"properties":{"scenario":{"type":"string"},"upload_name":{"type":"string"},"download":{"type":"string"}}}"#,
        )
        .unwrap();
        let upload = format!("{}-upload.txt", task.case_name);
        let params = serde_json::json!({
            "scenario": task.case_name,
            "upload_name": upload,
            "download": "hello.txt",
        });
        fs::write(
            dir.join("params.json"),
            serde_json::to_vec(&params).unwrap(),
        )
        .unwrap();
        fs::write(
            dir.join("result.json"),
            serde_json::to_vec(&serde_json::json!({
                "request_id": task.request_id,
                "protocol_id": task.protocol_id,
                "trace_id": task.trace_id,
                "proposal_id": task.proposal_id,
                "status": "ok",
                "added_steps": [],
            }))
            .unwrap(),
        )
        .unwrap();
        super::validate(task_path).unwrap();
        super::replay(task_path).expect(&task.case_name);
    }
    let loaded: Vec<_> = tasks
        .iter()
        .map(|path| super::Task::load(path).unwrap())
        .collect();
    super::report::write_report(&out, &loaded).unwrap();
    let report: serde_json::Value =
        serde_json::from_slice(&fs::read(out.join("report.json")).unwrap()).unwrap();
    assert_eq!(report["planned"], 5);
    assert_eq!(report["artifact_pass"], 5);
    assert_eq!(report["runtime_pass"], 5);
    assert_eq!(report["passed"], 0);
    assert_eq!(report["schema_validation"], "production_subset");
    let _ = fs::remove_dir_all(&out);
}

fn proc_alive(pid: u32) -> bool {
    Path::new(&format!("/proc/{pid}")).exists()
}

#[test]
fn root_exit_does_not_wait_for_descendant_stdout() {
    let dir = std::env::temp_dir().join(format!("mcn-eval-drain-{}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(&dir).unwrap();
    let pidfile = dir.join("child.pid");
    let mut supervisor = Supervisor::new(Vec::new());
    let pid = supervisor
        .spawn(&super::process::SpawnSpec {
            role: "shell".into(),
            argv: vec![
                "/bin/sh".into(),
                "-c".into(),
                format!(
                    "echo hello-from-root; sleep 0.6 & echo $! > '{}'",
                    pidfile.display()
                ),
            ],
            env: Vec::new(),
            cwd: dir.clone(),
            stdout_log: dir.join("out.log"),
            stderr_log: dir.join("err.log"),
            clear_afl: false,
        })
        .unwrap();
    let started = std::time::Instant::now();
    let status = supervisor
        .wait_for(pid, Duration::from_millis(1500))
        .unwrap();
    let elapsed = started.elapsed();
    assert!(status.success(), "{status:?}");
    assert!(
        elapsed < Duration::from_millis(400),
        "root wait blocked on inherited stdout: {elapsed:?}"
    );
    supervisor
        .finish_child_logs(pid, Duration::from_millis(500))
        .unwrap();
    let log = fs::read_to_string(dir.join("out.log")).unwrap_or_default();
    assert!(log.contains("hello-from-root"), "{log}");
    let child: u32 = fs::read_to_string(&pidfile)
        .unwrap()
        .trim()
        .parse()
        .unwrap();
    assert!(proc_alive(child), "descendant should still be running");
    supervisor.cleanup(Duration::from_millis(200)).unwrap();
    assert!(!proc_alive(child), "descendant survived cleanup");
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn cleanup_kills_orphans_and_term_ignorers() {
    let dir = std::env::temp_dir().join(format!("mcn-eval-orphan-{}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(&dir).unwrap();
    let pidfile = dir.join("orphan.pid");
    let mut supervisor = Supervisor::new(Vec::new());
    let root = supervisor
        .spawn(&super::process::SpawnSpec {
            role: "shell".into(),
            argv: vec![
                "/bin/sh".into(),
                "-c".into(),
                format!(
                    "sleep 30 </dev/null >/dev/null 2>&1 & echo $! > '{}'; exit 0",
                    pidfile.display()
                ),
            ],
            env: Vec::new(),
            cwd: dir.clone(),
            stdout_log: dir.join("out.log"),
            stderr_log: dir.join("err.log"),
            clear_afl: false,
        })
        .unwrap();
    supervisor.wait_for(root, Duration::from_secs(2)).unwrap();
    let orphan: u32 = fs::read_to_string(&pidfile)
        .unwrap()
        .trim()
        .parse()
        .unwrap();
    assert!(proc_alive(orphan));
    supervisor.cleanup(Duration::from_millis(200)).unwrap();
    assert!(!proc_alive(orphan), "orphan survived group cleanup");

    let mut ignoring = Supervisor::new(Vec::new());
    let sleeper = ignoring
        .spawn(&super::process::SpawnSpec {
            role: "ignore-term".into(),
            argv: vec![
                python_bin().display().to_string(),
                "-c".into(),
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
                    .into(),
            ],
            env: Vec::new(),
            cwd: dir.clone(),
            stdout_log: dir.join("ignore.out"),
            stderr_log: dir.join("ignore.err"),
            clear_afl: false,
        })
        .unwrap();
    assert!(proc_alive(sleeper));
    ignoring.cleanup(Duration::from_millis(150)).unwrap();
    assert!(!proc_alive(sleeper), "TERM-ignoring process survived KILL");

    let mut other = Supervisor::new(Vec::new());
    let bystander = other
        .spawn(&super::process::SpawnSpec {
            role: "bystander".into(),
            argv: vec!["/bin/sleep".into(), "30".into()],
            env: Vec::new(),
            cwd: dir.clone(),
            stdout_log: dir.join("by.out"),
            stderr_log: dir.join("by.err"),
            clear_afl: false,
        })
        .unwrap();
    supervisor.cleanup(Duration::from_millis(100)).unwrap();
    assert!(
        proc_alive(bystander),
        "cleanup touched a process from another supervisor"
    );
    other.cleanup(Duration::from_millis(200)).unwrap();
    assert!(!proc_alive(bystander));
    let _ = fs::remove_dir_all(&dir);
}

fn publish_replacement(task: &mut super::Task, task_path: &Path, source: &Path) {
    let request = task.request_dir();
    let _ = fs::remove_dir_all(request.join("published"));
    fs::copy(source, request.join("generator.py")).unwrap();
    task.validation_digest = None;
    task.stages
        .insert("validate".into(), super::task::StageRecord::not_run());
    task.stages
        .insert("replay".into(), super::task::StageRecord::not_run());
    task.save().unwrap();
    super::validate(task_path).unwrap();
}

fn require_loopback() {
    match std::net::TcpListener::bind("127.0.0.1:0") {
        Ok(listener) => drop(listener),
        Err(error)
            if error.kind() == std::io::ErrorKind::PermissionDenied
                || error.raw_os_error() == Some(1) =>
        {
            panic!("ENVIRONMENT: loopback bind not permitted: {error}");
        }
        Err(error) => panic!("loopback bind failed: {error}"),
    }
}

#[test]
fn late_sut_crash_is_not_a_pass() {
    require_loopback();
    let dir = std::env::temp_dir().join(format!("mcn-eval-crash-{}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    layout(&dir, "codex");
    let tasks = super::prepare(&Options {
        suite: dir.join("suite.toml"),
        profiles: vec!["stub".into()],
        cases: vec!["only".into()],
        repeat: 1,
        out: Some(dir.join("out")),
    })
    .unwrap();
    let task_path = &tasks[0];
    super::generate(task_path).unwrap();
    super::validate(task_path).unwrap();
    let mut task = super::Task::load(task_path).unwrap();
    let sut = dir.join("crash-sut.py");
    fs::write(
        &sut,
        b"import os,signal,socket,sys,time\ntext=open(sys.argv[1]).read()\nport=int([line.split('=',1)[1] for line in text.splitlines() if line.startswith('port=')][0])\ns=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(('127.0.0.1', port)); s.listen(1)\nconn,_=s.accept(); time.sleep(0.3); os.kill(os.getpid(), signal.SIGSEGV)\n",
    )
    .unwrap();
    let generator = task.request_dir().join("connect.py");
    fs::write(
        &generator,
        b"def generate(ctx, params):\n    ctx.send(b'x')\n",
    )
    .unwrap();
    let mut stored: serde_json::Value =
        serde_json::from_slice(&fs::read(task.request_dir().join("input.json")).unwrap()).unwrap();
    stored["generator"] = serde_json::Value::String(generator.display().to_string());
    fs::write(
        task.request_dir().join("input.json"),
        serde_json::to_vec(&stored).unwrap(),
    )
    .unwrap();
    task.target.binary = python_bin();
    task.target.args = vec![sut.display().to_string(), "@CONFIG@".into()];
    task.target.sut_exit_ms = 1500;
    task.stages
        .insert("replay".into(), super::task::StageRecord::not_run());
    // The published snapshot no longer matches input.json, so replay must reject
    // this tamper. The crash case below restores a matching digest first.
    let error = super::replay(task_path).unwrap_err();
    assert!(error.contains("artifact_changed"), "{error}");
    // Re-validate is impossible while published exists and the digest mismatches.
    // Point the stored digest at the tampered files by clearing it and removing
    // published copies so a fresh validate publishes the crash generator.
    let request = task.request_dir();
    let _ = fs::remove_dir_all(request.join("published"));
    fs::copy(&generator, request.join("generator.py")).unwrap();
    task.validation_digest = None;
    task.save().unwrap();
    super::validate(task_path).unwrap();
    let error = super::replay(task_path).unwrap_err();
    assert!(error.contains("sut_crash"), "{error}");
    let saved = super::Task::load(task_path).unwrap();
    assert_eq!(saved.stages["replay"].status, "failed");
    assert_ne!(
        saved.stages["replay"].failure_kind.as_deref(),
        Some("passed")
    );
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn tampered_published_snapshot_is_rejected() {
    let dir = std::env::temp_dir().join(format!("mcn-eval-tamper-{}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    layout(&dir, "codex");
    let tasks = super::prepare(&Options {
        suite: dir.join("suite.toml"),
        profiles: vec!["stub".into()],
        cases: vec!["only".into()],
        repeat: 1,
        out: Some(dir.join("out")),
    })
    .unwrap();
    let task_path = &tasks[0];
    super::generate(task_path).unwrap();
    super::validate(task_path).unwrap();
    let task = super::Task::load(task_path).unwrap();
    let request = task.request_dir();
    let backup = dir.join("backup");
    fs::create_dir_all(&backup).unwrap();
    for name in [
        "published/generator.py",
        "published/params.schema.json",
        "input.json",
    ] {
        let from = request.join(name);
        let to = backup.join(name.replace('/', "_"));
        fs::copy(&from, &to).unwrap();
    }
    let restore = || {
        for name in [
            "published/generator.py",
            "published/params.schema.json",
            "input.json",
        ] {
            fs::copy(backup.join(name.replace('/', "_")), request.join(name)).unwrap();
        }
    };
    fs::write(
        request.join("published/generator.py"),
        b"def generate(ctx, params):\n    return 'changed'\n",
    )
    .unwrap();
    let error = super::validate(task_path).unwrap_err();
    assert!(error.contains("artifact_changed"), "{error}");
    let error = super::replay(task_path).unwrap_err();
    assert!(error.contains("artifact_changed"), "{error}");
    restore();
    fs::write(
        request.join("published/params.schema.json"),
        br#"{"type":"object","tampered":true}"#,
    )
    .unwrap();
    assert!(super::validate(task_path)
        .unwrap_err()
        .contains("artifact_changed"));
    restore();
    let mut input: serde_json::Value =
        serde_json::from_slice(&fs::read(request.join("input.json")).unwrap()).unwrap();
    input["params"]["scenario"] = serde_json::Value::String("changed".into());
    fs::write(
        request.join("input.json"),
        serde_json::to_vec(&input).unwrap(),
    )
    .unwrap();
    assert!(super::replay(task_path)
        .unwrap_err()
        .contains("artifact_changed"));
    restore();
    input["generator"] = serde_json::Value::String("/tmp/not-the-published-generator.py".into());
    fs::write(
        request.join("input.json"),
        serde_json::to_vec(&input).unwrap(),
    )
    .unwrap();
    assert!(super::validate(task_path)
        .unwrap_err()
        .contains("artifact_changed"));
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn report_requires_every_stage_and_matches_run() {
    let dir = std::env::temp_dir().join(format!("mcn-eval-report-{}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    layout(&dir, "codex");
    let tasks = super::prepare(&Options {
        suite: dir.join("suite.toml"),
        profiles: vec!["stub".into()],
        cases: vec!["only".into()],
        repeat: 1,
        out: Some(dir.join("out")),
    })
    .unwrap();
    let mut skipped = super::Task::load(&tasks[0]).unwrap();
    skipped.stages.insert(
        "validate".into(),
        super::task::StageRecord::passed(1, Vec::new()),
    );
    skipped.stages.insert(
        "replay".into(),
        super::task::StageRecord::passed(1, Vec::new()),
    );
    let mut failed_generate = skipped.clone();
    failed_generate.stages.insert(
        "generate".into(),
        super::task::StageRecord::failed(5, "cli_exit", "stub failed"),
    );
    let out = dir.join("report");
    super::report::write_report(&out, &[skipped.clone(), failed_generate]).unwrap();
    let report: serde_json::Value =
        serde_json::from_slice(&fs::read(out.join("report.json")).unwrap()).unwrap();
    assert_eq!(report["artifact_pass"], 2);
    assert_eq!(report["runtime_pass"], 2);
    assert_eq!(report["passed"], 0);
    assert!(!super::report::instance_passed(&skipped));
    skipped.stages.insert(
        "generate".into(),
        super::task::StageRecord::passed(1, Vec::new()),
    );
    assert!(super::report::instance_passed(&skipped));
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn preflight_obeys_deadline_and_reaps_its_child() {
    let dir = std::env::temp_dir().join(format!("mcn-eval-version-{}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    layout(&dir, "codex");
    let pidfile = dir.join("version.pid");
    fs::write(dir.join("stub.py"), format!(
        "#!/usr/bin/env python3\nimport os,time\nopen({:?}, 'w').write(str(os.getpid()))\ntime.sleep(30)\n",
        pidfile.display().to_string(),
    )).unwrap();
    let paths = super::prepare(&Options {
        suite: dir.join("suite.toml"),
        profiles: vec!["stub".into()],
        cases: vec!["only".into()],
        repeat: 1,
        out: Some(dir.join("out")),
    })
    .unwrap();
    let mut task = super::Task::load(&paths[0]).unwrap();
    let started = std::time::Instant::now();
    {
        let mut session = DirectAgentSession::new(
            &mut task,
            crate::app::config::AgentProvider::Codex,
            Duration::from_millis(150),
            Vec::new(),
        );
        let (kind, _) = session.preflight().unwrap_err();
        assert_eq!(kind, "deadline");
    }
    assert!(started.elapsed() < Duration::from_secs(2));
    let pid = fs::read_to_string(&pidfile).unwrap();
    assert!(!Path::new(&format!("/proc/{}", pid.trim())).exists());
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn completed_child_logs_include_tail_with_inherited_pipes() {
    let dir = std::env::temp_dir().join(format!("mcn-eval-log-tail-{}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(&dir).unwrap();
    let mut supervisor = Supervisor::new(Vec::new());
    let pid = supervisor.spawn(&super::process::SpawnSpec {
        role: "log-writer".into(),
        argv: vec![python_bin().display().to_string(), "-c".into(),
            "import os,time\nos.write(1,b'x'*65536+b'completion-marker\\n')\nos.write(2,b'y'*65536+b'stderr-tail\\n')\nif os.fork()==0:\n time.sleep(30)\n os._exit(0)".into()],
        env: Vec::new(), cwd: dir.clone(), stdout_log: dir.join("out"),
        stderr_log: dir.join("err"), clear_afl: false,
    }).unwrap();
    supervisor.wait_for(pid, Duration::from_secs(2)).unwrap();
    let started = std::time::Instant::now();
    supervisor
        .finish_child_logs(pid, Duration::from_millis(500))
        .unwrap();
    assert!(started.elapsed() < Duration::from_secs(1));
    assert_eq!(
        fs::read(dir.join("out")).unwrap(),
        [vec![b'x'; 65536], b"completion-marker\n".to_vec()].concat()
    );
    assert_eq!(
        fs::read(dir.join("err")).unwrap(),
        [vec![b'y'; 65536], b"stderr-tail\n".to_vec()].concat()
    );
    supervisor.cleanup(Duration::from_millis(100)).unwrap();
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn adopted_setsid_zombie_is_reaped() {
    const CHILD: &str = "MCN_EVAL_ZOMBIE_TEST_CHILD";
    if std::env::var_os(CHILD).is_none() {
        // Subreaper is process-wide: exercise adoption in a fresh test process.
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "evaluation::tests::adopted_setsid_zombie_is_reaped",
                "--nocapture",
            ])
            .env(CHILD, "1")
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        return;
    }
    Supervisor::become_subreaper();
    let dir = std::env::temp_dir().join(format!("mcn-eval-zombie-{}", std::process::id()));
    fs::create_dir_all(&dir).unwrap();
    let pidfile = dir.join("orphan.pid");
    let mut supervisor = Supervisor::new(Vec::new());
    let root = supervisor.spawn(&super::process::SpawnSpec {
        role: "orphan-parent".into(),
        argv: vec![python_bin().display().to_string(), "-c".into(), format!(
            "import os\npid=os.fork()\nif pid==0:\n os.setsid()\n os._exit(0)\nopen({:?}, 'w').write(str(pid))", pidfile.display().to_string())],
        env: Vec::new(), cwd: dir.clone(), stdout_log: dir.join("out"),
        stderr_log: dir.join("err"), clear_afl: false,
    }).unwrap();
    supervisor.wait_for(root, Duration::from_secs(2)).unwrap();
    let pid: i32 = fs::read_to_string(pidfile).unwrap().trim().parse().unwrap();
    let stat = format!("/proc/{pid}/stat");
    let deadline = std::time::Instant::now() + Duration::from_secs(2);
    loop {
        let text = fs::read_to_string(&stat).unwrap();
        if text
            .rsplit_once(')')
            .unwrap()
            .1
            .trim_start()
            .starts_with('Z')
        {
            break;
        }
        assert!(
            std::time::Instant::now() < deadline,
            "child never became a zombie"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
    let cleanup = supervisor.cleanup(Duration::from_millis(100));
    let remains = Path::new(&stat).exists();
    // Reap on failure as well, so a failing regression leaves no orphan behind.
    if remains {
        unsafe {
            libc::waitpid(pid, std::ptr::null_mut(), 0);
        }
    }
    assert!(cleanup.is_ok(), "{cleanup:?}");
    assert!(!remains, "setsid zombie survived successful cleanup");
    fs::remove_dir_all(dir).unwrap();
}
