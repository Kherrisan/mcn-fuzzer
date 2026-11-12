"""Balanced contiguous source assignments, with chapter locality as a tie breaker."""

from itertools import accumulate, pairwise
from math import ceil

import tiktoken


def source_weight(batch):
    """Estimate reading work from original text, excluding delivery metadata."""
    encoding = tiktoken.get_encoding("cl100k_base")
    return sum(len(encoding.encode(s["text"], disallowed_special=())) for s in batch["segments"])


def partition_batches(batches, workers, *, capacity=None):
    """Minimize maximum load, then prefer chapter boundaries within 10% of optimum.

    Returns nonempty contiguous lists of existing batch names. Every input batch is
    assigned once; neither its source ranges nor the caller's objects are changed.
    """
    if type(workers) is not int or workers < 1:
        raise ValueError("workers must be a positive integer")
    if not batches:
        raise ValueError("no source batches to assign")
    names = [batch["batch_id"] for batch in batches]
    if len(set(names)) != len(names):
        raise ValueError("duplicate source batch names")
    weights = [source_weight(batch) for batch in batches]
    if any(weight <= 0 for weight in weights):
        raise ValueError("source batches must contain text")
    n, workers = len(batches), min(workers, len(batches))
    prefix = [0, *accumulate(weights)]

    # Linear partition DP: the first pass finds the smallest possible maximum load.
    best = {0: 0}
    for count in range(1, workers + 1):
        current = {}
        for end in range(count, n + 1):
            current[end] = min(
                max(load, prefix[end] - prefix[start])
                for start, load in best.items()
                if start < end
            )
        best = current
    if capacity is not None and best[n] > capacity:
        raise ValueError("source partition exceeds context capacity")
    limit = best[n] * 11 // 10
    if capacity is not None:
        limit = min(limit, capacity)

    # Among partitions inside that bound, minimize chapter cuts, document cuts,
    # and finally the cut positions. These costs are additive over boundaries.
    best = {0: (0, 0, ())}
    for count in range(1, workers + 1):
        current = {}
        for end in range(count, n + 1):
            candidates = []
            for start, (chapters, documents, cuts) in best.items():
                if start >= end or prefix[end] - prefix[start] > limit:
                    continue
                if start:
                    left, right = batches[start - 1], batches[start]
                    same_doc = left["doc_id"] == right["doc_id"]
                    left_path = left["segments"][-1]["section_path"]
                    right_path = right["segments"][0]["section_path"]
                    same_chapter = same_doc and left_path[:1] == right_path[:1]
                    candidates.append(
                        (chapters + same_chapter, documents + same_doc, (*cuts, start))
                    )
                else:
                    candidates.append((chapters, documents, cuts))
            if candidates:
                current[end] = min(candidates)
        best = current
    cuts = (0, *best[n][2], n)
    return [names[start:end] for start, end in pairwise(cuts)]


def source_capacity(context_tokens=258400, fixed_tokens=45000):
    """Initial heuristic: reserve a quarter of the window and double source cost."""
    if type(context_tokens) is not int or context_tokens <= 0:
        raise ValueError("context tokens must be a positive integer")
    if type(fixed_tokens) is not int or fixed_tokens < 0:
        raise ValueError("fixed tokens must be a nonnegative integer")
    capacity = (context_tokens * 3 // 4 - fixed_tokens) // 2
    if capacity <= 0:
        raise ValueError("fixed context overhead exceeds the context budget")
    return capacity


def plan_parsers(batches, *, parse_workers="auto", parse_max_workers=4,
                 parse_context_tokens=258400, parse_fixed_tokens=45000):
    """Choose whole-source partitions independently from active concurrency."""
    capacity = source_capacity(parse_context_tokens, parse_fixed_tokens)
    if not batches:
        raise ValueError("no source batches to assign")
    if type(parse_max_workers) is not int or parse_max_workers < 1:
        raise ValueError("parse_max_workers must be a positive integer")
    if parse_workers != "auto" and (type(parse_workers) is not int or parse_workers < 1):
        raise ValueError("parse_workers must be auto or a positive integer")
    weights = {batch["batch_id"]: source_weight(batch) for batch in batches}
    if len(weights) != len(batches) or any(weight <= 0 for weight in weights.values()):
        raise ValueError("source batches must have unique names and nonempty text")
    total = sum(weights.values())
    automatic = parse_workers == "auto"
    warnings = []
    if automatic:
        if max(weights.values()) > capacity:
            raise ValueError("source batch exceeds context capacity; split it during preprocessing")
        count = max(1, ceil(total / capacity))
        while True:
            try:
                groups = partition_batches(batches, count, capacity=capacity)
                break
            except ValueError:
                count += 1
                if count > len(batches):
                    raise
    else:
        groups = partition_batches(batches, parse_workers)
        if parse_workers > len(batches):
            warnings.append("requested more parsers than nonempty source batches")
    partitions = []
    for index, names in enumerate(groups, 1):
        size = sum(weights[name] for name in names)
        name = f"parser-{index}"
        if size > capacity:
            warnings.append(f"{name}: {size} source tokens exceed capacity {capacity}")
        partitions.append({"name": name, "batch_ids": names, "source_tokens": size,
                           "estimated_context_tokens": parse_fixed_tokens + 2 * size})
    return {
        "partitions": partitions,
        "parallel_agents": min(len(partitions), parse_max_workers),
        "capacity_tokens": capacity,
        "source_tokens": total,
        "warnings": warnings,
        "settings": {"parse_workers": parse_workers, "parse_max_workers": parse_max_workers,
                     "parse_context_tokens": parse_context_tokens,
                     "parse_fixed_tokens": parse_fixed_tokens},
    }
