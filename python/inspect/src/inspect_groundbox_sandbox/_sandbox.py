"""The Groundbox sandbox environment for Inspect AI: `sandbox="groundbox"`."""

from __future__ import annotations

import base64
import errno
import os
import posixpath
import re
import shlex
import sys
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from logging import getLogger
from typing import Any, Literal, overload
from urllib.parse import urlparse

import anyio
from groundbox import GroundboxError
from inspect_ai.util import (
    ExecResult,
    OutputLimitExceededError,
    SandboxConnection,
    SandboxEnvironment,
    SandboxEnvironmentConfigType,
    SandboxEnvironmentLimits,
    SandboxUnavailableError,
    sandboxenv,
    trace_message,
    warn_once,
)
from inspect_ai.util._sandbox.environment import HostMapping, PortMapping
from pydantic import BaseModel
from typing_extensions import override

from . import _client
from ._client import call, retry
from ._config import GroundboxSandboxEnvironmentConfig, SandboxSpec, resolve_spec
from ._shell import (
    JOIN_PARTS_SCRIPT,
    PINNED_PATH,
    PROBE_SCRIPT,
    READ_SCRIPT,
    SHELL,
    STDIN_FROM_FILE_SCRIPT,
    WRITE_SCRIPT,
    Identity,
    marker,
    strip_markers,
    wrap_for_user,
)

logger = getLogger(__name__)

PROVIDER = "groundbox"
INSPECT_METADATA = {"created_by": "inspect-ai"}

# POST /exec waits for a command for up to 60 minutes.
MAX_EXEC_SECONDS = 3600
# The exec request body is small (under 1 MB): larger stdin goes up as a file first.
INLINE_STDIN_BYTES = 256 * 1024
INLINE_COMMAND_BYTES = 128 * 1024
# Exec output is capped at a few MB, so larger files are read with the files API.
INLINE_READ_BYTES = 1024 * 1024
# File uploads are capped near 32 MB, so larger ones go up in parts.
UPLOAD_CHUNK_BYTES = 16 * 1024 * 1024
# How long a sample waits for a sandbox: a queued flex sandbox, a team at its limit, a first image build.
START_TIMEOUT_SECONDS = float(os.environ.get("INSPECT_GROUNDBOX_START_TIMEOUT", "1800"))

_SPAWN_FAILED = re.compile(r"^Could not start the command: spawn (?P<path>.+) (?P<code>E[A-Z]+)$")


@dataclass
class _RunState:
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    sandbox_ids: list[str] = field(default_factory=list)


_run_state: ContextVar[_RunState] = ContextVar("groundbox_run_state")
_templates: dict[str, anyio.Lock] = {}
_templates_ready: set[str] = set()


def _state() -> _RunState:
    try:
        return _run_state.get()
    except LookupError:
        state = _RunState()
        _run_state.set(state)
        return state


@sandboxenv(name=PROVIDER)
class GroundboxSandboxEnvironment(SandboxEnvironment):
    """A Groundbox sandbox (a Firecracker microVM) per sample."""

    def __init__(self, sandbox_id: str, spec: SandboxSpec | None = None) -> None:
        super().__init__()
        self.sandbox_id = sandbox_id
        self._spec = spec or SandboxSpec()
        self._identity: Identity | None = None
        self._identity_lock = anyio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @classmethod
    def config_files(cls) -> list[str]:
        return ["compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml", "Dockerfile"]

    @classmethod
    def is_docker_compatible(cls) -> bool:
        return True

    @classmethod
    def config_deserialize(cls, config: dict[str, Any]) -> BaseModel:
        return GroundboxSandboxEnvironmentConfig(**config)

    @override
    @classmethod
    async def task_init(cls, task_name: str, config: SandboxEnvironmentConfigType | None) -> None:
        _client.get_client()  # fails early without GROUNDBOX_API_KEY
        _run_state.set(_RunState())
        spec = resolve_spec(config)
        if spec.dockerfile is not None:
            await _ensure_template(spec)

    @override
    @classmethod
    async def sample_init(
        cls, task_name: str, config: SandboxEnvironmentConfigType | None, metadata: dict[str, str]
    ) -> dict[str, SandboxEnvironment]:
        spec = resolve_spec(config)
        create = dict(spec.create)
        if spec.dockerfile is not None:
            create["template"] = await _ensure_template(spec)
        state = _state()
        create["metadata"] = {
            **(create.get("metadata") or {}),
            **INSPECT_METADATA,
            "inspect_run_id": state.run_id,
            "task": task_name[:120],
            "name": _sandbox_name(task_name, metadata),
        }
        sandbox_id = await _create_sandbox(create, state)
        env = cls(sandbox_id, spec)
        try:
            if spec.working_dir:
                await env._make_working_dir(spec.working_dir)
        except BaseException:
            await _delete_sandbox(sandbox_id)
            raise
        trace_message(logger, PROVIDER, f"Created sandbox {sandbox_id} for task '{task_name}'")
        return {"default": env}

    @override
    @classmethod
    async def sample_cleanup(
        cls,
        task_name: str,
        config: SandboxEnvironmentConfigType | None,
        environments: dict[str, SandboxEnvironment],
        interrupted: bool,
    ) -> None:
        # An interrupted run is cleaned up in task_cleanup, or kept with --no-sandbox-cleanup.
        if interrupted:
            return
        running = _state().sandbox_ids
        for env in environments.values():
            sandbox_id = env.as_type(GroundboxSandboxEnvironment).sandbox_id
            try:
                await _delete_sandbox(sandbox_id)
                if sandbox_id in running:
                    running.remove(sandbox_id)
            except Exception as error:
                trace_message(
                    logger, PROVIDER, f"Could not delete sandbox {sandbox_id}: {error}. task_cleanup tries again."
                )

    @override
    @classmethod
    async def task_cleanup(cls, task_name: str, config: SandboxEnvironmentConfigType | None, cleanup: bool) -> None:
        state = _state()
        if not cleanup:
            if state.sandbox_ids:
                print(
                    f"Groundbox sandboxes left running: {', '.join(state.sandbox_ids)}\n"
                    "Delete them with: inspect sandbox cleanup groundbox <sandbox_id>"
                )
            return
        failed: list[str] = []
        done: set[str] = set()
        for sandbox_id in list(state.sandbox_ids):
            try:
                await _delete_sandbox(sandbox_id)
                done.add(sandbox_id)
            except Exception as error:
                failed.append(sandbox_id)
                logger.error(f"Could not delete Groundbox sandbox {sandbox_id}: {error}")
        # Second pass: anything this run made that was never tracked (a create
        # whose answer was lost, for one), found by the run's metadata.
        try:
            listed = await retry(
                _client.get_client().list_sandboxes, live=True, metadata={"inspect_run_id": state.run_id}
            )
            for sandbox in listed.get("data", []):
                if sandbox["id"] in done:
                    continue
                try:
                    await _delete_sandbox(sandbox["id"])
                except Exception as error:
                    failed.append(sandbox["id"])
                    logger.error(f"Could not delete Groundbox sandbox {sandbox['id']}: {error}")
        except Exception as error:
            logger.warning(f"Could not list this run's Groundbox sandboxes: {error}")
        if failed:
            logger.warning(f"Could not delete {len(failed)} Groundbox sandbox(es): {', '.join(failed)}")
        state.sandbox_ids.clear()

    @override
    @classmethod
    async def cli_cleanup(cls, id: str | None) -> None:
        client = _client.get_client()
        if id is not None:
            await _delete_sandbox(id)
            print(f"Deleted Groundbox sandbox {id}.")
            return
        listed = await retry(client.list_sandboxes, live=True, metadata=INSPECT_METADATA)
        ids = [s["id"] for s in listed.get("data", [])]
        if not ids:
            print("No Groundbox sandboxes from Inspect to clean up.")
            return
        print("Groundbox sandboxes made by Inspect:\n  " + "\n  ".join(ids))
        interactive = sys.stdin.isatty() and "CI" not in os.environ and "PYTEST_CURRENT_TEST" not in os.environ
        if interactive:
            answer = input(f"Delete all {len(ids)}? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("Cancelled.")
                return
        failures = 0
        for sandbox_id in ids:
            try:
                await _delete_sandbox(sandbox_id)
            except Exception as error:
                failures += 1
                print(f"Could not delete {sandbox_id}: {error}")
        print(f"Deleted {len(ids) - failures} sandbox(es).")
        if failures:
            sys.exit(1)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @override
    async def exec(
        self,
        cmd: list[str],
        input: str | bytes | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        user: str | None = None,
        timeout: int | None = None,
        timeout_retry: bool = True,
        concurrency: bool = True,
    ) -> ExecResult[str]:
        if not cmd:
            raise ValueError("exec() needs a command")
        run_as = user or self._spec.user
        identity = await self._get_identity() if (run_as is not None or (cwd and not posixpath.isabs(cwd))) else None
        if cwd is None:
            cwd = self._spec.working_dir
        elif not posixpath.isabs(cwd):
            cwd = posixpath.join(await self._base_dir(), cwd)

        uploads: list[str] = []
        stdin: str | bytes | None = None
        command = list(cmd)
        try:
            # The exec request body is small: a long command line goes up as a script.
            if sum(len(arg.encode("utf-8")) for arg in command) > INLINE_COMMAND_BYTES:
                script = await self._upload(f"exec {shlex.join(command)}\n".encode())
                uploads.append(script)
                command = [SHELL, script]
            if input is not None:
                data = input.encode("utf-8") if isinstance(input, str) else bytes(input)
                if len(data) <= INLINE_STDIN_BYTES:
                    stdin = input if isinstance(input, str) else data
                else:
                    stdin_file = await self._upload(data)
                    uploads.append(stdin_file)
                    command = [SHELL, "-c", STDIN_FROM_FILE_SCRIPT, "inspect-stdin", stdin_file, *command]

            wrapped = command
            if identity is not None:
                wrapped = wrap_for_user(command, run_as, env, identity)
            result = await self._run(
                wrapped, cwd=cwd, env=env, stdin=stdin, timeout=timeout, retry_transient=timeout_retry
            )
        finally:
            for upload in uploads:
                await self._remove_upload(upload)

        stdout, stderr = result.stdout_text, result.stderr_text
        if result.timed_out:
            error = TimeoutError(f"Command timed out after {timeout or MAX_EXEC_SECONDS} seconds")
            if stdout or stderr:
                error.truncated_output = stdout + stderr
            raise error

        spawn = _SPAWN_FAILED.match(stderr.strip()) if result.returncode == 127 else None
        if spawn is not None:
            if spawn["code"] == "EACCES":
                raise PermissionError(errno.EACCES, "Permission denied", spawn["path"])
            if wrapped is not command and spawn["path"] == wrapped[0]:
                raise SandboxUnavailableError(f"The sandbox has no {wrapped[0]} to run as {run_as}")
        if (
            wrapped is not command
            and result.returncode == 126
            and not stdout
            and re.fullmatch(r"(/usr/bin/)?env: .*Permission denied", stderr.strip())
        ):
            raise PermissionError(errno.EACCES, "Permission denied", cmd[0])

        limit = SandboxEnvironmentLimits.MAX_EXEC_OUTPUT_SIZE
        for raw in (result.stdout, result.stderr):
            if len(raw) > limit:
                raise OutputLimitExceededError(
                    limit_str=SandboxEnvironmentLimits.MAX_EXEC_OUTPUT_SIZE_STR,
                    truncated_output=raw[-limit:].decode("utf-8", errors="replace"),
                )
        return ExecResult(success=result.returncode == 0, returncode=result.returncode, stdout=stdout, stderr=stderr)

    async def _run(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stdin: str | bytes | None = None,
        timeout: int | None = None,
        retry_transient: bool = True,
    ) -> _Result:
        """One POST /exec, output as bytes (base64 on the wire) decoded leniently."""
        timeout_s = MAX_EXEC_SECONDS
        if timeout is not None:
            if timeout > MAX_EXEC_SECONDS:
                warn_once(logger, f"Groundbox runs a command for at most {MAX_EXEC_SECONDS} seconds; using that.")
            timeout_s = max(1, min(int(timeout), MAX_EXEC_SECONDS))
        client = _client.get_client()
        try:
            answer = await (retry if retry_transient else call)(
                client.run_command,
                self.sandbox_id,
                cmd,
                cwd=cwd,
                env=env or None,
                timeout_s=timeout_s,
                stdin=stdin,
                encoding="base64",
            )
        except GroundboxError as error:
            if error.code == "bad_request" and cwd and "not a folder" in error.message:
                return _Result(returncode=1, stdout=b"", stderr=f"{error.message}\n".encode(), timed_out=False)
            raise _unavailable(error, self.sandbox_id) from error
        return _Result(
            returncode=int(answer.get("exit_code", 1) if answer.get("exit_code") is not None else 1),
            stdout=base64.b64decode(answer.get("stdout") or ""),
            stderr=base64.b64decode(answer.get("stderr") or ""),
            timed_out=bool(answer.get("timed_out")),
        )

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------

    @override
    async def write_file(self, file: str, contents: str | bytes, *, user: str | None = None) -> None:
        file = await self._absolute(file)
        data = contents.encode("utf-8") if isinstance(contents, str) else bytes(contents)
        upload: str | None = None
        try:
            if len(data) <= INLINE_STDIN_BYTES:
                result = await self._run_as(user, [SHELL, "-c", WRITE_SCRIPT, "inspect-write", file, "-"], stdin=data)
            else:
                upload = await self._upload(data)
                result = await self._run_as(user, [SHELL, "-c", WRITE_SCRIPT, "inspect-write", file, upload])
        finally:
            if upload is not None:
                await self._remove_upload(upload)
        if result.returncode != 0:
            raise _file_error(result, file, "write")

    @overload
    async def read_file(self, file: str, text: Literal[True] = True, *, user: str | None = None) -> str: ...

    @overload
    async def read_file(self, file: str, text: Literal[False], *, user: str | None = None) -> bytes: ...

    @override
    async def read_file(self, file: str, text: bool = True, *, user: str | None = None) -> str | bytes:
        file = await self._absolute(file)
        limit = SandboxEnvironmentLimits.MAX_READ_FILE_SIZE
        inline = min(INLINE_READ_BYTES, limit)
        result = await self._run_as(user, [SHELL, "-c", READ_SCRIPT, "inspect-read", file, str(inline)])
        if result.returncode != 0:
            raise _file_error(result, file, "read")
        size_text = marker(result.stderr_text, "SIZE")
        if size_text is None:
            raise RuntimeError(f"Could not read {file}: {result.stderr_text.strip()}")
        size = int(size_text)
        if size > limit:
            raise OutputLimitExceededError(
                limit_str=SandboxEnvironmentLimits.MAX_READ_FILE_SIZE_STR, truncated_output=None
            )
        if size <= inline:
            data = result.stdout
        else:
            try:
                data = await retry(_client.get_client().read_file, self.sandbox_id, file)
            except GroundboxError as error:
                if error.code == "not_found" and error.message.startswith("No such file"):
                    raise FileNotFoundError(errno.ENOENT, "No such file or directory", file) from error
                raise _unavailable(error, self.sandbox_id) from error
            if len(data) > limit:
                raise OutputLimitExceededError(
                    limit_str=SandboxEnvironmentLimits.MAX_READ_FILE_SIZE_STR, truncated_output=None
                )
        if not text:
            return data
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise UnicodeDecodeError(
                error.encoding, error.object, error.start, error.end, f"Failed to decode {file}: {error.reason}"
            ) from error

    @override
    async def connection(self, *, user: str | None = None) -> SandboxConnection:
        ports: list[PortMapping] = []
        for port in self._spec.ports:
            try:
                preview = await retry(_client.get_client().expose_port, self.sandbox_id, port)
            except GroundboxError as error:
                warn_once(logger, f"Could not give port {port} a Groundbox preview: {error}")
                continue
            host = urlparse(preview.get("url") or "").hostname
            if host:
                ports.append(
                    PortMapping(
                        container_port=port, protocol="tcp", mappings=[HostMapping(host_ip=host, host_port=443)]
                    )
                )
        return SandboxConnection(
            type=PROVIDER,
            command=f"groundbox sandbox shell {self.sandbox_id}",
            ports=ports or None,
            container=self.sandbox_id,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _get_identity(self) -> Identity:
        if self._identity is None:
            async with self._identity_lock:
                if self._identity is None:
                    result = await self._run([SHELL, "-c", PROBE_SCRIPT], timeout=60)
                    if result.returncode != 0:
                        raise SandboxUnavailableError(
                            f"Could not read the user of sandbox {self.sandbox_id}: {result.stderr_text.strip()}"
                        )
                    self._identity = Identity.parse(result.stdout_text)
        return self._identity

    async def _base_dir(self) -> str:
        """Where relative paths resolve: the working folder, else the default user's home."""
        if self._spec.working_dir:
            return self._spec.working_dir
        return (await self._get_identity()).home

    async def _absolute(self, file: str) -> str:
        return file if posixpath.isabs(file) else posixpath.join(await self._base_dir(), file)

    async def _run_as(self, user: str | None, cmd: list[str], stdin: bytes | None = None) -> _Result:
        """Run one of our own scripts as `user` (else the default exec user), PATH pinned."""
        run_as = user or self._spec.user
        if run_as is not None:
            cmd = wrap_for_user(cmd, run_as, None, await self._get_identity())
        return await self._run(cmd, stdin=stdin, timeout=MAX_EXEC_SECONDS)

    async def _upload(self, data: bytes) -> str:
        """Put bytes in a fresh folder under /tmp with the files API; returns the file's path."""
        client = _client.get_client()
        folder = f"/tmp/.inspect-groundbox-{uuid.uuid4().hex}"
        try:
            if len(data) <= UPLOAD_CHUNK_BYTES:
                await retry(client.write_file, self.sandbox_id, f"{folder}/data", data)
            else:
                for index, start in enumerate(range(0, len(data), UPLOAD_CHUNK_BYTES)):
                    chunk = data[start : start + UPLOAD_CHUNK_BYTES]
                    await retry(client.write_file, self.sandbox_id, f"{folder}/part-{index:05d}", chunk)
                joined = await self._run([SHELL, "-c", JOIN_PARTS_SCRIPT, "inspect-join", folder], timeout=600)
                if joined.returncode != 0:
                    raise RuntimeError(f"Could not join an upload in sandbox {self.sandbox_id}: {joined.stderr_text}")
        except GroundboxError as error:
            raise _unavailable(error, self.sandbox_id) from error
        return f"{folder}/data"

    async def _remove_upload(self, path: str) -> None:
        try:
            await retry(_client.get_client().delete_file, self.sandbox_id, posixpath.dirname(path), recursive=True)
        except Exception as error:
            trace_message(logger, PROVIDER, f"Could not remove {path} from {self.sandbox_id}: {error}")

    async def _make_working_dir(self, working_dir: str) -> None:
        identity = await self._get_identity()
        owner = self._spec.user
        script = f'PATH={PINNED_PATH}; mkdir -p -- "$1"' + (' && chown -- "$2" "$1"' if owner else "")
        args = [SHELL, "-c", script, "inspect-workdir", working_dir, *([owner] if owner else [])]
        result = await self._run(wrap_for_user(args, "root", None, identity) if identity.uid != 0 else args, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(f"Could not make the working folder {working_dir}: {result.stderr_text.strip()}")


@dataclass
class _Result:
    """One command's answer, with its output as the bytes it printed."""

    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool

    @property
    def stdout_text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")


def _file_error(result: _Result, file: str, operation: str) -> Exception:
    stderr = result.stderr_text
    if result.returncode == 2 and marker(stderr, "ENOENT") is not None:
        return FileNotFoundError(errno.ENOENT, "No such file or directory", file)
    if result.returncode == 21 or "Is a directory" in stderr:
        return IsADirectoryError(errno.EISDIR, "Is a directory", file)
    if result.returncode == 13 or "Permission denied" in stderr or "Operation not permitted" in stderr:
        return PermissionError(errno.EACCES, "Permission denied", file)
    if "No such file or directory" in stderr or "Not a directory" in stderr:
        return FileNotFoundError(errno.ENOENT, "No such file or directory", file)
    detail = strip_markers(stderr).strip()
    return RuntimeError(f"Could not {operation} {file} (exit {result.returncode}): {detail}")


def _unavailable(error: GroundboxError, sandbox_id: str) -> Exception:
    """A sandbox that is not running (or gone) cannot run anything: Inspect's SandboxUnavailableError."""
    if error.code in ("conflict", "not_found") or error.status in (404, 409, 410):
        return SandboxUnavailableError(f"Groundbox sandbox {sandbox_id} cannot run commands: {error}")
    return error


async def _create_sandbox(create: dict[str, Any], state: _RunState) -> str:
    """Create a sandbox and wait until it runs.

    A team at its sandbox limit (429) waits for room. A lost answer is retried
    with the same idempotency key, so it never starts, and bills, two.
    """
    client = _client.get_client()
    deadline = anyio.current_time() + START_TIMEOUT_SECONDS
    key = uuid.uuid4().hex
    delay = 2.0
    while True:
        try:
            sandbox = await call(client.create_sandbox, idempotency_key=key, **create)
            break
        except GroundboxError as error:
            if anyio.current_time() > deadline:
                raise
            if error.status == 429 or error.code in ("concurrency_limit", "rate_limited"):
                key = uuid.uuid4().hex  # refused: the next try is a new request
            elif not _client.is_transient(error):
                raise
            trace_message(logger, PROVIDER, f"Sandbox create waiting: {error}")
            await anyio.sleep(delay)
            delay = min(30.0, delay * 1.5)
    sandbox_id = sandbox["id"]
    state.sandbox_ids.append(sandbox_id)
    if sandbox.get("status") != "running":
        sandbox = await call(client.wait_for_sandbox, sandbox_id, timeout_s=START_TIMEOUT_SECONDS)
    if sandbox.get("status") != "running":
        await _delete_sandbox(sandbox_id)
        raise RuntimeError(
            f"Groundbox sandbox {sandbox_id} did not start ({sandbox.get('status')}): {sandbox.get('error') or ''}".strip()
        )
    return sandbox_id


async def _delete_sandbox(sandbox_id: str) -> None:
    try:
        await retry(_client.get_client().delete_sandbox, sandbox_id)
    except GroundboxError as error:
        if error.status != 404:
            raise
    trace_message(logger, PROVIDER, f"Deleted sandbox {sandbox_id}")


async def _ensure_template(spec: SandboxSpec) -> str:
    """Build the spec's Dockerfile into a template once (per content and size), and return its name."""
    name = spec.template_name()
    assert name is not None and spec.dockerfile is not None
    if name in _templates_ready:
        return name
    lock = _templates.setdefault(name, anyio.Lock())
    async with lock:
        if name not in _templates_ready:
            await retry(_build_template, name, spec.dockerfile, spec.create)  # safe again: it reuses what exists
            _templates_ready.add(name)
    return name


def _build_template(name: str, dockerfile: str, create: dict[str, Any]) -> None:
    """Blocking: reuse a ready template of this name, else build it and follow the log."""
    client = _client.get_client()
    sizes = {k: create[k] for k in ("cpus", "memory_gb", "disk_gb") if create.get(k) is not None}
    try:
        template = client.get_template(name)
    except GroundboxError as error:
        if error.status != 404:
            raise
        try:
            template = client.create_template(name, dockerfile=dockerfile, idempotency_key=uuid.uuid4().hex, **sizes)
        except GroundboxError as create_error:
            if create_error.status != 409:
                raise
            template = client.get_template(name)
    if template.get("status") == "ready":
        return
    build = template.get("latest_build") or {}
    if build.get("status") in ("failed", "canceled"):
        build = client.build_template(name)
    version = build.get("version") or template.get("version")
    if version is None:
        raise RuntimeError(f"Groundbox template {name} has no build to follow: {template}")
    tail: list[str] = []
    logs = client.follow_template_logs(name, int(version))
    while True:
        try:
            line = next(logs)
        except StopIteration as stop:
            outcome = stop.value or {}
            break
        tail = (tail + [str(line.get("text", ""))])[-40:]
    if outcome.get("status") != "ready":
        raise RuntimeError(
            f"Building the Dockerfile into Groundbox template {name} failed ({outcome.get('status')}). "
            "The end of its log:\n" + "\n".join(tail)
        )


def _sandbox_name(task_name: str, metadata: dict[str, Any]) -> str:
    def slug(value: Any) -> str:
        return re.sub(r"[^a-z0-9_-]+", "-", str(value).lower()).strip("-_")[:40]

    parts = ["inspect", slug(task_name)]
    if metadata.get("__sample_id__") is not None:
        parts.append(slug(metadata["__sample_id__"]))
    parts.append(uuid.uuid4().hex[:8])
    return "-".join(p for p in parts if p)
