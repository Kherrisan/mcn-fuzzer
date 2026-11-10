"""Fair HC dispatcher: persisted assignments, independent credentials, fenced retries."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from rsc.agents import ADAPTERS, AgentContext, check_profiles
from rsc.agents.files import private_json

from .instructions import assignment_instructions
from .server import request
from .state import WorkflowError


class HcFailure(WorkflowError):
    def __init__(self, operation, code, message):
        super().__init__(f"HC {operation} failed ({code}): {message}")
        self.code = code


class HcObservationTimeout(WorkflowError):
    """Observer timeout does not prove process death or revoke an attempt."""


class Supervisor:
    def __init__(self, root, socket, config, hc="hc", retry_worker=None):
        self.root, self.socket = Path(root).resolve(), str(socket)
        self.config, self.hc, self.roles = config, hc, config["resolved_agents"]
        self.retry_worker = retry_worker
        self.path = self.root / "supervisor.json"
        self.state = (
            json.loads(self.path.read_text())
            if self.path.exists()
            else {"terminals": {}, "sessions": {}}
        )
        self.token = (self.root / "coordinator.token").read_text().strip()
        self.task_name = self.status()["name"]
        slug = re.sub("[^a-z0-9_-]", "-", self.task_name.lower())[:24]
        runtime = Path("/tmp") / f"rsc-{os.getuid()}-{slug}"
        runtime.mkdir(mode=0o700, exist_ok=True)
        owner = runtime / "owner.json"
        if owner.exists() and json.loads(owner.read_text())["root"] != str(self.root):
            raise WorkflowError(
                "HC runtime name belongs to another campaign; use a unique directory name"
            )
        private_json(owner, {"root": str(self.root)})
        self.env = {
            **os.environ,
            "HC_SESSION": f"rsc-{slug}",
            "XDG_RUNTIME_DIR": str(runtime),
            "XDG_STATE_HOME": str(self.root / "hc-state"),
            "XDG_CONFIG_HOME": str(self.root / "hc-config"),
            "HC_SOCKET": str(runtime / "herdr-core" / f"rsc-{slug}.sock"),
        }
        self.env.pop("HC_TERMINAL_ID", None)
        self.wait_index = 0

    def workflow_call(self, name, arguments=None):
        return request(
            self.socket,
            {
                "op": "call",
                "token": self.token,
                "name": name,
                "arguments": arguments or {},
                "origin": "supervisor",
            },
        )

    def status(self):
        return self.workflow_call("workflow_status")

    def save(self):
        private_json(self.path, self.state)

    def event(self, kind, **body):
        with (self.root / "supervisor-events.jsonl").open("a") as out:
            out.write(
                json.dumps({"time": time.time(), "campaign": self.task_name, "kind": kind, **body})
                + "\n"
            )

    def context(self, a):
        return AgentContext(
            self.root,
            self.socket,
            self.task_name,
            a["role"],
            self.config["workflow"]["batch_max_tokens"],
            worker=a["worker"],
            resume=a["worker"] in self.state["sessions"],
        )

    @staticmethod
    def observed(info):
        return (info.get("status") or {}).get("state", "unknown")

    def observe(self, worker):
        saved = self.state["terminals"][worker]
        try:
            info = self.hc_call("get", saved["address"])
        except HcFailure as error:
            if error.code == 3:
                return {"state": "missing", "name": saved["name"]}
            raise
        if info.get("name") != saved["name"]:
            raise WorkflowError("recorded HC address belongs to another terminal")
        return info

    def wait_once(self, address):
        return self.hc_call("wait", address, "--timeout", "1", timeout=5)

    def retire(self, worker):
        saved = self.state["terminals"][worker]
        info = self.observe(worker)
        if info["state"] not in {"finished", "missing"}:
            self.hc_call("kill", saved["address"])
            self.wait_once(saved["address"])
            info = self.observe(worker)
            if info["state"] not in {"finished", "missing"}:
                raise HcObservationTimeout("old worker has not confirmed termination")
        self.state.setdefault("retired", []).append({**saved, "worker": worker, "terminal": info})
        del self.state["terminals"][worker]
        self.save()

    def start_worker(self, a):
        context = self.context(a)
        profile = self.roles[a["role"]]["profile"]
        context.directory.mkdir(parents=True, exist_ok=True)
        fatal = context.directory / "fatal.json"
        if fatal.exists():
            fatal.rename(context.directory / f"fatal-{time.time_ns()}.json")
        launch = context.directory / "launch.json"
        ADAPTERS[profile["provider"]].prepare(profile, context).write(launch)
        # Persist launch intent; recovery looks up its unique name before starting
        # anything new when the run response was lost.
        intent = {"name": context.terminal_name, "assignment": a["name"], "attempt": a["attempt"]}
        self.state["launching"] = intent
        self.save()
        value = self.hc_call(
            "run",
            "--bg",
            "--no-parent",
            "--no-notify",
            "--cwd",
            str(self.root),
            "--name",
            context.terminal_name,
            "--",
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "agents" / "launch.py"),
            str(launch),
        )
        address = value.get("address") or value.get("terminal")
        if not isinstance(address, str):
            raise WorkflowError("HC run returned no terminal address")
        self.state["terminals"][a["worker"]] = {**intent, "address": address, "ready": False}
        self.state.pop("launching", None)
        self.save()
        self.ready(a)
        self.event(
            "worker_started",
            worker=a["worker"],
            terminal=address,
            model=profile["model"],
            effort=profile["effort"],
        )

    def ready(self, a):
        context = self.context(a)
        saved = self.state["terminals"][a["worker"]]
        adapter = ADAPTERS[self.roles[a["role"]]["profile"]["provider"]]
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            info = self.observe(a["worker"])
            if info["state"] in {"finished", "missing"}:
                return  # step() fences this confirmed failure before replacement.
            state = self.observed(info)
            if state in {"blocked", "error"}:
                raise WorkflowError(f"worker {a['worker']} startup needs attention: {state}")
            if state in {"idle", "done"} and adapter.ready(context):
                if self.roles[a["role"]]["profile"]["provider"] == "codex":
                    screen = self.hc_call("read", saved["address"]).get("text", "")
                    if f" · {context.terminal_name}" not in screen:
                        if not saved.get("rename_sent"):
                            saved["rename_sent"] = True
                            self.save()
                            self.hc_call(
                                "send", saved["address"], f"/rename {context.terminal_name}"
                            )
                        self.wait_once(saved["address"])
                        continue
                saved["ready"] = True
                self.state["sessions"][a["worker"]] = {
                    "name": context.terminal_name,
                    "provider": self.roles[a["role"]]["profile"]["provider"],
                }
                private_json(
                    context.directory / "session.json", self.state["sessions"][a["worker"]]
                )
                self.save()
                return
            self.wait_once(saved["address"])
        raise HcObservationTimeout(
            "worker initialization observation timed out; process left intact"
        )

    def recover_launch(self, assignments):
        intent = self.state.get("launching")
        if not intent:
            return
        try:
            info = self.hc_call("get", intent["name"])
        except HcFailure as error:
            if error.code != 3:
                raise
            self.state.pop("launching")
            self.save()
            return
        a = next(
            a
            for a in assignments
            if a["name"] == intent["assignment"] and a["attempt"] == intent["attempt"]
        )
        self.state["terminals"][a["worker"]] = {
            **intent,
            "address": info["address"],
            "ready": False,
        }
        self.state.pop("launching")
        self.save()

    def step(self):
        status = self.status()
        if status["phase"] == "published":
            return False
        if status["phase"] == "preprocess":
            self.workflow_call("prepare_sources")
            self.workflow_call("advance_phase")
            return True
        assignments = [
            a
            for a in status["assignments"]
            if a["phase"] == status["phase"] and a["round"] == status["round"]
        ]
        if all(a["status"] == "submitted" for a in assignments):
            for worker in list(self.state["terminals"]):
                self.retire(worker)
            self.workflow_call("advance_phase")
            return True
        self.recover_launch(assignments)
        active = [a for a in assignments if a["status"] != "submitted"]
        for a in active:
            if a["status"] == "failed":
                raise WorkflowError(f"worker {a['worker']} exhausted its two retries")
            saved = self.state["terminals"].get(a["worker"])
            if saved and (saved["assignment"], saved["attempt"]) != (a["name"], a["attempt"]):
                self.retire(a["worker"])
                saved = None
            if saved is None:
                self.start_worker(a)  # All startup/SQLite initialization is serialized.
            elif not saved["ready"]:
                self.ready(a)
        # Dispatch every ready worker before waiting on any one of them.
        waiting = []
        for a in active:
            try:
                info = self.observe(a["worker"])
            except HcObservationTimeout:
                continue
            saved = self.state["terminals"][a["worker"]]
            if info["state"] in {"finished", "missing"}:
                terminal = {
                    "name": saved["name"],
                    "state": "missing" if info["state"] == "missing" else "exited",
                    "details": info,
                }
                self.workflow_call(
                    "replace_attempt",
                    {"name": a["name"], "attempt": a["attempt"], "terminal": terminal},
                )
                self.retire(a["worker"])
                continue
            if self.retry_worker == a["worker"]:
                raise WorkflowError(
                    "cannot retry a live worker; observation timeout is not termination"
                )
            fatal = self.context(a).directory / "fatal.json"
            if fatal.exists() or self.observed(info) in {"blocked", "error"}:
                raise WorkflowError(
                    f"worker {a['worker']} needs attention; inspect its terminal and logs"
                )
            if (
                a["status"] == "pending"
                and saved["ready"]
                and self.observed(info) in {"idle", "done"}
            ):
                self.workflow_call("record_send", {"name": a["name"], "attempt": a["attempt"]})
                saved["send_turn"] = info.get("turn", 0)
                self.save()
                self.hc_call(
                    "send",
                    saved["address"],
                    assignment_instructions(a) + "\nMCP namespace: " + self.context(a).namespace,
                )
                self.event("assignment_sent", **a, terminal=saved["address"])
            elif self.observed(info) in {"idle", "done"} and info.get("turn", 0) > saved.get(
                "send_turn", 0
            ):
                # A turn ended, but the authoritative assignment did not submit.
                # Never infer success from the final terminal text.
                fresh = next(x for x in self.status()["assignments"] if x["name"] == a["name"])
                if fresh["status"] != "submitted":
                    raise WorkflowError(f"worker {a['worker']} ended its turn without submitting")
            waiting.append(saved["address"])
        if waiting:
            self.wait_once(waiting[self.wait_index % len(waiting)])
            self.wait_index += 1
        return True

    def cleanup(self):
        for worker in list(self.state["terminals"]):
            self.retire(worker)
        self.hc_call("server", "stop")
        self.state["cleanup_complete"] = True
        self.save()

    def run(self):
        check_profiles(self.roles)
        self.hc_call("server", "start")
        while self.step():
            pass
        self.cleanup()

    def hc_call(self, *args, timeout=65):
        try:
            result = subprocess.run(
                [self.hc, args[0], "--json", *args[1:]],
                env=self.env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            self.event("hc_observation_timeout", operation=args[0])
            if args[0] == "wait":
                return {"reason": "timeout"}
            raise HcObservationTimeout(
                f"HC {args[0]} observation timed out; inspect the recorded terminal before retrying"
            ) from error
        if result.returncode:
            raise HcFailure(args[0], result.returncode, result.stderr.strip())
        try:
            value = json.loads(result.stdout)
        except ValueError as error:
            raise WorkflowError(f"HC {args[0]} returned invalid JSON") from error
        return value.get("result", value)
