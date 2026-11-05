"""Stable ID allocation under independent, asynchronous extraction lists."""

import asyncio
from dataclasses import replace

import pytest

from rsc.protocol.ids import (
    AllocationKey,
    GlobalIdAllocator,
    IdConsistencyError,
    deterministic_id,
)
from rsc.protocol.merge import stable_id


def key(**changes):
    return replace(
        AllocationKey("FTP", "steps", "interactions", "list-001", "rfc959/block-1", "local:send"),
        **changes,
    )


def test_hash_ids_keep_the_first_eight_hex_characters():
    assert deterministic_id(key()) == "step-1b39bfdb"
    assert stable_id("group", ["a", "b"]) == "group-0473ef2d"


def test_concurrent_retry_and_cross_list_ids_are_stable():
    async def run(reverse):
        allocator = GlobalIdAllocator("FTP")
        keys = [key(list_id=f"list-{number:03}") for number in range(24)]
        calls = list(reversed(keys)) if reverse else keys
        responses = await asyncio.gather(*(allocator.reserve_many([item]) for item in calls * 3))
        result = {
            item: identifier for response in responses for item, identifier in response.items()
        }
        assert len(set(result.values())) == 24
        assert len(allocator.reservations) == 24
        return result

    assert asyncio.run(run(False)) == asyncio.run(run(True))


def test_collision_rolls_back_entire_batch_and_keeps_existing_reservations():
    async def run():
        allocator = GlobalIdAllocator(
            "FTP",
            id_factory=lambda item: "taken" if item.local_id != "local:free" else "free",
        )
        first = key()
        await allocator.reserve_many([first])
        with pytest.raises(IdConsistencyError, match="collision"):
            await allocator.reserve_many([key(local_id="local:free"), key(list_id="list-002")])
        assert dict(allocator.reservations) == {"taken": first}
        assert key(local_id="local:free") not in allocator.allocations
        assert await allocator.reserve_many([first]) == {first: "taken"}

    asyncio.run(run())


def test_new_batch_internal_collision_does_not_reserve_any_ids():
    async def run():
        allocator = GlobalIdAllocator("FTP", id_factory=lambda _: "same")
        with pytest.raises(IdConsistencyError):
            await allocator.reserve_many([key(), key(stage="repair")])
        assert not allocator.reservations
        assert not allocator.allocations

    asyncio.run(run())


def test_only_step_ids_are_allocated_and_retries_keep_them():
    async def run():
        allocator = GlobalIdAllocator("FTP")
        keys = [key(), key(local_id="local:reply", stage="repair", list_id=None)]
        allocated = await allocator.reserve_many(keys)
        assert len(set(allocated.values())) == len(keys)
        assert await allocator.reserve_many(keys) == allocated
        with pytest.raises(TypeError):
            allocator.reservations["injected"] = keys[0]

    asyncio.run(run())


def test_invalid_allocation_keys_are_rejected():
    with pytest.raises(ValueError, match="local:"):
        key(local_id="already-formal")
    with pytest.raises(ValueError, match="unsupported ID kind"):
        key(kind="messages")
    with pytest.raises(ValueError, match="owner"):
        key(owner_key=key())
    with pytest.raises(ValueError, match="protocol"):
        asyncio.run(GlobalIdAllocator("RTSP").reserve_many([key()]))
