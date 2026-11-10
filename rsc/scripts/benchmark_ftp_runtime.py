#!/usr/bin/env python3
"""Fresh, sequential FTP RSC trials in a pinned image, with a 20-minute deadline.

Run from the host. Each container receives a snapshot of the current Python package;
existing campaigns and image contents are not modified. Failed runs keep their artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

IMAGE = "sha256:559ecda16baaf0816eb4b83fb553bdc42711e1ac238dcb5af55ddd80f0f5c94f"
RFC_ORDERS = {
    "base-first": ("rfc959.html", "rfc3659.html"),
    "extensions-first": ("rfc3659.html", "rfc959.html"),
}


def write(path, obj):
    Path(path).write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


WORKFLOW_SETTINGS = {
    "batch_target_tokens": 8000,
    "batch_max_tokens": 12000,
    "max_review_rounds": 5,
}


def fingerprint(root):
    digest = hashlib.sha256()
    for p in sorted(root.rglob("*.py")):
        digest.update(str(p.relative_to(root)).encode())
        digest.update(p.read_bytes())
    return digest.hexdigest()


def audit_source_deliveries(data, traces, *, include_model=False):
    """Prove complete sources, optionally model/diff pages, were visible per dispatch.

    Sources are compared exactly; reviewer pages match canonical server response logs.
    Only delivery metadata is exported, never reasoning, credentials or model bodies.
    This proves delivery, not semantic comprehension or completion of the workflow.
    """
    import datetime
    import re

    from rsc.workflow.evidence import source_citations
    from rsc.workflow.source_format import decode_source_batch
    from rsc.workflow.sources import digest

    def objects(value, depth=0):
        if depth > 12:
            return
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from objects(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                yield from objects(child, depth + 1)
        elif isinstance(value, str):
            # text() may produce one complete JSON page or several JSON output items.
            stripped = value.strip()
            if stripped.startswith(("{", "[")):
                try:
                    decoded = json.loads(stripped)
                except ValueError:
                    decoded = None
                if decoded is not None:
                    yield from objects(decoded, depth + 1)
                    return
            for line in value.splitlines():
                line = line.strip()
                if line.startswith(("{", "[")):
                    try:
                        decoded = json.loads(line)
                    except ValueError:
                        continue
                    yield from objects(decoded, depth + 1)

    phases = [dict(p) for p in data.get("phase_history", [])]
    assignment = data.get("assignment", {})
    if assignment.get("dispatch_id") and data["phase"] != "published":
        phases.append(
            {**assignment, "started_at": assignment["created_at"], "finished_at": time.time()}
        )
    preparation = data.get("review_preparation")
    if data["phase"] == "parse" and preparation and not preparation.get("prepared"):
        phases.append(
            {
                **preparation,
                "started_at": preparation["created_at"],
                "finished_at": time.time(),
            }
        )
    expected = {batch["batch_id"]: batch for batch in data["batches"]}
    citations = source_citations(data["batches"])
    receipts = data["receipts"]
    verified = set()
    model_phases = [phase for phase in phases if phase["phase"] == "review"]
    model_responses = {
        (response["dispatch_id"], response["tool"], response["sha256"]): response
        for response in data.get("response_log", [])
        if response["tool"] in {"query_model", "get_changes"}
    }
    verified_models = set()
    for trace in traces:
        role = None
        for line in Path(trace).read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            payload = event.get("payload", {})
            if payload.get("type") == "custom_tool_call":
                match = re.search(
                    r"rsc_" + re.escape(data["task_id"]) + r"_(parser|reviewer)__",
                    str(payload.get("input", "")),
                )
                if match:
                    role = match[1]
            if payload.get("type") != "custom_tool_call_output" or role is None:
                continue
            try:
                stamp = datetime.datetime.fromisoformat(
                    event["timestamp"].replace("Z", "+00:00")
                ).timestamp()
            except (KeyError, TypeError, ValueError):
                continue
            for item in objects(payload.get("output", [])):
                if include_model and role == "reviewer" and "delivered_ids" in item:
                    actual = digest(item)
                    for phase in model_phases:
                        if phase["started_at"] <= stamp <= phase["finished_at"]:
                            for tool in ("query_model", "get_changes"):
                                key = (phase["dispatch_id"], tool, actual)
                                if key in model_responses:
                                    verified_models.add(key)
                if not isinstance(item.get("batch"), dict) or "receipt_id" not in item:
                    continue
                try:
                    batch = decode_source_batch(item["batch"])
                    bid = batch["batch_id"]
                    for segment in batch["segments"]:
                        if "citation" in segment:
                            if citations.get(segment.pop("citation")) != {
                                "batch_id": bid,
                                "block_id": segment["block_id"],
                            }:
                                raise ValueError("mismatched citation")
                        if "evidence" in segment:
                            if segment.pop("evidence") != {
                                "batch_id": bid,
                                "block_id": segment["block_id"],
                            }:
                                raise ValueError("mismatched evidence")
                    if batch != expected.get(bid):
                        continue
                except (KeyError, TypeError, ValueError):
                    continue
                for phase in phases:
                    actor = (
                        "reviewer" if phase["phase"] in {"review", "review_prepare"} else "parser"
                    )
                    key = phase["dispatch_id"] + ":" + bid
                    if (
                        actor == role
                        and phase.get("source_started_at", phase["started_at"])
                        <= stamp
                        <= phase["finished_at"]
                        and receipts.get(key) == item["receipt_id"]
                    ):
                        verified.add(key)
    reports = []
    for phase in phases:
        prefix = phase["dispatch_id"] + ":"
        required = {key for key in receipts if key.startswith(prefix)}
        if not required:
            continue
        missing = sorted(key[len(prefix) :] for key in required - verified)
        reports.append(
            {
                "dispatch_id": phase["dispatch_id"],
                "phase": phase["phase"],
                "round": phase["round"],
                "required_batches": len(required),
                "verified_batches": len(required & verified),
                "missing_batches": missing,
            }
        )
    result = {
        "verified": all(not row["missing_batches"] for row in reports),
        "scope": "Complete source delivery for issued receipts, per role and dispatch; "
        "workflow completion and semantic quality require separate checks.",
        "phases": reports,
    }
    if include_model:
        pages = []
        for phase in model_phases:
            required = {
                key: response
                for key, response in model_responses.items()
                if key[0] == phase["dispatch_id"]
            }
            required_tools = {"query_model"}
            if phase["round"] > 1:
                required_tools.add("get_changes")
            missing_tools = sorted(required_tools - {r["tool"] for r in required.values()})
            missing = [
                {
                    "tool": response["tool"],
                    "revision": response.get("revision"),
                    "delivered_ids": response.get("delivered_ids", []),
                }
                for key, response in required.items()
                if key not in verified_models
            ]
            pages.append(
                {
                    "dispatch_id": phase["dispatch_id"],
                    "round": phase["round"],
                    "required_pages": len(required),
                    "verified_pages": len(set(required) & verified_models),
                    "missing_pages": missing,
                    "missing_tools": missing_tools,
                }
            )
        result["model_pages"] = {
            "verified": all(not p["missing_pages"] and not p["missing_tools"] for p in pages),
            "scope": "Exact complete model/diff responses for each review dispatch; "
            "workflow gates must additionally prove unfiltered model and full diff coverage.",
            "phases": pages,
        }
        result["verified"] = result["verified"] and result["model_pages"]["verified"]
        result["scope"] += " Also checks complete reviewer model/diff page delivery."
    return result


def inside():
    import grp
    import pwd

    sys.path.insert(0, "/opt/pfb")
    import mcn_entry as entry

    output = Path("/output")
    output.mkdir(exist_ok=True)
    installed, _ = entry.install_auth()
    if not installed:
        raise RuntimeError("Codex auth was not installed")
    # Copied source permissions must allow the campaign account to import it.
    package = Path("/opt/fuzzers/mcn/venv/lib/python3.12/site-packages/rsc")
    for cache in package.rglob("__pycache__"):
        shutil.rmtree(cache)
    actual_source = fingerprint(package)
    if actual_source != os.environ["RSC_SOURCE_SHA256"]:
        raise RuntimeError("container source does not match benchmark snapshot")
    for p in package.rglob("*"):
        p.chmod(0o755 if p.is_dir() else 0o644)
    prime = time.monotonic()
    entry.prime_codex_state()
    prime_elapsed = time.monotonic() - prime
    config = output / "high.toml"
    workflow_toml = "\n".join(
        f"{key} = {str(value).lower() if isinstance(value, bool) else value}"
        for key, value in WORKFLOW_SETTINGS.items()
    )
    config.write_text(
        "[workflow]\n"
        + workflow_toml
        + """
[agents]
default_profile = "luna-high"
parser_profile = "luna-high"
reviewer_profile = "luna-high"
[agents.profiles.luna-high]
provider = "codex"
executable = "/opt/fuzzers/mcn/bin/codex"
model = "gpt-6-luna"
effort = "high"
"""
    )
    task = output / "task"
    command = [
        entry.RSC,
        "run",
        "--task-dir",
        str(task),
        "--protocol",
        "FTP",
        "--hc",
        entry.HC,
        "--config",
        str(config),
        "--max-review-rounds",
        "5",
    ]
    for name in RFC_ORDERS[os.environ.get("RSC_RFC_ORDER", "base-first")]:
        command += ["--rfc", "/tmp/ftp-rfcs/" + name]
    subprocess.run(["chown", "-R", "ubuntu:ubuntu", str(output)], check=True)
    start = time.time()
    write(
        output / "started.json",
        {"start_unix": start, "command": command, "prime_sec": prime_elapsed},
    )
    log = output / "rsc.log"
    with log.open("w") as handle:
        proc = subprocess.Popen(
            ["script", "-q", "-e", "-f", "-c", shlex.join(command), "/output/terminal.log"],
            env=entry.ubuntu_env(),
            user=pwd.getpwnam("ubuntu").pw_uid,
            group=grp.getgrnam("ubuntu").gr_gid,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        timed_out = False
        try:
            code = proc.wait(timeout=1200)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            code = 124
    elapsed = time.time() - start
    result = {
        "elapsed_sec": elapsed,
        "exit_code": code,
        "timeout": timed_out,
        "prime_sec": prime_elapsed,
        "model": "gpt-6-luna",
        "effort": "high",
        "source_sha256": actual_source,
        "performance_and_gates_passed": False,
        "semantic_audit": "pending",
    }
    db = task / "workflow.sqlite"
    if db.exists():
        c = sqlite3.connect(db)
        data = json.loads(c.execute("select body from task").fetchone()[0])
        events = [json.loads(x[0]) for x in c.execute("select body from events order by seq")]
        from rsc.workflow.metrics import phase_metrics

        result.update(
            phase=data["phase"],
            round=data["round"],
            revision=data["revision"],
            record_count=len(data["records"]),
            batch_count=len(data["batches"]),
            phases=phase_metrics(data, events),
            event_count=len(events),
            findings=data["findings"],
            tool_elapsed_sec=sum(x.get("elapsed_ms", 0) for x in events) / 1000,
        )
        from collections import Counter

        result["rejections"] = dict(
            Counter(x["error"] for x in events if x.get("status") == "rejected")
        )
        if data["phase"] == "published":
            from rsc.protocol.validate import validate_protocol

            model = json.loads((task / "protocol.json").read_text())
            errors = validate_protocol(model, data["documents"])
            unresolved_findings = [
                f for f in data["findings"].values() if f["status"] != "accepted"
            ]
            parsed = set(data["processed"]) == {b["batch_id"] for b in data["batches"]}
            result.update(
                validation_errors=errors,
                complete=model["meta"]["complete"],
                unresolved=model["unresolved"],
                parsed_all_batches=parsed,
                review_limit_reached=data.get("review_limit_reached"),
            )
            result["performance_and_gates_passed"] = (
                code == 0
                and elapsed < 1200
                and not errors
                and parsed
                and not unresolved_findings
                and not data.get("review_limit_reached")
            )
        c.close()
    usage = []
    truncations = []
    for trace in Path("/home/ubuntu/.codex/sessions").rglob("*.jsonl"):
        latest = None
        requests = {}
        for line in trace.read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
                payload = event.get("payload", {})
                if payload.get("type") == "custom_tool_call":
                    requests[payload.get("call_id")] = str(payload.get("input") or "")
                if payload.get("type") == "custom_tool_call_output":
                    output_text = str(payload.get("output", ""))
                    if (
                        "Warning: truncated output" in output_text
                        or "tokens truncated" in output_text
                    ):
                        request = requests.get(payload.get("call_id"), "")
                        truncations.append(
                            {
                                "session": trace.stem,
                                "call_id": payload.get("call_id"),
                                "timestamp": event.get("timestamp"),
                                "scope": (
                                    "tool_discovery"
                                    if "ALL_TOOLS" in request and "tools." not in request
                                    else "tool_output"
                                ),
                            }
                        )
                if payload.get("type") == "token_count":
                    latest = (payload.get("info") or {}).get("total_token_usage")
            except (ValueError, AttributeError):
                continue
        if latest:
            usage.append({"session": trace.stem, "usage": latest})
    result["codex_usage"] = usage
    result["delivery_truncations"] = truncations
    result["delivery_integrity_passed"] = not truncations
    if truncations:
        result["performance_and_gates_passed"] = False
        result["semantic_audit"] = "failed_output_truncation"
    write(output / "timing-result.json", result)
    return 0 if result["performance_and_gates_passed"] else 1


def host(args):
    root = Path(__file__).resolve().parents[1]
    package = root / "rsc"
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    docker = shlex.split(args.docker)
    snapshot = fingerprint(package)
    runner_snapshot = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    source_snapshot = output / "source" / "rsc"
    shutil.copytree(package, source_snapshot, ignore=shutil.ignore_patterns("__pycache__"))
    if fingerprint(source_snapshot) != snapshot:
        raise RuntimeError("source changed while making benchmark snapshot")
    rfcs = root / "tests/fixtures/profuzzbench-rfc/FTP"
    manifest = {
        "image": args.image,
        "source_sha256": snapshot,
        "runner_sha256": runner_snapshot,
        "workflow": WORKFLOW_SETTINGS,
        "model": "gpt-6-luna",
        "effort": "high",
        "rfc_order": list(RFC_ORDERS[args.rfc_order]),
        "runs": [],
        "rfcs": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(rfcs.glob("*.html"))
        },
    }
    write(output / "experiment.json", manifest)
    for index in range(1, args.runs + 1):
        if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != runner_snapshot:
            raise RuntimeError("benchmark runner changed; start a fresh experiment")
        if fingerprint(package) != snapshot:
            raise RuntimeError("source changed; start a new experiment to reset the streak")
        run = output / f"run-{index}"
        run.mkdir()
        name = "rsc-ftp-runtime-" + output.name.lower().replace("_", "-") + f"-{index}"
        command = docker + [
            "create",
            "--name",
            name,
            "--user",
            "root",
            "--network",
            "bridge",
            "--pids-limit",
            "4096",
            "-e",
            "MCN_CODEX_AUTH_FILE=/tmp/codex-auth",
            "-e",
            "MCN_CODEX_AUTH_NAME=auth.json",
            "-e",
            "RSC_SOURCE_SHA256=" + snapshot,
            "-e",
            "RSC_RFC_ORDER=" + args.rfc_order,
        ]
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
        ):
            if os.environ.get(key):
                value = (
                    os.environ[key]
                    .replace("127.0.0.1", "172.17.0.1")
                    .replace("localhost", "172.17.0.1")
                    if "proxy" in key.lower() and key.lower() != "no_proxy"
                    else os.environ[key]
                )
                command += ["-e", key + "=" + value]
        command += [
            "--entrypoint",
            "/opt/fuzzers/mcn/venv/bin/python",
            args.image,
            "/tmp/benchmark_ftp_runtime.py",
            "--in-container",
        ]
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
        for source, target in (
            (Path(__file__).resolve(), "/tmp/benchmark_ftp_runtime.py"),
            (args.auth.resolve(), "/tmp/codex-auth"),
            (rfcs, "/tmp/ftp-rfcs"),
            (str(source_snapshot) + "/.", "/opt/fuzzers/mcn/venv/lib/python3.12/site-packages/rsc"),
        ):
            subprocess.run(docker + ["cp", str(source), name + ":" + target], check=True)
        manifest["runs"].append({"container": name, "output": str(run)})
        write(output / "experiment.json", manifest)
        print(f"Started {name}: {run}", flush=True)
        with (run / "container.log").open("w") as log:
            subprocess.run(docker + ["start", "-a", name], stdout=log, stderr=subprocess.STDOUT)
        # Copy after the container is terminal, preserving timeout/error evidence.
        subprocess.run(docker + ["cp", name + ":/output/.", str(run)], check=True)
        # sudo docker cp creates root-owned 0600 SQLite files. Keep generated
        # diagnostics readable by the invoking host account for subsequent analysis.
        database = run / "task" / "workflow.sqlite"
        if database.exists() and database.stat().st_uid != os.getuid():
            subprocess.run(
                ["sudo", "-n", "chown", "-R", f"{os.getuid()}:{os.getgid()}", str(run)], check=True
            )
        report = (
            json.loads((run / "timing-result.json").read_text())
            if (run / "timing-result.json").exists()
            else {"performance_and_gates_passed": False, "error": "no timing report"}
        )
        manifest["runs"][-1]["result"] = report
        write(output / "experiment.json", manifest)
        print(
            json.dumps(
                {k: report.get(k) for k in ("elapsed_sec", "phase", "performance_and_gates_passed")}
            ),
            flush=True,
        )
        if not report["performance_and_gates_passed"]:
            return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-traces", type=Path)
    parser.add_argument("--audit-task", type=Path)
    parser.add_argument("--audit-output", type=Path)
    parser.add_argument("--in-container", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--auth", type=Path, default=Path("/home/user/.codex/auth.json"))
    parser.add_argument("--docker", default="sudo -n docker")
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--rfc-order", choices=RFC_ORDERS, default="base-first")
    args = parser.parse_args()
    if args.in_container:
        return inside()
    if args.audit_traces:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        if not args.audit_task or not args.audit_output:
            parser.error("--audit-traces requires --audit-task and --audit-output")
        database = args.audit_task / "workflow.sqlite"
        with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as db:
            data = json.loads(db.execute("select body from task").fetchone()[0])
        report = audit_source_deliveries(
            data, args.audit_traces.rglob("*.jsonl"), include_model=True
        )
        write(args.audit_output, report)
        print(json.dumps(report))
        return 0 if report["verified"] else 1
    if args.output is None or args.runs < 1:
        parser.error("--output and positive --runs are required")
    return host(args)


if __name__ == "__main__":
    raise SystemExit(main())
