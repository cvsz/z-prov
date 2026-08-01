"""Dry-run-first, digest-bound permanent deletion controls."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

Identifier = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,254}$")
]
Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
IdempotencyKey = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,255}$")
]


class DeletionControlError(RuntimeError):
    """A safe permanent-deletion policy or state failure."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DeletionTarget(StrictModel):
    provider: Literal["anthropic", "openai"]
    resource_type: Annotated[
        str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    ]
    resource_id: Identifier
    organization_id: Identifier


class ResolvedDeletionTarget(StrictModel):
    target: DeletionTarget
    organization_wide: bool
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    state: Annotated[
        dict[
            Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")],
            JsonValue,
        ],
        Field(max_length=32),
    ]
    dependent_resource_ids: tuple[Identifier, ...] = Field(default=(), max_length=1000)

    @field_validator("dependent_resource_ids")
    @classmethod
    def dependencies_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("dependent resource IDs must be unique")
        return value


class DeletionPreview(StrictModel):
    schema_version: Literal["1"] = "1"
    plan_id: UUID
    dry_run: Literal[True] = True
    target: DeletionTarget
    resolved: ResolvedDeletionTarget
    resolution_digest: Digest
    required_approvals: Literal[1, 2]
    created_at: datetime
    expires_at: datetime

    @property
    def confirmation_text(self) -> str:
        return (
            f"DELETE {self.target.provider}/{self.target.resource_type}/"
            f"{self.target.resource_id} {self.resolution_digest}"
        )


class AuthorizationGrant(StrictModel):
    principal_id: Identifier
    plan_id: UUID
    resolution_digest: Digest
    decision: Literal["allow", "deny"]
    issued_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def valid_window(self) -> AuthorizationGrant:
        if (
            self.issued_at.tzinfo is None
            or self.issued_at.utcoffset() is None
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
            or self.issued_at >= self.expires_at
        ):
            raise ValueError("authorization validity window is invalid")
        return self


class DeletionReceipt(StrictModel):
    schema_version: Literal["1"] = "1"
    plan_id: UUID
    target: DeletionTarget
    resolution_digest: Digest
    authorizing_principal_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=2)
    provider_operation_id: Identifier
    idempotency_key: IdempotencyKey
    deleted_at: datetime


TargetResolver = Callable[[DeletionTarget], Awaitable[ResolvedDeletionTarget]]
AuthorizationVerifier = Callable[[SecretStr], Awaitable[AuthorizationGrant]]
DeletionExecutor = Callable[
    [ResolvedDeletionTarget, str], Awaitable[str]
]


class PermanentDeletionCoordinator:
    def __init__(
        self,
        *,
        resolver: TargetResolver,
        authorization_verifier: AuthorizationVerifier,
        executor: DeletionExecutor,
        preview_lifetime_seconds: int = 900,
        clock: Callable[[], datetime] | None = None,
        plan_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        if not all(callable(item) for item in (resolver, authorization_verifier, executor)):
            raise TypeError("deletion control callbacks must be callable")
        if not 60 <= preview_lifetime_seconds <= 3600:
            raise ValueError("preview lifetime must be between 1 minute and 1 hour")
        self._resolver = resolver
        self._verify_authorization = authorization_verifier
        self._executor = executor
        self._preview_lifetime = preview_lifetime_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._plan_id_factory = plan_id_factory

    async def preview(self, target: DeletionTarget) -> DeletionPreview:
        if not isinstance(target, DeletionTarget):
            raise TypeError("a typed deletion target is required")
        now = _aware(self._clock())
        resolved = await self._resolve(target)
        digest = _resolution_digest(resolved)
        return DeletionPreview(
            plan_id=self._plan_id_factory(),
            target=target,
            resolved=resolved,
            resolution_digest=digest,
            required_approvals=2 if resolved.organization_wide else 1,
            created_at=now,
            expires_at=now + timedelta(seconds=self._preview_lifetime),
        )

    async def execute(
        self,
        preview: DeletionPreview,
        *,
        confirmation: str,
        authorization_tokens: tuple[SecretStr, ...],
        idempotency_key: str,
    ) -> DeletionReceipt:
        if not isinstance(preview, DeletionPreview):
            raise TypeError("a typed deletion preview is required")
        now = _aware(self._clock())
        if now < preview.created_at or now >= preview.expires_at:
            raise DeletionControlError("deletion preview is expired or not yet valid")
        if not hmac.compare_digest(confirmation, preview.confirmation_text):
            raise DeletionControlError("explicit deletion confirmation does not match")
        if len(authorization_tokens) != preview.required_approvals:
            raise DeletionControlError("required authorization count does not match")
        if not all(isinstance(token, SecretStr) for token in authorization_tokens):
            raise TypeError("authorization tokens must be SecretStr values")
        current = await self._resolve(preview.target)
        if _resolution_digest(current) != preview.resolution_digest:
            raise DeletionControlError("deletion target changed after dry-run")
        grants = [
            await self._verify_grant(token, preview, now)
            for token in authorization_tokens
        ]
        principals = tuple(grant.principal_id for grant in grants)
        if len(principals) != len(set(principals)):
            raise DeletionControlError("authorizations must come from distinct principals")
        key = _idempotency_key(idempotency_key)
        try:
            operation_id = await self._executor(current, key)
        except Exception as exc:
            raise DeletionControlError("provider deletion failed") from exc
        if not _valid_identifier(operation_id):
            raise DeletionControlError("provider deletion returned an invalid operation ID")
        return DeletionReceipt(
            plan_id=preview.plan_id,
            target=preview.target,
            resolution_digest=preview.resolution_digest,
            authorizing_principal_ids=principals,
            provider_operation_id=operation_id,
            idempotency_key=key,
            deleted_at=_aware(self._clock()),
        )

    async def _resolve(self, target: DeletionTarget) -> ResolvedDeletionTarget:
        try:
            resolved = await self._resolver(target)
        except Exception as exc:
            raise DeletionControlError("deletion target resolution failed") from exc
        if not isinstance(resolved, ResolvedDeletionTarget) or resolved.target != target:
            raise DeletionControlError("resolver returned a mismatched deletion target")
        return resolved

    async def _verify_grant(
        self,
        token: SecretStr,
        preview: DeletionPreview,
        now: datetime,
    ) -> AuthorizationGrant:
        try:
            grant = await self._verify_authorization(token)
        except Exception as exc:
            raise DeletionControlError("deletion authorization verification failed") from exc
        if not isinstance(grant, AuthorizationGrant):
            raise DeletionControlError("authorization verifier returned an invalid grant")
        if (
            grant.decision != "allow"
            or grant.plan_id != preview.plan_id
            or grant.resolution_digest != preview.resolution_digest
            or now < grant.issued_at
            or now >= grant.expires_at
        ):
            raise DeletionControlError("deletion authorization is invalid or unbound")
        return grant


def _resolution_digest(value: ResolvedDeletionTarget) -> str:
    encoded = json.dumps(
        value.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DeletionControlError("deletion control clock must be timezone-aware")
    return value


def _idempotency_key(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 255 or any(
        not (char.isalnum() or char in "._:-") for char in value
    ):
        raise ValueError("idempotency key is invalid")
    return value


def _valid_identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 255
        and value[0].isalnum()
        and all(char.isalnum() or char in "_.:/-" for char in value)
    )
