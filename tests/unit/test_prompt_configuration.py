from pathlib import Path

from sjtuclaw.runtime.prompt_configuration import PromptConfigurationService


def test_prompt_configuration_copies_defaults_once_and_persists_soul_updates(
    tmp_path: Path,
) -> None:
    prompts = PromptConfigurationService(
        root=tmp_path / "prompts",
        default_system_prompt="system default",
        default_soul="soul default",
    )

    initial = prompts.snapshot()

    assert initial.system_prompt == "system default"
    assert initial.soul == "soul default"
    assert initial.system_prompt_path == tmp_path / "prompts" / "system_prompt.md"
    assert initial.soul_path.read_text(encoding="utf-8") == "soul default\n"

    updated = prompts.update_soul("new soul")
    reloaded = PromptConfigurationService(
        root=tmp_path / "prompts",
        default_system_prompt="replacement system default",
        default_soul="replacement soul default",
    ).snapshot()

    assert updated.soul == "new soul"
    assert reloaded.system_prompt == "system default"
    assert reloaded.soul == "new soul"
