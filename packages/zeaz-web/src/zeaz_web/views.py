"""Read-only, bounded projections of agent sessions, audits, and receipts."""

from __future__ import annotations

import json
import re
import sqlite3
import stat
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Identifier = Annotated[str, StringConstraints(min_length=1, max_length=256)]
Sensitive = re.compile(
    r"^(?:api[_-]?key|provider[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"authorization|credential|password|secret|prompt|messages?|request_body)$",
    re.IGNORECASE,
)


class ViewModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SessionSummary(ViewModel):
    id: Identifier
    status: Annotated[str, StringConstraints(max_length=64)]
    execution_mode: Annotated[str, StringConstraints(max_length=64)]
    revision: int = Field(ge=0)
    turn_count: int = Field(ge=0, le=100_000)
    updated_at: Annotated[str, StringConstraints(max_length=128)]


class TurnView(ViewModel):
    session_id: Identifier
    sequence: int = Field(ge=0)
    role: Annotated[str, StringConstraints(max_length=32)]
    status: Annotated[str, StringConstraints(max_length=32)]
    blocks: tuple[dict[str, Any], ...] = Field(max_length=1024)


class AuditView(ViewModel):
    session_id: Identifier
    sequence: int = Field(ge=0)
    event_type: Annotated[str, StringConstraints(max_length=128)]
    actor: Annotated[str, StringConstraints(max_length=128)]
    subject_id: str | None = None
    details: dict[str, Any] = Field(max_length=256)
    created_at: Annotated[str, StringConstraints(max_length=128)]


class ReceiptView(ViewModel):
    id: Identifier
    job_id: Identifier
    session_id: Identifier
    state: Annotated[str, StringConstraints(max_length=32)]
    image_digest: Annotated[str, StringConstraints(max_length=128)]
    cleanup_complete: bool
    finished_at: Annotated[str, StringConstraints(max_length=128)]


class DecisionView(ViewModel):
    session_id: Identifier
    sequence: int = Field(ge=0)
    event_type: Annotated[str, StringConstraints(max_length=128)]
    subject_id: str | None = None
    details: dict[str, Any] = Field(max_length=256)
    created_at: Annotated[str, StringConstraints(max_length=128)]


class StateSnapshot(ViewModel):
    sessions: tuple[SessionSummary, ...]
    turns: tuple[TurnView, ...]
    audit: tuple[AuditView, ...]
    receipts: tuple[ReceiptView, ...]
    plans: tuple[DecisionView, ...]
    approvals: tuple[DecisionView, ...]
    warnings: tuple[str, ...]


class StateReader:
    def __init__(
        self,
        *,
        session_db_path: Path | None = None,
        audit_log_path: Path | None = None,
        receipts_dir: Path | None = None,
        max_items: int = 500,
        max_bytes: int = 67_108_864,
    ) -> None:
        if not 1 <= max_items <= 5000 or not 1_048_576 <= max_bytes <= 1_073_741_824:
            raise ValueError("web state bounds are invalid")
        self.session_db_path = _safe_path(session_db_path)
        self.audit_log_path = _safe_path(audit_log_path)
        self.receipts_dir = _safe_dir(receipts_dir)
        self.max_items = max_items
        self.max_bytes = max_bytes

    def snapshot(self) -> StateSnapshot:
        warnings: list[str] = []
        sessions, turns = self._sessions(warnings)
        audit = self._audit(warnings)
        receipts = self._receipts(warnings)
        plans = tuple(
            DecisionView(
                session_id=item.session_id,
                sequence=item.sequence,
                event_type=item.event_type,
                subject_id=item.subject_id,
                details=item.details,
                created_at=item.created_at,
            )
            for item in audit
            if ".plan." in item.event_type or item.event_type.startswith("plan.")
        )
        approvals = tuple(
            DecisionView(
                session_id=item.session_id,
                sequence=item.sequence,
                event_type=item.event_type,
                subject_id=item.subject_id,
                details=item.details,
                created_at=item.created_at,
            )
            for item in audit
            if ".approval." in item.event_type
            or item.event_type.startswith("approval.")
            or ".permission." in item.event_type
            or item.event_type.startswith("permission.")
        )
        return StateSnapshot(
            sessions=tuple(sessions),
            turns=tuple(turns),
            audit=tuple(audit),
            receipts=tuple(receipts),
            plans=plans,
            approvals=approvals,
            warnings=tuple(warnings[:16]),
        )

    def _sessions(
        self, warnings: list[str]
    ) -> tuple[list[SessionSummary], list[TurnView]]:
        if self.session_db_path is None:
            return [], []
        sessions: list[SessionSummary] = []
        turns: list[TurnView] = []
        try:
            uri = f"file:{self.session_db_path}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=2) as connection:
                rows = connection.execute(
                    "SELECT session_id, revision, document FROM sessions ORDER BY rowid DESC LIMIT ?",
                    (self.max_items,),
                ).fetchall()
            for session_id, revision, document in rows:
                value = json.loads(document)
                if not isinstance(value, dict) or value.get("id") != session_id:
                    raise ValueError("session identity mismatch")
                raw_turns = value.get("turns", [])
                sessions.append(
                    SessionSummary(
                        id=session_id,
                        status=str(value.get("status", "unknown")),
                        execution_mode=str(value.get("execution_mode", "unknown")),
                        revision=int(revision),
                        turn_count=len(raw_turns) if isinstance(raw_turns, list) else 0,
                        updated_at=str(value.get("updated_at", "")),
                    )
                )
                if isinstance(raw_turns, list):
                    for turn in raw_turns[: self.max_items]:
                        if isinstance(turn, dict):
                            turns.append(_turn(session_id, turn))
        except Exception:
            warnings.append("session state unavailable")
        return sessions, turns[: self.max_items]

    def _audit(self, warnings: list[str]) -> list[AuditView]:
        if self.audit_log_path is None:
            return []
        result: list[AuditView] = []
        try:
            if self.audit_log_path.stat().st_size > self.max_bytes:
                raise ValueError("audit state exceeds bound")
            with self.audit_log_path.open("rb") as handle:
                for index, line in enumerate(handle):
                    if index >= self.max_items:
                        break
                    if len(line) > 1_048_576:
                        continue
                    value = json.loads(line)
                    event = value["event"]
                    result.append(
                        AuditView(
                            session_id=str(event["session_id"]),
                            sequence=int(event["sequence"]),
                            event_type=str(event["event_type"]),
                            actor=str(event["actor"]),
                            subject_id=event.get("subject_id"),
                            details=_redact(event.get("details", {})),
                            created_at=str(event["created_at"]),
                        )
                    )
        except Exception:
            warnings.append("audit state unavailable")
        return result

    def _receipts(self, warnings: list[str]) -> list[ReceiptView]:
        if self.receipts_dir is None:
            return []
        result: list[ReceiptView] = []
        try:
            for path in sorted(self.receipts_dir.iterdir())[: self.max_items]:
                if not path.is_file() or path.is_symlink():
                    continue
                if path.stat().st_size > 1_048_576:
                    continue
                value = json.loads(path.read_text())
                result.append(
                    ReceiptView(
                        id=str(value["id"]),
                        job_id=str(value["job_id"]),
                        session_id=str(value["session_id"]),
                        state=str(value["state"]),
                        image_digest=str(value["image_digest"]),
                        cleanup_complete=bool(value["cleanup_complete"]),
                        finished_at=str(value["finished_at"]),
                    )
                )
        except Exception:
            warnings.append("execution receipts unavailable")
        return result


def _turn(session_id: str, value: dict[str, Any]) -> TurnView:
    blocks: list[dict[str, Any]] = []
    for block in value.get("content", [])[:1024]:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            blocks.append({"type": "text", "text": str(block.get("text", ""))[:4096]})
        elif block_type == "tool_call":
            call = block.get("call", {})
            blocks.append(
                {
                    "type": "tool_call",
                    "id": str(call.get("id", "")),
                    "name": str(call.get("name", "")),
                }
            )
        elif block_type == "tool_result":
            result = block.get("result", {})
            blocks.append(
                {
                    "type": "tool_result",
                    "tool_call_id": str(result.get("tool_call_id", "")),
                    "is_error": bool(result.get("is_error", False)),
                    "output": _redact(result.get("output", "")),
                }
            )
    return TurnView(
        session_id=session_id,
        sequence=int(value.get("sequence", 0)),
        role=str(value.get("role", "unknown")),
        status=str(value.get("status", "unknown")),
        blocks=tuple(blocks),
    )


def _redact(value: Any, depth: int = 0) -> Any:
    if depth > 12:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {
            str(key)[:128]: "[REDACTED]" if Sensitive.fullmatch(str(key)) else _redact(item, depth + 1)
            for key, item in list(value.items())[:256]
        }
    if isinstance(value, list):
        return [_redact(item, depth + 1) for item in value[:256]]
    if isinstance(value, str):
        return value[:4096]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return "[REDACTED]"


def _safe_path(value: Path | None) -> Path | None:
    if value is None:
        return None
    if not value.is_absolute():
        raise ValueError("web state path must be absolute")
    return value


def _safe_dir(value: Path | None) -> Path | None:
    if value is None:
        return None
    if not value.is_absolute():
        raise ValueError("web state directory must be absolute")
    if value.exists() and (value.is_symlink() or not stat.S_ISDIR(value.stat().st_mode)):
        raise ValueError("web state directory must be a real directory")
    return value
