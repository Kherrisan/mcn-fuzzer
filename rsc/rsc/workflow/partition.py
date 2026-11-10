"""Balanced contiguous source assignments, with chapter locality as a tie breaker."""

from itertools import accumulate, pairwise

import tiktoken


def source_weight(batch):
    """Estimate reading work from original text, excluding delivery metadata."""
    encoding = tiktoken.get_encoding("cl100k_base")
    return sum(len(encoding.encode(s["text"], disallowed_special=())) for s in batch["segments"])


def partition_batches(batches, workers):
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
    limit = best[n] * 11 // 10

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
