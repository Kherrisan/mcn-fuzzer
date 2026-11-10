#!/usr/bin/env python3
"""Opt-in paid smoke: native DeepSeek tools, then three isolated HC/RSC workflows.

Read DEEPSEEK_API_KEY from the environment or a non-echoing terminal prompt.
Never accepts a key as an argument or copies ambient credentials into task files.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rsc.workflow.audit import audit_run
from rsc.workflow.state import Workflow
from scripts.benchmark_parallel_rsc import cleanup as cleanup_task


def api_probe(key):
    messages = [{"role": "user", "content": "Call rsc_probe with nonce=RSC_OK, then reply RSC_OK."}]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "rsc_probe",
                "description": "Test tool connection",
                "parameters": {
                    "type": "object",
                    "properties": {"nonce": {"type": "string"}},
                    "required": ["nonce"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    calls, usage = 0, []
    started = time.monotonic()
    for _ in range(3):
        request = urllib.request.Request(
            "https://api.deepseek.com/chat/completions",
            data=json.dumps(
                {
                    "model": "deepseek-flash",
                    "messages": messages,
                    "tools": tools,
                    "thinking": {"type": "enabled"},
                    "reasoning_effort": "high",
                    "max_tokens": 4096,
                }
            ).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.load(response)
        usage.append(body.get("usage", {}))
        message = body["choices"][0]["message"]
        messages.append(message)
        if not message.get("tool_calls"):
            if calls and "RSC_OK" in (message.get("content") or ""):
                return {
                    "passed": True,
                    "model": "deepseek-flash",
                    "effort": "high",
                    "tool_calls": calls,
                    "elapsed_seconds": time.monotonic() - started,
                    "usage": usage,
                }
            raise RuntimeError("DeepSeek probe did not call the tool and complete its follow-up")
        for call in message["tool_calls"]:
            if call["function"]["name"] != "rsc_probe" or json.loads(
                call["function"]["arguments"]
            ) != {"nonce": "RSC_OK"}:
                raise RuntimeError("DeepSeek probe returned an unexpected tool call")
            calls += 1
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": "RSC_OK"})
    raise RuntimeError("DeepSeek probe exceeded three requests")


def assert_no_key(root, key):
    needle = key.encode()
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            try:
                found = needle in path.read_bytes()
            except OSError:
                continue
            if found:
                raise RuntimeError(f"credential unexpectedly persisted in test artifact: {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    key = os.environ.get("DEEPSEEK_API_KEY") or getpass.getpass("DeepSeek test key: ")
    if not key:
        raise RuntimeError("DeepSeek key is required")
    root = Path(args.out).resolve()
    root.mkdir(parents=True, exist_ok=True)
    probe = api_probe(key)
    (root / "api-probe.json").write_text(json.dumps(probe, indent=2))
    print("DeepSeek official API tool/follow-up probe passed", flush=True)
    source = root / "rfc9999.txt"
    source.write_text("""RFC 9999

1. Greeting protocol

The client establishes a TCP connection to the server's configured port 9000.
The client sends HELLO followed by a token and CRLF. The server replies WELCOME
followed by the same token and CRLF. WELCOME must follow HELLO on that connection.
Both messages contain a token field. No other messages are specified.
""")
    environment = {
        **os.environ,
        "DEEPSEEK_API_KEY": key,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "PI_CODING_AGENT_DIR": str(root / "pi-config"),
    }
    (root / "pi-config").mkdir(exist_ok=True)
    results = {}
    for name, parser_profile, merge_profile in [
        ("pipi", "pi-deepseek-flash", "pi-deepseek-flash"),
        ("codex", "codex-luna-max", "codex-luna-max"),
        ("mixed", "codex-luna-max", "pi-deepseek-flash"),
    ]:
        task = root / name
        if task.exists():
            raise RuntimeError("smoke requires unused task directories")
        print(f"Starting {name}: {parser_profile} / {merge_profile}", flush=True)
        started = time.monotonic()
        with (
            (root / f"{name}.stdout.log").open("w") as out,
            (root / f"{name}.stderr.log").open("w") as err,
        ):
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "rsc.workflow.cli",
                    "run",
                    "--task-dir",
                    str(task),
                    "--config",
                    str(Path(args.config).resolve()),
                    "--protocol",
                    "Greeting/RFC9999",
                    "--rfc",
                    str(source),
                    "--parser-profile",
                    parser_profile,
                    "--merge-profile",
                    merge_profile,
                ],
                env=environment,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
            try:
                code = child.wait()
                if code:
                    raise RuntimeError(
                        f"{name} workflow failed ({code}); inspect {name}.stderr.log"
                    )
            finally:
                if child.poll() is None:
                    child.send_signal(signal.SIGINT)
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.terminate()
                        child.wait(timeout=10)
                cleanup_task(task)
        report = audit_run(task)
        state = Workflow(task)
        events = [json.loads(x[0]) for x in state.db.execute("SELECT body FROM events")]
        report.update(
            elapsed_seconds=time.monotonic() - started,
            tool_calls=len(events),
            rejected_calls=sum(e.get("status") == "rejected" for e in events),
            model_counts={
                k: len(v) for k, v in state.publication_model().items() if isinstance(v, list)
            },
        )
        state.db.close()
        (task / "verification.json").write_text(json.dumps(report, indent=2))
        assert_no_key(root, key)
        results[name] = report
        (root / "results.json").write_text(json.dumps(results, indent=2))
        print(f"{name} published and audited: {report['model_counts']}", flush=True)


if __name__ == "__main__":
    main()
