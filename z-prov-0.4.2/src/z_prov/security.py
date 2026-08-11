from __future__ import annotations

import hashlib
import re
import time
import uuid
from collections import defaultdict, deque

_SECRET = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{10,}|AIza[0-9A-Za-z_-]{20,}|AKIA[0-9A-Z]{16}|"
    r"Bearer\s+[A-Za-z0-9._~+/-]{10,})",
    re.IGNORECASE,
)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def redact(value: str, limit: int = 2048) -> str:
    return _SECRET.sub("[REDACTED]", value[:limit])


def request_id(value: str | None) -> str:
    if value and _REQUEST_ID.fullmatch(value):
        return value
    return uuid.uuid4().hex


def client_bucket(authorization: str | None, api_key: str | None, peer: str) -> str:
    identity = api_key or authorization or peer
    return hashlib.sha256(identity.encode()).hexdigest()


class InMemoryRateLimiter:
    def __init__(self, limit: int, window_seconds: float = 60.0, *, clock=time.monotonic):
        self.limit = limit
        self.window_seconds = window_seconds
        self.clock = clock
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._last_sweep = clock()

    def allow(self, bucket: str) -> tuple[bool, int]:
        if self.limit <= 0:
            return True, 0
        now = self.clock()
        self._sweep(now)
        events = self._events[bucket]
        cutoff = now - self.window_seconds
        while events and events[0] <= cutoff:
            events.popleft()
        remaining = max(0, self.limit - len(events))
        if not remaining:
            return False, 0
        events.append(now)
        return True, remaining - 1

    def _sweep(self, now: float) -> None:
        # `_events` is a defaultdict(deque) keyed by client bucket, and a
        # bucket's deque is only ever pruned when *that same bucket* makes
        # another request (see the eviction loop in allow()). A client that
        # is seen once and never again -- a rotated credential, a one-off
        # source IP, a scanner -- leaves its deque sitting in the dict
        # forever, since nothing else ever touches that key again. Over the
        # lifetime of a long-running, internet-facing gateway this is an
        # unbounded memory leak, one entry per distinct client ever seen.
        #
        # This periodic sweep (at most once per window) drops any bucket
        # whose newest recorded event has already aged out of the window,
        # i.e. buckets with no client activity in the current window at
        # all. It runs on the same clock as rate limiting itself so no
        # extra timer/thread is needed, and it is O(active buckets) only
        # once per window rather than on every request.
        if now - self._last_sweep < self.window_seconds:
            return
        self._last_sweep = now
        cutoff = now - self.window_seconds
        idle = [b for b, events in self._events.items() if not events or events[-1] <= cutoff]
        for b in idle:
            del self._events[b]
