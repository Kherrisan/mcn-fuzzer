"""Persistent typed agent history with deterministic, retrievable compaction."""

from __future__ import annotations

import json
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from rsc.llm.budget import RequestBudgetError, RequestLimits, estimate_tokens

from .contract import COLLECTIONS
from .options import ContextOptions

KINDS = COLLECTIONS


def resolve_context_capacity(model, limits: RequestLimits, context_options: ContextOptions) -> int:
    """Resolve an explicit stage limit, global override, or SDK model capacity."""

    capacity = (
        limits.context_window_tokens
        or context_options.max_tokens
        or getattr(model, "context_window", None)
    )
    if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
        raise ValueError(
            "Model context capacity is unknown; configure compile.context.max_tokens "
            "or request_limits.<stage>.context_window_tokens"
        )
    return capacity


def _json_value(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return deepcopy(value)


def _program_message(value: dict, *, metadata: dict | None = None) -> ModelRequest:
    return ModelRequest(
        [UserPromptPart(json.dumps(value, ensure_ascii=False, separators=(",", ":")))],
        metadata={"rsc_session": True, **(metadata or {})},
    )


def _row_key(row: dict) -> str:
    if row.get("name"):
        return "name:" + row["name"]
    return json.dumps(
        {key: row.get(key) for key in ("prior_step", "dependent_step", "condition")},
        sort_keys=True,
    )


@dataclass
class SessionTurn:
    """A single trial, with a fresh run budget and an independently audited outcome."""

    run_id: str
    phase: str
    stage: str
    payload: dict
    status: str = "running"
    committed: bool | None = None


class SessionState:
    """One writer's durable effective history, backed by the canonical model store.

    Compaction changes only the effective prefix sent on subsequent requests.
    Audit records remain append-only and contain actual model-authored output;
    generated snapshots are always application/user messages, never assistant text.
    """

    def __init__(
        self,
        session_id: str,
        *,
        context_options: ContextOptions | None = None,
        events: Any = None,
    ) -> None:
        self.session_id = session_id
        self.context_options = context_options or ContextOptions()
        self.events = events
        self.messages: list = []
        self.turns: list[SessionTurn] = []
        self.phase: str | None = None
        self.model_revision = 0
        self.model: dict = {}
        self.issues: list = []
        self.pending_groups: list = []
        self.active_turn: SessionTurn | None = None
        self.last_input_tokens = 0
        self.counting_method: str | None = None
        self.compaction_count = 0
        self.catalog_only = False
        self._recent_touches: list[tuple[str, str]] = []
        self._audit("created")

    def _audit(self, event: str, **data) -> None:
        if self.events is not None:
            self.events.append(
                "context-compactions.jsonl" if event == "compaction" else "session-messages.jsonl",
                {
                    "session_id": self.session_id,
                    "event": event,
                    "phase": self.phase,
                    "model_revision": self.model_revision,
                    **data,
                },
            )

    def update_model(self, model, issues, revision: int, *, pending_groups=None) -> None:
        """Refresh canonical state without rewriting conversation history."""

        self.model = _json_value(model)
        self.issues = _json_value(issues)
        self.model_revision = revision
        if pending_groups is not None:
            self.pending_groups = _json_value(pending_groups)

    def handoff(self, phase: str, model, issues, revision: int, *, pending_groups=None) -> None:
        """Append the authoritative model revision at a stage boundary."""

        if self.active_turn is not None:
            raise RuntimeError("Cannot hand off a session while a trial is running")
        self.phase = phase
        self.update_model(model, issues, revision, pending_groups=pending_groups)
        message = _program_message(
            {
                "event": "stage_handoff",
                "phase": phase,
                "model_revision": revision,
                "instruction": "This committed model supersedes earlier object definitions.",
                "model": self.model,
                "unresolved": self.issues,
            },
            metadata={"kind": "handoff"},
        )
        self.messages.append(message)
        self._audit(
            "handoff", messages=ModelMessagesTypeAdapter.dump_python([message], mode="json")
        )

    def begin_turn(self, run_id: str, stage: str, phase: str, payload: dict) -> None:
        """Acquire the session for one agent trial without clearing prior history."""

        if self.active_turn is not None:
            raise RuntimeError(f"Session {self.session_id} already has an active trial")
        self.phase = phase
        self.active_turn = SessionTurn(run_id, phase, stage, deepcopy(payload))
        self.turns.append(self.active_turn)
        self._audit("turn_start", run_id=run_id, stage=stage, focus=self._focus())

    def record_commit(
        self, run_id: str, items, issues=(), *, accepted: bool = True, changes=None
    ) -> None:
        """Record the subset actually committed, including recovery from a failed run.

        This is an application receipt, not reconstructed model output. Rejected
        proposals remain in the audit trail but are not retained as accepted
        responses during source-history compaction.
        """

        if self.active_turn is not None:
            raise RuntimeError("Commit receipts must follow the completed trial")
        for turn in self.turns:
            if turn.run_id == run_id:
                turn.committed = accepted
        items = _json_value(items) if accepted else {}
        for kind in KINDS:
            for row in items.get(kind, []):
                reference = kind, _row_key(row)
                if reference in self._recent_touches:
                    self._recent_touches.remove(reference)
                self._recent_touches.append(reference)
        self._recent_touches = self._recent_touches[-64:]
        receipt = {
            "event": "commit_receipt",
            "accepted": accepted,
            "model_revision": self.model_revision,
            "items": items,
            "unresolved": _json_value(issues),
        }
        if changes is not None:
            receipt["changes"] = _json_value(changes)
        message = _program_message(
            receipt,
            metadata={"kind": "commit_receipt"},
        )
        self.messages.append(message)
        self._audit(
            "commit",
            run_id=run_id,
            accepted=accepted,
            messages=ModelMessagesTypeAdapter.dump_python([message], mode="json"),
        )

    def _focus(self) -> dict:
        payload = self.active_turn.payload if self.active_turn else {}
        result = {}
        for key in ("doc_id", "block_id", "issue", "group", "tracking_id"):
            if key in payload:
                result[key] = deepcopy(payload[key])
        block = payload.get("focus_block", payload.get("block"))
        if isinstance(block, dict):
            for key in ("doc_id", "block_id"):
                if key in block:
                    result[key] = block[key]
        return result

    def _prefix_end(self, messages: list) -> int:
        if self.active_turn is None:
            raise RuntimeError("No active session trial")
        for index, message in enumerate(messages):
            if message.run_id == self.active_turn.run_id:
                return index
        # Frameworks may normalize an unanswered request. Preserve that entire
        # final request, which contains the current user input, in that case.
        return max(0, len(messages) - 1)

    def _snapshot(self) -> ModelRequest:
        return _program_message(
            {
                "event": "model_snapshot",
                "model_revision": self.model_revision,
                "instruction": "Use this committed state instead of earlier definitions.",
                "model": self.model,
                "unresolved": self.issues,
                "pending_groups": self.pending_groups,
            },
            metadata={"kind": "snapshot"},
        )

    def _pruned_prefix(self, prefix: list) -> list:
        accepted = {
            turn.run_id
            for turn in self.turns
            if turn.status == "ok" and turn.committed is not False
        }
        anchor_index = -1
        for index, message in enumerate(prefix):
            if isinstance(message, ModelRequest) and (message.metadata or {}).get("kind") in {
                "snapshot",
                "handoff",
                "catalog",
            }:
                anchor_index = index
        last_outputs = {}
        for message in prefix[anchor_index + 1 :]:
            if isinstance(message, ModelResponse) and message.run_id in accepted:
                if not any(isinstance(part, ToolCallPart) for part in message.parts):
                    last_outputs[message.run_id] = message
        result = []
        # The latest handoff is sufficient; older snapshots explicitly became stale.
        if anchor_index >= 0:
            result.append(prefix[anchor_index])
        turn_by_id = {turn.run_id: turn for turn in self.turns}
        for message in prefix[anchor_index + 1 :]:
            if last_outputs.get(message.run_id) is message:
                turn = turn_by_id[message.run_id]
                result.append(
                    _program_message(
                        {"event": "accepted_turn", "stage": turn.stage, "phase": turn.phase},
                        metadata={"kind": "receipt"},
                    )
                )
                result.append(message)
            elif (
                isinstance(message, ModelRequest)
                and (message.metadata or {}).get("kind") == "commit_receipt"
            ):
                result.append(message)
        return result

    def _catalog_entries(self) -> list[tuple[str, dict]]:
        focus = json.dumps(self._focus(), ensure_ascii=False).casefold()
        entries = []
        for kind in KINDS:
            for item in self.model.get(kind, []):
                name = item.get("name") or (
                    f"{item.get('prior_step', '')} -> {item.get('dependent_step', '')}"
                )
                if not name.strip(" ->"):
                    continue
                entry = {"name": name}
                if item.get("description"):
                    entry["description"] = item["description"]
                entries.append((kind, entry))
        # Prefer names mentioned by the current issue/group if only a subset fits.
        return sorted(entries, key=lambda entry: entry[1]["name"].casefold() not in focus)

    def _recent_results(self) -> list[tuple[str, dict]]:
        """Prefer recently committed canonical definitions relevant to current work."""

        focus = json.dumps(self._focus(), ensure_ascii=False).casefold()
        rows = []
        for kind, key in reversed(self._recent_touches):
            # Resolve against the latest canonical model, so a handoff cannot
            # revive the older definition that originally touched this name.
            rows.extend(
                (kind, deepcopy(row))
                for row in reversed(self.model.get(kind, []))
                if _row_key(row) == key
            )
        rows.sort(
            key=lambda entry: (
                not any(
                    entry[1].get(key) and entry[1][key].casefold() in focus
                    for key in ("name", "prior_step", "dependent_step")
                )
            )
        )
        return rows[:8]

    def _catalog(
        self,
        entries: list[tuple[str, dict]],
        *,
        truncated: bool,
        recent_results: Sequence[tuple[str, dict]] = (),
    ) -> ModelRequest:
        catalog = {kind: [] for kind in KINDS}
        for kind, item in entries:
            catalog[kind].append(item)
        return _program_message(
            {
                "event": "name_catalog",
                "model_revision": self.model_revision,
                "instruction": (
                    "Full definitions remain in the object store. Use search_objects and "
                    "lookup_object when needed. Read a complete definition before editing it."
                ),
                "catalog": catalog,
                "recent_results": {
                    kind: [row for row_kind, row in recent_results if row_kind == kind]
                    for kind in KINDS
                },
                "truncated": truncated,
                "current_work": self._focus(),
            },
            metadata={"kind": "catalog"},
        )

    def prepare_messages(
        self,
        messages: list,
        parameters: dict,
        settings: dict,
        limits: RequestLimits,
        model,
    ) -> list:
        """Compact completed history and return the same effective message list."""

        capacity = resolve_context_capacity(model, limits, self.context_options)
        reserve = limits.output_token_reserve or settings.get("max_tokens", 8192)
        usable = capacity - reserve - limits.safety_tokens
        if usable <= 0:
            raise RequestBudgetError("context budget leaves no room for input")
        model_name = getattr(model, "model_name", "")

        def measure(candidate):
            value = {
                "messages": ModelMessagesTypeAdapter.dump_python(candidate, mode="json"),
                "model_settings": settings,
                "request_parameters": parameters,
            }
            tokens, method = estimate_tokens(value, model_name)
            size = (
                len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                + 1024
            )
            return tokens, size, method

        before_tokens, before_bytes, method = measure(messages)
        tokens, size = before_tokens, before_bytes
        reason = "byte_limit" if size > limits.max_input_bytes else "token_threshold"
        if tokens >= usable * self.context_options.compact_at or size > limits.max_input_bytes:
            current_index = self._prefix_end(messages)
            prefix, current = messages[:current_index], messages[current_index:]
            target = usable * self.context_options.compact_to
            levels = []
            if prefix:
                messages[:] = self._pruned_prefix(prefix) + current
                levels.append(1)
                tokens, size, method = measure(messages)
                if tokens > target or size > limits.max_input_bytes:
                    messages[:] = [self._snapshot(), *current]
                    levels.append(2)
                    tokens, size, method = measure(messages)
                if tokens > target or size > limits.max_input_bytes:
                    entries = self._catalog_entries()
                    total_entries = len(entries)
                    recent = self._recent_results()
                    messages[:] = [
                        self._catalog(entries, truncated=False, recent_results=recent),
                        *current,
                    ]
                    levels.append(3)
                    tokens, size, method = measure(messages)
                    # Recent complete definitions are optional. Remove them
                    # before reducing the name directory or current work.
                    while recent and (tokens > target or size > limits.max_input_bytes):
                        recent.pop()
                        messages[:] = [
                            self._catalog(entries, truncated=False, recent_results=recent),
                            *current,
                        ]
                        tokens, size, method = measure(messages)
                    if tokens > target or size > limits.max_input_bytes:
                        entries = [(kind, {"name": item["name"]}) for kind, item in entries]
                        messages[:] = [
                            self._catalog(entries, truncated=False, recent_results=recent),
                            *current,
                        ]
                        tokens, size, method = measure(messages)
                    while entries and (tokens > target or size > limits.max_input_bytes):
                        entries = entries[: len(entries) // 2]
                        messages[:] = [
                            self._catalog(
                                entries,
                                truncated=len(entries) < total_entries,
                                recent_results=recent,
                            ),
                            *current,
                        ]
                        tokens, size, method = measure(messages)
                    self.catalog_only = True
                elif 2 in levels:
                    self.catalog_only = False
                self.compaction_count += 1
                self._audit(
                    "compaction",
                    run_id=self.active_turn.run_id,
                    stage=self.active_turn.stage,
                    focus=self._focus(),
                    reason=reason,
                    levels=levels,
                    before_tokens=before_tokens,
                    after_tokens=tokens,
                    before_bytes=before_bytes,
                    after_bytes=size,
                    counting_method=method,
                    retained_current_messages=len(current),
                    replacement_prefix=ModelMessagesTypeAdapter.dump_python(
                        messages[: len(messages) - len(current)], mode="json"
                    ),
                )
        self.last_input_tokens = tokens
        self.counting_method = method
        self._audit(
            "request_budget",
            run_id=self.active_turn.run_id,
            stage=self.active_turn.stage,
            input_tokens=tokens,
            context_limit_tokens=capacity,
            output_reserve=reserve,
            safety_tokens=limits.safety_tokens,
            request_bytes=size,
            counting_method=method,
        )
        effective_limits = RequestLimits(
            max_input_bytes=limits.max_input_bytes,
            context_window_tokens=capacity,
            output_token_reserve=limits.output_token_reserve,
            safety_tokens=limits.safety_tokens,
        )
        effective_limits.check(size, reserve, input_tokens=tokens)
        return messages

    def finish_turn(self, messages: list, *, status: str, error: str | None = None) -> None:
        """Persist actual history and close tool calls interrupted by a failed trial."""

        if self.active_turn is None:
            return
        self.active_turn.status = status
        self.messages = list(messages)
        pending = {}
        for message in self.messages:
            for part in message.parts:
                if isinstance(part, ToolCallPart):
                    pending[part.tool_call_id] = part
                elif isinstance(part, ToolReturnPart):
                    pending.pop(part.tool_call_id, None)
                elif isinstance(part, RetryPromptPart) and part.tool_call_id:
                    pending.pop(part.tool_call_id, None)
        if pending:
            self.messages.append(
                ModelRequest(
                    [
                        ToolReturnPart(
                            call.tool_name,
                            {"status": "error", "message": error or "Agent trial interrupted"},
                            call.tool_call_id,
                            outcome="interrupted",
                        )
                        for call in pending.values()
                    ],
                    run_id=self.active_turn.run_id,
                )
            )
        new_messages = [
            message for message in self.messages if message.run_id == self.active_turn.run_id
        ]
        self._audit(
            "turn_end",
            run_id=self.active_turn.run_id,
            stage=self.active_turn.stage,
            focus=self._focus(),
            status=status,
            error=error,
            closed_tool_calls=len(pending),
            messages=ModelMessagesTypeAdapter.dump_python(new_messages, mode="json"),
        )
        # Completed turns need identity and status, not a second retained copy of
        # every source block after it has been removed from effective history.
        self.active_turn.payload = self._focus()
        self.active_turn = None
