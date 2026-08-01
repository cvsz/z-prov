"""Minimal exact-destination SOCKS5 proxy for the sandbox egress sidecar."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import signal
import socket
from collections.abc import Sequence
from dataclasses import dataclass

SOCKS_VERSION = 5
STATUS_SUCCEEDED = 0
STATUS_GENERAL_FAILURE = 1
STATUS_NOT_ALLOWED = 2
STATUS_COMMAND_UNSUPPORTED = 7
STATUS_ADDRESS_UNSUPPORTED = 8


@dataclass(frozen=True)
class ProxyLimits:
    max_connections: int = 128
    idle_timeout_seconds: int = 60
    max_connection_bytes: int = 67_108_864


class DestinationPolicy:
    def __init__(self, destinations: Sequence[str]) -> None:
        allowed: dict[str, set[int]] = {}
        for value in destinations:
            host, separator, raw_port = value.rpartition(":")
            if not separator or not host or not raw_port.isdigit():
                raise ValueError("destination must be host:port")
            normalized = host.lower().rstrip(".")
            port = int(raw_port)
            if not 1 <= port <= 65535:
                raise ValueError("destination port is invalid")
            allowed.setdefault(normalized, set()).add(port)
        if not allowed or sum(len(ports) for ports in allowed.values()) > 8192:
            raise ValueError("destination policy is empty or excessive")
        self._allowed = {
            host: frozenset(ports)
            for host, ports in allowed.items()
        }

    def allows(self, host: str, port: int) -> bool:
        return port in self._allowed.get(host.lower().rstrip("."), ())

    async def resolve(self, host: str, port: int) -> tuple[str, ...]:
        if not self.allows(host, port):
            return ()
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        loop = asyncio.get_running_loop()
        try:
            records = await loop.getaddrinfo(
                host,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except OSError:
            return ()
        addresses: list[str] = []
        for _, _, _, _, sockaddr in records:
            address = ipaddress.ip_address(sockaddr[0])
            if not _safe_resolution(address, explicitly_allowed=literal):
                continue
            normalized = str(address)
            if normalized not in addresses:
                addresses.append(normalized)
        return tuple(addresses)


class SocksEgressProxy:
    def __init__(
        self,
        policy: DestinationPolicy,
        *,
        limits: ProxyLimits | None = None,
    ) -> None:
        limits = limits or ProxyLimits()
        if not 1 <= limits.max_connections <= 4096:
            raise ValueError("max_connections is invalid")
        if not 1 <= limits.idle_timeout_seconds <= 600:
            raise ValueError("idle_timeout_seconds is invalid")
        if not 1024 <= limits.max_connection_bytes <= 1_073_741_824:
            raise ValueError("max_connection_bytes is invalid")
        self._policy = policy
        self._limits = limits
        self._slots = asyncio.Semaphore(limits.max_connections)

    async def handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        async with self._slots:
            try:
                await asyncio.wait_for(
                    self._handle(reader, writer),
                    timeout=self._limits.idle_timeout_seconds,
                )
            except (OSError, EOFError, TimeoutError, ValueError):
                pass
            finally:
                writer.close()
                await writer.wait_closed()

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        greeting = await reader.readexactly(2)
        if greeting[0] != SOCKS_VERSION or greeting[1] == 0:
            raise ValueError("invalid SOCKS greeting")
        methods = await reader.readexactly(greeting[1])
        if 0 not in methods:
            writer.write(bytes((SOCKS_VERSION, 0xFF)))
            await writer.drain()
            return
        writer.write(bytes((SOCKS_VERSION, 0)))
        await writer.drain()

        header = await reader.readexactly(4)
        if header[0] != SOCKS_VERSION or header[2] != 0:
            raise ValueError("invalid SOCKS request")
        if header[1] != 1:
            await _reply(writer, STATUS_COMMAND_UNSUPPORTED)
            return
        host = await _read_address(reader, header[3])
        port = int.from_bytes(await reader.readexactly(2), "big")
        addresses = await self._policy.resolve(host, port)
        if not addresses:
            await _reply(writer, STATUS_NOT_ALLOWED)
            return

        upstream_reader: asyncio.StreamReader | None = None
        upstream_writer: asyncio.StreamWriter | None = None
        for address in addresses:
            try:
                upstream_reader, upstream_writer = await asyncio.open_connection(
                    address,
                    port,
                    family=(
                        socket.AF_INET6
                        if ipaddress.ip_address(address).version == 6
                        else socket.AF_INET
                    ),
                )
                break
            except OSError:
                continue
        if upstream_reader is None or upstream_writer is None:
            await _reply(writer, STATUS_GENERAL_FAILURE)
            return
        await _reply(writer, STATUS_SUCCEEDED)
        try:
            await _relay_bidirectional(
                reader,
                writer,
                upstream_reader,
                upstream_writer,
                maximum=self._limits.max_connection_bytes,
            )
        finally:
            upstream_writer.close()
            await upstream_writer.wait_closed()


async def _read_address(reader: asyncio.StreamReader, address_type: int) -> str:
    if address_type == 1:
        return str(ipaddress.IPv4Address(await reader.readexactly(4)))
    if address_type == 4:
        return str(ipaddress.IPv6Address(await reader.readexactly(16)))
    if address_type == 3:
        length = (await reader.readexactly(1))[0]
        if not 1 <= length <= 253:
            raise ValueError("invalid SOCKS domain length")
        try:
            return (await reader.readexactly(length)).decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError("SOCKS domain is not ASCII") from exc
    raise ValueError("unsupported SOCKS address type")


async def _reply(writer: asyncio.StreamWriter, status: int) -> None:
    writer.write(bytes((SOCKS_VERSION, status, 0, 1, 0, 0, 0, 0, 0, 0)))
    await writer.drain()


async def _relay_bidirectional(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
    *,
    maximum: int,
) -> None:
    remaining = maximum
    lock = asyncio.Lock()

    async def copy(
        source: asyncio.StreamReader,
        destination: asyncio.StreamWriter,
    ) -> None:
        nonlocal remaining
        while True:
            chunk = await source.read(min(65_536, remaining + 1))
            if not chunk:
                break
            async with lock:
                if len(chunk) > remaining:
                    raise ValueError("connection byte limit exceeded")
                remaining -= len(chunk)
            destination.write(chunk)
            await destination.drain()
        if destination.can_write_eof():
            destination.write_eof()
            await destination.drain()

    async with asyncio.TaskGroup() as group:
        group.create_task(copy(client_reader, upstream_writer))
        group.create_task(copy(upstream_reader, client_writer))


def _safe_resolution(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    explicitly_allowed: ipaddress.IPv4Address | ipaddress.IPv6Address | None,
) -> bool:
    if (
        address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or str(address) == "169.254.169.254"
    ):
        return False
    if explicitly_allowed is not None:
        return address == explicitly_allowed
    return address.is_global


async def _serve(args: argparse.Namespace) -> None:
    proxy = SocksEgressProxy(
        DestinationPolicy(args.allow),
        limits=ProxyLimits(
            max_connections=args.max_connections,
            idle_timeout_seconds=args.idle_timeout,
            max_connection_bytes=args.max_connection_bytes,
        ),
    )
    server = await asyncio.start_server(
        proxy.handle,
        host="0.0.0.0",
        port=1080,
        limit=65_536,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    async with server:
        await stop.wait()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow", action="append", required=True)
    parser.add_argument("--max-connections", type=int, default=128)
    parser.add_argument("--idle-timeout", type=int, default=60)
    parser.add_argument("--max-connection-bytes", type=int, default=67_108_864)
    asyncio.run(_serve(parser.parse_args()))


if __name__ == "__main__":
    main()
