from pathlib import Path

from z_prov.config import load_settings


def test_load_config_expands_environment(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret")
    path = tmp_path / "providers.yaml"
    path.write_text(
        """
default_model: test
providers:
  local:
    api: openai
    base_url: http://localhost:1/v1
    api_key: ${TEST_PROVIDER_KEY}
models:
  test:
    provider: local
    model: test-model
""",
        encoding="utf-8",
    )
    value = load_settings(path)
    assert value.providers["local"].api_key == "secret"
    assert value.models["test"].primary.model == "test-model"


def test_disabled_provider_is_omitted(monkeypatch):
    monkeypatch.setenv("CUSTOM_PROVIDER_ENABLED", "false")
    value = load_settings("config/providers.example.yaml")
    assert "custom" not in value.providers


def test_enabled_provider_with_explicit_enabled_key_loads(monkeypatch):
    # `enabled: true` (or any truthy value) must not be forwarded as a
    # ProviderConfig kwarg -- ProviderConfig has no `enabled` field, so an
    # explicit truthy value previously raised TypeError at load time and
    # took down every provider defined with the `enabled:` key, not just
    # the disabled ones.
    monkeypatch.setenv("CUSTOM_PROVIDER_ENABLED", "true")
    value = load_settings("config/providers.example.yaml")
    assert "custom" in value.providers
    assert not hasattr(value.providers["custom"], "enabled")
