#!/usr/bin/env python3
"""Opt-in live HC/Codex acceptance: replace a lost parser, retain reviewer and task.

This starts real agents with the supplied model config. Unlike the normal pytest suite,
it uses the configured provider. Run with an unused --out directory and a small RFC.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from rsc.agents.profiles import bind_profiles
from rsc.config import load_workflow_config
from rsc.workflow import Workflow
from rsc.workflow.server import request
from rsc.workflow.supervisor import Supervisor


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--config")
    args = p.parse_args()
    root = Path(args.out).resolve()
    task = Workflow.create(root, [args.source], "Live HC recovery verification")
    assignment = task.data["assignment"].copy()
    task.db.close()
    socket = root / "workflow.sock"
    token = (root / "parser.token").read_text().strip()
    with (root / "service.log").open("w") as log:
        service = subprocess.Popen(
            [sys.executable, "-m", "rsc.workflow.cli", "serve", "--task-dir", str(root)],
            stdout=log,
            stderr=log,
        )
    first = None
    try:
        deadline = time.monotonic() + 10
        while True:
            if service.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("MCP service failed to start; inspect service.log")
            try:
                request(socket, {"op": "list", "token": token})
                break
            except (ConnectionRefusedError, FileNotFoundError):
                time.sleep(0.05)
        config = load_workflow_config(args.config)
        config["resolved_agents"] = bind_profiles(root, config, existing=False, args=args)
        first = Supervisor(root, socket, config)
        first.start()
        old = first.state["terminals"].copy()
        # No assignment has been delivered yet. This is an explicit crash/replacement test.
        first.hc_call("kill", old["parser"])
        first.hc_call("wait", old["parser"], "--timeout", "30", timeout=35)
        recovered = Supervisor(root, socket, config, retry_role="parser")
        recovered.start()
        new = recovered.state["terminals"].copy()
        status = recovered.status()
        assert new["parser"] != old["parser"]
        assert new["reviewer"] == old["reviewer"]
        assert status["assignment"]["dispatch_id"] == assignment["dispatch_id"]
        assert status["round"] == 0
        # start() is idempotent: run() must not replace the new parser a second time.
        recovered.run()
        state = Workflow(root)
        report = {
            "old_terminals": old,
            "new_terminals": new,
            "same_assignment": status["assignment"]["dispatch_id"] == assignment["dispatch_id"],
            "published": state.data["phase"] == "published",
            "rounds": state.data["round"],
        }
        assert recovered.state["terminals"] == new
        assert report["published"]
        (root / "recovery-verification.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report))
    finally:
        if service.poll() is None:
            service.terminate()
            service.wait(timeout=10)
        if first is not None:
            first.hc_call("server", "stop")


if __name__ == "__main__":
    main()
