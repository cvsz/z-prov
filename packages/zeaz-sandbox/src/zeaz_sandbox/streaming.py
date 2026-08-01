"""Bounded stdout/stderr streaming with literal secret redaction."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from enum import StrEnum
from typing import Protocol


class OutputChannel(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"


class OutputSink(Protocol):
    async def emit(self, channel: OutputChannel, data: bytes) -> None: ...


class CallbackOutputSink:
    def __init__(
        self,
        callback: Callable[[OutputChannel, bytes], Awaitable[None]],
    ) -> None:
        self._callback = callback

    async def emit(self, channel: OutputChannel, data: bytes) -> None:
        await self._callback(channel, data)


class NullOutputSink:
    async def emit(self, channel: OutputChannel, data: bytes) -> None:
        del channel, data


class OutputSinkError(RuntimeError):
    """The output consumer failed or stopped making progress."""


class BoundedOutputStreamer:
    """Share one raw-byte budget across both channels and redact before emit."""

    def __init__(
        self,
        sink: OutputSink,
        *,
        max_bytes: int,
        secrets: Sequence[bytes] = (),
        emit_timeout_seconds: float = 5,
    ) -> None:
        if not 1024 <= max_bytes <= 67_108_864:
            raise ValueError("max_bytes must be between 1 KiB and 64 MiB")
        normalized = tuple(sorted(set(secrets), key=len, reverse=True))
        if len(normalized) > 128 or any(
            not 4 <= len(secret) <= 4096 or b"\x00" in secret
            for secret in normalized
        ):
            raise ValueError("redaction secrets must be 4-4096 non-NUL bytes")
        if not 0 < emit_timeout_seconds <= 30:
            raise ValueError("emit_timeout_seconds must be between 0 and 30")
        self._sink = sink
        self._max_bytes = max_bytes
        self._emit_timeout = emit_timeout_seconds
        self._redactors = {
            channel: _LiteralRedactor(normalized)
            for channel in OutputChannel
        }
        self._lock = asyncio.Lock()
        self._stdout_bytes = 0
        self._stderr_bytes = 0
        self._total_bytes = 0
        self._truncated = False
        self._finished = False

    @property
    def stdout_bytes(self) -> int:
        return self._stdout_bytes

    @property
    def stderr_bytes(self) -> int:
        return self._stderr_bytes

    @property
    def truncated(self) -> bool:
        return self._truncated

    async def feed(self, channel: OutputChannel, data: bytes) -> bool:
        """Return false when the output limit has been reached."""
        if not data:
            return not self._truncated
        async with self._lock:
            if self._finished:
                raise RuntimeError("output streamer is already finished")
            remaining = self._max_bytes - self._total_bytes
            accepted = data[:remaining]
            if channel is OutputChannel.STDOUT:
                self._stdout_bytes += len(accepted)
            else:
                self._stderr_bytes += len(accepted)
            self._total_bytes += len(accepted)
            if accepted:
                redacted = self._redactors[channel].feed(accepted)
                if redacted:
                    await self._emit(channel, redacted)
            if len(accepted) != len(data):
                self._truncated = True
            return not self._truncated

    async def finish(self) -> None:
        async with self._lock:
            if self._finished:
                return
            self._finished = True
            for channel, redactor in self._redactors.items():
                trailing = redactor.finish()
                if trailing:
                    await self._emit(channel, trailing)

    async def _emit(self, channel: OutputChannel, data: bytes) -> None:
        try:
            await asyncio.wait_for(
                self._sink.emit(channel, data),
                timeout=self._emit_timeout,
            )
        except TimeoutError as exc:
            raise OutputSinkError("output consumer timed out") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise OutputSinkError("output consumer failed") from exc


class _LiteralRedactor:
    def __init__(self, secrets: tuple[bytes, ...]) -> None:
        self._secrets = secrets
        self._buffer = bytearray()

    def feed(self, data: bytes) -> bytes:
        self._buffer.extend(data)
        return self._drain(final=False)

    def finish(self) -> bytes:
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> bytes:
        output = bytearray()
        while self._buffer:
            match = next(
                (
                    secret
                    for secret in self._secrets
                    if self._buffer.startswith(secret)
                ),
                None,
            )
            if match is not None:
                output.extend(b"[REDACTED]")
                del self._buffer[: len(match)]
                continue
            if not final and any(
                secret.startswith(self._buffer)
                for secret in self._secrets
                if len(self._buffer) < len(secret)
            ):
                break
            output.append(self._buffer[0])
            del self._buffer[0]
        return bytes(output)
