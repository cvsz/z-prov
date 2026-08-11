from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TypeVar

T = TypeVar("T")


class ResponseLimitExceeded(RuntimeError):
    pass


class RequestConcurrencyLimiter:
    def __init__(self, limit: int):
        self.limit = limit
        self._active = 0
        self._lock = asyncio.Lock()
        self._semaphore: asyncio.Semaphore | None = None

    @property
    def active(self) -> int:
        return self._active

    async def try_acquire(self) -> bool:
        if self.limit <= 0:
            async with self._lock:
                self._active += 1
            return True
        
        if self._semaphore is None:
            async with self._lock:
                if self._semaphore is None:
                    self._semaphore = asyncio.Semaphore(self.limit)
        
        acquired = False
        try:
            acquired = await self._semaphore.acquire()
        except Exception:
            return False
        
        if not acquired:
            return False
        
        async with self._lock:
            self._active += 1
        return True

    async def release(self) -> None:
        async with self._lock:
            if self._active <= 0:
                raise RuntimeError("Concurrency slot released without acquisition")
            self._active -= 1
        
        if self._semaphore is not None:
            self._semaphore.release()


async def bounded_stream(source: AsyncIterator[bytes], maximum: int) -> AsyncIterator[bytes]:
    total = 0
    async for chunk in source:
        total += len(chunk)
        if total > maximum:
            raise ResponseLimitExceeded("Response exceeded configured byte limit")
        yield chunk
