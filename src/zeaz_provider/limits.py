from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator


class ResponseLimitExceeded(RuntimeError):
    pass


class RequestConcurrencyLimiter:
    def __init__(self, limit: int):
        self.limit = limit
        self._active = 0
        self._lock = asyncio.Lock()

    @property
    def active(self) -> int:
        return self._active

    async def try_acquire(self) -> bool:
        async with self._lock:
            if self.limit > 0 and self._active >= self.limit:
                return False
            self._active += 1
            return True

    async def release(self) -> None:
        async with self._lock:
            if self._active <= 0:
                raise RuntimeError("Concurrency slot released without acquisition")
            self._active -= 1


async def bounded_stream(source: AsyncIterator[bytes], maximum: int) -> AsyncIterator[bytes]:
    total = 0
    async for chunk in source:
        total += len(chunk)
        if total > maximum:
            raise ResponseLimitExceeded("Response exceeded configured byte limit")
        yield chunk
