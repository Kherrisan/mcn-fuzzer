"""Shared async request throttling for document-review model calls."""

import asyncio
from time import monotonic

from pydantic_ai.models.wrapper import WrapperModel


class RequestRateLimiter:
    """Space requests evenly so concurrent tasks share one request rate.

    The slot is reserved under the lock and the wait happens outside it, so the
    event loop stays free and cancellation interrupts a pending delay.
    """

    def __init__(self, requests_per_second: float):
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self.interval = 1.0 / requests_per_second
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def wait(self):
        async with self._lock:
            now = monotonic()
            delay = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + self.interval
        if delay:
            await asyncio.sleep(delay)


class RateLimitedModel(WrapperModel):
    """Throttle every underlying model request, including provider retries."""

    def __init__(self, model, limiter: RequestRateLimiter):
        super().__init__(model)
        self.limiter = limiter

    async def request(self, messages, model_settings, model_request_parameters):
        await self.limiter.wait()
        return await super().request(messages, model_settings, model_request_parameters)
