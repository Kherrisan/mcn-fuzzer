"""Deterministic source partitioning without constructing extraction windows."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .context import Corpus

PARTITION_VERSION = "byte-balanced-boundaries-v1"


@dataclass(frozen=True, slots=True)
class FocusBlock:
    doc_id: str
    block_id: str
    sequence: int
    list_id: str

    @property
    def key(self) -> tuple[str, str]:
        return self.doc_id, self.block_id

    def ref(self) -> dict[str, str]:
        return {"doc_id": self.doc_id, "block_id": self.block_id}


@dataclass(frozen=True, slots=True)
class BlockList:
    list_id: str
    order: int
    blocks: tuple[FocusBlock, ...]


@dataclass(frozen=True, slots=True)
class BlockListPlan:
    requested_lists: int
    lists: tuple[BlockList, ...]
    partition_version: str = PARTITION_VERSION

    def snapshot(self) -> dict:
        return {
            "partition_version": self.partition_version,
            "requested_lists": self.requested_lists,
            "lists": [
                {
                    "list_id": row.list_id,
                    "order": row.order,
                    "blocks": [asdict(block) for block in row.blocks],
                }
                for row in self.lists
            ],
        }

    def focus_map(self) -> dict[tuple[str, str], FocusBlock]:
        return {block.key: block for row in self.lists for block in row.blocks}


def plan_block_lists(corpus: Corpus, requested_lists: int = 8) -> BlockListPlan:
    """Balance UTF-8 bytes, preferring nearby document and section boundaries.

    The worker count is deliberately absent. Changing execution concurrency cannot
    change source ownership, visibility, or ID allocation keys.
    """

    if type(requested_lists) is not int or requested_lists < 1:
        raise ValueError("block_lists must be a positive integer")
    ordered = [
        (doc_id, block)
        for doc_id in sorted(corpus.documents)
        for block in sorted(corpus.documents[doc_id]["blocks"], key=lambda row: row["order"])
        if block["text"].strip()
    ]
    if not ordered:
        raise ValueError("input documents contain no nonempty focus blocks")
    count = min(requested_lists, len(ordered))
    prefix = [0]
    for _, block in ordered:
        prefix.append(prefix[-1] + len(block["text"].encode("utf-8")))
    target_size = prefix[-1] / count
    cuts = [0]
    for number in range(1, count):
        target = prefix[-1] * number / count
        candidates = range(cuts[-1] + 1, len(ordered) - (count - number) + 1)

        def score(position: int, target: float = target) -> tuple[float, int]:
            before, after = ordered[position - 1], ordered[position]
            if before[0] != after[0]:
                penalty = 0
            elif before[1]["section_path"] != after[1]["section_path"]:
                penalty = target_size * 0.03
            else:
                penalty = target_size * 0.15
            return abs(prefix[position] - target) + penalty, position

        cuts.append(min(candidates, key=score))
    cuts.append(len(ordered))
    lists = []
    for order, (start, end) in enumerate(zip(cuts, cuts[1:], strict=False)):
        list_id = f"list-{order + 1:03d}"
        blocks = tuple(
            FocusBlock(ordered[index][0], ordered[index][1]["block_id"], index, list_id)
            for index in range(start, end)
        )
        lists.append(BlockList(list_id, order, blocks))
    return BlockListPlan(requested_lists, tuple(lists))
