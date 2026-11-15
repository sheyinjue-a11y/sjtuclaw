"""Official offline DeepSeek-V4 tokenizer and message encoder integration."""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast

from sjtuclaw.errors import ConfigurationError
from sjtuclaw.infrastructure.llm.tool_protocol import (
    json_messages,
    native_messages,
    native_tool_definitions,
)
from sjtuclaw.models.llm import LLMRequest, LLMToolMode


class _Tokenizer(Protocol):
    def encode(self, text: str) -> Sequence[int]: ...


_MessageEncoder = Callable[..., str]


class DeepSeekTokenCounter:
    """Count a V4 Chat request using only pinned local official artifacts."""

    def __init__(
        self,
        tokenizer_path: Path,
        *,
        tokenizer: _Tokenizer | None = None,
        encode_messages: _MessageEncoder | None = None,
    ) -> None:
        if (tokenizer is None) != (encode_messages is None):
            raise ValueError("tokenizer and encode_messages must be injected together")
        if tokenizer is None or encode_messages is None:
            tokenizer, encode_messages = _load_official_components(tokenizer_path)
        self._tokenizer = tokenizer
        self._encode_messages = encode_messages

    def count(self, request: LLMRequest) -> int:
        messages = (
            json_messages(request.messages, request.tools)
            if request.tool_mode is LLMToolMode.JSON
            else native_messages(request.messages, request.tools)
        )
        if request.tool_mode is LLMToolMode.NATIVE:
            _attach_to_first_system(
                messages,
                "tools",
                native_tool_definitions(request.tools),
            )
        elif request.tool_mode is LLMToolMode.JSON:
            _attach_to_first_system(
                messages,
                "response_format",
                {"type": "json_object"},
            )

        try:
            prompt = self._encode_messages(
                messages,
                thinking_mode="thinking",
                drop_thinking=True,
            )
            tokens = self._tokenizer.encode(prompt)
        except Exception as exc:
            raise ConfigurationError(
                "official DeepSeek-V4 tokenizer failed to count the request"
            ) from exc
        return len(tokens)


def _attach_to_first_system(
    messages: list[dict[str, object]],
    field: str,
    value: object,
) -> None:
    for message in messages:
        if message.get("role") == "system":
            message[field] = value
            return
    raise ConfigurationError("tool-aware token counting requires a system message")


def _load_official_components(
    tokenizer_path: Path,
) -> tuple[_Tokenizer, _MessageEncoder]:
    encoder_path = tokenizer_path / "encoding" / "encoding_dsv4.py"
    tokenizer_json = tokenizer_path / "tokenizer.json"
    tokenizer_config = tokenizer_path / "tokenizer_config.json"
    missing = [
        path.name
        for path in (encoder_path, tokenizer_json, tokenizer_config)
        if not path.is_file()
    ]
    if missing:
        names = ", ".join(missing)
        raise ConfigurationError(
            f"official DeepSeek-V4 tokenizer artifacts missing in {tokenizer_path}: {names}"
        )

    try:
        transformers = importlib.import_module("transformers")
        auto_tokenizer = transformers.AutoTokenizer
    except (ImportError, AttributeError) as exc:
        raise ConfigurationError(
            "official DeepSeek tokenizer mode requires transformers==4.46.3 "
            "and tokenizers==0.20.4"
        ) from exc

    try:
        tokenizer = auto_tokenizer.from_pretrained(
            str(tokenizer_path),
            use_fast=True,
            local_files_only=True,
        )
        module = _load_encoder_module(encoder_path)
        encode_messages = module.encode_messages
    except Exception as exc:
        raise ConfigurationError(
            "failed to initialize pinned local DeepSeek-V4 tokenizer artifacts"
        ) from exc
    if not callable(encode_messages):
        raise ConfigurationError("official DeepSeek-V4 encode_messages is not callable")
    return cast(_Tokenizer, tokenizer), cast(_MessageEncoder, encode_messages)


def _load_encoder_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("_sjtuclaw_deepseek_v4_encoding", path)
    if spec is None or spec.loader is None:
        raise ConfigurationError("cannot load official DeepSeek-V4 message encoder")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
