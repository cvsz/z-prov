from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ENV_PATTERN = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(?::-([^}]*))?\}")


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


@dataclass(frozen=True)
class RouteTarget:
    provider: str
    model: str


@dataclass(frozen=True)
class ModelRoute:
    alias: str
    primary: RouteTarget
    fallbacks: tuple[RouteTarget, ...] = ()


@dataclass(frozen=True)
class Settings:
    providers: dict[str, ProviderConfig]
    models: dict[str, ModelRoute]
    client_keys: frozenset[str]
    default_model: str
    max_request_bytes: int = 4 * 1024 * 1024
    max_output_tokens: int = 131072
    rate_limit_per_minute: int = 120
    max_file_bytes: int = 32 * 1024 * 1024


def load_settings(path: str | Path | None = None) -> Settings:
    config_path = Path(path or os.getenv("Z_PROV_CONFIG", "config/providers.yaml"))
    raw = _expand(yaml.safe_load(config_path.read_text(encoding="utf-8")) or {})
    providers = {
        name: ProviderConfig(name=name, **{k: v for k, v in value.items() if k != "enabled"})
        for name, value in (raw.get("providers") or {}).items()
        if _enabled(value.get("enabled", True))
    }
    models = {}
    for alias, value in (raw.get("models") or {}).items():
        primary = RouteTarget(provider=value["provider"], model=value["model"])
        if primary.provider not in providers:
            continue
        fallbacks = tuple(
            RouteTarget(provider=item["provider"], model=item["model"])
            for item in value.get("fallbacks", [])
            if item["provider"] in providers
        )
        models[alias] = ModelRoute(alias=alias, primary=primary, fallbacks=fallbacks)
    keys = frozenset(filter(None, os.getenv("Z_PROV_CLIENT_KEYS", "").split(",")))
    if not providers:
        raise RuntimeError("No providers are enabled")
    if not models:
        raise RuntimeError("No model routes are configured")
    default_model = raw.get("default_model") or next(iter(models))
    if default_model not in models:
        raise RuntimeError(f"Default model route is missing: {default_model}")
    return Settings(
        providers=providers,
        models=models,
        client_keys=keys,
        default_model=default_model,
        max_request_bytes=int(os.getenv("Z_PROV_MAX_REQUEST_BYTES", 4 * 1024 * 1024)),
        max_output_tokens=int(os.getenv("Z_PROV_MAX_OUTPUT_TOKENS", 131072)),
        rate_limit_per_minute=int(os.getenv("Z_PROV_RATE_LIMIT_PER_MINUTE", 120)),
        max_file_bytes=int(os.getenv("Z_PROV_MAX_FILE_BYTES", 32 * 1024 * 1024)),
    )
