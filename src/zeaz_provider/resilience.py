from __future__ import annotations

import asyncio
import math
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

from .errors import ErrorKind, ProviderError

T = TypeVar("T")


@dataclass(frozen=True)
class ResiliencePolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 5.0
    max_retry_after_seconds: float = 30.0
    failure_threshold: int = 5
    reset_timeout_seconds: float = 30.0
    total_timeout_seconds: float = 135.0


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    reset_timeout_seconds: float = 30.0
    clock: Callable[[], float] = time.monotonic
    _failures: int = field(default=0, init=False)
    _state: str = field(default="closed", init=False)
    _opened_at: float = field(default=0.0, init=False)
    _probe_in_flight: bool = field(default=False, init=False)

    @property
    def state(self) -> str:
        return self._state

    def before_call(self) -> None:
        if self._state == "half_open" and self._probe_in_flight:
            raise ProviderError(
                "Provider circuit probe is already in progress",
                503,
                kind=ErrorKind.CIRCUIT_OPEN,
                fallback_allowed=True,
                circuit_failure=False,
            )
        if self._state != "open":
            return
        if self.clock() - self._opened_at < self.reset_timeout_seconds:
            raise ProviderError(
                "Provider circuit is open",
                503,
                kind=ErrorKind.CIRCUIT_OPEN,
                fallback_allowed=True,
                circuit_failure=False,
            )
        if self._probe_in_flight:
            raise ProviderError(
                "Provider circuit probe is already in progress",
                503,
                kind=ErrorKind.CIRCUIT_OPEN,
                fallback_allowed=True,
                circuit_failure=False,
            )
        self._state = "half_open"
        self._probe_in_flight = True

    def success(self) -> None:
        self._failures = 0
        self._probe_in_flight = False
        self._state = "closed"

    def failure(self) -> None:
        self._failures += 1
        self._probe_in_flight = False
        if self._state == "half_open" or self._failures >= self.failure_threshold:
            self._state = "open"
            self._opened_at = self.clock()


class ResilienceExecutor:
    def __init__(
        self,
        policy: ResiliencePolicy | None = None,
        *,
        breaker: CircuitBreaker | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        uniform: Callable[[float, float], float] = random.uniform,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.policy = policy or ResiliencePolicy()
        self.breaker = breaker or CircuitBreaker(
            failure_threshold=self.policy.failure_threshold,
            reset_timeout_seconds=self.policy.reset_timeout_seconds,
        )
        self.sleep = sleep
        self.uniform = uniform
        self.clock = clock

    async def call(self, operation: Callable[[], Awaitable[T]]) -> T:
        last_error: ProviderError | None = None
        started_at = self.clock()
        for attempt in range(self.policy.max_attempts):
            remaining = self.policy.total_timeout_seconds - (self.clock() - started_at)
            if remaining <= 0:
                raise self._deadline_error(last_error)
            self.breaker.before_call()
            try:
                async with asyncio.timeout(remaining):
                    result = await operation()
            except TimeoutError as exc:
                timeout_error = ProviderError(
                    "Provider request deadline exceeded",
                    504,
                    retryable=False,
                    kind=ErrorKind.TIMEOUT,
                    fallback_allowed=True,
                    circuit_failure=True,
                )
                self.breaker.failure()
                raise timeout_error from exc
            except ProviderError as exc:
                last_error = exc
                if exc.circuit_failure:
                    self.breaker.failure()
                elif self.breaker.state == "half_open":
                    self.breaker.success()
                if not exc.retryable or attempt + 1 >= self.policy.max_attempts:
                    raise
                delay = self._delay(exc, attempt)
                remaining = self.policy.total_timeout_seconds - (self.clock() - started_at)
                if delay >= remaining:
                    raise self._deadline_error(exc) from exc
                await self.sleep(delay)
            else:
                self.breaker.success()
                return result
        assert last_error is not None
        raise last_error

    @staticmethod
    def _deadline_error(last_error: ProviderError | None) -> ProviderError:
        return ProviderError(
            "Provider request deadline exceeded",
            504,
            kind=ErrorKind.TIMEOUT,
            fallback_allowed=True,
            circuit_failure=True,
            details={"last_error": str(last_error) if last_error else None},
        )

    def _delay(self, error: ProviderError, attempt: int) -> float:
        if (
            isinstance(error.retry_after, (int, float))
            and not isinstance(error.retry_after, bool)
            and math.isfinite(error.retry_after)
        ):
            return min(max(0.0, error.retry_after), self.policy.max_retry_after_seconds)
        ceiling = min(
            self.policy.max_delay_seconds,
            self.policy.base_delay_seconds * (2**attempt),
        )
        return self.uniform(0.0, ceiling)
