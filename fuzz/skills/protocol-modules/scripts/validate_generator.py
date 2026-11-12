#!/usr/bin/env python3
"""Validate generator artifacts without publishing them or running the SUT.

The same static checks are embedded in Rust acceptance. --check-imports adds a
bounded, isolated import probe for the agent; it never calls generate().
"""

from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


class ValidationError(ValueError):
    pass


def read_json(path):
    def reject_constant(value):
        raise ValueError(f"non-JSON number {value}")

    try:
        return json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=reject_constant)
    except (OSError, ValueError) as exc:
        raise ValidationError(f"{Path(path).name}: {exc}") from exc


def check_result(result, request_id):
    if not isinstance(result, dict) or result.get("request_id") != request_id:
        raise ValidationError("result.json does not match the dispatched request")
    if result.get("status") != "ok":
        raise ValidationError(str(result.get("error") or "generator synthesis failed"))
    added = result.get("added_steps")
    if not isinstance(added, list) or any(not isinstance(step, str) for step in added):
        raise ValidationError("result.json added_steps must be an array of strings")


def reject_remote_refs(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "$ref" and isinstance(child, str) and ("://" in child or child.startswith("http")):
                raise ValidationError("parameter schema remote $ref is not allowed")
            reject_remote_refs(child)
    elif isinstance(value, list):
        for child in value:
            reject_remote_refs(child)


def check_value(value, path):
    if isinstance(value, (str, bool)):
        return
    if type(value) is int:
        if not -(1 << 63) <= value < (1 << 63):
            raise ValidationError(f"{path}: only signed 64-bit integer parameters are representable")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            check_value(item, f"{path}[{index}]")
        return
    if isinstance(value, float):
        raise ValidationError(f"{path}: only integer parameters can be stored in GeneratorInput")
    raise ValidationError(f"{path}: objects and null are not representable as ParamValue")


def check_parameters(schema, params):
    """Production subset only: do not invent full JSON Schema constraints."""
    reject_remote_refs(schema)
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValidationError("parameter schema must describe an object")
    if not isinstance(params, dict):
        raise ValidationError("params.json must be an object")
    required = schema.get("required")
    if isinstance(required, list):
        for name in required:
            if isinstance(name, str) and name not in params:
                raise ValidationError(f"params.json is missing required field {name}")
    properties = schema.get("properties")
    if isinstance(properties, dict):
        if schema.get("additionalProperties") is False:
            for key in params:
                if key not in properties:
                    raise ValidationError(f"params.json field {key} is not in the schema")
        types = {"string": str, "integer": int, "boolean": bool, "array": list}
        for key, spec in properties.items():
            if key in params and isinstance(spec, dict):
                kind = spec.get("type")
                if isinstance(kind, str) and kind in types and type(params[key]) is not types[kind]:
                    article = "an" if kind in ("integer", "array") else "a"
                    raise ValidationError(f"{key} must be {article} {kind}")
    for key, value in params.items():
        check_value(value, f"params.json.{key}")


def check_generator(path):
    class YieldCheck(ast.NodeVisitor):
        found = False

        def visit_Yield(self, node):
            self.found = True

        visit_YieldFrom = visit_Yield

        def visit_FunctionDef(self, node):
            pass

        visit_AsyncFunctionDef = visit_FunctionDef
        visit_ClassDef = visit_FunctionDef
        visit_Lambda = visit_FunctionDef

    try:
        source = Path(path).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        compile(tree, str(path), "exec")
    except (OSError, SyntaxError, ValueError) as exc:
        raise ValidationError(f"generator syntax: {exc}") from exc
    decorated = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Name) and decorator.func.id == "step_handler":
                decorated.append((node, "step"))
            elif isinstance(decorator, ast.Name) and decorator.id == "global_init":
                decorated.append((node, "init"))
    if any(kind == "step" for _, kind in decorated):
        for node, _ in decorated:
            if isinstance(node, ast.AsyncFunctionDef):
                raise ValidationError("step handlers and global initializer must be synchronous")
            args = node.args
            positional = args.posonlyargs + args.args
            if (len(positional) - len(args.defaults) > 2
                    or (len(positional) < 2 and args.vararg is None)
                    or any(default is None for default in args.kw_defaults)):
                raise ValidationError("step handlers and global initializer must accept (ctx, params)")
            visitor = YieldCheck()
            for statement in node.body:
                visitor.visit(statement)
            if visitor.found:
                raise ValidationError("step handlers and global initializer must not yield")
        return
    raise ValidationError("generator must define at least one top-level @step_handler")


IMPORT_PROBE = r'''
import _thread, contextlib, importlib.util, inspect, json, os, sys, threading
sys.dont_write_bytecode = True
path = sys.argv[1]
sys.path[:0] = [p for p in sys.argv[2].split(__import__('os').pathsep) if p]
from generator_server.registry import Registry
def deny_thread(*args, **kwargs):
    raise RuntimeError("generator import must not start threads")
threading.Thread.start = deny_thread
_thread.start_new_thread = deny_thread
def audit(event, args):
    if event in {"socket.__new__", "subprocess.Popen", "os.fork", "os.forkpty",
                 "os.posix_spawn", "os.exec", "os.system"}:
        raise RuntimeError("generator import must not create case resources: " + event)
    if event == "open" and isinstance(args[2], int) and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
        raise RuntimeError("generator import must not write case files")
    if event in {"os.mkdir", "os.remove", "os.rename", "os.rmdir"}:
        raise RuntimeError("generator import must not modify case files: " + event)
sys.addaudithook(audit)
try:
    with contextlib.redirect_stdout(sys.stderr):
        function = Registry().get(path)
        if inspect.iscoroutinefunction(function) or inspect.isgeneratorfunction(function):
            raise TypeError("generate must be a synchronous function")
        inspect.signature(function).bind(object(), {})
    print(json.dumps({"status": "ok"}))
except BaseException as exc:
    print(json.dumps({"status": "error", "error": type(exc).__name__ + ": " + str(exc)}))
    sys.exit(1)
'''


def check_imports(generator, pythonpath, timeout):
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Match publication: the request directory is not an import path for helpers.
    with tempfile.TemporaryDirectory(prefix="mcn-generator-check-") as cwd:
        try:
            run = subprocess.run([sys.executable, "-c", IMPORT_PROBE, str(generator), pythonpath],
                                 cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise ValidationError(f"generator import exceeded {timeout:g} seconds") from exc
    try:
        report = json.loads(run.stdout)
    except ValueError as exc:
        raise ValidationError(f"generator import failed: {(run.stderr or run.stdout)[-2000:]}") from exc
    if run.returncode or report.get("status") != "ok":
        raise ValidationError(f"generator import failed: {report.get('error', run.stderr[-2000:])}")


def validate(output_dir, request_id, result_path=None,
             *, import_check=False, pythonpath="", timeout=5.0):
    output_dir = Path(output_dir).resolve()
    checks = []
    result = read_json(result_path or output_dir / "result.json")
    check_result(result, request_id)
    checks.append("request id, status and added_steps")
    schema = read_json(output_dir / "params.schema.json")
    params = read_json(output_dir / "params.json")
    check_parameters(schema, params)
    checks.append("production schema subset and recursive signed-64-bit parameter representation")
    generator = output_dir / "generator.py"
    check_generator(generator)
    checks.append("Python compilation and synchronous generator interface")
    if import_check:
        check_imports(generator, pythonpath, timeout)
        checks.append("isolated import, callable signature and import-time resource guard")
    return {"status": "ok", "checks": checks, "schema_validation": "production_subset",
            "imports_checked": import_check, "runtime_executed": False,
            "note": "generate() and the SUT were not run; protocol semantics, lazy imports and case cleanup need runtime verification"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--proposal", help="frozen proposal.json; its request_id must match --request-id")
    parser.add_argument("--result", type=Path, help="draft completion JSON; default: OUTPUT_DIR/result.json")
    parser.add_argument("--check-imports", action="store_true")
    parser.add_argument("--pythonpath", default=os.environ.get("PYTHONPATH", ""))
    parser.add_argument("--import-timeout", type=float, default=5.0)
    args = parser.parse_args()
    if not 0 < args.import_timeout <= 60:
        parser.error("--import-timeout must be positive and at most 60 seconds")
    try:
        if args.proposal:
            proposal = read_json(args.proposal)
            if proposal.get("request_id") != args.request_id:
                raise ValidationError("proposal request_id does not match --request-id")
        report = validate(args.output_dir, args.request_id, args.result,
                          import_check=args.check_imports,
                          pythonpath=args.pythonpath, timeout=args.import_timeout)
    except (ValidationError, RecursionError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
