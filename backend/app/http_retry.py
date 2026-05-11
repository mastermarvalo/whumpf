"""Retry + circuit-breaker helpers for outbound HTTP.

Outbound HTTP to TiTiler, AWDB, CAIC, and Strava is wrapped to:
- retry on transient network errors and 5xx responses
- short-circuit when an upstream has been failing recently

Without this, a single CAIC or Strava blip propagates straight to the
browser as a 502 and the next request immediately hammers the dead
upstream again.
"""

from __future__ import annotations

import asyncio
import logging
from time import monotonic

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

logger = logging.getLogger("whumpf.http_retry")


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.RequestError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


class CircuitOpenError(Exception):
    """Raised when an upstream's breaker is open — fail fast, don't dial."""


class CircuitBreaker:
    """Closed → Open → Half-open state machine, per named upstream.

    Five consecutive failures opens the breaker for ``cooldown_s``. After that
    one trial request is allowed; success closes, failure re-opens.
    """

    def __init__(self, name: str, failure_threshold: int = 5, cooldown_s: float = 30.0) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self._failures = 0
        self._open_until = 0.0
        self._lock = asyncio.Lock()

    def _state(self) -> str:
        if self._open_until > monotonic():
            return "open"
        if self._failures >= self.failure_threshold:
            return "half_open"
        return "closed"

    async def call(self, coro_factory):
        """Run ``await coro_factory()`` under the breaker.

        ``coro_factory`` is a zero-arg callable returning a coroutine — passed
        instead of a coroutine so we can refuse the call without ever creating
        the awaitable.
        """
        if self._state() == "open":
            raise CircuitOpenError(f"{self.name} circuit open")
        try:
            result = await coro_factory()
        except Exception:
            async with self._lock:
                self._failures += 1
                if self._failures >= self.failure_threshold:
                    self._open_until = monotonic() + self.cooldown_s
                    logger.warning(
                        "circuit OPEN: %s (cooldown %.0fs)", self.name, self.cooldown_s
                    )
            raise
        else:
            if self._failures > 0:
                logger.info("circuit recovered: %s", self.name)
            self._failures = 0
            self._open_until = 0.0
            return result


_BREAKERS: dict[str, CircuitBreaker] = {}


def breaker(name: str) -> CircuitBreaker:
    """Return the singleton CircuitBreaker for ``name``, creating it on first use."""
    b = _BREAKERS.get(name)
    if b is None:
        b = CircuitBreaker(name)
        _BREAKERS[name] = b
    return b


async def call_with_resilience(name: str, coro_factory, *, attempts: int = 3):
    """Run a coroutine under retry + circuit breaker.

    The breaker observes the *post-retry* outcome — one open-loop failure
    burst should count as one failure, not three.
    """
    async def _retry_wrapper():
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(attempts),
                wait=wait_exponential_jitter(initial=0.5, max=4.0),
                retry=retry_if_exception(_is_retryable),
                reraise=True,
            ):
                with attempt:
                    return await coro_factory()
        except RetryError as exc:
            # Should not happen with reraise=True, but defensive.
            raise exc.last_attempt.exception() from exc

    return await breaker(name).call(_retry_wrapper)
