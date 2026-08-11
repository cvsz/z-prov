import httpx
import pytest

from z_prov.capabilities import CapabilityRecord, ModelLifecycle
from z_prov.config import ModelRoute, ProviderConfig, RouteTarget, Settings
from z_prov.providers import ProviderError
from z_prov.router import ProviderRouter


@pytest.fixture
def gateway():
    settings = Settings(
        providers={"local": ProviderConfig(name="local", api="openai", base_url="http://local/v1")},
        models={
            "alias": ModelRoute(
                alias="alias",
                primary=RouteTarget(provider="local", model="backend"),
            )
        },
        client_keys=frozenset(),
        default_model="alias",
    )
    return ProviderRouter(settings, httpx.AsyncClient(trust_env=False))


def test_model_alias(gateway):
    assert gateway.route("alias").primary.model == "backend"


def test_unknown_model(gateway):
    with pytest.raises(ProviderError) as error:
        gateway.route("missing")
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_fallback_uses_provider_specific_model():
    settings = Settings(
        providers={
            "primary": ProviderConfig(name="primary", api="openai", base_url="http://primary/v1"),
            "free": ProviderConfig(name="free", api="openai", base_url="http://free/v1"),
        },
        models={
            "z-prov-free": ModelRoute(
                alias="z-prov-free",
                primary=RouteTarget(provider="primary", model="local-model"),
                fallbacks=(RouteTarget(provider="free", model="cloud-free-model"),),
            )
        },
        client_keys=frozenset(),
        default_model="z-prov-free",
    )
    gateway = ProviderRouter(settings, httpx.AsyncClient(trust_env=False))
    seen = []

    async def operation(client, model):
        seen.append((client.config.name, model))
        if client.config.name == "primary":
            raise ProviderError("model unavailable", 404, retryable=True)
        return "ok"

    assert await gateway.execute(gateway.route("z-prov-free"), operation) == "ok"
    assert seen == [
        ("primary", "local-model"),
        ("free", "cloud-free-model"),
    ]


@pytest.mark.asyncio
async def test_stream_falls_back_only_before_first_byte():
    settings = Settings(
        providers={
            "primary": ProviderConfig(name="primary", api="openai", base_url="http://primary/v1"),
            "free": ProviderConfig(name="free", api="openai", base_url="http://free/v1"),
        },
        models={
            "z-prov-free": ModelRoute(
                alias="z-prov-free",
                primary=RouteTarget(provider="primary", model="local-model"),
                fallbacks=(RouteTarget(provider="free", model="cloud-free-model"),),
            )
        },
        client_keys=frozenset(),
        default_model="z-prov-free",
    )
    gateway = ProviderRouter(settings, httpx.AsyncClient(trust_env=False))
    seen = []

    async def operation(client, model):
        seen.append((client.config.name, model))
        if client.config.name == "primary":
            raise ProviderError("unavailable", 503, fallback_allowed=True)
        yield b"data: ok\n\n"

    assert [chunk async for chunk in gateway.stream(gateway.route("z-prov-free"), operation)] == [
        b"data: ok\n\n"
    ]
    assert seen == [
        ("primary", "local-model"),
        ("free", "cloud-free-model"),
    ]


@pytest.mark.asyncio
async def test_stream_does_not_replay_after_first_byte():
    settings = Settings(
        providers={
            "primary": ProviderConfig(name="primary", api="openai", base_url="http://primary/v1"),
            "free": ProviderConfig(name="free", api="openai", base_url="http://free/v1"),
        },
        models={
            "z-prov-free": ModelRoute(
                alias="z-prov-free",
                primary=RouteTarget(provider="primary", model="local-model"),
                fallbacks=(RouteTarget(provider="free", model="cloud-free-model"),),
            )
        },
        client_keys=frozenset(),
        default_model="z-prov-free",
    )
    gateway = ProviderRouter(settings, httpx.AsyncClient(trust_env=False))

    async def operation(client, _model):
        if client.config.name == "primary":
            yield b"data: partial\n\n"
            raise ProviderError("disconnected", 503, fallback_allowed=True)
        yield b"data: duplicate\n\n"

    stream = gateway.stream(gateway.route("z-prov-free"), operation)
    assert await anext(stream) == b"data: partial\n\n"
    with pytest.raises(ProviderError):
        await anext(stream)


def test_alias_resolution_skips_confirmed_retired_primary():
    settings = Settings(
        providers={
            "old": ProviderConfig(name="old", api="openai", base_url="http://old/v1"),
            "new": ProviderConfig(name="new", api="openai", base_url="http://new/v1"),
        },
        models={
            "z-prov-auto": ModelRoute(
                alias="z-prov-auto",
                primary=RouteTarget(provider="old", model="retired-model"),
                fallbacks=(RouteTarget(provider="new", model="active-model"),),
            )
        },
        client_keys=frozenset(),
        default_model="z-prov-auto",
    )
    gateway = ProviderRouter(settings, httpx.AsyncClient(trust_env=False))
    gateway.capabilities._records[("old", "retired-model")] = CapabilityRecord(
        provider="old",
        model="retired-model",
        lifecycle=ModelLifecycle.RETIRED,
        source="provider_models_api",
    )
    assert gateway.route("z-prov-auto").primary == RouteTarget(
        provider="new",
        model="active-model",
    )


def test_model_list_omits_fully_retired_alias_without_failing_others():
    settings = Settings(
        providers={"local": ProviderConfig(name="local", api="openai", base_url="http://local/v1")},
        models={
            "good": ModelRoute(alias="good", primary=RouteTarget(provider="local", model="backend")),
            "dead": ModelRoute(alias="dead", primary=RouteTarget(provider="local", model="gone")),
        },
        client_keys=frozenset(),
        default_model="good",
    )
    gateway = ProviderRouter(settings, httpx.AsyncClient(trust_env=False))
    gateway.capabilities._records[("local", "gone")] = CapabilityRecord(
        provider="local",
        model="gone",
        lifecycle=ModelLifecycle.RETIRED,
        source="provider_models_api",
    )
    listed = {item["id"] for item in gateway.model_list()}
    assert listed == {"good"}
