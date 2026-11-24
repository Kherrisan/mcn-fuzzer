"""Append model exchanges independently of the extraction checkpoint."""

import os
from asyncio import CancelledError
from datetime import UTC, datetime

from pydantic import TypeAdapter
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.wrapper import WrapperModel

from rsc.workflow.store import dumps

REQUEST_PARAMETERS = TypeAdapter(ModelRequestParameters)


def discard_incomplete_line(fd):
    """Preserve complete events if a previous process stopped during an append."""
    end = os.lseek(fd, 0, os.SEEK_END)
    if not end or os.pread(fd, 1, end - 1) == b"\n":
        return
    while end:
        start = max(0, end - 8192)
        newline = os.pread(fd, end - start, start).rfind(b"\n")
        if newline >= 0:
            end = start + newline + 1
            break
        end = start
    os.ftruncate(fd, end)


class ModelJournal(AbstractCapability):
    def __init__(self, path=None, assignment_attempt=1, run_id=None):
        self.path = path
        self.assignment_attempt = assignment_attempt
        self.run_id = run_id
        self.request_number = 0

    async def for_run(self, ctx):
        if ctx.deps is None:
            return self
        return ModelJournal(ctx.deps.model_log_path, ctx.deps.assignment_attempt, ctx.run_id)

    def get_wrapper_model(self, model):
        if self.path is not None:
            return JournaledModel(model, self)
        return None

    def append(self, event, request_number, **payload):
        row = {
            "event": event,
            "time": datetime.now(UTC).isoformat(),
            "run_id": self.run_id,
            "assignment_attempt": self.assignment_attempt,
            "request_number": request_number,
            **payload,
        }
        line = dumps(row) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            discard_incomplete_line(fd)
            stream.write(line)


class JournaledModel(WrapperModel):
    def __init__(self, model, journal):
        super().__init__(model)
        self.journal = journal

    async def request(self, messages, model_settings, model_request_parameters):
        self.journal.request_number += 1
        number = self.journal.request_number
        self.journal.append(
            "request",
            number,
            model=self.model_id,
            messages=ModelMessagesTypeAdapter.dump_python(messages, mode="json"),
            model_settings=model_settings,
            model_request_parameters=REQUEST_PARAMETERS.dump_python(
                model_request_parameters, mode="json"
            ),
        )
        try:
            response = await super().request(messages, model_settings, model_request_parameters)
        except (Exception, CancelledError) as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            if isinstance(exc, ModelHTTPError):
                error.update(status_code=exc.status_code, body=exc.body)
            self.journal.append("error", number, error=error)
            raise
        self.journal.append(
            "response",
            number,
            response=ModelMessagesTypeAdapter.dump_python([response], mode="json")[0],
        )
        return response
