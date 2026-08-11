from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, TypeVar

import httpx

from .capabilities import CapabilityRegistry, ModelLifecycle
from .config import ModelRoute, Settings
from .providers import ProviderClient, ProviderError

T = TypeVar("T")


class ProviderRouter:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.clients = {
            name: ProviderClient(config, client)
            for name, config in settings.providers.items()
        }
        self.capabilities = CapabilityRegistry(settings)

    def route(self, alias: str | None) -> ModelRoute:
        name = alias or self.settings.default_model
        try:
            configured = self.settings.models[name]
        except KeyError as exc:
            raise ProviderError(f"Unknown model: {name}", 404) from exc
        candidates = (configured.primary, *configured.fallbacks)
        available = [
            target
            for target in candidates
            if (
                (record := self.capabilities.get(target.provider, target.model)) is None
                or record.lifecycle != ModelLifecycle.RETIRED
            )
        ]
        if not available:
            raise ProviderError(
                f"All configured targets for {name} are retired",
                410,
                fallback_allowed=False,
                circuit_failure=False,
            )
        return ModelRoute(
            alias=configured.alias,
            primary=available[0],
            fallbacks=tuple(available[1:]),
        )

    async def execute(
        self,
        route: ModelRoute,
        operation: Callable[[ProviderClient, str], Awaitable[T]],
    ) -> T:
        candidates = (route.primary, *route.fallbacks)
        errors: list[str] = []
        for target in candidates:
            client = self.clients.get(target.provider)
            if not client:
                errors.append(f"{target.provider}: not configured")
                continue
            try:
                return await operation(client, target.model)
            except ProviderError as exc:
                errors.append(f"{target.provider}/{target.model}: {exc}")
                if not exc.fallback_allowed:
                    raise
        raise ProviderError("All providers failed: " + "; ".join(errors), 502)

    async def stream(
        self,
        route: ModelRoute,
        operation: Callable[[ProviderClient, str], AsyncIterator[bytes]],
    ) -> AsyncIterator[bytes]:
        errors: list[str] = []
        for target in (route.primary, *route.fallbacks):
            client = self.clients.get(target.provider)
            if not client:
                errors.append(f"{target.provider}: not configured")
                continue
            emitted = False
            try:
                async for chunk in operation(client, target.model):
                    emitted = True
                    yield chunk
                return
            except ProviderError as exc:
                if emitted or not exc.fallback_allowed:
                    raise
                errors.append(f"{target.provider}/{target.model}: {exc}")
        raise ProviderError("All streaming providers failed: " + "; ".join(errors), 502)

    def model_list(self) -> list[dict[str, Any]]:
        listing: list[dict[str, Any]] = []
        for route in self.settings.models.values():
            try:
                resolved = self.route(route.alias)
            except ProviderError:
                # An alias whose every configured target has since been
                # retired shouldn't take down model discovery for every
                # other alias -- omit just this one, same as a client
                # would see if the alias didn't exist, rather than letting
                # the 410 from route() propagate and fail the whole
                # /v1/models response.
                continue
            listing.append({
                "id": resolved.alias,
                "object": "model",
                "owned_by": resolved.primary.provider,
                "provider_model": resolved.primary.model,
                "fallbacks": [
                    {"provider": target.provider, "model": target.model}
                    for target in resolved.fallbacks
                ],
                "capability": (
                    record.public()
                    if (record := self.capabilities.get(
                        resolved.primary.provider,
                        resolved.primary.model,
                    ))
                    else None
                ),
            })
        return listing
