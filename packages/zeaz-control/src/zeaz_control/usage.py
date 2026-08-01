"""Idempotent normalized usage and cost-event ingestion."""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field, StringConstraints, field_validator

from zeaz_control.models import (
    ControlStore,
    Identifier,
    ProviderExtendedModel,
    _append_audit,
)

Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0, max_digits=28, decimal_places=12)]


class ControlUsageError(RuntimeError):
    """A sanitized usage/cost ingestion failure."""


class UsageEvent(ProviderExtendedModel):
    schema_version: Literal["1"] = "1"
    id: Identifier
    account: Identifier = "default"
    model: Identifier
    input_tokens: int = Field(ge=0, le=10_000_000_000)
    output_tokens: int = Field(ge=0, le=10_000_000_000)
    cached_input_tokens: int = Field(default=0, ge=0, le=10_000_000_000)
    requests: int = Field(default=1, ge=1, le=1_000_000_000)
    source: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def observed_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must include a UTC offset")
        return value


class CostEvent(ProviderExtendedModel):
    schema_version: Literal["1"] = "1"
    id: Identifier
    usage_event_id: Identifier
    account: Identifier = "default"
    model: Identifier
    amount: NonNegativeDecimal
    currency: Currency
    pricing_source: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    pricing_observed_on: date
    observed_at: datetime

    @field_validator("amount", mode="before")
    @classmethod
    def amount_is_exact(cls, value: object) -> object:
        if isinstance(value, float):
            raise ValueError("cost amount must not be a floating-point value")
        return value

    @field_validator("observed_at")
    @classmethod
    def observed_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must include a UTC offset")
        return value


class UsageCostService:
    def __init__(self, store: ControlStore) -> None:
        self._store = store
        with store._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS control_usage_events (
                    id TEXT PRIMARY KEY,
                    payload BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS control_cost_events (
                    id TEXT PRIMARY KEY,
                    usage_event_id TEXT NOT NULL,
                    payload BLOB NOT NULL,
                    FOREIGN KEY (usage_event_id) REFERENCES control_usage_events(id)
                );
                """
            )
            connection.commit()

    def ingest_usage(self, event: UsageEvent) -> UsageEvent:
        return self._ingest(
            table="control_usage_events",
            event=event,
            event_type="control.usage.ingested",
        )

    def ingest_cost(self, event: CostEvent) -> CostEvent:
        encoded = event.model_dump_json().encode()
        with self._store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            usage = connection.execute(
                "SELECT payload FROM control_usage_events WHERE id = ?",
                (event.usage_event_id,),
            ).fetchone()
            if usage is None:
                raise ControlUsageError("cost event references unknown usage")
            try:
                usage_event = UsageEvent.model_validate_json(usage[0])
            except Exception as exc:
                raise ControlUsageError("stored usage event is invalid") from exc
            if (
                event.provider,
                event.account,
                event.model,
            ) != (
                usage_event.provider,
                usage_event.account,
                usage_event.model,
            ):
                raise ControlUsageError("cost event scope does not match usage")
            row = connection.execute(
                "SELECT payload FROM control_cost_events WHERE id = ?",
                (event.id,),
            ).fetchone()
            if row is not None:
                connection.rollback()
                if row[0] != encoded:
                    raise ControlUsageError("cost event ID conflicts with stored payload")
                return CostEvent.model_validate_json(row[0])
            connection.execute(
                "INSERT INTO control_cost_events (id, usage_event_id, payload) "
                "VALUES (?, ?, ?)",
                (event.id, event.usage_event_id, encoded),
            )
            _append_audit(
                connection,
                event_id=uuid4(),
                event_type="control.cost.ingested",
                subject_id=event.id,
                details={
                    "payload_sha256": hashlib.sha256(encoded).hexdigest(),
                    "pricing_source": event.pricing_source,
                    "pricing_observed_on": event.pricing_observed_on.isoformat(),
                },
                created_at=event.observed_at,
            )
            connection.commit()
        return event

    def usage(self) -> tuple[UsageEvent, ...]:
        return self._list("control_usage_events", UsageEvent)

    def costs(self) -> tuple[CostEvent, ...]:
        return self._list("control_cost_events", CostEvent)

    def _ingest(self, *, table: str, event: UsageEvent, event_type: str) -> UsageEvent:
        encoded = event.model_dump_json().encode()
        with self._store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT payload FROM {table} WHERE id = ?",  # noqa: S608
                (event.id,),
            ).fetchone()
            if row is not None:
                connection.rollback()
                if row[0] != encoded:
                    raise ControlUsageError("usage event ID conflicts with stored payload")
                return UsageEvent.model_validate_json(row[0])
            connection.execute(
                f"INSERT INTO {table} (id, payload) VALUES (?, ?)",  # noqa: S608
                (event.id, encoded),
            )
            _append_audit(
                connection,
                event_id=uuid4(),
                event_type=event_type,
                subject_id=event.id,
                details={"payload_sha256": hashlib.sha256(encoded).hexdigest()},
                created_at=event.observed_at,
            )
            connection.commit()
        return event

    def _list(self, table: str, model):
        with self._store._connect() as connection:
            rows = connection.execute(
                f"SELECT payload FROM {table} ORDER BY id"  # noqa: S608
            ).fetchall()
        try:
            return tuple(model.model_validate_json(row[0]) for row in rows)
        except Exception as exc:
            raise ControlUsageError("stored usage or cost event is invalid") from exc
