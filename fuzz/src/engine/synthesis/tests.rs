use serde_json::Value;
use std::collections::BTreeMap;
use std::sync::mpsc;
use std::time::Duration;

use libafl_bolts::rands::StdRand;

use super::*;
use crate::{
    agents::{clear_boot_for_test, install_boot_for_test, BootRecord, MockCore},
    app::config::{
        AgentCatalog, AgentProfile, AgentProvider, Campaign, CampaignPaths, CaptureConfig,
        ProtocolGenerationConfig, SourceCoverageConfig, TargetConfig, Timeouts,
    },
    engine::mutator::mutate_generator,
};

struct BootGuard;
impl Drop for BootGuard {
    fn drop(&mut self) {
        clear_boot_for_test();
    }
}

fn bare(output: &Path) -> Campaign {
    Campaign {
        target: TargetConfig {
            binary: PathBuf::from("/bin/true"),
            source_dir: None,
            workdir: PathBuf::from("/tmp"),
            fixture: PathBuf::from("/tmp"),
            config_template: PathBuf::from("/tmp/fftp.conf"),
            config_output: "fftp.conf".to_string(),
            args: vec!["@CONFIG@".to_string()],
            cert: "certs/my.crt".to_string(),
            key: "certs/my.pem".to_string(),
            ca: None,
            host: "127.0.0.1".to_string(),
            port: 0,
            extra_env: Vec::new(),
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
        capture: CaptureConfig::default(),
        coverage: SourceCoverageConfig::default(),
        _legacy_source_coverage: (),
        protocol_generation: ProtocolGenerationConfig {
            enabled: false,
            model: PathBuf::from("/no/such/protocol.json"),
            profile: "mock".to_string(),
            output: output.to_path_buf(),
            _legacy_bindings: (),
            init_timeout_secs: 1,
            task_timeout_secs: 1,
            trace_len_min: 1,
            trace_len_max: 1,
            sample_attempts: 32,
            _legacy_hc: (),
        },
        agents: AgentCatalog {
            hc_executable: PathBuf::from("hc"),
            profiles: BTreeMap::from([(
                "mock".to_string(),
                AgentProfile {
                    provider: AgentProvider::Codex,
                    executable: PathBuf::from("/bin/true"),
                    model: None,
                    effort: None,
                    args: Vec::new(),
                    env: Vec::new(),
                },
            )]),
        },
        parties: None,
        base: PathBuf::from("/tmp"),
    }
}

fn client_server_parties() -> crate::app::config::PartiesConfig {
    crate::app::config::PartiesConfig {
        sut: crate::app::config::PartySelector::Names(vec!["server".to_string()]),
        generator: crate::app::config::PartySelector::Names(vec!["client".to_string()]),
    }
}

fn model_file(dir: &Path) -> PathBuf {
    let path = dir.join("protocol.json");
    let steps: Vec<_> = (0..8)
        .map(|index| {
            serde_json::json!({
                "id": format!("step-{index:04}"),
                "name": format!("step-{index}"),
                "message_name": "ping",
                "channel_name": "control",
                "sender_name": "client",
                "receiver_name": "server",
                "condition": "start",
            })
        })
        .collect();
    let body = serde_json::json!({
        "meta": {"schema_version": "7.0.0", "protocol": "demo", "complete": false, "documents": [{"doc_id": "d", "rfc": "RFC 1", "input_sha256": "aa", "document_sha256": "bb", "path": "doc.json"}]},
        "parties": [{"name": "client", "description": "c"}, {"name": "server", "description": "s"}],
        "messages": [{"name": "ping", "associated_fields": []}],
        "channels": [{"name": "control", "description": "c", "transport": "TCP", "connection": "connection_oriented", "endpoint_party_names": ["client", "server"], "initiator_party_name": "client", "acceptor_party_name": "server", "endpoint_sources": []}],
        "steps": steps,
        "step_dependencies": [{"prior_step": "step-0", "dependent_step": "step-0", "condition": "self", "field_relations": []}],
        "unresolved": [{"description": "open", "object_names": ["ping"]}],
    });
    std::fs::write(&path, serde_json::to_vec(&body).unwrap()).unwrap();
    path
}

fn wait_out(stage: &mut GeneratorSynthesisStage, budget: Duration) -> Outcome {
    let deadline = Instant::now() + budget;
    while Instant::now() < deadline {
        if let Some(outcome) = stage.recv_one(Duration::from_millis(40)) {
            return outcome;
        }
    }
    panic!("synthesis outcome was not ready within {budget:?}");
}

fn request_count(output: &Path) -> usize {
    std::fs::read_dir(output.join("requests"))
        .map(|entries| {
            entries
                .flatten()
                .filter(|entry| entry.path().is_dir())
                .count()
        })
        .unwrap_or(0)
}

#[test]
fn disabled_campaign_does_not_load_a_protocol_model() {
    let mut stage =
        GeneratorSynthesisStage::from_campaign(&bare(Path::new("/tmp/mcn-synth-off"))).unwrap();
    assert!(!stage.enabled);
    stage.initialize().unwrap();
}

#[test]
fn bitmap_stagnation_orders_stages_without_queuing_while_busy() {
    use std::cell::Cell;

    use libafl::{
        corpus::{CorpusId, InMemoryCorpus},
        fuzzer::{Evaluator, ExecuteInputResult},
        mutators::{MutationResult, Mutator},
        state::StdState,
    };

    use crate::{
        coverage::{CoverageHost, LlvmCoverageStage},
        engine::mutator::GeneratorMutator,
    };

    struct Host(Cell<usize>);
    impl CoverageHost for Host {
        fn main_forkserver_pid(&self) -> i32 {
            self.0.set(self.0.get() + 1);
            0
        }
        fn main_generator_pid(&self) -> i32 {
            0
        }
        fn coverage_map_prefix(&self) -> Vec<u8> {
            vec![1, 1]
        }
    }

    struct NoEvaluation;
    impl<S> Evaluator<Host, (), GeneratorInput, S> for NoEvaluation {
        fn evaluate_filtered(
            &mut self,
            _: &mut S,
            _: &mut Host,
            _: &mut (),
            _: &GeneratorInput,
        ) -> Result<(ExecuteInputResult, Option<CorpusId>), Error> {
            panic!("no synthesis result was delivered")
        }
        fn evaluate_input(
            &mut self,
            _: &mut S,
            _: &mut Host,
            _: &mut (),
            _: &GeneratorInput,
        ) -> Result<(ExecuteInputResult, Option<CorpusId>), Error> {
            panic!("no synthesis result was delivered")
        }
        fn add_input(
            &mut self,
            _: &mut S,
            _: &mut Host,
            _: &mut (),
            _: GeneratorInput,
        ) -> Result<CorpusId, Error> {
            unreachable!()
        }
        fn add_disabled_input(&mut self, _: &mut S, _: GeneratorInput) -> Result<CorpusId, Error> {
            unreachable!()
        }
    }

    for (coverage_enabled, init_failure) in [
        (false, None),
        (true, None),
        (false, Some("timeout")),
        (false, Some("wrong-session")),
        (false, Some("disconnected")),
    ] {
        let dir = std::env::temp_dir().join(format!(
            "mcn-stagnation-order-{}-{coverage_enabled}-{init_failure:?}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let mut campaign = bare(&dir.join("out"));
        campaign.protocol_generation.model = model_file(&dir);
        campaign.parties = Some(client_server_parties());
        campaign.coverage.enabled = coverage_enabled;
        // Failure must also advance the trigger without starting a coverage target.
        campaign.coverage.binary = dir.join("missing-coverage-binary");
        let context = SynthesisPromptContext::from_campaign(&campaign).unwrap();
        let mut synthesis = GeneratorSynthesisStage::from_campaign(&campaign).unwrap();
        synthesis.enabled = true;
        synthesis.model = Some(context.model().clone());
        synthesis.binding = context.binding().clone();
        synthesis.prompt_context = Some(context);
        let (commands, requests) = mpsc::channel();
        let (outcome_tx, outcomes) = mpsc::channel();
        synthesis.worker = Some(AgentClient::from_channels(commands, outcomes));
        synthesis.launch = Some(
            command_line(
                campaign.agents.profiles.get("mock").unwrap(),
                &campaign.protocol_generation.output,
            )
            .unwrap(),
        );
        // No worker is receiving yet: initialization must only queue the command.
        synthesis.initialize().unwrap();
        assert!(matches!(
            requests.try_recv().unwrap(),
            AgentCommand::Init { .. }
        ));
        synthesis.initialize().unwrap();
        assert!(
            requests.try_recv().is_err(),
            "initialization must be queued once"
        );
        assert!(!synthesis.initialized);
        let mut coverage = LlvmCoverageStage::from_campaign(&campaign);
        let mut state = StdState::new(
            StdRand::with_seed(7),
            InMemoryCorpus::<GeneratorInput>::new(),
            InMemoryCorpus::<GeneratorInput>::new(),
            &mut (),
            &mut (),
        )
        .unwrap();
        state.add_metadata(BitmapProgress::new(3).unwrap());
        state
            .metadata_mut::<BitmapProgress>()
            .unwrap()
            .observe(&[1, 1], true);
        let mut fuzzer = NoEvaluation;
        let mut host = Host(Cell::new(0));
        let mut mutator = GeneratorMutator::new();
        let mut input = GeneratorInput::new(
            PathBuf::from("generator.py"),
            BTreeMap::from([("n".to_string(), ParamValue::Int(1))]),
        );

        // Entering the first fuzz round alone must not start synthesis.
        synthesis
            .perform(&mut fuzzer, &mut host, &mut state, &mut ())
            .unwrap();
        assert!(requests.try_recv().is_err());
        for _ in 0..2 {
            assert_eq!(
                mutator.mutate(&mut state, &mut input).unwrap(),
                MutationResult::Mutated
            );
            state
                .metadata_mut::<BitmapProgress>()
                .unwrap()
                .observe(&[1, 1], true);
            mutator.post_exec(&mut state, None).unwrap();
        }
        coverage
            .perform(&mut fuzzer, &mut host, &mut state, &mut ())
            .unwrap();
        assert_eq!(host.0.get(), 0);
        assert!(!state.metadata::<BitmapProgress>().unwrap().synthesis_due());

        mutator.mutate(&mut state, &mut input).unwrap();
        state
            .metadata_mut::<BitmapProgress>()
            .unwrap()
            .observe(&[1, 1], true);
        mutator.post_exec(&mut state, None).unwrap();
        synthesis
            .perform(&mut fuzzer, &mut host, &mut state, &mut ())
            .unwrap();
        assert!(
            requests.try_recv().is_err(),
            "source coverage must come first"
        );
        coverage
            .perform(&mut fuzzer, &mut host, &mut state, &mut ())
            .unwrap();
        assert_eq!(host.0.get(), usize::from(coverage_enabled));
        assert!(state.metadata::<BitmapProgress>().unwrap().synthesis_due());
        assert!(
            requests.try_recv().is_err(),
            "coverage must not dispatch synthesis itself"
        );

        // An outstanding trigger survives skipped rounds while init is pending.
        for _ in 0..3 {
            synthesis
                .perform(&mut fuzzer, &mut host, &mut state, &mut ())
                .unwrap();
            assert!(requests.try_recv().is_err());
            assert!(state.metadata::<BitmapProgress>().unwrap().synthesis_due());
            assert_eq!(request_count(&campaign.protocol_generation.output), 0);
        }
        if let Some(reason) = init_failure {
            if reason == "disconnected" {
                drop(outcome_tx);
            } else if reason == "timeout" {
                outcome_tx
                    .send(Outcome::Failed(AgentFailure {
                        reason: "initialization timed out".to_string(),
                        fact: TerminalFact::default(),
                        timeout: true,
                    }))
                    .unwrap();
            } else {
                std::fs::write(
                    campaign.protocol_generation.output.join("init/ready.json"),
                    br#"{"status":"ready","session":"another-campaign"}"#,
                )
                .unwrap();
                outcome_tx
                    .send(Outcome::Ready((), TerminalFact::default()))
                    .unwrap();
            }
            // Timeouts retry initialization; other init failures stop the campaign.
            let result = synthesis.perform(&mut fuzzer, &mut host, &mut state, &mut ());
            if reason == "timeout" {
                result.unwrap();
                assert!(matches!(
                    requests.try_recv().unwrap(),
                    AgentCommand::Init { .. }
                ));
            } else if reason == "disconnected" {
                assert!(result.is_err());
            } else {
                result.unwrap();
            }
            synthesis
                .perform(&mut fuzzer, &mut host, &mut state, &mut ())
                .unwrap();
            assert_eq!(synthesis.paused, reason != "timeout");
            assert!(!synthesis.initialized);
            assert!(requests.try_recv().is_err());
            assert!(!state.metadata::<BitmapProgress>().unwrap().synthesis_due());
            assert!(!state
                .metadata::<SynthesisProgress>()
                .unwrap()
                .failures
                .is_empty());
            mutator.mutate(&mut state, &mut input).unwrap();
            state
                .metadata_mut::<BitmapProgress>()
                .unwrap()
                .observe(&[1, 1], true);
            mutator.post_exec(&mut state, None).unwrap();
            assert_eq!(
                state
                    .metadata::<BitmapProgress>()
                    .unwrap()
                    .consecutive_no_coverage,
                1
            );
            drop(synthesis);
            let _ = std::fs::remove_dir_all(dir);
            continue;
        }
        std::fs::write(
            campaign.protocol_generation.output.join("init/ready.json"),
            serde_json::to_vec(
                &serde_json::json!({"status": "ready", "session": synthesis.session}),
            )
            .unwrap(),
        )
        .unwrap();
        outcome_tx
            .send(Outcome::Ready((), TerminalFact::default()))
            .unwrap();
        synthesis
            .perform(&mut fuzzer, &mut host, &mut state, &mut ())
            .unwrap();
        assert!(matches!(
            requests.try_recv().unwrap(),
            AgentCommand::Run { .. }
        ));
        assert!(synthesis.inflight);
        assert_eq!(request_count(&campaign.protocol_generation.output), 1);

        // Even more than k valid mutations, including a newly discovered slot,
        // cannot create another trigger while the first task is running.
        for _ in 0..10 {
            mutator.mutate(&mut state, &mut input).unwrap();
            state
                .metadata_mut::<BitmapProgress>()
                .unwrap()
                .observe(&[1, 1, 1], true);
            mutator.post_exec(&mut state, None).unwrap();
            coverage
                .perform(&mut fuzzer, &mut host, &mut state, &mut ())
                .unwrap();
            synthesis
                .perform(&mut fuzzer, &mut host, &mut state, &mut ())
                .unwrap();
            let progress = state.metadata::<BitmapProgress>().unwrap();
            assert_eq!(progress.consecutive_no_coverage, 0);
            assert!(!progress.source_coverage_due());
            assert!(!progress.synthesis_due());
            assert!(requests.try_recv().is_err());
        }
        assert_eq!(host.0.get(), usize::from(coverage_enabled));

        // A completed refusal still releases the counter, without immediately
        // dispatching a replacement. Only a fresh k executions can do that.
        outcome_tx
            .send(Outcome::Artifact {
                request: "test-request".into(),
                result: Err("refused".to_string()),
                fact: TerminalFact::default(),
            })
            .unwrap();
        synthesis
            .perform(&mut fuzzer, &mut host, &mut state, &mut ())
            .unwrap();
        assert!(!synthesis.inflight);
        assert!(requests.try_recv().is_err());
        for expected in 1..=3 {
            synthesis
                .perform(&mut fuzzer, &mut host, &mut state, &mut ())
                .unwrap();
            mutator.mutate(&mut state, &mut input).unwrap();
            state
                .metadata_mut::<BitmapProgress>()
                .unwrap()
                .observe(&[1, 1, 1], true);
            mutator.post_exec(&mut state, None).unwrap();
            assert_eq!(
                state
                    .metadata::<BitmapProgress>()
                    .unwrap()
                    .consecutive_no_coverage,
                expected
            );
            coverage
                .perform(&mut fuzzer, &mut host, &mut state, &mut ())
                .unwrap();
            assert!(requests.try_recv().is_err());
        }
        assert_eq!(host.0.get(), 2 * usize::from(coverage_enabled));
        assert!(state.metadata::<BitmapProgress>().unwrap().synthesis_due());
        synthesis
            .perform(&mut fuzzer, &mut host, &mut state, &mut ())
            .unwrap();
        assert!(matches!(
            requests.try_recv().unwrap(),
            AgentCommand::Run { .. }
        ));
        assert_eq!(request_count(&campaign.protocol_generation.output), 2);
        assert!(!state.metadata::<BitmapProgress>().unwrap().synthesis_due());
        synthesis
            .perform(&mut fuzzer, &mut host, &mut state, &mut ())
            .unwrap();
        assert!(requests.try_recv().is_err());
        drop(synthesis);
        let _ = std::fs::remove_dir_all(&dir);
    }
}

#[test]
fn bootstrap_outcomes_clear_the_inflight_request() {
    for scenario in ["success", "refusal", "failure"] {
        let dir = std::env::temp_dir().join(format!(
            "mcn-bootstrap-finish-{}-{scenario}",
            std::process::id()
        ));
        let mut stage = GeneratorSynthesisStage::from_campaign(&bare(&dir)).unwrap();
        stage.enabled = true;
        stage.initialized = true;
        stage.inflight = true;
        stage.current_request = Some("bootstrap".to_string());
        let (sender, outcomes) = mpsc::channel();
        let (commands, _requests) = mpsc::channel();
        stage.worker = Some(AgentClient::from_channels(commands, outcomes));
        let outcome = match scenario {
            "success" => Outcome::Artifact {
                request: "test-request".into(),
                result: Ok(ReadyInput {
                    request: "bootstrap".to_string(),
                    input: GeneratorInput::new(dir.join("generator.py"), BTreeMap::new()),
                    origin: TraceOrigin {
                        protocol_id: "p".to_string(),
                        trace_id: "t".to_string(),
                        step_ids: Vec::new(),
                        added_steps: Vec::new(),
                    },
                }),
                fact: TerminalFact::default(),
            },
            "refusal" => Outcome::Artifact {
                request: "test-request".into(),
                result: Err("refused".to_string()),
                fact: TerminalFact::default(),
            },
            _ => Outcome::Failed(AgentFailure {
                reason: "failed".to_string(),
                fact: TerminalFact::default(),
                timeout: false,
            }),
        };
        sender.send(outcome).unwrap();
        let result = stage.bootstrap_first(&mut StdRand::with_seed(1), |_, _| Ok(true));
        assert_eq!(result.is_ok(), scenario == "success");
        assert!(!stage.inflight);
        assert!(stage.current_request.is_none());
        assert!(stage.closed_requests.contains("bootstrap"));
        assert_eq!(stage.paused, scenario == "failure");
        drop(stage);
        let _ = std::fs::remove_dir_all(dir);
    }
}

#[test]
fn closed_request_pauses_further_dispatch() {
    let mut stage =
        GeneratorSynthesisStage::from_campaign(&bare(Path::new("/tmp/mcn-synth-closed"))).unwrap();
    stage.enabled = true;
    stage.current_request = Some("req".to_string());
    stage.inflight = true;
    stage.finish_request(true);
    assert!(stage.paused);
    assert!(stage.closed_requests.contains("req"));
    let mut rand = StdRand::with_seed(1);
    stage.dispatch_with(&mut rand).unwrap();
    assert!(stage.current_request.is_none());
}

#[test]
fn artifact_schema_rejects_unrepresentable_params_and_republish() {
    let dir = std::env::temp_dir().join(format!("mcn-artifact-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let request = GeneratorRequest {
        id: "r".to_string(),
        output_dir: dir.clone(),
        protocol_id: "p".to_string(),
        trace_id: "t".to_string(),
    };
    std::fs::write(
        dir.join("generator.py"),
        b"def generate(ctx, params):\n    return None\n",
    )
    .unwrap();
    std::fs::write(
        dir.join("params.schema.json"),
        br#"{"type":"object","additionalProperties":true}"#,
    )
    .unwrap();
    std::fs::write(dir.join("params.json"), br#"{"verb":1.5}"#).unwrap();
    std::fs::write(
        dir.join("result.json"),
        br#"{"request_id":"r","protocol_id":"p","trace_id":"t","status":"ok","added_steps":[]}"#,
    )
    .unwrap();
    let error = request.accept().unwrap_err();
    assert!(error.contains("integer"), "{error}");
    std::fs::write(dir.join("params.json"), br#"{"verb":{"nested":true}}"#).unwrap();
    let error = request.accept().unwrap_err();
    assert!(error.contains("not representable"), "{error}");
    std::fs::write(dir.join("params.json"), br#"{"verb":null}"#).unwrap();
    assert!(request.accept().is_err());
    std::fs::write(
        dir.join("params.schema.json"),
        br#"{"type":"object","additionalProperties":false,"required":["verb"],"properties":{"verb":{"type":"string"}}}"#,
    )
    .unwrap();
    std::fs::write(dir.join("params.json"), br#"{"verb":1.5}"#).unwrap();
    let error = request.accept().unwrap_err();
    assert!(error.contains("string"), "{error}");
    std::fs::write(
        dir.join("generator.py"),
        b"def other(ctx, params):\n    return None\n",
    )
    .unwrap();
    std::fs::write(dir.join("params.json"), br#"{"verb":"NOOP"}"#).unwrap();
    let error = request.accept().unwrap_err();
    assert!(error.contains("generate"), "{error}");
    std::fs::write(
        dir.join("generator.py"),
        b"def generate(ctx, params):\n    return None\n",
    )
    .unwrap();
    let ready = request.accept().unwrap();
    assert!(ready.input.generator.ends_with("generator.py"));
    let error = request.accept().unwrap_err();
    assert!(error.contains("already exists"), "{error}");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn mock_core_covers_init_delay_failure_timeout_and_schema() {
    let _guard = BootGuard;
    let root = std::env::temp_dir().join(format!("mcn-mock-hc-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    std::fs::create_dir_all(&root).unwrap();
    let socket = root.join("core.sock");
    let core = MockCore::spawn(socket.clone()).unwrap();
    install_boot_for_test(BootRecord {
        socket,
        session: "sess-test".to_string(),
    });
    let mut campaign = bare(&root.join("out"));
    campaign.protocol_generation.enabled = true;
    campaign.protocol_generation.model = model_file(&root);
    campaign.parties = Some(client_server_parties());
    let mut stage = GeneratorSynthesisStage::from_campaign(&campaign).unwrap();
    stage.initialize().unwrap();
    stage.wait_initialized().unwrap();
    let ready: Value =
        serde_json::from_slice(&std::fs::read(root.join("out/init/ready.json")).unwrap()).unwrap();
    assert_eq!(ready["session"], "sess-test");
    assert!(!root.join("out/ready.json").exists());
    assert_eq!(stage.session_state.phase, SessionPhase::Ready);
    assert_eq!(stage.session_state.transition, "ready");
    assert_eq!(stage.session_state.hc_state.as_deref(), Some("live"));
    assert_eq!(stage.session_state.hc_status.as_deref(), Some("idle"));
    let session_log = std::fs::read_to_string(root.join("out/session.jsonl")).unwrap();
    assert!(session_log.contains("\"transition\":\"launch\""));
    assert!(session_log.contains("\"transition\":\"ready\""));
    assert!(session_log.contains("\"phase\":\"initializing\""));

    let mut rand = StdRand::with_seed(11);
    stage.mode_hint = "delay".to_string();
    let started = Instant::now();
    stage.dispatch_with(&mut rand).unwrap();
    assert_eq!(stage.session_state.phase, SessionPhase::Busy);
    assert_eq!(stage.session_state.transition, "send");
    assert_eq!(stage.session_state.hc_status.as_deref(), Some("idle"));
    assert!(stage.inflight);
    assert!(stage.recv_one(Duration::from_millis(30)).is_none());
    assert!(started.elapsed() < Duration::from_millis(250));
    let queued = request_count(&root.join("out"));
    stage.dispatch_with(&mut rand).unwrap();
    assert_eq!(request_count(&root.join("out")), queued);
    let (first_request, first_output_dir, first_artifacts) =
        match wait_out(&mut stage, Duration::from_secs(3)) {
            Outcome::Artifact {
                result: Ok(ready), ..
            } => {
                assert_eq!(
                    ready.input.params.get("verb"),
                    Some(&ParamValue::Str("NOOP".to_string()))
                );
                assert!(!ready.origin.added_steps.is_empty());
                let mut mutated = ready.input.clone();
                let before = mutated.params.get("verb").cloned();
                let mut mutator_rand = StdRand::with_seed(9);
                assert_eq!(
                    mutate_generator(&mut mutator_rand, &mut mutated),
                    libafl::mutators::MutationResult::Mutated
                );
                assert_eq!(mutated.generator, ready.input.generator);
                assert_ne!(mutated.params.get("verb"), before.as_ref());
                let output_dir = root.join("out/requests").join(&ready.request);
                assert_eq!(
                    ready.input.generator,
                    output_dir.join("published/generator.py")
                );
                let artifacts = [
                    "receipt.json",
                    "generator.py",
                    "params.schema.json",
                    "params.json",
                    "result.json",
                    "published/generator.py",
                    "published/params.schema.json",
                ]
                .into_iter()
                .map(|name| (name, std::fs::read(output_dir.join(name)).unwrap()))
                .collect::<Vec<_>>();
                (ready.request, output_dir, artifacts)
            }
            other => panic!("delay mode did not publish an artifact: {other:?}"),
        };
    stage.finish_request(false);

    stage.mode_hint.clear();
    stage.dispatch_with(&mut rand).unwrap();
    match wait_out(&mut stage, Duration::from_secs(3)) {
        Outcome::Artifact {
            result: Ok(ready), ..
        } => {
            let output_dir = root.join("out/requests").join(&ready.request);
            assert_ne!(ready.request, first_request);
            assert_ne!(output_dir, first_output_dir);
            assert_eq!(
                ready.input.generator,
                output_dir.join("published/generator.py")
            );
            let result: Value =
                serde_json::from_slice(&std::fs::read(output_dir.join("result.json")).unwrap())
                    .unwrap();
            assert_eq!(result["request_id"], ready.request);
            for (name, bytes) in &first_artifacts {
                assert_eq!(std::fs::read(first_output_dir.join(name)).unwrap(), *bytes);
            }
        }
        other => panic!("second request did not publish an artifact: {other:?}"),
    }
    for name in [
        "ready.json",
        "receipt.json",
        "generator.py",
        "params.schema.json",
        "params.json",
        "result.json",
    ] {
        assert!(
            !root.join("out").join(name).exists(),
            "{name} was written into the fixed campaign workspace"
        );
    }
    stage.finish_request(false);

    for (mode, needle) in [
        ("fail", "agent failed"),
        ("schema", "remote"),
        ("nostatus", "failed"),
        ("stale", "does not match"),
    ] {
        stage.mode_hint = mode.to_string();
        stage.dispatch_with(&mut rand).unwrap();
        match wait_out(&mut stage, Duration::from_secs(3)) {
            Outcome::Artifact {
                result: Err(reason),
                fact,
                ..
            } => {
                assert!(reason.contains(needle), "{mode}: {reason}");
                stage.record_session("blocked", SessionPhase::Ready, &fact, Some(reason));
                stage.finish_request(false);
            }
            other => panic!("{mode} returned {other:?}"),
        }
    }

    stage.mode_hint = "silent".to_string();
    stage.dispatch_with(&mut rand).unwrap();
    match wait_out(&mut stage, Duration::from_secs(3)) {
        Outcome::Failed(failure) => {
            assert!(failure.reason.contains("timed out"), "{}", failure.reason);
            assert!(failure.timeout);
            stage.note_terminal_failure(&failure);
            stage.finish_request(true);
        }
        other => panic!("silent mode returned {other:?}"),
    }
    let after_pause = request_count(&root.join("out"));
    stage.dispatch_with(&mut rand).unwrap();
    assert_eq!(request_count(&root.join("out")), after_pause);
    drop(stage);

    let mut held = bare(&root.join("hold"));
    held.protocol_generation.enabled = true;
    held.protocol_generation.model = root.join("protocol.json");
    held.parties = Some(client_server_parties());
    let mut held_stage = GeneratorSynthesisStage::from_campaign(&held).unwrap();
    held_stage.init_hold = true;
    held_stage.initialize().unwrap();
    assert!(!held_stage.initialized);
    let error = held_stage.wait_initialized().expect_err("held init");
    assert!(
        format!("{error:?}").contains("initialization") || format!("{error:?}").contains("timed")
    );
    assert!(held_stage.paused);
    drop(held_stage);
    drop(core);
    let held_log = std::fs::read_to_string(root.join("hold/session.jsonl")).unwrap_or_default();
    assert!(held_log.contains("\"phase\":\"paused\""));
    assert!(
        held_log.contains("\"transition\":\"timeout\"")
            || held_log.contains("\"transition\":\"blocked\"")
    );
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn session_snapshot_retains_raw_status_and_omits_env_values() {
    use libafl::prelude::HasMetadata;
    use libafl_bolts::serdeany::SerdeAnyMap;

    struct Meta(SerdeAnyMap);
    impl HasMetadata for Meta {
        fn metadata_map(&self) -> &SerdeAnyMap {
            &self.0
        }
        fn metadata_map_mut(&mut self) -> &mut SerdeAnyMap {
            &mut self.0
        }
    }

    let dir = std::env::temp_dir().join(format!("mcn-session-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let mut stage = GeneratorSynthesisStage::from_campaign(&bare(&dir)).unwrap();
    stage.enabled = true;
    stage.secrets = vec!["super-secret-token".to_string()];
    let raw = TerminalFact {
        terminal: Some("t1".to_string()),
        state: Some("live".to_string()),
        status: Some("{\"state\":\"blocked\",\"token\":\"super-secret-token\"}".to_string()),
        error: Some("terminal blocked super-secret-token".to_string()),
    };
    stage.record_session("launch", SessionPhase::Initializing, &raw, None);
    stage.record_session("ready", SessionPhase::Ready, &raw, None);
    stage.record_session("send", SessionPhase::Busy, &TerminalFact::default(), None);
    stage.record_session(
        "result",
        SessionPhase::Ready,
        &TerminalFact {
            status: Some("idle".to_string()),
            ..TerminalFact::default()
        },
        None,
    );
    stage.record_session(
        "blocked",
        SessionPhase::Ready,
        &raw,
        Some(raw.error.clone().unwrap()),
    );
    assert_eq!(stage.session_state.phase, SessionPhase::Ready);
    assert_eq!(stage.session_state.transition, "blocked");
    assert_eq!(
        stage.session_state.hc_status.as_deref(),
        Some("{\"state\":\"blocked\",\"token\":\"[redacted]\"}")
    );
    assert_eq!(
        stage.session_state.hc_error.as_deref(),
        Some("terminal blocked [redacted]")
    );
    stage.record_session(
        "timeout",
        SessionPhase::Paused,
        &TerminalFact {
            status: Some("idle".to_string()),
            ..TerminalFact::default()
        },
        Some("request timed out".to_string()),
    );
    assert_eq!(stage.session_state.phase, SessionPhase::Paused);
    assert_eq!(stage.session_state.hc_status.as_deref(), Some("idle"));
    assert_eq!(
        stage.session_state.hc_error.as_deref(),
        Some("request timed out")
    );
    stage.shutdown();
    assert_eq!(stage.session_state.phase, SessionPhase::Exited);
    assert_eq!(stage.session_state.transition, "stop");
    assert_eq!(
        stage.session_state.hc_error.as_deref(),
        Some("request timed out")
    );

    let mut state = Meta(SerdeAnyMap::new());
    stage.mirror_session(&mut state);
    let progress = state.metadata::<SynthesisProgress>().unwrap();
    assert_eq!(progress.session.phase, SessionPhase::Exited);
    let stored = serde_json::to_string(progress).unwrap();
    assert!(!stored.contains("super-secret-token"), "{stored}");
    let log = std::fs::read_to_string(dir.join("session.jsonl")).unwrap();
    assert!(!log.contains("super-secret-token"), "{log}");
    for transition in [
        "launch", "ready", "send", "result", "blocked", "timeout", "stop",
    ] {
        assert!(
            log.contains(&format!("\"transition\":\"{transition}\"")),
            "{log}"
        );
    }
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn prompts_name_every_generator_role_and_the_ctx_api() {
    let dir = std::env::temp_dir().join(format!("mcn-prompt-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let model_path = model_file(&dir);
    let mut body: Value = serde_json::from_slice(&std::fs::read(&model_path).unwrap()).unwrap();
    body["parties"]
        .as_array_mut()
        .unwrap()
        .push(serde_json::json!({
            "name": "upstream",
            "description": "supporting service",
        }));
    body["channels"][0]["endpoint_party_names"]
        .as_array_mut()
        .unwrap()
        .push("upstream".into());
    std::fs::write(&model_path, serde_json::to_vec(&body).unwrap()).unwrap();
    let parties = crate::app::config::PartiesConfig {
        sut: crate::app::config::PartySelector::Names(vec!["server".into()]),
        generator: crate::app::config::PartySelector::Wildcard("*".into()),
    };
    let mut campaign = bare(&dir.join("out"));
    campaign.protocol_generation.model = dir.join("protocol.json");
    campaign.campaign.pythonpath = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("python");
    campaign.target.host = "127.0.0.3".into();
    campaign.target.binary = dir.join("sut with spaces");
    campaign.target.extra_env = vec![("TOKEN".into(), "secret-not-for-prompts".into())];
    campaign.timeouts.case_ms = 1234;
    campaign.parties = Some(parties);
    let context =
        crate::engine::synthesis::SynthesisPromptContext::from_campaign(&campaign).unwrap();
    let model = context.model().clone();
    let mut stage = GeneratorSynthesisStage::from_campaign(&campaign).unwrap();
    stage.enabled = true;
    stage.model = Some(model.clone());
    stage.binding = context.binding().clone();
    stage.prompt_context = Some(context);
    stage.session = "sess-test".into();
    let workspace = campaign.protocol_generation.output.clone();
    let init_output_dir = workspace.join("init");
    let init = stage.initialization_prompt(&init_output_dir).unwrap();
    assert!(init.contains(&format!("\nWORKSPACE={}\n", workspace.display())));
    assert!(init.contains(&format!("\nOUTPUT_DIR={}\n", init_output_dir.display())));
    assert!(init.contains("WRITE ready.json"));
    assert!(init.contains("SESSION=sess-test"));
    assert!(init.contains("client: c [generator]"));
    assert!(init.contains("server: s [sut]"));
    assert!(init.contains("upstream: supporting service [generator]"));
    assert!(init.contains("server, upstream, and user"));
    assert!(init.contains("Do not write a generator yet"));
    assert!(init.contains("ready.json must include this session id"));
    assert!(init.starts_with("# Role and campaign background"));
    assert!(init.contains(&campaign.protocol_generation.model.display().to_string()));
    assert!(init.contains("generator_server/context.py"));
    let ready_json = init
        .split("~~~json\n")
        .nth(1)
        .unwrap()
        .split("\n~~~")
        .next()
        .unwrap();
    let ready: Value = serde_json::from_str(ready_json).unwrap();
    assert_eq!(ready["status"], "ready");
    assert_eq!(ready["session"], stage.session);

    let trace = crate::protocol::StepTrace::new(vec![model.steps[0].clone()]);
    let output_dir = workspace.join("requests").join("req-1");
    let request = request::GeneratorRequest {
        id: "req-1".to_string(),
        output_dir: output_dir.clone(),
        protocol_id: model.protocol_id.clone(),
        trace_id: trace.id.0.clone(),
    };
    let prompt = request
        .prompt("", stage.prompt_context.as_ref().unwrap(), &trace)
        .unwrap();
    let receipt = output_dir.join("receipt.json");
    assert!(prompt.contains(&format!("\nWORKSPACE={}\n", workspace.display())));
    assert!(prompt.contains(&format!("\nOUTPUT_DIR={}\n", output_dir.display())));
    assert!(prompt.contains(&format!("RECEIPT={}", receipt.display())));
    assert!(prompt.contains("REQUEST=req-1"));
    assert!(prompt.contains(&format!("PROTOCOL={}", model.protocol_id)));
    assert!(prompt.contains(&format!("TRACE={}", trace.id.0)));
    assert!(prompt.contains("GENERATOR_PARTIES=client, upstream\n"));
    assert!(prompt.contains("result.json in OUTPUT_DIR."));
    assert!(prompt.contains("added_steps"));
    assert!(prompt.contains("ctx.connect"));
    assert!(prompt.contains("ctx.accept"));
    assert!(prompt.contains("listener.take(timeout=None)"));
    assert!(prompt.contains("ctx.send"));
    assert!(prompt.contains("ctx.recv"));
    assert!(prompt.contains("channel"));
    assert!(prompt.contains("/usr/bin/python3"));
    assert!(prompt.contains("pyftpdlib"));
    assert!(prompt.contains("Do not install dependencies"));
    assert!(prompt.contains("socket API"));
    assert!(prompt.contains("prepare every listener"));
    assert!(!prompt.contains("listener-ready"));
    assert!(!init.contains("{{"));
    assert!(!init.contains("{%"));
    assert!(!prompt.contains("{{"));
    assert!(!prompt.contains("{%"));
    assert!(prompt.contains("127.0.0.3"));
    assert!(prompt.contains("whole case=1234 ms"));
    assert!(prompt.contains("sut with spaces"));
    assert!(prompt.contains("BEFORE forking"));
    assert!(prompt.contains("Only generator.py and params.schema.json are published"));
    assert!(prompt.contains("ctx.recv(channel=None, until=None, max_bytes=4096)"));
    assert!(prompt.contains("receipt.model_path"));
    assert!(prompt.contains("validate_generator.py"));
    assert!(prompt.contains("--check-imports --pythonpath"));
    assert!(!init.contains("secret-not-for-prompts"));
    assert!(!prompt.contains("secret-not-for-prompts"));
    let result_blocks: Vec<_> = prompt.split("~~~json\n").skip(1).collect();
    assert_eq!(result_blocks.len(), 2);
    for (index, block) in result_blocks.into_iter().enumerate() {
        let result: Value = serde_json::from_str(block.split("\n~~~").next().unwrap()).unwrap();
        assert_eq!(result["request_id"], request.id);
        assert_eq!(result["protocol_id"], model.protocol_id);
        assert_eq!(result["trace_id"], trace.id.0);
        assert_eq!(result["status"], if index == 0 { "ok" } else { "error" });
        assert!(result["added_steps"].as_array().unwrap().is_empty());
    }
    let mode_prompt = request
        .prompt("delay", stage.prompt_context.as_ref().unwrap(), &trace)
        .unwrap();
    assert!(mode_prompt.contains("MODE=delay\nRECEIPT="));
    assert!(!prompt.contains("MODE="));
    stage.session = "session-\"测试".into();
    let escaped = stage.initialization_prompt(&init_output_dir).unwrap();
    let ready_json = escaped
        .split("~~~json\n")
        .nth(1)
        .unwrap()
        .split("\n~~~")
        .next()
        .unwrap();
    let ready: Value = serde_json::from_str(ready_json).unwrap();
    assert_eq!(ready["session"], stage.session);
    let _ = std::fs::remove_dir_all(&dir);
}
