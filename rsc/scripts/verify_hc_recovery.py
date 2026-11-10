#!/usr/bin/env python3
"""Opt-in paid HC recovery check against the current parallel workflow.

Run in a fresh --out directory. Interrupt one running Parser after its first batch
submission, then let the normal coordinator fence and replace its attempt. No extra
model agent is created.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rsc.workflow.audit import audit_run
from scripts.benchmark_parallel_rsc import cleanup


def interrupt_running_parser(root):
    """Return saved progress only after HC confirms this owned terminal's death."""
    path = root / "supervisor.json"
    if not path.exists() or not (root / "workflow.sqlite").exists():
        return None
    state = json.loads(path.read_text())
    saved = state["terminals"].get("parser-1")
    if not saved or not saved.get("ready"):
        return None
    db = sqlite3.connect((root / "workflow.sqlite").as_uri() + "?mode=ro", uri=True)
    try:
        row = db.execute(
            "SELECT name, attempt, status, worker FROM assignments WHERE name=? AND phase='parse'",
            (saved["assignment"],),
        ).fetchone()
        if not row or row[2] != "running" or row[1] != saved["attempt"]:
            return None
        revision = db.execute("SELECT count(*) FROM items WHERE owner=?", (row[3],)).fetchone()[0]
        if revision == 0:
            return None  # Exercise recovery of actual progress, not just an empty claim.
        reports = db.execute(
            "SELECT count(*) FROM submissions WHERE assignment=?", (row[0],)
        ).fetchone()[0]
    finally:
        db.close()
    env = {**os.environ, **state["hc_env"]}
    env.pop("HC_TERMINAL_ID", None)

    def hc(*args):
        result = subprocess.run(
            ["hc", args[0], "--json", *args[1:]],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        value = json.loads(result.stdout)
        return value.get("result", value)

    info = hc("get", saved["address"])
    if info["name"] != saved["name"]:
        raise RuntimeError("refusing to interrupt a terminal with changed identity")
    if info["state"] == "finished":
        return None
    hc("kill", saved["address"])
    hc("wait", saved["address"], "--timeout", "5")
    if hc("get", saved["address"])["state"] != "finished":
        raise RuntimeError("owned worker has not confirmed termination")
    return {**saved, "item_count": revision, "completed_batches": reports}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--config")
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 2400:
        parser.error("--timeout must be within 1..2400 seconds")
    root = args.out.resolve()
    root.mkdir(parents=True, exist_ok=False)
    command = [
        sys.executable,
        "-m",
        "rsc.cli",
        "run",
        "--task-dir",
        str(root),
        "--protocol",
        "Live HC recovery verification",
        "--rfc",
        str(Path(args.source).resolve()),
    ]
    if args.config:
        command.extend(["--config", str(Path(args.config).resolve())])
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    interrupted = None
    started = time.monotonic()
    with (root / "recovery.log").open("w") as log:
        child = subprocess.Popen(
            command,
            env=environment,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        try:
            while child.poll() is None:
                if time.monotonic() - started >= args.timeout:
                    raise RuntimeError("recovery trial reached its diagnostic deadline")
                if interrupted is None:
                    interrupted = interrupt_running_parser(root)
                time.sleep(0.2)
            if child.returncode:
                raise RuntimeError("workflow failed; inspect recovery.log")
            if interrupted is None:
                raise RuntimeError("no running Parser was interrupted; recovery is unverified")
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=10)
            cleanup(root)
    report = audit_run(root)
    db = sqlite3.connect((root / "workflow.sqlite").as_uri() + "?mode=ro", uri=True)
    try:
        attempts = db.execute(
            "SELECT attempt,status,body FROM attempts WHERE assignment=? ORDER BY attempt",
            (interrupted["assignment"],),
        ).fetchall()
        if len(attempts) != 2 or [a[1] for a in attempts] != ["failed", "submitted"]:
            raise RuntimeError("expected exactly one fenced replacement of the same assignment")
        if json.loads(attempts[0][2])["terminal"]["name"] != interrupted["name"]:
            raise RuntimeError("replacement terminal evidence differs")
        retained = db.execute(
            "SELECT count(*) FROM submissions WHERE assignment=? AND attempt=?",
            (interrupted["assignment"], interrupted["attempt"]),
        ).fetchone()[0]
        if retained < interrupted["completed_batches"]:
            raise RuntimeError("replacement lost completed batch submissions")
    finally:
        db.close()
    report.update(
        interrupted=interrupted,
        attempts=len(attempts),
        elapsed_seconds=time.monotonic() - started,
    )
    (root / "recovery-verification.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == "__main__":
    main()
