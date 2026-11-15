"""Volatile, non-interactive per-run shell manager.

The manager constrains the recorded working directory to the Workspace.  It is
not an operating-system sandbox: commands may still use absolute paths, invoke
other programs, or access the network.
"""

from __future__ import annotations

import asyncio
import locale
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from sjtuclaw.errors import ToolExecutionError
from sjtuclaw.infrastructure.tools.advanced import ShellCommand, ShellStart
from sjtuclaw.models.tool import ToolExecutionContext
from sjtuclaw.ports.runtime import IdGenerator

_MAX_OUTPUT_BYTES = 1_048_576


@dataclass(slots=True)
class _Shell:
    process: asyncio.subprocess.Process
    session_id: str
    request_id: str
    workspace_root: Path
    workspace_revision: int
    cwd: Path


class PersistentShellManager:
    """Own at most one non-interactive shell per Session in this process."""

    def __init__(self, *, ids: IdGenerator) -> None:
        self._ids = ids
        self._shells: dict[str, _Shell] = {}
        self._lock = asyncio.Lock()

    async def new_shell(
        self,
        *,
        context: ToolExecutionContext,
        cwd: Path,
    ) -> ShellStart:
        root = _workspace_root(context)
        resolved_cwd = _within_workspace(root, cwd)
        async with self._lock:
            old = self._shells.pop(context.session_id, None)
            replaced = old is not None
            if old is not None:
                await _terminate(old.process)
            try:
                process = await _start_shell(resolved_cwd)
            except OSError as error:
                raise ToolExecutionError("SHELL_START_FAILED") from error
            self._shells[context.session_id] = _Shell(
                process=process,
                session_id=context.session_id,
                request_id=context.request_id,
                workspace_root=root,
                workspace_revision=context.workspace_revision or 0,
                cwd=resolved_cwd,
            )
        return ShellStart(
            cwd=str(resolved_cwd),
            workspace_revision=context.workspace_revision or 0,
            replaced_existing=replaced,
        )

    async def run_command(
        self,
        *,
        context: ToolExecutionContext,
        command: str,
        timeout_seconds: int,
    ) -> ShellCommand:
        if not 1 <= timeout_seconds <= 900:
            raise ToolExecutionError("COMMAND_TIMEOUT_INVALID")
        root = _workspace_root(context)
        async with self._lock:
            shell = self._shells.get(context.session_id)
            if (
                shell is None
                or shell.process.returncode is not None
                or shell.request_id != context.request_id
                or shell.workspace_revision != context.workspace_revision
                or shell.workspace_root != root
            ):
                raise ToolExecutionError("SHELL_NOT_STARTED")
            try:
                cwd_before = _within_workspace(root, shell.cwd)
            except ToolExecutionError:
                await self._drop_shell(context.session_id, shell)
                raise
            marker = self._ids.new_id("shell_marker")
            stdout_start = f"__SJTUCLAW_OUT_START_{marker}__"
            stdout_end = f"__SJTUCLAW_OUT_END_{marker}__"
            stderr_start = f"__SJTUCLAW_ERR_START_{marker}__"
            stderr_end = f"__SJTUCLAW_ERR_END_{marker}__"
            script = _command_script(
                command,
                stdout_start=stdout_start,
                stdout_end=stdout_end,
                stderr_start=stderr_start,
                stderr_end=stderr_end,
            )
            assert shell.process.stdin is not None
            assert shell.process.stdout is not None
            assert shell.process.stderr is not None
            try:
                shell.process.stdin.write(script.encode(_console_encoding(), errors="replace"))
                await shell.process.stdin.drain()
                stdout_task = asyncio.create_task(
                    _read_marked_output(shell.process.stdout, stdout_start, stdout_end)
                )
                stderr_task = asyncio.create_task(
                    _read_marked_output(shell.process.stderr, stderr_start, stderr_end)
                )
                stdout, stdout_marker, stdout_truncated = await asyncio.wait_for(
                    stdout_task,
                    timeout=timeout_seconds,
                )
                stderr, _, stderr_truncated = await asyncio.wait_for(
                    stderr_task,
                    timeout=1,
                )
            except TimeoutError:
                for task in (locals().get("stdout_task"), locals().get("stderr_task")):
                    if isinstance(task, asyncio.Task):
                        task.cancel()
                await self._drop_shell(context.session_id, shell)
                return ShellCommand(
                    command=command,
                    cwd_before=str(cwd_before),
                    cwd_after=None,
                    exit_code=None,
                    stdout="",
                    stderr="",
                    timed_out=True,
                    truncated=False,
                )
            except (OSError, UnicodeError) as error:
                await self._drop_shell(context.session_id, shell)
                raise ToolExecutionError("SHELL_PROTOCOL_ERROR") from error
            finally:
                for task in (locals().get("stdout_task"), locals().get("stderr_task")):
                    if isinstance(task, asyncio.Task) and not task.done():
                        task.cancel()

            exit_code, cwd_after = _parse_stdout_marker(stdout_marker)
            try:
                resolved_after = _within_workspace(root, Path(cwd_after))
            except ToolExecutionError:
                await self._drop_shell(context.session_id, shell)
                raise ToolExecutionError("SHELL_LEFT_WORKSPACE") from None
            shell.cwd = resolved_after
            return ShellCommand(
                command=command,
                cwd_before=str(cwd_before),
                cwd_after=str(resolved_after),
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                timed_out=False,
                truncated=stdout_truncated or stderr_truncated,
            )

    async def close_for_context(self, context: ToolExecutionContext) -> None:
        async with self._lock:
            shell = self._shells.get(context.session_id)
            if shell is not None and shell.request_id == context.request_id:
                await self._drop_shell(context.session_id, shell)

    async def aclose(self) -> None:
        async with self._lock:
            shells = tuple(self._shells.items())
            self._shells.clear()
            for _, shell in shells:
                await _terminate(shell.process)

    async def _drop_shell(self, session_id: str, shell: _Shell) -> None:
        if self._shells.get(session_id) is shell:
            self._shells.pop(session_id, None)
        await _terminate(shell.process)


def _workspace_root(context: ToolExecutionContext) -> Path:
    if (
        context.workspace_binding_id is None
        or context.workspace_root is None
        or context.workspace_revision is None
    ):
        raise ToolExecutionError("WORKSPACE_NOT_CONFIGURED")
    try:
        return context.workspace_root.resolve(strict=True)
    except OSError as error:
        raise ToolExecutionError("WORKSPACE_UNAVAILABLE") from error


def _within_workspace(root: Path, candidate: Path) -> Path:
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ToolExecutionError("WORKSPACE_UNAVAILABLE") from error
    if not resolved.is_relative_to(root):
        raise ToolExecutionError("SHELL_LEFT_WORKSPACE")
    return resolved


async def _start_shell(cwd: Path) -> asyncio.subprocess.Process:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name.casefold() in {"path", "systemroot", "comspec", "temp", "tmp", "home", "userprofile"}
    }
    if sys.platform == "win32":
        # cmd.exe prints the prompt before every command it reads.  A one-space
        # prompt keeps the marker protocol parseable while avoiding a path
        # prefix on every echoed line.
        environment["PROMPT"] = "$S"
        executable = environment.get("COMSPEC", "cmd.exe")
        return await asyncio.create_subprocess_exec(
            executable,
            "/Q",
            "/D",
            "/K",
            cwd=str(cwd),
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0),
        )
    return await asyncio.create_subprocess_exec(
        "/bin/sh",
        cwd=str(cwd),
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )


def _command_script(
    command: str,
    *,
    stdout_start: str,
    stdout_end: str,
    stderr_start: str,
    stderr_end: str,
) -> str:
    if sys.platform == "win32":
        return (
            f"echo {stdout_start}\r\n"
            f"echo {stderr_start} 1>&2\r\n"
            f"{command}\r\n"
            "set \"SJTUCLAW_EXIT=%ERRORLEVEL%\"\r\n"
            "for /f \"delims=\" %A in ('cd') do set \"SJTUCLAW_CWD=%A\"\r\n"
            f"echo {stdout_end}^|%SJTUCLAW_EXIT%^|%SJTUCLAW_CWD%\r\n"
            f"echo {stderr_end} 1>&2\r\n"
        )
    return (
        f"printf '%s\\n' '{stdout_start}'\n"
        f"printf '%s\\n' '{stderr_start}' >&2\n"
        f"{{ {command}; }}\n"
        "sjtuclaw_exit=$?\n"
        "sjtuclaw_cwd=$(pwd)\n"
        f"printf '%s|%s|%s\\n' '{stdout_end}' \"$sjtuclaw_exit\" \"$sjtuclaw_cwd\"\n"
        f"printf '%s\\n' '{stderr_end}' >&2\n"
    )


def _console_encoding() -> str:
    """Return the code page cmd.exe uses for piped input and built-in output.

    cmd.exe reads piped stdin and echoes `cd` in the system OEM code page
    (GBK on zh-CN systems), not UTF-8. Encoding the protocol script and
    decoding its marker lines with the same code page keeps non-ASCII
    workspace paths intact instead of replacing them with U+FFFD.
    """

    try:
        encoding = locale.getpreferredencoding(False)
    except Exception:
        encoding = "utf-8"
    return encoding or "utf-8"


async def _read_marked_output(
    stream: asyncio.StreamReader,
    start: str,
    end: str,
) -> tuple[str, str, bool]:
    started = False
    chunks: list[bytes] = []
    size = 0
    truncated = False
    while True:
        line = await stream.readline()
        if not line:
            raise ToolExecutionError("SHELL_PROTOCOL_ERROR")
        decoded = line.decode(_console_encoding(), errors="replace")
        if not started:
            # cmd.exe prefixes every echoed line with the prompt (a single
            # space when PROMPT=$S), and `echo marker 1>&2` keeps a trailing
            # space on stderr.  Tolerate both instead of requiring an exact
            # match so the protocol also works on Windows.
            if decoded.strip().endswith(start):
                started = True
            continue
        if end in decoded:
            return (
                b"".join(chunks).decode(_console_encoding(), errors="replace"),
                decoded.strip(),
                truncated,
            )
        if size < _MAX_OUTPUT_BYTES:
            accepted = line[: _MAX_OUTPUT_BYTES - size]
            chunks.append(accepted)
            size += len(accepted)
            truncated = truncated or len(accepted) != len(line)
        else:
            truncated = True


def _parse_stdout_marker(marker: str) -> tuple[int, str]:
    try:
        _, exit_code_raw, cwd = marker.split("|", maxsplit=2)
        return int(exit_code_raw), cwd
    except (TypeError, ValueError) as error:
        raise ToolExecutionError("SHELL_PROTOCOL_ERROR") from error


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except (TimeoutError, ProcessLookupError):
        if process.returncode is None:
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                if sys.platform == "win32":
                    _terminate_process_tree(process.pid)


def _terminate_process_tree(pid: int) -> None:
    """Kill a Windows process and its descendants.

    The shell may have spawned children (for example `powershell.exe`) that
    inherit its pipes and survive the parent's termination, keeping the
    process alive long after the owning shell was dropped.
    """

    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        pass
