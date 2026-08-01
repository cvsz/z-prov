from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import re
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

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


def client_key_digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def verify_client_key(candidate: str, configured_hashes: frozenset[bytes]) -> bool:
    if not candidate or not configured_hashes:
        return False
    candidate_hash = client_key_digest(candidate)
    matched = False
    for configured_hash in configured_hashes:
        matched |= hmac.compare_digest(candidate_hash, configured_hash)
    return matched


@dataclass(frozen=True)
class TrustedProxyPolicy:
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()

    @classmethod
    def from_cidrs(cls, cidrs: tuple[str, ...]) -> TrustedProxyPolicy:
        return cls(tuple(ipaddress.ip_network(value, strict=False) for value in cidrs))

    def client_ip(self, peer: str, cf_connecting_ip: str | None) -> str:
        """Resolve Cloudflare's client IP only when the direct peer is trusted."""
        try:
            direct = ipaddress.ip_address(peer)
        except ValueError:
            return peer
        if not any(direct.version == network.version and direct in network for network in self.networks):
            return direct.compressed
        if not cf_connecting_ip:
            return direct.compressed
        try:
            forwarded = ipaddress.ip_address(cf_connecting_ip.strip())
        except ValueError:
            return direct.compressed
        return forwarded.compressed


class RateLimitBackendError(RuntimeError):
    pass


class RateLimitBackend(Protocol):
    async def allow(self, bucket: str) -> tuple[bool, int]: ...

    async def close(self) -> None: ...


class InMemoryRateLimiter:
    def __init__(self, limit: int, window_seconds: float = 60.0, *, clock=time.monotonic):
        self.limit = limit
        self.window_seconds = window_seconds
        self.clock = clock
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, bucket: str) -> tuple[bool, int]:
        async with self._lock:
            if self.limit <= 0:
                return True, 0
            now = self.clock()
            events = self._events[bucket]
            cutoff = now - self.window_seconds
            while events and events[0] <= cutoff:
                events.popleft()
            remaining = max(0, self.limit - len(events))
            if not remaining:
                return False, 0
            events.append(now)
            return True, remaining - 1

    async def close(self) -> None:
        return None


_REDIS_ALLOW_SCRIPT = """
local current = redis.call('TIME')
local now = (current[1] * 1000) + math.floor(current[2] / 1000)
local cutoff = now - tonumber(ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', cutoff)
local count = redis.call('ZCARD', KEYS[1])
local limit = tonumber(ARGV[2])
if count >= limit then
  redis.call('PEXPIRE', KEYS[1], ARGV[1])
  return {0, 0}
end
redis.call('ZADD', KEYS[1], now, tostring(now) .. ':' .. ARGV[3])
redis.call('PEXPIRE', KEYS[1], ARGV[1])
return {1, limit - count - 1}
"""


class RedisRateLimiter:
    def __init__(
        self,
        client: Redis,
        limit: int,
        *,
        window_seconds: float = 60.0,
        key_prefix: str = "zeaz:rate-limit:",
    ):
        self.client = client
        self.limit = limit
        self.window_ms = max(1, int(window_seconds * 1000))
        self.key_prefix = key_prefix

    async def allow(self, bucket: str) -> tuple[bool, int]:
        if self.limit <= 0:
            return True, 0
        try:
            result = await self.client.eval(
                _REDIS_ALLOW_SCRIPT,
                1,
                f"{self.key_prefix}{bucket}",
                self.window_ms,
                self.limit,
                uuid.uuid4().hex,
            )
        except RedisError as exc:
            raise RateLimitBackendError("Distributed rate-limit backend unavailable") from exc
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise RateLimitBackendError("Distributed rate-limit backend returned an invalid result")
        try:
            return bool(int(result[0])), max(0, int(result[1]))
        except (TypeError, ValueError) as exc:
            raise RateLimitBackendError(
                "Distributed rate-limit backend returned an invalid result"
            ) from exc

    async def close(self) -> None:
        await self.client.aclose()
