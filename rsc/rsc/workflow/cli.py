"""Workflow CLI commands; the stdio entry point prints only MCP messages."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from rsc.agents.files import private_json
from rsc.agents.profiles import bind_profiles, selection_arguments
from rsc.config import load_workflow_config

from .locking import exclusive
from .server import request, serve, stdio
from .state import Workflow, WorkflowError


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def register(sub):
    for name in ("run", "serve", "mcp", "audit"):
        p = sub.add_parser(name, help=f"RFC MCP workflow {name}")
        p.set_defaults(func=command)
        if name == "mcp":
            p.add_argument("--socket", required=True)
            p.add_argument("--token-file", required=True)
            p.add_argument("--catalog", choices=["phase", "role"], default="phase")
        else:
            p.add_argument("--task-dir", required=True)
            p.add_argument("--socket")
            if name == "run":
                p.add_argument("--rfc", action="append", default=[])
                p.add_argument("--protocol")
                p.add_argument("--config")
                selection_arguments(p)
                p.add_argument(
                    "--max-review-rounds",
                    type=positive_int,
                    help="maximum reviews including the first; overrides config (default: 5)",
                )
                p.add_argument("--hc", default="hc")
                p.add_argument(
                    "--retry-worker",
                    help="retry a worker only after its original terminal is confirmed dead",
                )


def command(args):
    if args.command == "mcp":
        stdio(args.socket, args.token_file, args.catalog)
        return 0
    root = Path(args.task_dir).resolve()
    if args.command == "audit":
        from .audit import audit_run

        report = audit_run(root)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    socket = args.socket or str(root / "workflow.sock")
    if args.command == "serve":
        serve(root, socket)
        return 0
    root.mkdir(parents=True, exist_ok=True)
    with exclusive(root / "supervisor.lock"):
        return run_task(args, root, socket)


def run_task(args, root, socket):
    from .supervisor import Supervisor

    config_record = root / "runner-config.json"
    config_path = args.config
    if config_path is None and config_record.exists():
        config_path = json.loads(config_record.read_text()).get("config_path")
    config = load_workflow_config(config_path)
    options = config.get("workflow", {})
    if args.max_review_rounds is not None:
        options["max_review_rounds"] = args.max_review_rounds
    if not (root / "workflow.sqlite").exists():
        if not args.rfc or not args.protocol:
            raise WorkflowError("new task requires --rfc and --protocol")
        config["resolved_agents"] = bind_profiles(root, config, existing=False, args=args)
        task = Workflow.create(
            root,
            args.rfc,
            args.protocol,
            target_tokens=options.get("batch_target_tokens", 8000),
            max_tokens=options.get("batch_max_tokens", 12000),
            max_review_rounds=options.get("max_review_rounds", 5),
            parse_workers=options["parse_workers"],
            review_workers=options["review_workers"],
        )
        task.db.close()
        private_json(
            config_record,
            {"config_path": str(Path(config_path).resolve()) if config_path else None},
        )
    else:
        task = Workflow(root)
        try:
            config["resolved_agents"] = bind_profiles(root, config, existing=True, args=args)
        except ValueError:
            task.db.close()
            raise
        if (
            args.max_review_rounds is not None
            and args.max_review_rounds != task.data["max_review_rounds"]
        ):
            task.db.close()
            raise WorkflowError(
                "cannot change the review limit of an existing task; use a new task directory"
            )
        if args.protocol and args.protocol != task.data["protocol"]:
            raise WorkflowError("cannot change the protocol of an existing task")
        if args.rfc and [str(Path(p).resolve()) for p in args.rfc] != task.data["sources"]:
            raise WorkflowError("cannot change the sources of an existing task")
        config["workflow"] = {
            "batch_target_tokens": task.data["target_tokens"],
            "batch_max_tokens": task.data["max_tokens"],
            "max_review_rounds": task.data["max_review_rounds"],
            "parse_workers": task.data["parse_workers"],
            "review_workers": task.data["review_workers"],
        }
        if task.data["phase"] == "published":
            task.export()
            if not (root / "supervisor.json").exists():
                task.db.close()
                return 0
        task.db.close()
    # Connection refusal is evidence of a dead service, unlike a timeout.
    token = (root / "coordinator.token").read_text().strip()

    def healthy():
        return request(
            socket,
            {
                "op": "call",
                "token": token,
                "name": "workflow_status",
                "arguments": {},
                "origin": "health",
            },
        )

    child = None
    try:
        healthy()
        needs_service = False
    except (ConnectionRefusedError, FileNotFoundError):
        needs_service = True
    if needs_service:
        with (
            (root / "server.stdout.log").open("a") as out,
            (root / "server.stderr.log").open("a") as err,
        ):
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "rsc.workflow.cli",
                    "serve",
                    "--task-dir",
                    str(root),
                    "--socket",
                    socket,
                ],
                stdout=out,
                stderr=err,
                env={
                    **os.environ,
                    "PYTHONPATH": str(Path(__file__).resolve().parents[2])
                    + os.pathsep
                    + os.environ.get("PYTHONPATH", ""),
                },
            )
        import time

        deadline = time.monotonic() + 10
        while True:
            if child.poll() is not None or time.monotonic() > deadline:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=10)
                raise WorkflowError("MCP service failed to become ready; inspect server.stderr.log")
            try:
                healthy()
                break
            except (ConnectionRefusedError, FileNotFoundError):
                time.sleep(0.05)
    try:
        Supervisor(root, socket, config, args.hc, retry_worker=args.retry_worker).run()
    finally:
        # Leave a resumed external service alone; only stop the one we created.
        if child:
            child.terminate()
            child.wait(timeout=10)
    return 0


def main():
    p = argparse.ArgumentParser(prog="rsc-workflow")
    sub = p.add_subparsers(dest="command", required=True)
    register(sub)
    args = p.parse_args()
    try:
        return command(args)
    except (ValueError, OSError) as error:
        print(f"rsc: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
