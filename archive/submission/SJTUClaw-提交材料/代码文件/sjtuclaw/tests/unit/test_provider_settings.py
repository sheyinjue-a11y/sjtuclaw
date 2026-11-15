from __future__ import annotations

import json
from pathlib import Path

import pytest

from sjtuclaw.config import ProviderConfiguration, ProviderKind
from sjtuclaw.infrastructure.persistence.provider_settings import ProviderSettingsStore


class FakeProtector:
    def protect(self, plaintext: bytes) -> bytes:
        return b"protected:" + plaintext[::-1]

    def unprotect(self, ciphertext: bytes) -> bytes:
        assert ciphertext.startswith(b"protected:")
        return ciphertext.removeprefix(b"protected:")[::-1]


def _configuration() -> ProviderConfiguration:
    return ProviderConfiguration(
        provider=ProviderKind.OPENAI_COMPATIBLE,
        model="gpt-4.1-mini",
        base_url="https://api.example.test/v1",
        api_key="secret-api-key",
        thinking_enabled=False,
    )


def test_anthropic_defaults_are_explicit() -> None:
    defaults = ProviderConfiguration.defaults(
        ProviderKind.ANTHROPIC,
        api_key="secret-api-key",
    )

    assert defaults.model == "claude-sonnet-4-20250514"
    assert defaults.base_url == "https://api.anthropic.com"


def test_provider_configuration_rejects_non_http_base_url() -> None:
    with pytest.raises(ValueError, match="http or https"):
        ProviderConfiguration(
            provider=ProviderKind.DEEPSEEK,
            model="deepseek-v4-flash",
            base_url="file:///private/key",
            api_key="secret-api-key",
        )


def test_store_round_trips_protected_configuration_without_plaintext(tmp_path: Path) -> None:
    store = ProviderSettingsStore(tmp_path, protector=FakeProtector())
    configuration = _configuration()

    store.save(configuration)

    persisted = (tmp_path / "provider-settings.bin").read_bytes()
    assert b"secret-api-key" not in persisted
    restored = ProviderSettingsStore(tmp_path, protector=FakeProtector()).load()
    assert restored == configuration
    assert restored.thinking_enabled is False


def test_store_defaults_legacy_configuration_to_thinking_enabled(tmp_path: Path) -> None:
    protector = FakeProtector()
    legacy = json.dumps(
        {
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "base_url": "https://api.deepseek.com",
            "api_key": "secret-api-key",
        }
    ).encode("utf-8")
    (tmp_path / "provider-settings.bin").write_bytes(protector.protect(legacy))

    restored = ProviderSettingsStore(tmp_path, protector=protector).load()

    assert restored is not None
    assert restored.thinking_enabled is True


def test_public_snapshot_marks_saved_key_without_exposing_it(tmp_path: Path) -> None:
    store = ProviderSettingsStore(tmp_path, protector=FakeProtector())

    snapshot = store.public_snapshot(_configuration())

    assert snapshot.key_saved is True
    assert snapshot.thinking_enabled is False
    assert "secret-api-key" not in repr(snapshot)
