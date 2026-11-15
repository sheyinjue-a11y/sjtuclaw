"""Environment-backed configuration with secret-safe representations."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlparse

from dotenv import dotenv_values

from sjtuclaw.errors import ConfigurationError

ToolMode = Literal["native", "json"]
TokenCounterMode = Literal["deepseek", "approximate"]


class ProviderKind(StrEnum):
    """Supported model API wire protocols."""

    DEEPSEEK = "deepseek"
    OPENAI_COMPATIBLE = "openai_compatible"
    ANTHROPIC = "anthropic"


_PROVIDER_DEFAULTS = {
    ProviderKind.DEEPSEEK: ("deepseek-v4-flash", "https://api.deepseek.com"),
    ProviderKind.OPENAI_COMPATIBLE: ("gpt-4.1-mini", "https://api.openai.com/v1"),
    ProviderKind.ANTHROPIC: ("claude-sonnet-4-20250514", "https://api.anthropic.com"),
}

_MODEL_CONTEXT_WINDOWS = {
    "deepseek-v4-flash": 1_048_576,
    "deepseek-v4-pro": 1_048_576,
}
_MODEL_DISPLAY_NAMES = {
    "deepseek-v4-flash": "DeepSeek V4 Flash",
    "deepseek-v4-pro": "DeepSeek V4 Pro",
}


@dataclass(frozen=True, slots=True)
class ModelProfile:
    model_id: str
    display_name: str
    context_window_tokens: int


@dataclass(frozen=True, slots=True)
class ProviderConfiguration:
    """Validated runtime connection configuration; its Key is never repr-safe."""

    provider: ProviderKind
    model: str
    base_url: str
    api_key: str = field(repr=False)
    thinking_enabled: bool = True

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("model must not be empty")
        if not self.api_key.strip():
            raise ValueError("api key must not be empty")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base URL must use http or https")

    @classmethod
    def defaults(
        cls,
        provider: ProviderKind,
        *,
        api_key: str,
    ) -> ProviderConfiguration:
        model, base_url = _PROVIDER_DEFAULTS[provider]
        return cls(provider=provider, model=model, base_url=base_url, api_key=api_key)


@dataclass(frozen=True, slots=True)
class ProviderPublicSnapshot:
    """Safe provider data returned to the local settings UI."""

    provider: ProviderKind
    model: str
    base_url: str
    key_saved: bool
    token_counter_mode: TokenCounterMode
    thinking_enabled: bool


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated runtime settings. The API key is excluded from repr output."""

    api_key: str = field(repr=False)
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-flash"
    data_dir: Path = Path(".sjtuclaw")
    skill_root: Path = Path("skills")
    tokenizer_path: Path = Path("vendor/deepseek-v4-flash-tokenizer")
    tool_root: Path = Path(".")
    tool_max_file_bytes: int = 3_145_728
    attachment_process_timeout_seconds: int = 60
    tool_mode: ToolMode = "native"
    agent_max_steps: int = 20
    agent_repeat_call_limit: int = 5
    context_budget_tokens: int = 1_048_576
    compaction_trigger_ratio: float = 0.0625
    compaction_target_ratio: float = 0.03125
    compaction_trigger_tokens: int = 65_536
    compaction_target_tokens: int = 32_768
    compaction_keep_recent_turns: int = 4
    llm_output_reserve_tokens: int = 8_192
    memory_retrieval_limit: int = 8
    memory_context_max_tokens: int = 2_048
    token_counter_mode: TokenCounterMode = "deepseek"

    @classmethod
    def from_mapping(cls, values: Mapping[str, str | None]) -> Settings:
        """Build settings from an explicit mapping without reading the real environment."""

        api_key = _required(values, "LLM_API_KEY")
        base_url = _value(values, "LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
        model = _value(values, "LLM_MODEL", "deepseek-v4-flash")
        tool_mode_raw = _choice(values, "LLM_TOOL_MODE", "native", {"native", "json"})
        token_mode_raw = _choice(
            values,
            "TOKEN_COUNTER_MODE",
            "deepseek",
            {"deepseek", "approximate"},
        )
        context_window_tokens = _model_context_window(values, model)
        compaction_trigger_tokens = _integer(
            values,
            "COMPACTION_TRIGGER_TOKENS",
            65_536,
            minimum=1,
        )
        compaction_target_tokens = _integer(
            values,
            "COMPACTION_TARGET_TOKENS",
            32_768,
            minimum=1,
        )
        legacy_trigger_ratio = _ratio(values, "COMPACTION_TRIGGER_RATIO", 0.80)
        legacy_target_ratio = _ratio(values, "COMPACTION_TARGET_RATIO", 0.50)
        if legacy_target_ratio >= legacy_trigger_ratio:
            raise ConfigurationError(
                "COMPACTION_TARGET_RATIO must be smaller than COMPACTION_TRIGGER_RATIO"
            )

        settings = cls(
            api_key=api_key,
            base_url=base_url,
            model=model,
            data_dir=Path(_value(values, "SJTUCLAW_DATA_DIR", ".sjtuclaw")),
            skill_root=Path(_value(values, "SKILL_ROOT", "skills")),
            tokenizer_path=Path(
                _value(
                    values,
                    "DEEPSEEK_TOKENIZER_PATH",
                    "vendor/deepseek-v4-flash-tokenizer",
                )
            ),
            tool_root=Path(_value(values, "TOOL_ROOT", ".")),
            tool_max_file_bytes=_integer(
                values,
                "TOOL_MAX_FILE_BYTES",
                3_145_728,
                minimum=1,
            ),
            attachment_process_timeout_seconds=_integer(
                values,
                "ATTACHMENT_PROCESS_TIMEOUT_SECONDS",
                60,
                minimum=1,
            ),
            tool_mode=cast(ToolMode, tool_mode_raw),
            agent_max_steps=_integer(values, "AGENT_MAX_STEPS", 20, minimum=0),
            agent_repeat_call_limit=_integer(
                values,
                "AGENT_REPEAT_CALL_LIMIT",
                5,
                minimum=1,
            ),
            context_budget_tokens=context_window_tokens,
            compaction_trigger_ratio=(
                compaction_trigger_tokens / context_window_tokens
            ),
            compaction_target_ratio=(compaction_target_tokens / context_window_tokens),
            compaction_trigger_tokens=compaction_trigger_tokens,
            compaction_target_tokens=compaction_target_tokens,
            compaction_keep_recent_turns=_integer(
                values,
                "COMPACTION_KEEP_RECENT_TURNS",
                4,
                minimum=1,
            ),
            llm_output_reserve_tokens=_integer(
                values,
                "LLM_OUTPUT_RESERVE_TOKENS",
                8_192,
                minimum=1,
            ),
            memory_retrieval_limit=_integer(
                values,
                "MEMORY_RETRIEVAL_LIMIT",
                8,
                minimum=1,
            ),
            memory_context_max_tokens=_integer(
                values,
                "MEMORY_CONTEXT_MAX_TOKENS",
                2_048,
                minimum=1,
            ),
            token_counter_mode=cast(TokenCounterMode, token_mode_raw),
        )
        settings._validate_relationships()
        return settings

    @classmethod
    def load(
        cls,
        dotenv_path: str | Path = ".env",
        environ: Mapping[str, str] | None = None,
    ) -> Settings:
        """Load `.env`, then override it with the supplied or process environment."""

        merged: dict[str, str | None] = dict(dotenv_values(dotenv_path))
        merged.update(os.environ if environ is None else environ)
        return cls.from_mapping(merged)

    def _validate_relationships(self) -> None:
        if not self.base_url.startswith(("https://", "http://")):
            raise ConfigurationError("LLM_BASE_URL must start with http:// or https://")
        if not (
            0
            < self.compaction_target_tokens
            < self.compaction_trigger_tokens
            < self.context_budget_tokens
        ):
            raise ConfigurationError(
                "compaction tokens must satisfy 0 < target < trigger < model context window"
            )
        if self.llm_output_reserve_tokens >= self.context_budget_tokens:
            raise ConfigurationError(
                "LLM_OUTPUT_RESERVE_TOKENS must be smaller than the model context window"
            )

    @property
    def model_profile(self) -> ModelProfile:
        return ModelProfile(
            model_id=self.model,
            display_name=_MODEL_DISPLAY_NAMES.get(self.model, self.model),
            context_window_tokens=self.context_budget_tokens,
        )


def _required(values: Mapping[str, str | None], name: str) -> str:
    value = values.get(name)
    if value is None or not value.strip():
        raise ConfigurationError(f"{name} is required")
    return value.strip()


def _value(values: Mapping[str, str | None], name: str, default: str) -> str:
    value = values.get(name)
    return default if value is None or not value.strip() else value.strip()


def _choice(
    values: Mapping[str, str | None],
    name: str,
    default: str,
    allowed: set[str],
) -> str:
    value = _value(values, name, default).lower()
    if value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ConfigurationError(f"{name} must be one of: {choices}")
    return value


def _integer(
    values: Mapping[str, str | None],
    name: str,
    default: int,
    *,
    minimum: int,
) -> int:
    raw = _value(values, name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}")
    return value


def _model_context_window(values: Mapping[str, str | None], model: str) -> int:
    raw = _value(values, "MODEL_CONTEXT_WINDOW_TOKENS", "auto")
    if raw.lower() == "auto":
        resolved = _MODEL_CONTEXT_WINDOWS.get(model)
        if resolved is None:
            raise ConfigurationError(
                "MODEL_CONTEXT_WINDOW_TOKENS must be set for an unknown LLM_MODEL"
            )
        return resolved
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(
            "MODEL_CONTEXT_WINDOW_TOKENS must be 'auto' or an integer"
        ) from exc
    if value < 1:
        raise ConfigurationError("MODEL_CONTEXT_WINDOW_TOKENS must be at least 1")
    return value


def _ratio(values: Mapping[str, str | None], name: str, default: float) -> float:
    raw = _value(values, name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if not 0 < value < 1:
        raise ConfigurationError(f"{name} must be between 0 and 1")
    return value
