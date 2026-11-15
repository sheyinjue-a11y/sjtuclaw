from __future__ import annotations

from pathlib import Path

import pytest

from sjtuclaw.config import Settings
from sjtuclaw.errors import ConfigurationError


def test_settings_use_confirmed_defaults_and_hide_secret() -> None:
    settings = Settings.from_mapping({"LLM_API_KEY": "secret-value"})

    assert settings.base_url == "https://api.deepseek.com"
    assert settings.model == "deepseek-v4-flash"
    assert settings.data_dir == Path(".sjtuclaw")
    assert settings.tokenizer_path == Path("vendor/deepseek-v4-flash-tokenizer")
    assert settings.tool_root == Path(".")
    assert settings.tool_max_file_bytes == 3_145_728
    assert settings.agent_max_steps == 20
    assert settings.context_budget_tokens == 1_048_576
    assert settings.compaction_trigger_tokens == 65_536
    assert settings.compaction_target_tokens == 32_768
    assert settings.memory_retrieval_limit == 8
    assert settings.memory_context_max_tokens == 2_048
    assert settings.model_profile.display_name == "DeepSeek V4 Flash"
    assert "secret-value" not in repr(settings)
    assert "api_key" not in repr(settings)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({}, "LLM_API_KEY is required"),
        ({"LLM_API_KEY": "key", "LLM_TOOL_MODE": "auto"}, "LLM_TOOL_MODE"),
        ({"LLM_API_KEY": "key", "TOKEN_COUNTER_MODE": "auto"}, "TOKEN_COUNTER_MODE"),
        ({"LLM_API_KEY": "key", "AGENT_MAX_STEPS": "-1"}, "AGENT_MAX_STEPS"),
        ({"LLM_API_KEY": "key", "TOOL_MAX_FILE_BYTES": "0"}, "TOOL_MAX_FILE_BYTES"),
        ({"LLM_API_KEY": "key", "MEMORY_RETRIEVAL_LIMIT": "0"}, "MEMORY_RETRIEVAL_LIMIT"),
        (
            {"LLM_API_KEY": "key", "MEMORY_CONTEXT_MAX_TOKENS": "0"},
            "MEMORY_CONTEXT_MAX_TOKENS",
        ),
        (
            {
                "LLM_API_KEY": "key",
                "COMPACTION_TRIGGER_RATIO": "0.5",
                "COMPACTION_TARGET_RATIO": "0.5",
            },
            "COMPACTION_TARGET_RATIO",
        ),
    ],
)
def test_settings_reject_invalid_values(values: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        Settings.from_mapping(values)


def test_settings_allow_explicit_approximate_mode_and_tokenizer_path() -> None:
    settings = Settings.from_mapping(
        {
            "LLM_API_KEY": "key",
            "TOKEN_COUNTER_MODE": "approximate",
            "DEEPSEEK_TOKENIZER_PATH": "D:/tokenizers/deepseek-v4",
        }
    )

    assert settings.token_counter_mode == "approximate"
    assert settings.tokenizer_path == Path("D:/tokenizers/deepseek-v4")


def test_unknown_model_requires_explicit_context_window() -> None:
    with pytest.raises(ConfigurationError, match="MODEL_CONTEXT_WINDOW_TOKENS"):
        Settings.from_mapping({"LLM_API_KEY": "key", "LLM_MODEL": "custom-model"})

    settings = Settings.from_mapping(
        {
            "LLM_API_KEY": "key",
            "LLM_MODEL": "custom-model",
            "MODEL_CONTEXT_WINDOW_TOKENS": "200000",
        }
    )

    assert settings.model_profile.context_window_tokens == 200_000
    assert settings.model_profile.display_name == "custom-model"
