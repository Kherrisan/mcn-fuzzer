"""Source branch coverage loaded from a frozen llvm-cov JSON export.

A "branch" here is a source condition with independent true/false counters,
not every IR CFG edge. Functions keep their LLVM symbol and are joined to the
callgraph through verified aliases such as ``ftpserv.c:cleanup_handler``.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PAGE = 20
MAX_PAGE = 100
MAX_SOURCE_LINES = 200
DEFAULT_SOURCE_LINES = 80


class CoverageError(ValueError):
    pass


@dataclass(frozen=True)
class Branch:
    file: str
    line: int
    column: int
    end_line: int
    end_column: int
    true_count: int
    false_count: int
    kind: int

    @property
    def key(self):
        return (self.file, self.line, self.column, self.end_line, self.end_column, self.kind)

    def directions(self):
        return ((True, self.true_count), (False, self.false_count))


@dataclass
class FunctionCoverage:
    symbol: str
    files: list
    branches: list = field(default_factory=list)
    count: int = 0


@dataclass
class CoverageData:
    functions: dict
    files: dict
    aliases: dict
    totals: dict
    binaries: list
    source_root: Path | None
    digest: str = ""

    def function(self, name):
        symbol = self.aliases.get(name, name)
        return self.functions.get(symbol)

    def branch_records(self, symbol):
        function = self.function(symbol)
        return function.branches if function else []

    def symbol_file(self, symbol):
        function = self.functions.get(symbol)
        if not function or not function.files:
            return ""
        return function.files[0]

    def stats_for(self, records):
        total = covered = 0
        for branch in records:
            for _, count in branch.directions():
                total += 1
                covered += 1 if count > 0 else 0
        missed = total - covered
        percent = round(covered * 100.0 / total, 2) if total else None
        return {
            "total_branches": total,
            "covered_branches": covered,
            "missed_branches": missed,
            "branch_coverage_percent": percent,
        }

    def function_stats(self, symbol):
        function = self.function(symbol)
        if function is None:
            raise CoverageError(f"unknown function {symbol}")
        return self.stats_for(function.branches)

    def cumulative(self, symbols):
        seen = set()
        chosen = []
        for symbol in symbols:
            function = self.function(symbol)
            if function is None:
                continue
            for branch in function.branches:
                if branch.key in seen:
                    continue
                seen.add(branch.key)
                chosen.append(branch)
        return self.stats_for(chosen)

    def file_stats(self, path):
        records = self.files.get(path, [])
        return self.stats_for(records)

    def all_stats(self):
        covered, total = 0, 0
        seen = set()
        for function in self.functions.values():
            for branch in function.branches:
                if branch.key in seen:
                    continue
                seen.add(branch.key)
                for _, count in branch.directions():
                    total += 1
                    covered += 1 if count > 0 else 0
        return {
            "total_branches": total,
            "covered_branches": covered,
            "missed_branches": total - covered,
            "branch_coverage_percent": round(covered * 100.0 / total, 2) if total else None,
        }

    def relative(self, path):
        if self.source_root:
            try:
                return Path(path).resolve().relative_to(self.source_root).as_posix()
            except ValueError:
                pass
        return Path(path).as_posix()

    def module_of(self, path):
        relative = self.relative(path)
        parent = Path(relative).parent.as_posix()
        return "." if parent in ("", ".") else parent


def _load_branches(records, filenames):
    branches = []
    for row in records or []:
        if len(row) < 9:
            continue
        line, column, end_line, end_column, true_count, false_count, file_id, _expanded, kind = row[:9]
        path = filenames[file_id] if 0 <= file_id < len(filenames) else ""
        branches.append(
            Branch(str(path), int(line), int(column), int(end_line), int(end_column),
                   int(true_count), int(false_count), int(kind))
        )
    return branches


def load_coverage(json_path, source_root=None):
    path = Path(json_path)
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise CoverageError(f"cannot read coverage JSON {path}: {error}") from error
    data = document.get("data")
    if not isinstance(data, list) or not data:
        raise CoverageError("coverage JSON has no data array")
    entry = data[0]
    functions = {}
    for item in entry.get("functions") or []:
        name = item.get("name")
        if not name:
            continue
        filenames = [str(name) for name in item.get("filenames") or []]
        functions[name] = FunctionCoverage(
            symbol=name,
            files=filenames,
            branches=_load_branches(item.get("branches"), filenames),
            count=int(item.get("count") or 0),
        )
    files = {}
    for item in entry.get("files") or []:
        filename = str(item.get("filename") or "")
        if not filename:
            continue
        files[filename] = _load_branches(item.get("branches"), [filename])
    totals = (entry.get("totals") or {}).get("branches") or {}
    binaries = sorted({value for function in functions.values() for value in function.files if value})
    aliases = {}
    for name in functions:
        aliases[name] = name
    root = Path(source_root).resolve() if source_root else None
    return CoverageData(functions, files, aliases, totals, binaries, root)


def build_aliases(coverage, callgraph_symbols):
    """Join coverage names to callgraph symbols without guessing collisions.

    Exact matches win. A ``file.c:symbol`` coverage name is accepted only when
    the suffix names exactly one callgraph symbol and no exact match exists.
    """
    symbols = set(callgraph_symbols)
    aliases = {name: name for name in coverage.functions if name in symbols}
    diagnostics = []
    for name in coverage.functions:
        if name in aliases:
            continue
        suffix = name.rsplit(":", 1)[-1] if ":" in name else name
        matches = [symbol for symbol in symbols if symbol == suffix]
        if len(matches) == 1:
            aliases[name] = matches[0]
        elif name in symbols:
            aliases[name] = name
        else:
            diagnostics.append({"coverage_name": name, "reason": "no unique callgraph symbol"})
    for target in list(aliases.values()):
        aliases.setdefault(target, target)
    for name in list(coverage.functions):
        target = aliases.get(name, name)
        if target == name:
            continue
        function = coverage.functions.pop(name)
        function.symbol = target
        if target in coverage.functions:
            existing = coverage.functions[target]
            seen = {branch.key for branch in existing.branches}
            existing.branches.extend(branch for branch in function.branches
                                     if branch.key not in seen)
            existing.files = sorted(set(existing.files) | set(function.files))
        else:
            coverage.functions[target] = function
    coverage.aliases = aliases
    return diagnostics


def page(items, offset=0, limit=DEFAULT_PAGE, key=None):
    limit = max(1, min(int(limit), MAX_PAGE))
    offset = max(0, int(offset))
    ordered = sorted(items, key=key) if key else list(items)
    chunk = ordered[offset:offset + limit]
    return {
        "items": chunk,
        "total": len(ordered),
        "offset": offset,
        "limit": limit,
        "next_offset": offset + limit if offset + limit < len(ordered) else None,
    }


def source_slice(source_root, files, path, start=None, end=None):
    root = Path(source_root).resolve() if source_root else None
    candidate = Path(path)
    if not candidate.is_absolute() and root:
        candidate = root / candidate
    resolved = candidate.resolve()
    if root is not None and root not in resolved.parents and resolved != root:
        raise CoverageError("source path escapes the configured source root")
    if resolved.suffix not in {".c", ".h", ".cc", ".cpp", ".hpp"}:
        raise CoverageError("source path is not a C/C++ file")
    try:
        lines = resolved.read_text(errors="replace").splitlines()
    except OSError as error:
        raise CoverageError(f"cannot read {resolved}: {error}") from error
    start = max(1, int(start or 1))
    end = int(end) if end else start + DEFAULT_SOURCE_LINES - 1
    end = min(max(start, end), len(lines), start + MAX_SOURCE_LINES - 1)
    snippet = [{"line": number, "text": lines[number - 1]}
               for number in range(start, end + 1)]
    branches = []
    for record in files:
        if Path(record.file).name != resolved.name and str(record.file) != str(resolved):
            continue
        if record.end_line < start or record.line > end:
            continue
        branches.append({
            "file": str(record.file),
            "line": record.line,
            "column": record.column,
            "end_line": record.end_line,
            "end_column": record.end_column,
            "true_count": record.true_count,
            "false_count": record.false_count,
        })
    return {"path": str(resolved), "start": start, "end": end,
            "lines": snippet, "branches": branches}
