from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from .config import Settings
from .providers import ProviderClient, ProviderError


class ModelLifecycle(StrEnum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    RETIRED = "retired"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ModelCapabilities:
    context_window: int | None = None
    max_output_tokens: int | None = None
    vision: bool | None = None
    tools: bool | None = None
    streaming: bool | None = None
    structured_output: bool | None = None
    adaptive_thinking: bool | None = None
    mid_conversation_system: bool | None = None


@dataclass(frozen=True)
class CapabilityRecord:
    provider: str
    model: str
    account: str = "default"
    region: str = "global"
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)
    lifecycle: ModelLifecycle = ModelLifecycle.UNKNOWN
    source: str = "configuration"
    observed_at: float = 0.0
    expires_at: float = 0.0

    def public(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "account": self.account,
            "region": self.region,
            "lifecycle": self.lifecycle,
            "source": self.source,
            "observed_at": self.observed_at,
            "expires_at": self.expires_at,
            "capabilities": {
                key: value
                for key, value in asdict(self.capabilities).items()
                if value is not None
            },
        }


class CapabilityRegistry:
    def __init__(
        self,
        settings: Settings,
        *,
        ttl_seconds: float = 900.0,
        clock=time.time,
    ):
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._records: dict[tuple[str, str], CapabilityRecord] = {}
        self._configured: dict[tuple[str, str], CapabilityRecord] = {}
        for route in settings.models.values():
            for target in (route.primary, *route.fallbacks):
                provider_config = settings.providers[target.provider]
                record = CapabilityRecord(
                    provider=target.provider,
                    model=target.model,
                    account=provider_config.account,
                    region=provider_config.region,
                )
                self._configured.setdefault((target.provider, target.model), record)
                self._records.setdefault((target.provider, target.model), record)

    def get(self, provider: str, model: str) -> CapabilityRecord | None:
        key = (provider, model)
        record = self._records.get(key)
        if record and record.expires_at and record.expires_at <= self.clock():
            return self._configured.get(key)
        return record

    def list(self) -> list[CapabilityRecord]:
        records = (
            self.get(provider, model)
            for provider, model in self._records
        )
        return sorted(
            (record for record in records if record is not None),
            key=lambda item: (item.provider, item.account, item.region, item.model),
        )

    async def refresh_provider(self, provider: str, client: ProviderClient) -> int:
        payload = await client.models()
        models = payload.get("data", [])
        if not isinstance(models, list):
            raise ProviderError("Provider Models API returned an invalid data field")
        observed = self.clock()
        count = 0
        for item in models:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            model = item["id"]
            raw_capabilities = item.get("capabilities")
            if not isinstance(raw_capabilities, dict):
                raw_capabilities = {}
            self._records[(provider, model)] = CapabilityRecord(
                provider=provider,
                model=model,
                account=client.config.account,
                region=client.config.region,
                capabilities=ModelCapabilities(
                    context_window=_positive_int(
                        item.get("max_input_tokens", item.get("context_window"))
                    ),
                    max_output_tokens=_positive_int(
                        item.get("max_tokens", item.get("max_output_tokens"))
                    ),
                    vision=_optional_bool(raw_capabilities.get("vision")),
                    tools=_optional_bool(raw_capabilities.get("tools")),
                    streaming=_optional_bool(raw_capabilities.get("streaming")),
                    structured_output=_optional_bool(
                        raw_capabilities.get("structured_output")
                    ),
                    adaptive_thinking=_optional_bool(
                        raw_capabilities.get("adaptive_thinking")
                    ),
                    mid_conversation_system=_optional_bool(
                        raw_capabilities.get("mid_conversation_system")
                    ),
                ),
                lifecycle=_lifecycle(item.get("status")),
                source="provider_models_api",
                observed_at=observed,
                expires_at=observed + self.ttl_seconds,
            )
            count += 1
        return count


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and value > 0 else None


def _lifecycle(value: Any) -> ModelLifecycle:
    try:
        return ModelLifecycle(str(value).lower())
    except ValueError:
        return ModelLifecycle.ACTIVE
