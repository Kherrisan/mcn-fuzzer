"""Parallel draft isolation, durable ownership, atomic writes and retry fencing."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from rsc.workflow.store import Store, StoreError


def identity(store, worker):
    return store.authenticate((store.root / f"{worker}.token").read_text())


@pytest.fixture
def store(tmp_path):
    s = Store.create(tmp_path / "run", {"phase": "preprocess", "name": "example"})
    coordinator = identity(s, "coordinator")
    s.install_sources(
        coordinator,
        {"doc-z": {"text": "first"}, "doc-a": {"text": "second"}},
        [
            {"batch_id": "b1"},
            {"batch_id": "b2"},
            {"batch_id": "b3"},
        ],
    )
    s.activate(
        coordinator,
        [
            {
                "name": "parse/parser-1",
                "worker": "parser-1",
                "draft": "parse/parser-1",
                "batches": ["b1", "b2"],
            },
            {
                "name": "parse/parser-2",
                "worker": "parser-2",
                "draft": "parse/parser-2",
                "batches": ["b3"],
            },
        ],
        phase="parse",
        round_=0,
        base_revision=0,
    )
    yield s
    s.close()


def claim(s, worker="parser-1"):
    who = identity(s, worker)
    s.claim(who, who.assignment, who.attempt)
    return who


def mutate(s, who, name="CLIENT", revision=0, operation="add", changes=None):
    value = {"kind": "parties", "value": {"name": name, "description": who.worker}, "evidence": []}
    changes = changes if changes is not None else {"parties/" + name: (None, value)}
    return s.commit_records(
        who,
        revision=revision,
        operation_id=operation,
        arguments={"name": name, "revision": revision},
        changes=changes,
        result={"revision": revision + 1, "name": name},
    )


def report(s, who, batch, revision=0):
    receipt = s.deliver(who, [batch])[batch]
    value = {"batch": batch, "receipt": receipt, "report": {"summary": "source compared"}}
    s.complete(who, [value], revision)
    return value


def test_parallel_writers_have_independent_names_revisions_and_operations(store):
    # Separate connections emulate concurrent arrivals and a service restart;
    # same-name records and same operation names must never collide.
    barrier = Barrier(2)

    def run(worker):
        s = Store(store.root)
        try:
            who = claim(s, worker)
            barrier.wait(timeout=5)
            return mutate(s, who)
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ["parser-1", "parser-2"]))
    assert [r["revision"] for r in results] == [1, 1]
    for worker in ("parser-1", "parser-2"):
        draft, records = store.draft("parse/" + worker)
        assert draft["revision"] == 1
        assert records["parties/CLIENT"]["value"]["description"] == worker
    assert list(store.sources()[0]) == ["doc-z", "doc-a"]


def test_agent_cannot_dispatch_prepare_or_claim_another_worker(store):
    who = identity(store, "parser-1")
    with pytest.raises(StoreError, match="coordinator"):
        store.record_send(who, who.assignment, 1)
    with pytest.raises(StoreError, match="coordinator"):
        store.install_sources(who, {}, [])
    with pytest.raises(StoreError, match="stale"):
        store.claim(who, "parse/parser-2", 1)
    with pytest.raises(StoreError, match="claim"):
        mutate(store, who)


def test_atomic_rollback_and_operation_retry_after_restart(store):
    who = claim(store)
    original = mutate(store, who)
    before = store.draft(who.assignment)
    with pytest.raises(StoreError, match="before-image"):
        mutate(
            store,
            who,
            revision=1,
            operation="broken",
            changes={
                "parties/CLIENT": (before[1]["parties/CLIENT"], None),
                "parties/missing": ({"imaginary": True}, None),
            },
        )
    assert store.draft(who.assignment) == before
    assert store.db.execute("SELECT COUNT(*) FROM history WHERE revision>0").fetchone()[0] == 1
    reopened = Store(store.root)
    try:
        assert mutate(reopened, who) == original
        with pytest.raises(StoreError, match="different arguments"):
            mutate(reopened, who, name="SERVER")
        with pytest.raises(StoreError, match="revision changed"):
            mutate(reopened, who, name="SERVER", operation="second")
        assert reopened.draft(who.assignment) == before
    finally:
        reopened.close()


def test_a_draft_write_does_not_rewrite_sources_other_drafts_or_workflow(store):
    who = claim(store)
    statements = []
    store.db.set_trace_callback(statements.append)
    mutate(store, who)
    store.db.set_trace_callback(None)
    writes = [q for q in statements if q.startswith(("INSERT", "UPDATE", "DELETE"))]
    assert writes
    assert not any(table in q for q in writes for table in ("batches", "documents", "workflow"))
    assert not any("parser-2" in q for q in writes)


def test_cross_range_reads_do_not_change_batch_ownership_and_bulk_reports_are_atomic(store):
    who = claim(store)
    receipts = store.deliver(who, ["b1", "b2", "b3"])
    reports = [{"batch": b, "receipt": receipts[b], "report": {"summary": b}} for b in ("b1", "b3")]
    with pytest.raises(StoreError, match="another worker"):
        store.complete(who, reports, 0)
    assert not store.completed(who.assignment)
    with pytest.raises(StoreError, match="source order"):
        report(store, who, "b2")
    report(store, who, "b1")
    report(store, who, "b2")
    assert store.completed(who.assignment) == {"b1", "b2"}


def test_receipt_cannot_be_fabricated_or_reused_by_another_worker(store):
    first, second = claim(store), claim(store, "parser-2")
    receipt = store.deliver(first, ["b3"])["b3"]
    with pytest.raises(StoreError, match="delivery"):
        store.complete(second, [{"batch": "b3", "receipt": receipt, "report": {}}], 0)
    with pytest.raises(StoreError, match="delivery"):
        store.complete(second, [{"batch": "b3", "receipt": "parse/parser-2/1/b3", "report": {}}], 0)


def test_retry_revokes_old_identity_and_receipts_but_retains_draft_and_reports(store):
    old_token = (store.root / "parser-1.token").read_text()
    old = claim(store)
    mutate(store, old)
    report(store, old, "b1", revision=1)
    old_receipt = store.deliver(old, ["b2"])["b2"]
    coordinator = identity(store, "coordinator")
    store.replace_attempt(
        coordinator,
        old.assignment,
        attempt=1,
        terminal={"name": "rsc-test-parser-1", "state": "exited", "exit_code": 1},
    )
    with pytest.raises(StoreError, match="revoked"):
        store.authenticate(old_token)
    with pytest.raises(StoreError, match="attempt changed"):
        mutate(store, old)
    with pytest.raises(StoreError, match="attempt changed"):
        store.deliver(old, ["b2"])
    new = claim(store)
    assert new.attempt == 2
    assert store.completed(new.assignment) == {"b1"}
    assert store.draft(new.assignment)[0]["revision"] == 1
    with pytest.raises(StoreError, match="delivery"):
        store.complete(new, [{"batch": "b2", "receipt": old_receipt, "report": {}}], 1)
    report(store, new, "b2", revision=1)
    store.submit(new)
    assert store.draft(new.assignment)[0]["sealed"] == 1


def test_timeouts_do_not_revoke_credentials_and_retries_stop_at_two(store):
    who = claim(store)
    coordinator = identity(store, "coordinator")
    for state in ("running", "idle", "timeout", "unknown"):
        with pytest.raises(StoreError, match="confirmed"):
            store.replace_attempt(
                coordinator, who.assignment, attempt=1, terminal={"name": "t1", "state": state}
            )
    assert identity(store, "parser-1") == who
    for attempt in range(1, 4):
        outcome = store.replace_attempt(
            coordinator,
            who.assignment,
            attempt=attempt,
            terminal={"name": "t1", "state": "missing"},
        )
    assert outcome["attempt"] == 3 and outcome["status"] == "failed"
    with pytest.raises(StoreError, match="revoked"):
        identity(store, "parser-1")
    other = claim(store, "parser-2")
    report(store, other, "b3")
    store.submit(other)
    with pytest.raises(StoreError, match="not been submitted"):
        store.activate(
            coordinator,
            [{"name": "merge/parser-2", "worker": "parser-2", "draft": "merged", "batches": []}],
            phase="merge",
            round_=0,
            base_revision=0,
        )


def test_send_intent_recovery_claim_and_idempotent_submission(store):
    coordinator = identity(store, "coordinator")
    who = identity(store, "parser-1")
    store.record_send(coordinator, who.assignment, 1)
    reopened = Store(store.root)
    try:
        assert reopened.assignment(who.assignment)["status"] == "sent"
        # Lost hc send reply does not mint a new attempt.
        reopened.record_send(coordinator, who.assignment, 1)
        reopened.claim(who, who.assignment, 1)
        with pytest.raises(StoreError, match="not awaiting dispatch"):
            reopened.record_send(coordinator, who.assignment, 1)
        with pytest.raises(StoreError, match="incomplete"):
            reopened.submit(who)
        first = report(reopened, who, "b1")
        reopened.complete(who, [first], 0)
        report(reopened, who, "b2")
        result = reopened.submit(who)
        assert reopened.submit(who) == result
        with pytest.raises(StoreError, match="submitted"):
            mutate(reopened, who)
    finally:
        reopened.close()


def test_parse_reports_do_not_complete_review_and_reviewer_cannot_write(store):
    coordinator = identity(store, "coordinator")
    for worker, batches in (("parser-1", ["b1", "b2"]), ("parser-2", ["b3"])):
        who = claim(store, worker)
        for b in batches:
            report(store, who, b)
        store.submit(who)
    store.activate(
        coordinator,
        [{"name": "review-1/reviewer-1", "worker": "reviewer-1", "batches": ["b1", "b2", "b3"]}],
        phase="review",
        round_=1,
        base_revision=1,
    )
    reviewer = claim(store, "reviewer-1")
    assert not store.completed(reviewer.assignment)
    with pytest.raises(StoreError, match="incomplete"):
        store.submit(reviewer)
    with pytest.raises(StoreError, match="only a parser"):
        mutate(store, reviewer)
    with pytest.raises(StoreError, match="revision changed"):
        report(store, reviewer, "b1", revision=0)


def test_legacy_database_is_rejected_without_mutation(tmp_path):
    path = tmp_path / "workflow.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE task (id INTEGER PRIMARY KEY, body TEXT)")
        db.execute("INSERT INTO task VALUES (1, ?)", (json.dumps({"phase": "published"}),))
    before = path.read_bytes()
    with pytest.raises(StoreError, match="archived source"):
        Store(tmp_path)
    assert path.read_bytes() == before


def test_private_credentials_recovered_after_interrupted_materialization(store):
    expected = identity(store, "parser-1")
    path = store.root / "parser-1.token"
    path.unlink()
    store.write_credentials()
    assert identity(store, "parser-1") == expected
    for path in (store.root / "workflow.sqlite", *store.root.glob("*.token")):
        assert path.stat().st_mode & 0o777 == 0o600
