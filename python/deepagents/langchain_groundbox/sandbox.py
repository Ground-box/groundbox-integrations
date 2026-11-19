"""Groundbox sandbox backend for Deep Agents."""

from __future__ import annotations

import re
import shlex
import time
from collections.abc import Callable
from typing import TypeVar

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox
from groundbox import Groundbox, GroundboxError

DEFAULT_WORKDIR = "/home/runner"
"""Home of the `runner` user in Groundbox's own image (`/root` for a Docker `image`)."""

DEFAULT_COMMAND_TIMEOUT = 30 * 60
"""Seconds a command may run when `execute()` gets no timeout."""

MAX_EXEC_TIMEOUT = 60 * 60
"""Longest command `POST /exec` waits for. Longer (or unlimited) commands run in the background."""

TIMEOUT_EXIT_CODE = 124
"""Exit code reported for a command that ran past its timeout (as `timeout(1)` does)."""

T = TypeVar("T")

_POLL_SECONDS = 0.5

_RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0)
"""Waits between tries when the API refuses a call for now."""

_MAX_RETRY_WAIT = 30.0

_TRANSIENT_CODES = frozenset({"no_capacity", "rate_limited"})
"""Refusals where the call never reached the sandbox, so trying again cannot run anything twice:
the sandbox's machine was briefly unreachable, or the key sent too many requests."""

_RETRY_AFTER = re.compile(r"try again in (\d+) seconds?", re.IGNORECASE)

# Prefer bash, as other Deep Agents sandboxes do, but still work on images without it.
_SHELL_WRAPPER = (
    'if command -v bash >/dev/null 2>&1; then exec bash -c "$1"; else exec sh -c "$1"; fi'
)


def _combine_output(stdout: str | None, stderr: str | None) -> str:
    output = stdout or ""
    if stderr:
        output += "\n" + stderr if output else stderr
    return output


def _with_retry(call: Callable[[], T]) -> T:
    """Run an API call, trying again while the API refuses it for now (see `_TRANSIENT_CODES`)."""
    for delay in _RETRY_DELAYS:
        try:
            return call()
        except GroundboxError as exc:
            if exc.code not in _TRANSIENT_CODES:
                raise
            asked = _RETRY_AFTER.search(exc.message or "")
            wait = float(asked.group(1)) if asked else delay
        time.sleep(min(max(wait, delay), _MAX_RETRY_WAIT))
    return call()


def _file_error(error: GroundboxError) -> str:
    """Map a Groundbox file API error onto Deep Agents' standard file error codes."""
    message = (error.message or "").lower()
    if error.status == 404 or error.code == "not_found":
        return "file_not_found"
    if "is a folder" in message or "is a directory" in message:
        return "is_directory"
    if "not a folder" in message or "not a directory" in message:
        return "file_not_found"
    if error.status == 403 or "permission" in message:
        return "permission_denied"
    if "absolute path" in message or error.code == "bad_request":
        return "invalid_path"
    return f"{error.code}: {error.message}"


class GroundboxSandbox(BaseSandbox):
    """A Deep Agents sandbox backend on a running Groundbox sandbox.

    Commands run through the Groundbox exec API (bash when the image has it,
    `sh` otherwise) in `workdir`. Files move through the Groundbox file API.
    The file helpers Deep Agents builds on top of `execute()` (read, edit, ls,
    glob, grep) need `python3` in the sandbox, which Groundbox's own image has.

    Example:
        ```python
        from groundbox import Groundbox
        from langchain_groundbox import GroundboxSandbox

        gb = Groundbox()  # GROUNDBOX_API_KEY
        sandbox = gb.create_sandbox(cpus=2)
        backend = GroundboxSandbox(client=gb, sandbox_id=sandbox["id"])
        print(backend.execute("uname -a").output)
        gb.delete_sandbox(sandbox["id"])
        ```
    """

    def __init__(
        self,
        *,
        client: Groundbox,
        sandbox_id: str,
        workdir: str | None = DEFAULT_WORKDIR,
        timeout: int = DEFAULT_COMMAND_TIMEOUT,
        env: dict[str, str] | None = None,
    ) -> None:
        """Wrap an existing Groundbox sandbox.

        Args:
            client: Groundbox client (`groundbox.Groundbox`).
            sandbox_id: The sandbox's id (`sbx_...`).
            workdir: Folder commands start in. `None` starts them in the
                sandbox user's home folder.
            timeout: Default command timeout in seconds when `execute()` is
                called without one. 0 means no timeout.
            env: Extra environment variables for every command.
        """
        if timeout < 0:
            msg = f"timeout must be non-negative, got {timeout}"
            raise ValueError(msg)
        self._client = client
        self._sandbox_id = sandbox_id
        self._workdir = workdir
        self._default_timeout = timeout
        self._env = dict(env) if env else None

    @property
    def id(self) -> str:
        """The Groundbox sandbox id."""
        return self._sandbox_id

    @property
    def client(self) -> Groundbox:
        """The Groundbox client this backend calls."""
        return self._client

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """Run a shell command in the sandbox.

        Args:
            command: Shell command string.
            timeout: Seconds to wait for it. `None` uses the backend's default;
                0 waits as long as the sandbox lives.

        Returns:
            Combined stdout and stderr, the exit code, and whether output was cut.

        Raises:
            ValueError: If `timeout` is negative.
        """
        effective = self._default_timeout if timeout is None else timeout
        if effective < 0:
            msg = f"timeout must be non-negative, got {effective}"
            raise ValueError(msg)

        args = ["sh", "-c", _SHELL_WRAPPER, "sh", command]
        try:
            if 0 < effective <= MAX_EXEC_TIMEOUT:
                result = _with_retry(
                    lambda: self._client.run_command(
                        self._sandbox_id,
                        args,
                        cwd=self._workdir,
                        env=self._env,
                        timeout_s=effective,
                    )
                )
                output = _combine_output(result.get("stdout"), result.get("stderr"))
                if result.get("timed_out"):
                    return self._timed_out(output, effective)
                return ExecuteResponse(
                    output=output,
                    exit_code=result.get("exit_code"),
                    truncated=False,
                )
            return self._execute_in_background(args, effective)
        except GroundboxError as exc:
            return ExecuteResponse(
                output=f"Error executing command ({exc.code}): {exc.message}",
                exit_code=1,
                truncated=False,
            )

    def _execute_in_background(self, args: list[str], timeout: int) -> ExecuteResponse:
        """Run a command past the 60-minute `/exec` limit: start it, then read its output."""
        started = _with_retry(
            lambda: self._client.start_command(
                self._sandbox_id,
                args,
                cwd=self._workdir,
                env=self._env,
                timeout_s=timeout or None,
            )
        )
        exec_id = started["id"]
        deadline = time.monotonic() + timeout if timeout else None
        stdout: list[str] = []
        stderr: list[str] = []
        seq = 0
        while True:
            part = _with_retry(
                lambda s=seq: self._client.get_command_output(self._sandbox_id, exec_id, seq=s)
            )
            seq = part.get("seq", seq)
            stdout.append(part.get("stdout") or "")
            stderr.append(part.get("stderr") or "")
            if part.get("done"):
                output = _combine_output("".join(stdout), "".join(stderr))
                if part.get("timed_out"):
                    return self._timed_out(output, timeout)
                return ExecuteResponse(
                    output=output, exit_code=part.get("exit_code"), truncated=False
                )
            if deadline is not None and time.monotonic() > deadline + 30:
                # The sandbox ends the command at its timeout; this only guards a lost answer.
                self._client.kill_command(self._sandbox_id, exec_id)
                return self._timed_out(_combine_output("".join(stdout), "".join(stderr)), timeout)
            if not part.get("more"):
                time.sleep(_POLL_SECONDS)

    @staticmethod
    def _timed_out(output: str, timeout: int) -> ExecuteResponse:
        note = f"Command timed out after {timeout} seconds"
        return ExecuteResponse(
            output=f"{output}\n{note}" if output else note,
            exit_code=TIMEOUT_EXIT_CODE,
            truncated=False,
        )

    def _read_file(self, path: str) -> FileDownloadResponse:
        if not path.startswith("/"):
            return FileDownloadResponse(path=path, content=None, error="invalid_path")
        try:
            content = _with_retry(lambda: self._client.read_file(self._sandbox_id, path))
        except GroundboxError as exc:
            return FileDownloadResponse(path=path, content=None, error=_file_error(exc))
        return FileDownloadResponse(path=path, content=bytes(content), error=None)

    def _write_file(self, path: str, content: bytes) -> FileUploadResponse:
        if not path.startswith("/"):
            return FileUploadResponse(path=path, error="invalid_path")
        try:
            _with_retry(lambda: self._client.write_file(self._sandbox_id, path, content))
        except GroundboxError as exc:
            error = _file_error(exc)
            if error.startswith("internal") or exc.status >= 500:
                error = self._diagnose_write(path) or error
            return FileUploadResponse(path=path, error=error)
        return FileUploadResponse(path=path, error=None)

    def _diagnose_write(self, path: str) -> str | None:
        """Name the reason a write failed when the file API only says it could not."""
        quoted = shlex.quote(path)
        probe = (
            f'p={quoted}; if [ -d "$p" ]; then echo is_directory; exit 0; fi; '
            'd=$(dirname "$p"); while [ ! -e "$d" ]; do d=$(dirname "$d"); done; '
            '[ -d "$d" ] || echo file_not_found'
        )
        try:
            result = self._client.run_command(self._sandbox_id, probe, timeout_s=30)
        except GroundboxError:
            return None
        answer = (result.get("stdout") or "").strip()
        return answer or None

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Download files from the sandbox, in the order asked."""
        return [self._read_file(path) for path in paths]

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Upload files into the sandbox, in the order given. Parent folders are made."""
        return [self._write_file(path, content) for path, content in files]

    def __repr__(self) -> str:
        return f"GroundboxSandbox(id={self._sandbox_id!r}, workdir={self._workdir!r})"
