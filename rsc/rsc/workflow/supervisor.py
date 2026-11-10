"""Code-only HC dispatcher. Workflow gates, not terminal output, decide progress."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from rsc.agents import ADAPTERS, AgentContext, check_profiles

from .instructions import assignment_instructions
from .server import request
from .state import WorkflowError


class HcFailure(WorkflowError):
    def __init__(self, operation, code, message):
        super().__init__(f"HC {operation} failed ({code}): {message}")
        self.code = code


class HcObservationTimeout(WorkflowError):
    """The observer timed out; this says nothing about the terminal's liveness."""


class Supervisor:
    def __init__(self, root, socket, config=None, hc="hc", retry_role=None):
        self.root = Path(root).resolve()
        self.socket = str(socket)
        self.config = config or {}
        self.hc = hc
        self.roles = self.config["resolved_agents"]
        self.retry_role = retry_role
        self.retry_dispatch = None
        self.path = self.root / "supervisor.json"
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {"terminals": {}}
        self.token = (self.root / "parser.token").read_text().strip()
        task = self.status()["task_id"]
        self.task_id = task
        runtime = Path("/tmp") / f"rsc-hc-{task}"
        runtime.mkdir(mode=0o700, exist_ok=True)
        for name in ("hc-state", "hc-config"):
            (self.root / name).mkdir(exist_ok=True)
        self.env = {
            **os.environ,
            "HC_SESSION": f"rsc-{task}",
            "XDG_RUNTIME_DIR": str(runtime),
            "XDG_STATE_HOME": str(self.root / "hc-state"),
            "XDG_CONFIG_HOME": str(self.root / "hc-config"),
            "HC_SOCKET": str(runtime / "herdr-core" / f"rsc-{task}.sock"),
        }
        # Never accidentally inherit the caller's terminal ownership.
        self.env.pop("HC_TERMINAL_ID", None)

    def workflow_call(self, name, arguments=None):
        return request(
            self.socket,
            {
                "op": "call",
                "token": self.token,
                "name": name,
                "arguments": {} if arguments is None else arguments,
                "origin": "supervisor",
            },
        )

    def status(self):
        return self.workflow_call("workflow_status")

    def preprocess(self):
        status = self.status()
        if status["phase"] != "preprocess":
            return False
        assignment = status["assignment"]
        dispatch = assignment["dispatch_id"]
        owned = self.state.get("preprocess_dispatch_id") == dispatch
        if not owned and (assignment["claimed"] or self.state.get("dispatch_id") == dispatch):
            # Existing/uncertain agent delivery keeps its original ownership path.
            return False
        self.state["preprocess_dispatch_id"] = dispatch
        self.save()  # Intent precedes RPC, so lost replies can resume safely.
        if not assignment["claimed"]:
            status = self.workflow_call("claim_assignment", {"dispatch_id": dispatch})
        if not status["sources_prepared"]:
            self.workflow_call("prepare_sources")
        # The original source/snapshot/batch gates remain mandatory.
        self.workflow_call("advance_phase")
        self.event("preprocess_completed", dispatch_id=dispatch, origin="supervisor")
        return True

    def save(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2))
        temporary.replace(self.path)

    def event(self, kind, **body):
        with (self.root / "supervisor-events.jsonl").open("a") as out:
            out.write(
                json.dumps(
                    {
                        "time": time.time(),
                        "task_id": getattr(self, "task_id", None),
                        "kind": kind,
                        **body,
                    }
                )
                + "\n"
            )

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

    def start(self):
        check_profiles(self.roles)
        status = self.status()
        current = status["assignment"]
        if self.retry_role is not None and self.retry_role != current["role"]:
            raise WorkflowError("--retry-role must match the active assignment role")
        if not self.state.get("core_started"):
            self.hc_call("server", "start")
            self.state["core_started"] = True
            self.save()
        for role in ("parser", "reviewer"):
            if role == self.retry_role and role in self.state["terminals"]:
                old = self.state["terminals"][role]
                try:
                    info = self.hc_call("get", old)
                    expected = f"rsc-{role}-{self.task_id}"
                    if info.get("name") != expected:
                        raise WorkflowError("recorded HC address no longer belongs to this role")
                    if info.get("state") != "finished":
                        self.hc_call("kill", old)
                        self.hc_call("wait", old, "--timeout", "30", timeout=35)
                except HcFailure as error:
                    if error.code != 3:
                        raise
                self.state.setdefault("retired_terminals", []).append(
                    {"role": role, "terminal": old}
                )
                del self.state["terminals"][role]
                self.save()
            if role == self.retry_role:
                self.retry_dispatch = current["dispatch_id"]
            if role in self.state["terminals"]:
                self.hc_call("get", self.state["terminals"][role])
                continue
            selected = self.roles[role]
            profile = selected["profile"]
            context = AgentContext(
                self.root,
                self.socket,
                self.task_id,
                role,
                self.config.get("workflow", {}).get("batch_max_tokens", 12000),
            )
            spec = ADAPTERS[profile["provider"]].prepare(profile, context)
            path = context.directory / "launch.json"
            fatal = context.directory / "fatal.json"
            if fatal.exists():
                fatal.rename(context.directory / f"fatal-{time.time_ns()}.json")
            spec.write(path)
            argv = [
                sys.executable,
                str(Path(__file__).resolve().parents[1] / "agents" / "launch.py"),
                str(path),
            ]
            metadata = {
                "profile": selected["name"],
                "provider": profile["provider"],
                "model": profile["model"],
                "effort": profile["effort"],
                "namespace": context.namespace,
            }
            self.state.setdefault("agents", {})[role] = metadata
            value = self.hc_call(
                "run",
                "--bg",
                "--no-parent",
                "--cwd",
                str(self.root),
                "--name",
                f"rsc-{role}-{self.status()['task_id']}",
                "--",
                *argv,
            )
            terminal = value.get("address") or value.get("terminal")
            if not isinstance(terminal, str):
                raise WorkflowError("HC run did not return a terminal address")
            self.state["terminals"][role] = terminal
            self.save()
            self.event("agent_started", role=role, terminal=terminal, **metadata)
        self.retry_role = None

    @staticmethod
    def observed(info):
        status = info.get("status") or {}
        if isinstance(status, str):
            return status
        return status.get("state", "unknown")

    def step(self):
        status = self.status()
        if status["phase"] == "published":
            return False
        a = status["assignment"]
        terminal = self.state["terminals"][a["role"]]
        fatal = self.root / "agents" / a["role"] / "fatal.json"
        if fatal.exists():
            raise WorkflowError(
                "agent adapter reported a fatal delivery/model error; inspect " + str(fatal)
            )
        try:
            info = self.hc_call("get", terminal)
        except HcObservationTimeout:
            return True
        state = self.observed(info)
        if info.get("state") == "finished" or state in {"blocked", "error"}:
            raise WorkflowError(f"agent {terminal} needs attention: {state}")
        metadata = self.state.get("agents", {}).get(a["role"])
        if metadata:
            context = AgentContext(self.root, self.socket, self.task_id, a["role"])
            if not ADAPTERS[metadata["provider"]].ready(context):
                self.hc_call(
                    "wait", terminal, "--output", "RSC adapter ready", "--timeout", "30", timeout=35
                )
                return True
        if state not in {"idle", "done"}:
            self.hc_call("wait", terminal, "--timeout", "30", timeout=35)
            return True
        retrying = getattr(self, "retry_dispatch", None) == a["dispatch_id"]
        if a["claimed"] and not retrying:
            raise WorkflowError(
                "agent stopped without completing its assignment; inspect HC terminal"
            )
        if self.state.get("dispatch_id") == a["dispatch_id"] and not retrying:
            raise WorkflowError(
                "delivery uncertain or not claimed; inspect terminal before retrying"
            )
        # Persist intent first: a crash during send must not cause blind redelivery.
        self.retry_dispatch = None
        self.state.update(dispatch_id=a["dispatch_id"], delivery="sending")
        self.save()
        self.hc_call(
            "send",
            terminal,
            assignment_instructions(
                a["role"],
                a["dispatch_id"],
                a["phase"],
                round_number=a.get("round"),
            )
            + "\nMCP namespace: "
            + self.state.get("agents", {}).get(a["role"], {}).get("namespace", "rsc"),
        )
        self.state["delivery"] = "sent"
        self.save()
        self.event(
            "task_sent",
            terminal=terminal,
            pid=info.get("foreground", {}).get("pid", info.get("pid")),
            **a,
        )
        self.hc_call("wait", terminal, "--timeout", "30", timeout=35)
        return True

    def cleanup(self):
        if self.state.get("cleanup_complete"):
            return
        # Starting this task's own core is idempotent and can restore its holders.
        self.hc_call("server", "start")
        for role, terminal in self.state["terminals"].items():
            try:
                info = self.hc_call("get", terminal)
            except HcFailure as error:
                if error.code == 3:
                    continue
                raise
            if info.get("name") != f"rsc-{role}-{self.task_id}":
                raise WorkflowError("cleanup refused: terminal identity does not match this task")
            if info.get("state") != "finished":
                self.hc_call("kill", terminal)
                self.hc_call("wait", terminal, "--timeout", "30", timeout=35)
        self.hc_call("server", "stop")
        self.state.update(cleanup_complete=True, core_started=False)
        self.save()
        self.event("cleanup_complete")

    def run(self):
        if self.status()["phase"] == "published":
            self.cleanup()
            return
        self.start()
        self.preprocess()
        while self.step():
            pass
        self.cleanup()
