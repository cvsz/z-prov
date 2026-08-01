"""Anthropic Standard Webhooks verification with durable replay rejection."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import sqlite3
import stat
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretBytes,
    SecretStr,
    StringConstraints,
    ValidationError,
    field_validator,
)

Identifier = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}$")
]


class WebhookVerificationError(RuntimeError):
    """A sanitized authenticity, freshness, schema, or replay failure."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WebhookData(StrictModel):
    type: Annotated[
        str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
    ]
    id: Identifier
    organization_id: Identifier
    workspace_id: Identifier
    details: dict[
        Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")],
        JsonValue,
    ] = Field(default={}, max_length=16)


class WebhookEvent(StrictModel):
    type: Literal["event"]
    id: Identifier
    created_at: datetime
    data: WebhookData

    @field_validator("created_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a UTC offset")
        return value


class VerifiedWebhook(StrictModel):
    event: WebhookEvent
    duplicate: bool


class SQLiteWebhookReplayStore:
    """Records only event IDs and receipt times; never webhook payloads."""

    def __init__(self, path: Path, *, retention_seconds: int = 2_592_000) -> None:
        if not path.is_absolute():
            raise ValueError("webhook replay database path must be absolute")
        if not 300 <= retention_seconds <= 31_536_000:
            raise ValueError("retention_seconds must be between 5 minutes and 1 year")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.parent.is_symlink():
            raise ValueError("webhook replay database parent cannot be a symlink")
        if path.exists():
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
                raise ValueError("webhook replay database must be a regular file")
        else:
            descriptor = os.open(
                path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            os.close(descriptor)
        os.chmod(path, 0o600)
        self._path = path
        self._retention = retention_seconds
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS webhook_events (
                    event_id TEXT PRIMARY KEY,
                    received_at INTEGER NOT NULL
                ) STRICT
                """
            )

    def claim(self, event_id: str, received_at: int) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM webhook_events WHERE received_at < ?",
                (received_at - self._retention,),
            )
            cursor = connection.execute(
                "INSERT OR IGNORE INTO webhook_events(event_id, received_at) VALUES (?, ?)",
                (event_id, received_at),
            )
            connection.commit()
            return cursor.rowcount == 1

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection


class AnthropicWebhookVerifier:
    def __init__(
        self,
        signing_secret: SecretStr,
        replay_store: SQLiteWebhookReplayStore,
        *,
        tolerance_seconds: int = 300,
        max_body_bytes: int = 1_048_576,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(signing_secret, SecretStr):
            raise TypeError("webhook signing secret must be SecretStr")
        encoded = signing_secret.get_secret_value()
        if not encoded.startswith("whsec_"):
            raise ValueError("webhook signing secret must use the whsec_ prefix")
        try:
            key = base64.b64decode(encoded[6:], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("webhook signing secret is malformed") from exc
        if len(key) != 32:
            raise ValueError("webhook signing secret must decode to 32 bytes")
        if not isinstance(replay_store, SQLiteWebhookReplayStore):
            raise TypeError("a durable webhook replay store is required")
        if not 1 <= tolerance_seconds <= 900:
            raise ValueError("tolerance_seconds must be between 1 and 900")
        if not 256 <= max_body_bytes <= 16_777_216:
            raise ValueError("max_body_bytes must be between 256 B and 16 MiB")
        self._key = SecretBytes(key)
        self._replay_store = replay_store
        self._tolerance = tolerance_seconds
        self._max_body_bytes = max_body_bytes
        self._clock = clock

    def verify(self, body: bytes, headers: Mapping[str, str]) -> VerifiedWebhook:
        if not isinstance(body, bytes) or not body or len(body) > self._max_body_bytes:
            raise WebhookVerificationError("webhook body is invalid or too large")
        normalized = {key.lower(): value for key, value in headers.items()}
        webhook_id = _header(normalized, "webhook-id")
        timestamp_text = _header(normalized, "webhook-timestamp")
        signature_text = _header(normalized, "webhook-signature")
        try:
            timestamp = int(timestamp_text)
        except ValueError as exc:
            raise WebhookVerificationError("webhook timestamp is invalid") from exc
        now = int(self._clock())
        if abs(now - timestamp) > self._tolerance:
            raise WebhookVerificationError("webhook timestamp is stale")
        signed = webhook_id.encode() + b"." + timestamp_text.encode() + b"." + body
        expected = hmac.new(
            self._key.get_secret_value(), signed, hashlib.sha256
        ).digest()
        signatures = _signatures(signature_text)
        if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
            raise WebhookVerificationError("webhook signature is invalid")
        event = _event(body)
        if event.id != webhook_id:
            raise WebhookVerificationError("webhook ID does not match signed delivery")
        claimed = self._replay_store.claim(event.id, now)
        return VerifiedWebhook(event=event, duplicate=not claimed)


def _header(headers: Mapping[str, str], name: str) -> str:
    value = headers.get(name)
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise WebhookVerificationError(f"required {name} header is invalid")
    return value


def _signatures(value: str) -> tuple[bytes, ...]:
    decoded: list[bytes] = []
    for part in value.split():
        version, separator, signature = part.partition(",")
        if version != "v1" or not separator:
            continue
        try:
            candidate = base64.b64decode(signature, validate=True)
        except (binascii.Error, ValueError):
            continue
        if len(candidate) == hashlib.sha256().digest_size:
            decoded.append(candidate)
    if not decoded or len(decoded) > 16:
        raise WebhookVerificationError("webhook signature header is invalid")
    return tuple(decoded)


def _event(body: bytes) -> WebhookEvent:
    try:
        value = json.loads(body)
        if not isinstance(value, dict) or set(value) != {
            "type",
            "id",
            "created_at",
            "data",
        }:
            raise ValueError
        data = value["data"]
        if not isinstance(data, dict):
            raise ValueError
        required = {"type", "id", "organization_id", "workspace_id"}
        if not required.issubset(data) or len(data) > 20:
            raise ValueError
        value["data"] = {
            **{key: data[key] for key in required},
            "details": {key: item for key, item in data.items() if key not in required},
        }
        return WebhookEvent.model_validate(value)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
        ValidationError,
    ) as exc:
        raise WebhookVerificationError("webhook payload is invalid") from exc
