from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from .security import client_key_digest

ENV_PATTERN = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(?::-([^}]*))?\}")
CLIENT_KEY_HASH_PATTERN = re.compile(r"^(?:sha256:)?([a-fA-F0-9]{64})$")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            return os.getenv(name, default or "")

        return ENV_PATTERN.sub(replace, value)
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    return value


def _enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    api: str
    base_url: str
    api_key: str = ""
    account: str = "default"
    region: str = "global"
    headers: dict[str, str] = field(default_factory=dict)
    api_version: str = ""
    timeout_seconds: float = 120.0
    total_timeout_seconds: float = 135.0
    max_attempts: int = 3
    retry_base_seconds: float = 0.25
    retry_max_seconds: float = 5.0
    circuit_failure_threshold: int = 5
    circuit_reset_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not 1 <= len(self.name) <= 128:
            raise ValueError("provider name is invalid")
        if not isinstance(self.api, str) or self.api not in {
            "anthropic",
            "azure",
            "openai",
            "responses",
        }:
            raise ValueError("provider API must be anthropic, azure, openai, or responses")
        _validate_origin(self.base_url, "provider base_url")
        if not isinstance(self.api_key, str) or "\x00" in self.api_key:
            raise ValueError("provider api_key is invalid")
        if not isinstance(self.account, str) or not 1 <= len(self.account) <= 128:
            raise ValueError("provider account is invalid")
        if not isinstance(self.region, str) or not 1 <= len(self.region) <= 128:
            raise ValueError("provider region is invalid")
        if not isinstance(self.headers, dict) or len(self.headers) > 64:
            raise ValueError("provider headers are invalid")
        for key, value in self.headers.items():
            if (
                not isinstance(key, str)
                or not key
                or len(key) > 128
                or not isinstance(value, str)
                or "\r" in key
                or "\n" in key
                or "\r" in value
                or "\n" in value
            ):
                raise ValueError("provider headers are invalid")
        if (
            not isinstance(self.api_version, str)
            or len(self.api_version) > 128
            or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                   for character in self.api_version)
        ):
            raise ValueError("provider api_version is invalid")
        _require_number(self.timeout_seconds, "provider timeout_seconds", minimum=0.001, maximum=600)
        _require_number(
            self.total_timeout_seconds,
            "provider total_timeout_seconds",
            minimum=0.001,
            maximum=3600,
        )
        _require_integer(self.max_attempts, "provider max_attempts", minimum=1, maximum=10)
        _require_number(
            self.retry_base_seconds,
            "provider retry_base_seconds",
            minimum=0,
            maximum=60,
        )
        _require_number(
            self.retry_max_seconds,
            "provider retry_max_seconds",
            minimum=0,
            maximum=120,
        )
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("provider retry_max_seconds cannot be lower than retry_base_seconds")
        _require_integer(
            self.circuit_failure_threshold,
            "provider circuit_failure_threshold",
            minimum=1,
            maximum=100,
        )
        _require_number(
            self.circuit_reset_seconds,
            "provider circuit_reset_seconds",
            minimum=0.001,
            maximum=3600,
        )


@dataclass(frozen=True)
class RouteTarget:
    provider: str
    model: str

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or not 1 <= len(self.provider) <= 128:
            raise ValueError("route provider is invalid")
        if (
            not isinstance(self.model, str)
            or not 1 <= len(self.model) <= 512
            or any(character in self.model for character in "\r\n\x00")
        ):
            raise ValueError("route model is invalid")


@dataclass(frozen=True)
class ModelRoute:
    alias: str
    primary: RouteTarget
    fallbacks: tuple[RouteTarget, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.alias, str) or not 1 <= len(self.alias) <= 256:
            raise ValueError("model alias is invalid")


@dataclass(frozen=True)
class Settings:
    providers: dict[str, ProviderConfig]
    models: dict[str, ModelRoute]
    client_key_hashes: frozenset[bytes]
    default_model: str
    max_request_bytes: int = 4 * 1024 * 1024
    max_output_tokens: int = 131072
    rate_limit_per_minute: int = 120
    metrics_enabled: bool = True
    otlp_metrics_enabled: bool = False
    rate_limit_backend: str = "memory"
    redis_url: str = ""
    rate_limit_key_prefix: str = "zeaz:rate-limit:"
    trusted_proxy_cidrs: tuple[str, ...] = ()
    max_concurrent_requests: int = 100
    max_response_bytes: int = 16 * 1024 * 1024


def load_settings(path: str | Path | None = None) -> Settings:
    config_path = Path(path or os.getenv("ZEAZ_CONFIG", "config/providers.yaml"))
    raw_value = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if raw_value is None:
        raw_value = {}
    if not isinstance(raw_value, Mapping):
        raise RuntimeError("Provider configuration must be a YAML object")
    raw = _expand(dict(raw_value))
    raw_providers = raw.get("providers", {})
    if not isinstance(raw_providers, Mapping):
        raise RuntimeError("Provider configuration providers must be an object")
    providers = {}
    for name, value in raw_providers.items():
        if not isinstance(name, str):
            raise RuntimeError("Provider configuration names must be strings")
        if not isinstance(value, Mapping):
            raise RuntimeError("Provider configuration entries must be objects")
        if not _enabled(value.get("enabled", True)):
            continue
        provider_value = dict(value)
        provider_value.pop("enabled", None)
        try:
            providers[name] = ProviderConfig(name=name, **provider_value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid provider configuration: {name}") from exc
    raw_models = raw.get("models", {})
    if not isinstance(raw_models, Mapping):
        raise RuntimeError("Provider configuration models must be an object")
    models = {}
    for alias, value in raw_models.items():
        if not isinstance(alias, str):
            raise RuntimeError("Model route names must be strings")
        if not isinstance(value, Mapping):
            raise RuntimeError("Model route entries must be objects")
        try:
            primary = RouteTarget(provider=value["provider"], model=value["model"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid model route: {alias}") from exc
        if primary.provider not in providers:
            continue
        raw_fallbacks = value.get("fallbacks", [])
        if not isinstance(raw_fallbacks, list):
            raise RuntimeError(f"Invalid model route fallbacks: {alias}")
        fallbacks: list[RouteTarget] = []
        for item in raw_fallbacks:
            if not isinstance(item, Mapping) or "provider" not in item or "model" not in item:
                raise RuntimeError(f"Invalid model route fallback: {alias}")
            try:
                fallback = RouteTarget(provider=item["provider"], model=item["model"])
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"Invalid model route fallback: {alias}") from exc
            if fallback.provider not in providers:
                continue
            fallbacks.append(fallback)
        try:
            models[alias] = ModelRoute(alias=alias, primary=primary, fallbacks=tuple(fallbacks))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid model route: {alias}") from exc
    key_hashes: set[bytes] = {
        client_key_digest(key.strip())
        for key in os.getenv("ZEAZ_CLIENT_KEYS", "").split(",")
        if key.strip()
    }
    for configured_hash in os.getenv("ZEAZ_CLIENT_KEY_HASHES", "").split(","):
        if not configured_hash.strip():
            continue
        match = CLIENT_KEY_HASH_PATTERN.fullmatch(configured_hash.strip())
        if not match:
            raise RuntimeError("ZEAZ_CLIENT_KEY_HASHES contains an invalid SHA-256 hash")
        key_hashes.add(bytes.fromhex(match.group(1)))
    if not providers:
        raise RuntimeError("No providers are enabled")
    if not models:
        raise RuntimeError("No model routes are configured")
    default_model = raw.get("default_model") or next(iter(models))
    if not isinstance(default_model, str):
        raise RuntimeError("Default model route must be a string")
    if default_model not in models:
        raise RuntimeError(f"Default model route is missing: {default_model}")
    rate_limit_backend = os.getenv("ZEAZ_RATE_LIMIT_BACKEND", "memory").strip().lower()
    if rate_limit_backend not in {"memory", "redis"}:
        raise RuntimeError("ZEAZ_RATE_LIMIT_BACKEND must be memory or redis")
    redis_url = os.getenv("ZEAZ_REDIS_URL", "")
    if rate_limit_backend == "redis" and not redis_url:
        raise RuntimeError("ZEAZ_REDIS_URL is required for the Redis rate-limit backend")
    trusted_proxy_cidrs = tuple(
        item.strip()
        for item in os.getenv("ZEAZ_TRUSTED_PROXY_CIDRS", "").split(",")
        if item.strip()
    )
    try:
        trusted_proxy_cidrs = tuple(
            str(ipaddress.ip_network(item, strict=False)) for item in trusted_proxy_cidrs
        )
    except ValueError as exc:
        raise RuntimeError("ZEAZ_TRUSTED_PROXY_CIDRS contains an invalid CIDR") from exc
    max_request_bytes = _env_int(
        "ZEAZ_MAX_REQUEST_BYTES",
        4 * 1024 * 1024,
        minimum=1024,
        maximum=67_108_864,
    )
    max_output_tokens = _env_int(
        "ZEAZ_MAX_OUTPUT_TOKENS",
        131072,
        minimum=1,
        maximum=1_000_000,
    )
    rate_limit_per_minute = _env_int(
        "ZEAZ_RATE_LIMIT_PER_MINUTE",
        120,
        minimum=1,
        maximum=1_000_000,
    )
    max_concurrent_requests = _env_int(
        "ZEAZ_MAX_CONCURRENT_REQUESTS",
        100,
        minimum=1,
        maximum=10_000,
    )
    max_response_bytes = _env_int(
        "ZEAZ_MAX_RESPONSE_BYTES",
        16 * 1024 * 1024,
        minimum=1024,
        maximum=1_073_741_824,
    )
    rate_limit_key_prefix = os.getenv("ZEAZ_RATE_LIMIT_KEY_PREFIX", "zeaz:rate-limit:")
    if (
        not 1 <= len(rate_limit_key_prefix) <= 128
        or any(character in rate_limit_key_prefix for character in "\r\n\x00")
    ):
        raise RuntimeError("ZEAZ_RATE_LIMIT_KEY_PREFIX is invalid")
    return Settings(
        providers=providers,
        models=models,
        client_key_hashes=frozenset(key_hashes),
        default_model=default_model,
        max_request_bytes=max_request_bytes,
        max_output_tokens=max_output_tokens,
        rate_limit_per_minute=rate_limit_per_minute,
        metrics_enabled=_enabled(os.getenv("ZEAZ_METRICS_ENABLED", "true")),
        otlp_metrics_enabled=_enabled(os.getenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "")),
        rate_limit_backend=rate_limit_backend,
        redis_url=redis_url,
        rate_limit_key_prefix=rate_limit_key_prefix,
        trusted_proxy_cidrs=trusted_proxy_cidrs,
        max_concurrent_requests=max_concurrent_requests,
        max_response_bytes=max_response_bytes,
    )


def _validate_origin(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError(f"{label} is invalid")
    if any(character in value for character in "\r\n\x00"):
        raise ValueError(f"{label} is invalid")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{label} must be an HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{label} must not contain credentials, query, or fragment")
    try:
        _port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} has an invalid port") from exc


def _require_integer(value: Any, label: str, *, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")


def _require_number(value: Any, label: str, *, minimum: float, maximum: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    try:
        value = int(default if raw is None else raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    if value > maximum:
        raise RuntimeError(f"{name} must be at most {maximum}")
    return value
