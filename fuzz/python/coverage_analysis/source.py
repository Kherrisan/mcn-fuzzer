"""Cached C/C++ syntax ranges, independent of coverage branch locations."""

from functools import lru_cache
from pathlib import Path


class SourceError(ValueError):
    pass


@lru_cache(maxsize=64)
def _definitions(path, mtime_ns, size):
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_c
        import tree_sitter_cpp
    except ImportError as error:
        raise SourceError("source parsing requires tree-sitter, tree-sitter-c and tree-sitter-cpp") from error
    language = tree_sitter_c if Path(path).suffix == ".c" else tree_sitter_cpp
    tree = Parser(Language(language.language())).parse(Path(path).read_bytes())
    definitions = []
    pending = [tree.root_node]
    while pending:
        node = pending.pop()
        if node.type != "function_definition":
            pending.extend(node.named_children)
            continue
        body = node.child_by_field_name("body")
        declarator = node.child_by_field_name("declarator")
        while declarator is not None:
            child = declarator.child_by_field_name("declarator")
            if child is None:
                break
            declarator = child
        # Macro calls may produce interior errors without invalidating the
        # outer function braces. Never accept a recovered/missing closing brace.
        if (body is None or declarator is None or not body.children
                or body.children[-1].type != "}" or body.children[-1].is_missing):
            continue
        name = declarator.text.decode("utf-8", errors="replace")
        definitions.append((name, node.start_point.row + 1, body.end_point.row + 1))
    return tuple(definitions)


def function_range(path, symbol, branches):
    path = Path(path)
    if path.suffix.lower() not in {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx"}:
        raise SourceError(f"unsupported source language: {path}")
    stat = path.stat()
    definitions = _definitions(str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    # LLVM prefixes static C symbols with a filename. C++ mangled names can
    # instead be located by their coverage coordinates, without demangling.
    name = symbol.split(":", 1)[1] if ":" in symbol and "::" not in symbol else symbol
    candidates = [row for row in definitions if row[0] == name]
    if not candidates and symbol.startswith("_Z") and branches:
        candidates = list(definitions)
    if candidates and branches:
        candidates = _narrow(path, symbol, candidates, branches)
    if not candidates:
        raise SourceError(
            f"{symbol!r} has no function definition in {path}; macro-generated or "
            "fully inlined code has no syntax range, so read it by file and line")
    if len(candidates) != 1:
        raise SourceError(f"cannot uniquely locate function {symbol!r} in {path}: "
                          f"{len(candidates)} matching syntax ranges; check compiled source consistency")
    _, first, last = candidates[0]
    return first, last


def _narrow(path, symbol, candidates, branches):
    """Pick the definition that explains most branch records.

    llvm-cov attributes macro-expansion and inline branches to the enclosing
    function while recording the line where the macro or callee is declared.
    Those lines can sit outside the function body (a macro above the
    definition, an inline in a header), so requiring the full branch span to
    be inside the body rejects valid functions.
    """
    lines = [row.line for row in branches]
    scored = []
    for candidate in candidates:
        start, end = candidate[1], candidate[2]
        inside = sum(1 for line in lines if start <= line <= end)
        scored.append((inside, candidate))
    best = max(score for score, _ in scored)
    if best == 0:
        raise SourceError(
            f"no branch of {symbol!r} falls inside a definition in {path}; "
            "the coverage and source trees disagree")
    winners = [candidate for score, candidate in scored if score == best]
    return winners
