"""Current-user protected storage for runtime model provider settings."""

from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path
from typing import Protocol

from sjtuclaw.config import (
    ProviderConfiguration,
    ProviderKind,
    ProviderPublicSnapshot,
    TokenCounterMode,
)

_SETTINGS_FILE = "provider-settings.bin"


class DataProtector(Protocol):
    def protect(self, plaintext: bytes) -> bytes: ...

    def unprotect(self, ciphertext: bytes) -> bytes: ...


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


class WindowsDataProtector:
    """Use DPAPI so only the current Windows user can decrypt credentials."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows DPAPI is unavailable on this platform")
        self._crypt32 = ctypes.windll.crypt32
        self._kernel32 = ctypes.windll.kernel32
        self._crypt32.CryptProtectData.argtypes = (
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        )
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._crypt32.CryptUnprotectData.argtypes = self._crypt32.CryptProtectData.argtypes
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
        self._kernel32.LocalFree.restype = wintypes.HLOCAL

    def protect(self, plaintext: bytes) -> bytes:
        return self._transform(plaintext, protect=True)

    def unprotect(self, ciphertext: bytes) -> bytes:
        return self._transform(ciphertext, protect=False)

    def _transform(self, value: bytes, *, protect: bool) -> bytes:
        source = ctypes.create_string_buffer(value)
        source_blob = _DataBlob(
            len(value),
            ctypes.cast(source, ctypes.POINTER(ctypes.c_byte)),
        )
        target_blob = _DataBlob()
        function = (
            self._crypt32.CryptProtectData
            if protect
            else self._crypt32.CryptUnprotectData
        )
        if not function(
            ctypes.byref(source_blob),
            "SJTUClaw provider settings" if protect else None,
            None,
            None,
            None,
            0,
            ctypes.byref(target_blob),
        ):
            raise OSError("Windows DPAPI operation failed")
        try:
            return ctypes.string_at(target_blob.pbData, target_blob.cbData)
        finally:
            self._kernel32.LocalFree(ctypes.cast(target_blob.pbData, wintypes.HLOCAL))


class ProviderSettingsStore:
    """Persist a Provider configuration only when a current-user protector exists."""

    def __init__(
        self,
        root: Path,
        *,
        protector: DataProtector | None = None,
    ) -> None:
        self._root = root
        self._protector = protector if protector is not None else _default_protector()
        self._configuration: ProviderConfiguration | None = None

    def load(self) -> ProviderConfiguration | None:
        if self._configuration is not None:
            return self._configuration
        if self._protector is None:
            return None
        path = self._root / _SETTINGS_FILE
        if not path.is_file():
            return None
        payload = json.loads(self._protector.unprotect(path.read_bytes()).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("saved provider settings must be an object")
        configuration = ProviderConfiguration(
            provider=ProviderKind(_required_text(payload, "provider")),
            model=_required_text(payload, "model"),
            base_url=_required_text(payload, "base_url"),
            api_key=_required_text(payload, "api_key"),
            thinking_enabled=_optional_bool(payload, "thinking_enabled", default=True),
        )
        self._configuration = configuration
        return configuration

    def save(self, configuration: ProviderConfiguration) -> None:
        self._configuration = configuration
        if self._protector is None:
            return
        self._root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "provider": configuration.provider.value,
                "model": configuration.model,
                "base_url": configuration.base_url,
                "api_key": configuration.api_key,
                "thinking_enabled": configuration.thinking_enabled,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        path = self._root / _SETTINGS_FILE
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(self._protector.protect(payload))
        temporary.replace(path)

    @staticmethod
    def public_snapshot(
        configuration: ProviderConfiguration,
    ) -> ProviderPublicSnapshot:
        token_counter_mode: TokenCounterMode = (
            "deepseek" if configuration.provider is ProviderKind.DEEPSEEK else "approximate"
        )
        return ProviderPublicSnapshot(
            provider=configuration.provider,
            model=configuration.model,
            base_url=configuration.base_url,
            key_saved=True,
            token_counter_mode=token_counter_mode,
            thinking_enabled=configuration.thinking_enabled,
        )


def _default_protector() -> DataProtector | None:
    return WindowsDataProtector() if os.name == "nt" else None


def _required_text(payload: dict[object, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"saved provider setting {field} must be text")
    return value


def _optional_bool(payload: dict[str, object], key: str, *, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"saved provider setting {key!r} must be a boolean")
    return value
