#!/usr/bin/env python3
"""Bounded single/parallel Parser comparison against one frozen source snapshot.

Ordinary tests never invoke this paid-agent runner. Results distinguish execution,
structural audit, timing and the separate manual semantic assessment.
"""

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def cleanup(root):
    """Stop only this diagnostic's verified terminals after its explicit deadline."""
    path = root / "supervisor.json"
    if not path.exists():
        return
    saved = json.loads(path.read_text())
    if saved.get("cleanup_complete"):
        return
    environment = {**os.environ, **saved["hc_env"]}
    environment.pop("HC_TERMINAL_ID", None)
    owner = Path(environment["XDG_RUNTIME_DIR"]) / "owner.json"
    if json.loads(owner.read_text())["root"] != str(root):
        raise RuntimeError("diagnostic HC namespace belongs to another root")

    def hc(*args):
        p = subprocess.run(
            ["hc", args[0], "--json", *args[1:]],
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if p.returncode == 3:
            return {"state": "missing"}
        if p.returncode:
            raise RuntimeError(p.stderr.strip())
        value = json.loads(p.stdout)
        return value.get("result", value)

    for worker, terminal in saved["terminals"].items():
        info = hc("get", terminal["address"])
        if info["state"] == "missing":
            continue
        if info["name"] != terminal["name"]:
            raise RuntimeError("refusing to stop a terminal with changed identity")
        if info["state"] != "finished":
            write(
                root / "agents" / worker / "diagnostic-terminal.json",
                hc("read", terminal["address"]),
            )
            hc("kill", terminal["address"])
            hc("wait", terminal["address"], "--timeout", "5")
            if hc("get", terminal["address"])["state"] not in {"finished", "missing"}:
                raise RuntimeError("terminal has not confirmed termination")
    for terminal in saved["terminals"].values():
        hc("rm", terminal["address"])
    write(root / "diagnostic-cleanup.json", {"complete": True, "time": time.time()})


def codex_usage(root):
    """Read only this campaign's named threads; never export account or credential data."""
    state = json.loads((root / "supervisor.json").read_text())
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    databases = sorted(home.glob("state_*.sqlite"), key=lambda p: int(p.stem.split("_")[-1]))
    if not databases:
        return {"available": False, "reason": "Codex thread database is unavailable"}
    db = sqlite3.connect(databases[-1].as_uri() + "?mode=ro", uri=True)
    workers, total = {}, {}
    try:
        for worker, session in state["sessions"].items():
            query = (
                "SELECT id, name, rollout_path, model, reasoning_effort FROM threads "
                "WHERE name=? AND cwd=?"
            )
            arguments = [session["name"], str(root)]
            if session.get("id"):
                query += " AND id=?"
                arguments.append(session["id"])
            rows = db.execute(query, arguments).fetchall()
            if len(rows) != 1:
                workers[worker] = {"available": False, "matching_threads": len(rows)}
                continue
            thread, name, path, model, effort = rows[0]
            counts, truncations = None, []
            peak_input, context_window, compactions = 0, None, 0
            with Path(path).open() as stream:
                for line in stream:
                    event = json.loads(line)
                    payload = event.get("payload", {})
                    if payload.get("type") == "token_count" and payload.get("info"):
                        info = payload["info"]
                        counts = info["total_token_usage"]
                        peak_input = max(
                            peak_input, info.get("last_token_usage", {}).get("input_tokens", 0)
                        )
                        context_window = info.get("model_context_window", context_window)
                    if event.get("type") == "compacted":
                        compactions += 1
                    if payload.get("type") == "function_call_output" and any(
                        marker in str(payload.get("output", ""))
                        for marker in (
                            "Warning: truncated output",
                            "Output exceeded available",
                            "tokens truncated",
                        )
                    ):
                        truncations.append(event.get("timestamp"))
            local = root / "agents" / worker / "codex-session.jsonl"
            shutil.copyfile(path, local)
            local.chmod(0o600)
            workers[worker] = {
                "available": counts is not None,
                "thread": thread,
                "name": name,
                "model": model,
                "effort": effort,
                "token_usage": counts,
                "peak_input_tokens": peak_input,
                "context_window": context_window,
                "compactions": compactions,
                "truncated_outputs": truncations,
                "log": str(local),
            }
            for key, value in (counts or {}).items():
                total[key] = total.get(key, 0) + value
    finally:
        db.close()
    return {
        "available": bool(workers) and all(w["available"] for w in workers.values()),
        "workers": workers,
        "total": total,
    }


def summarize(root, returncode, timed_out, elapsed):
    from rsc.workflow.audit import audit_run

    report = {
        "output": str(root), "exit_code": returncode, "timed_out": timed_out,
        "process_elapsed_seconds": elapsed, "semantic_review": False, "complete": False,
    }
    if not (root / "state.json").exists():
        return {**report, "outcome": "initialization_failed"}
    state = json.loads((root / "state.json").read_text())
    report["phase"] = state["phase"]
    report["semantic_review"] = any(root.glob("reviews/*/review.json"))
    report["review_passed"] = bool(state.get("semantic_review"))
    for filename, key in (("parse-plan.json", "parse_plan"), ("phases.json", "metrics")):
        if (root / filename).exists():
            report[key] = json.loads((root / filename).read_text())
    outcome = (
        "timeout" if timed_out else "execution_failed" if returncode else "incomplete_workflow"
    )
    if state["phase"] == "published" and not timed_out and not returncode:
        try:
            report["audit"] = audit_run(root)
            report["complete"] = True
            report["semantic_review"] = True
            protocol = json.loads((root / "protocol.json").read_text())
            report["protocol_complete"] = protocol["meta"]["complete"]
            report["unresolved"] = len(protocol["unresolved"])
            outcome = "published"
        except ValueError as error:
            report["audit"] = {"verified": False, "error": str(error)}
            outcome = "audit_failed"
    report["outcome"] = outcome
    report["under_20_minutes"] = report["complete"] and elapsed < 1200
    if (root / "supervisor.json").exists():
        try:
            report["codex"] = codex_usage(root)
        except (OSError, ValueError, sqlite3.Error) as error:
            report["codex"] = {"available": False, "error": str(error)}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--parse-workers", nargs="+", default=["auto", "auto", "auto", "2"])
    parser.add_argument("--max-feedback-rounds", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=2400)
    parser.add_argument("--source-snapshot", type=Path,
                        help="reuse an earlier experiment's frozen source directory")
    parser.add_argument("--keep-going", action="store_true",
                        help="complete the planned repetitions even when a trial fails")
    args = parser.parse_args()
    if any(n != "auto" and (not n.isdecimal() or int(n) < 1) for n in args.parse_workers):
        parser.error("worker counts must be auto or positive integers")
    if args.max_feedback_rounds < 1:
        parser.error("--max-feedback-rounds must be positive")
    if not 1 <= args.timeout <= 2400:
        parser.error("diagnostic timeout must be within 1..2400 seconds")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    project = Path(__file__).resolve().parents[1]
    origin = args.source_snapshot.resolve() if args.source_snapshot else project
    if not (origin / "rsc/__init__.py").is_file() or not (origin / "schemas").is_dir():
        parser.error("source snapshot must contain rsc/ and schemas/")
    source = out / "source"
    shutil.copytree(
        origin / "rsc", source / "rsc", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    shutil.copytree(origin / "schemas", source / "schemas")
    inputs = (origin.parent / "inputs" if args.source_snapshot
              else project / "tests/fixtures/profuzzbench-rfc/FTP")
    shutil.copytree(inputs, out / "inputs")
    sys.path.insert(0, str(source))
    from rsc.agents.adapters import CodexAdapter

    executable = CodexAdapter.executable({"executable": "codex", "envs": []})
    config_text = (
        "[workflow]\nparse_workers=WORKERS\n"
        f"max_feedback_rounds={args.max_feedback_rounds}\n"
        "batch_target_tokens=8000\nbatch_max_tokens=12000\n\n[agents]\n"
        'default_profile="luna-high"\n\n[agents.profiles.luna-high]\nprovider="codex"\n'
        f'executable={json.dumps(executable)}\nmodel="gpt-6-luna"\neffort="high"\nargs=[]\nenvs=[]\n'
    )
    environment = {**os.environ, "PYTHONPATH": str(source)}
    write(
        out / "manifest.json",
        {
            "created_at": time.time(),
            "source_snapshot": str(source),
            "source_origin": str(origin),
            "rfc_order": ["rfc959.html", "rfc3659.html"],
            "model": "gpt-6-luna",
            "effort": "high",
            "parse_workers": args.parse_workers,
            "merge": "single agent only for multiple parser results",
            "max_feedback_rounds": args.max_feedback_rounds,
            "runs": len(args.parse_workers),
            "deadline_seconds": args.timeout,
            "target_seconds": 1200,
            "semantic_audit": "independent reviewer plus deterministic checks",
        },
    )
    results = []
    for number, workers in enumerate(args.parse_workers, 1):
        config = out / f"config-{number}.toml"
        selected_workers = '"auto"' if workers == "auto" else workers
        config.write_text(config_text.replace("WORKERS", selected_workers))
        root = out / f"ftp-{time.strftime('%m%d%H%M%S', time.gmtime())}-r{number}"
        root.mkdir()
        command = [
            sys.executable,
            "-m",
            "rsc.cli",
            "run",
            "--task-dir",
            str(root),
            "--config",
            str(config),
            "--protocol",
            "FTP RFC 959 and RFC 3659",
        ]
        for name in ("rfc959.html", "rfc3659.html"):
            command += ["--rfc", str(out / "inputs" / name)]
        write(out / "current.json", {"run": number, "output": str(root), "started_at": time.time()})
        print(f"run {number} started: {root}", flush=True)
        start, timed_out = time.monotonic(), False
        with (root / "run.log").open("w") as log:
            process = subprocess.Popen(
                command,
                cwd=source,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                process.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)
        elapsed = time.monotonic() - start
        cleanup(root)
        result = summarize(root, process.returncode, timed_out, elapsed)
        write(root / "timing-result.json", result)
        results.append(result)
        write(out / "results.json", results)
        print(f"run {number} finished: {result['outcome']} in {elapsed:.1f}s", flush=True)
        if not result["complete"] and not args.keep_going:
            return 1  # Diagnose once instead of repeating the same paid failure.
    return 0 if all(result["complete"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
