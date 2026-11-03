"""Initialize Logfire once and capture content for RSC Agents only."""

from __future__ import annotations

import os
from threading import Lock

import logfire
from opentelemetry import trace
from pydantic_ai.models.instrumented import InstrumentationSettings

_initialization_lock = Lock()
_initialized = False


def agent_instrumentation() -> InstrumentationSettings:
    """Capture prompts, completions and tool content using the current trace provider.

    Preserve providers configured by an embedding application. Standalone runs
    send telemetry when project credentials are available, without interactive
    setup or console output. Explicit Logfire environment settings still apply.
    """

    global _initialized
    with _initialization_lock:
        if not _initialized:
            if isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
                logfire.configure(
                    service_name=os.environ.get("LOGFIRE_SERVICE_NAME", "rsc"),
                    send_to_logfire=(
                        None
                        if "LOGFIRE_SEND_TO_LOGFIRE" in os.environ
                        else "if-token-present"
                    ),
                    console=False,
                    inspect_arguments=False,
                )
            _initialized = True
    return InstrumentationSettings(include_content=True)
