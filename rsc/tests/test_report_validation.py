"""Actionable diagnostics for real report failures, preserving all-or-nothing submission."""

import copy

import pytest
import test_parallel_workflow as h

from rsc.workflow import WorkflowError
from rsc.workflow.audit import audit_run
from rsc.workflow.tools import schema

run = h.run


def test_report_schema_errors_identify_nested_fields_without_echoing_entire_reports(run):
    h.merged(run)
    status = h.claim(run, "reviewer-1")
    report = {
        "summary": "UNRELATED_LONG_REPORT_TEXT " * 1000,
        "objects": [],
        "requirements": [
            {
                "requirement": "Compare greeting",
                "objects": [],
                "result": "pass",
                "findings": [],
                "evidence": ["this field belongs to a finding"],
            }
        ],
    }
    with pytest.raises(WorkflowError) as error:
        h.call(
            run,
            "reviewer-1",
            "complete_batch",
            revision=1,
            reports=[
                {
                    "batch_id": status["pending_batches"][0],
                    "receipt": "not-yet-read",
                    "report": report,
                },
                {
                    "check_name": status["pending_checks"][0],
                    "report": {"summary": "Other report", "objects": "incorrect type"},
                },
            ],
        )
    text = str(error.value)
    assert all(
        value in text
        for value in ("reports[0].report.requirements[0]", "evidence", "reports[1].report.objects")
    )
    assert "UNRELATED_LONG_REPORT_TEXT" not in text and len(text) < 1000
    assert "revision=1" in text
    description = schema("complete_batch")["description"]
    assert "requirements?:Array<{requirement:string,objects:Array<" in description


def test_report_errors_include_all_rows_and_query_scopes_without_partial_writes(run):
    h.merged(run)
    status = h.claim(run, "reviewer-1")
    sources = h.read(run, "reviewer-1")
    batch, check = status["pending_batches"][0], status["pending_checks"][0]
    query = {"kind": "messages", "filters": {"name": "HELLO"}}
    requirement = {
        "requirement": "Client sends greeting",
        "objects": [],
        "result": "pass",
        "findings": [],
    }
    reports = [
        {
            "batch_id": batch,
            "receipt": sources[batch]["receipt"],
            "report": {
                "summary": "Compared source",
                "objects": [],
                "requirements": [requirement],
                "queries": [query],
            },
        },
        {
            "check_name": check,
            "report": {
                "summary": "Compared cross-range",
                "objects": [],
                "requirements": [{**requirement, "result": "finding"}],
            },
        },
    ]
    original = copy.deepcopy(reports)
    with pytest.raises(WorkflowError) as error:
        h.call(run, "reviewer-1", "complete_batch", revision=1, reports=reports)
    text = str(error.value)
    assert all(value in text for value in ("reports[0]", "reports[1]", batch, check, "revision=1"))
    assert 'query_model({"filters":{"name":"HELLO"},"kind":"messages"})' in text
    assert "owned model record" in text and "a discrepancy requires submitted findings" in text
    assert reports == original
    assert not run.store.completed(status["assignment"]["name"])
    assert run.db.execute("SELECT count(*) FROM conclusions").fetchone()[0] == 0

    # Following the suggested exact query resolves the failure; complete model delivery alone
    # does not silently rewrite the report's claimed query scope.
    h.call(run, "reviewer-1", "query_model")
    h.call(run, "reviewer-1", "query_model", **query)
    for item in reports:
        item["report"]["objects"] = [h.CLIENT]
        item["report"]["requirements"] = [requirement]
    h.call(run, "reviewer-1", "complete_batch", revision=1, reports=reports)
    h.call(run, "reviewer-1", "complete_batch", revision=1, reports=reports)
    assert run.store.completed(status["assignment"]["name"]) == {batch}
    assert run.db.execute("SELECT count(*) FROM conclusions").fetchone()[0] == 2
    h.call(run, "reviewer-1", "advance_phase")
    h.report_review(run, "reviewer-2")
    h.call(run, "reviewer-2", "advance_phase")
    h.advance(run)
    assert audit_run(run.root)["verified"]


@pytest.mark.parametrize("selectors", [{}, {"batch_id": "b1", "check_name": "c1"}])
def test_ambiguous_report_selector_is_rejected_before_submission(run, selectors):
    h.claim(run, "parser-1")
    with pytest.raises(WorkflowError, match=r"reports\[0\]: use exactly one"):
        h.call(
            run,
            "parser-1",
            "complete_batch",
            revision=0,
            reports=[{**selectors, "report": {"summary": "Invalid selector", "objects": []}}],
        )
    assert not run.store.completed("parse-0/parser-1")
