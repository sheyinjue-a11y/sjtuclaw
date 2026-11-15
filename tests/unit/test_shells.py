"""Windows cmd marker protocol survives prompt prefixes and pipe escaping."""

import asyncio
import sys
from pathlib import Path

import pytest

from sjtuclaw.infrastructure.tools.shells import (
    _command_script,
    _parse_stdout_marker,
    _read_marked_output,
    _start_shell,
    _terminate,
)


async def _consume(
    lines: tuple[str, ...],
    start: str,
    end: str,
) -> tuple[str, str, bool]:
    reader = asyncio.StreamReader()
    reader.feed_data("".join(lines).encode("utf-8"))
    reader.feed_eof()
    return await _read_marked_output(reader, start, end)


def test_command_script_escapes_pipe_in_end_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    script = _command_script(
        "echo hi",
        stdout_start="__OUT__",
        stdout_end="__OUT_END__",
        stderr_start="__ERR__",
        stderr_end="__ERR_END__",
    )
    assert script.startswith("echo __OUT__\r\n")
    assert "^|%SJTUCLAW_EXIT%^|%SJTUCLAW_CWD%" in script
    assert "__OUT_END__|%SJTUCLAW_EXIT%|%SJTUCLAW_CWD%" not in script


def test_read_marked_output_tolerates_cmd_prompt_prefix() -> None:
    out, marker, truncated = asyncio.run(
        _consume(
            (
                "C:\\Users\\sunyo\\Desktop\\test>__S_OUT__\r\n",
                "\r\n",
                "C:\\Users\\sunyo\\Desktop\\test>3074\r\n",
                "\r\n",
                "C:\\Users\\sunyo\\Desktop\\test>__S_OUT_END__|0|C:\\Users\\sunyo\\Desktop\\test\r\n",
            ),
            "__S_OUT__",
            "__S_OUT_END__",
        )
    )
    assert truncated is False
    assert "3074" in out
    assert _parse_stdout_marker(marker) == (0, "C:\\Users\\sunyo\\Desktop\\test")


def test_read_marked_output_tolerates_one_space_prompt_and_stderr_space() -> None:
    out, marker, _ = asyncio.run(
        _consume(
            (
                " __S_OUT__\r\n",
                "\r\n",
                " hello\r\n",
                "\r\n",
                " __S_OUT_END__|0|C:\\tmp\r\n",
            ),
            "__S_OUT__",
            "__S_OUT_END__",
        )
    )
    assert "hello" in out
    assert _parse_stdout_marker(marker) == (0, "C:\\tmp")

    err, _, _ = asyncio.run(
        _consume(
            (
                "__S_ERR__ \r\n",
                "boom\r\n",
                "__S_ERR_END__ \r\n",
            ),
            "__S_ERR__",
            "__S_ERR_END__",
        )
    )
    assert err == "boom\r\n"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows cmd protocol")
def test_real_cmd_shell_round_trip(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = tmp_path
        process = await _start_shell(workspace)
        try:
            script = _command_script(
                "echo hello",
                stdout_start="__RT_OUT__",
                stdout_end="__RT_OUT_END__",
                stderr_start="__RT_ERR__",
                stderr_end="__RT_ERR_END__",
            )
            assert process.stdin is not None
            assert process.stdout is not None
            assert process.stderr is not None
            process.stdin.write(script.encode("utf-8"))
            await process.stdin.drain()
            out_task = asyncio.create_task(
                _read_marked_output(process.stdout, "__RT_OUT__", "__RT_OUT_END__")
            )
            err_task = asyncio.create_task(
                _read_marked_output(process.stderr, "__RT_ERR__", "__RT_ERR_END__")
            )
            out, marker, _ = await asyncio.wait_for(out_task, timeout=10)
            err, _, _ = await asyncio.wait_for(err_task, timeout=10)
            assert "hello" in out
            assert err == ""
            exit_code, cwd = _parse_stdout_marker(marker)
            assert exit_code == 0
            assert cwd == str(workspace)
        finally:
            await _terminate(process)

    asyncio.run(scenario())
