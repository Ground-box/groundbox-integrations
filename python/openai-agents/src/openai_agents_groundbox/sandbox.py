"""Groundbox (https://groundbox.dev) sandbox backend for the OpenAI Agents SDK.

A Groundbox sandbox is a Firecracker microVM with its own cores. This module plugs it into the
Agents SDK's sandbox interface (``BaseSandboxClient`` / ``BaseSandboxSession``), the same way the
in-tree E2B, Modal and Daytona extensions do, so a ``SandboxAgent`` can run on Groundbox by
passing ``GroundboxSandboxClient`` to ``SandboxRunConfig``.

Commands and files go through the ``groundbox`` Python SDK (REST). Interactive PTY sessions
(``exec_command`` with a later ``write_stdin``) use Groundbox's command stream, a WebSocket at
``/sandboxes/{id}/exec/stream``, because the Python SDK does not speak it yet.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import math
import shlex
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeVar
from urllib.parse import urlsplit

from agents.sandbox._mount_security import redact_mount_error_data
from agents.sandbox.errors import (
    ExecTimeoutError,
    ExecTransportError,
    ExposedPortUnavailableError,
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
    WorkspaceReadNotFoundError,
    WorkspaceStartError,
    WorkspaceWriteTypeError,
)
from agents.sandbox.manifest import Manifest
from agents.sandbox.session import SandboxSession, SandboxSessionState
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.session.dependencies import Dependencies
from agents.sandbox.session.manager import Instrumentation
from agents.sandbox.session.pty_output import collect_pty_output
from agents.sandbox.session.pty_types import (
    PTY_PROCESSES_MAX,
    PTY_PROCESSES_WARNING,
    PtyExecUpdate,
    allocate_pty_process_id,
    clamp_pty_yield_time_ms,
    process_id_to_prune_from_meta,
    resolve_pty_write_yield_time_ms,
)
from agents.sandbox.session.runtime_helpers import (
    RESOLVE_WORKSPACE_PATH_HELPER,
    RuntimeHelperScript,
)
from agents.sandbox.session.sandbox_client import BaseSandboxClient, BaseSandboxClientOptions
from agents.sandbox.session.tar_workspace import shell_tar_exclude_args
from agents.sandbox.snapshot import SnapshotBase, SnapshotSpec, resolve_snapshot
from agents.sandbox.types import ExecResult, ExposedPortEndpoint, User
from agents.sandbox.util.tar_utils import UnsafeTarMemberError, validate_tar_bytes
from agents.sandbox.workspace_paths import (
    coerce_posix_path,
    posix_path_as_path,
    posix_path_for_error,
    sandbox_path_str,
)
from groundbox import Groundbox, GroundboxError
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

#: The default image runs commands as the user ``runner`` (with passwordless sudo).
DEFAULT_GROUNDBOX_WORKSPACE_ROOT = "/home/runner/workspace"

# POST /exec waits for a command for up to 60 minutes; longer commands run in the background.
_MAX_SYNC_EXEC_S = 3600
_SNAPSHOT_REF_KIND = "groundbox-snapshot"
_LIVE_STATUSES = ("queued", "starting", "resuming")

WorkspacePersistence = Literal["tar", "snapshot"]
OnTimeout = Literal["delete", "stop", "pause"]

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def _retryable(error: BaseException) -> bool | None:
    """Whether a Groundbox failure is worth retrying: network, 429 and 5xx are; other 4xx are not."""
    if isinstance(error, GroundboxError):
        if error.status == 0 or error.status == 429 or error.status >= 500:
            return True
        if 400 <= error.status < 500:
            return False
    if isinstance(error, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True
    return None


def _error_context(error: BaseException) -> dict[str, object]:
    context: dict[str, object] = {"backend": "groundbox"}
    if isinstance(error, GroundboxError):
        context["provider_error_code"] = error.code
        context["http_status"] = error.status
        if error.request_id:
            context["request_id"] = error.request_id
    context["provider_error"] = f"{type(error).__name__}: {error}"
    return context


def _exec_transport_error(command: tuple[str | Path, ...], cause: BaseException) -> ExecTransportError:
    return ExecTransportError(
        command=command,
        context=_error_context(cause),
        cause=cause,
        message=f"Groundbox exec failed: {cause}",
        retryable=_retryable(cause),
    )


def _is_not_found(error: BaseException) -> bool:
    return isinstance(error, GroundboxError) and (error.code == "not_found" or error.status == 404)


# ---------------------------------------------------------------------------
# Options and state
# ---------------------------------------------------------------------------


class GroundboxSandboxTimeouts(BaseModel):
    """Timeouts, in seconds, for operations on a Groundbox sandbox."""

    model_config = {"frozen": True}

    #: The limit for a command run with no timeout of its own. Up to an hour a command runs
    #: with one POST /exec call; past that it runs in the background and is polled.
    exec_timeout_unbounded_s: int = Field(default=60 * 60, ge=1)
    #: How long to wait for a new sandbox to be running (the first sandbox from a Docker
    #: image waits while the image is turned into a disk).
    create_s: int = Field(default=30 * 60, ge=1)
    #: Quick calls: status checks, stdin writes, opening a command stream.
    fast_op_s: int = Field(default=30, ge=1)
    #: Cleanup calls: killing a PTY command, removing temporary archives.
    cleanup_s: int = Field(default=30, ge=1)
    #: Archiving or unpacking the workspace with tar.
    workspace_tar_s: int = Field(default=600, ge=1)
    #: A terminal (tty) command with nobody attached for this long is ended by Groundbox, so a
    #: crashed agent process does not leave shells running.
    pty_hangup_s: int = Field(default=600, ge=1)
    #: Polling interval for commands longer than an hour, which run in the background.
    poll_s: float = Field(default=1.0, gt=0)


class GroundboxSandboxClientOptions(BaseSandboxClientOptions):
    """How to start a Groundbox sandbox. Every field maps to ``POST /v1/sandboxes``.

    Leave a field ``None`` for Groundbox's default: 2 vCPUs, 2 GB of memory per vCPU, a 20 GB
    disk, a 60 minute timeout that deletes the sandbox, and Groundbox's own Ubuntu image
    (user ``runner``, with sudo, Python, Node.js, git and the usual build tools).
    """

    type: Literal["groundbox"] = "groundbox"
    #: vCPUs, 1 to 16.
    cpus: int | None = None
    #: Memory in GB, up to 4 per vCPU.
    memory_gb: int | None = None
    #: The least disk size in GB.
    disk_gb: int | None = None
    #: Minutes until the sandbox times out (up to 43,200). See ``on_timeout``.
    timeout_minutes: int | None = None
    #: What the timeout does: "delete" (Groundbox's default), "stop" (keep the disk) or
    #: "pause" (keep memory too). "stop" and "pause" need the paid plan.
    on_timeout: OnTimeout | None = None
    #: A public Docker image to start from (commands run as root), or one in your Groundbox
    #: registry. Not with ``template``.
    image: str | None = None
    #: A Groundbox template id, name or name:version. Not with ``image``.
    template: str | None = None
    #: Environment variables for every command in the sandbox.
    env: dict[str, str] | None = None
    #: Key/value labels on the sandbox (``list_sandboxes(metadata=...)`` filters by them).
    metadata: dict[str, str] | None = None
    #: Egress allowlist: the sandbox reaches these hosts and nothing else.
    allow_hosts: list[str] | None = None
    #: Egress deny list: the sandbox reaches everything but these.
    deny_hosts: list[str] | None = None
    #: Team secrets given to every command as variables of the same name.
    secrets: list[str] | None = None
    #: Half price; starts when a machine has spare room, so it may queue for a while.
    flex: bool | None = None
    #: Pause the sandbox after this many idle seconds (30 at least); the next call wakes it.
    idle_timeout_seconds: int | None = None
    #: On session shutdown, pause the sandbox (memory and disk kept, storage billed) instead
    #: of deleting it, so ``client.resume(state)`` can pick it up again.
    pause_on_exit: bool = False
    #: How the workspace is persisted on ``stop()``: "tar" (an archive of the workspace root,
    #: works on every plan) or "snapshot" (a Groundbox kept snapshot of the whole sandbox,
    #: memory and disk; paid plan).
    workspace_persistence: WorkspacePersistence = "tar"
    #: Ports to give an HTTPS address with ``resolve_exposed_port`` (Groundbox previews, paid plan).
    exposed_ports: tuple[int, ...] = ()
    #: Make exposed ports public. Private (the default) ports resolve to a share link that
    #: carries a token valid for 24 hours.
    public_ports: bool = False
    timeouts: GroundboxSandboxTimeouts | dict[str, object] | None = None


class GroundboxSandboxSessionState(SandboxSessionState):
    """Serializable state of a Groundbox-backed session. It holds no API key."""

    type: Literal["groundbox"] = "groundbox"
    sandbox_id: str
    #: The ``POST /sandboxes`` body this session's sandbox was made with, to make a
    #: replacement when the sandbox is gone on resume.
    create_spec: dict[str, Any] = Field(default_factory=dict)
    pause_on_exit: bool = False
    workspace_persistence: WorkspacePersistence = "tar"
    public_ports: bool = False
    timeouts: GroundboxSandboxTimeouts = Field(default_factory=GroundboxSandboxTimeouts)
    #: The kept snapshot the current sandbox was started from, if any.
    restored_from_snapshot_id: str | None = None


def _encode_snapshot_ref(snapshot_id: str) -> bytes:
    return json.dumps({"kind": _SNAPSHOT_REF_KIND, "snapshot_id": snapshot_id}).encode("utf-8")


def _decode_snapshot_ref(raw: bytes) -> str | None:
    if not raw.startswith(b"{"):
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("kind") != _SNAPSHOT_REF_KIND:
        return None
    snapshot_id = payload.get("snapshot_id")
    return snapshot_id if isinstance(snapshot_id, str) and snapshot_id else None


# ---------------------------------------------------------------------------
# PTY sessions over the command stream
# ---------------------------------------------------------------------------


@dataclass
class _GroundboxPtyEntry:
    tty: bool
    exec_id: str | None = None
    ws: Any = None
    seq: int = 0
    output_chunks: deque[bytes] = field(default_factory=deque)
    output_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    output_notify: asyncio.Event = field(default_factory=asyncio.Event)
    output_closed: asyncio.Event = field(default_factory=asyncio.Event)
    last_used: float = field(default_factory=time.monotonic)
    exit_code: int | None = None
    closing: bool = False
    reader_task: asyncio.Task[None] | None = None


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class GroundboxSandboxSession(BaseSandboxSession):
    """A sandbox session backed by one Groundbox sandbox."""

    state: GroundboxSandboxSessionState
    _gb: Groundbox

    def __init__(self, *, state: GroundboxSandboxSessionState, groundbox: Groundbox) -> None:
        self.state = state
        self._gb = groundbox
        self._pty_lock = asyncio.Lock()
        self._pty_sessions: dict[int, _GroundboxPtyEntry] = {}
        self._reserved_pty_process_ids: set[int] = set()

    @classmethod
    def from_state(
        cls, state: GroundboxSandboxSessionState, *, groundbox: Groundbox
    ) -> GroundboxSandboxSession:
        return cls(state=state, groundbox=groundbox)

    @property
    def sandbox_id(self) -> str:
        return self.state.sandbox_id

    async def _call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        # The groundbox SDK is synchronous (standard library HTTP); keep the event loop free.
        return await asyncio.to_thread(fn, *args, **kwargs)

    # -- lifecycle -----------------------------------------------------------

    async def _shutdown_backend(self) -> None:
        try:
            if self.state.pause_on_exit:
                await self._call(self._gb.pause_sandbox, self.state.sandbox_id)
            else:
                await self._call(self._gb.delete_sandbox, self.state.sandbox_id)
        except Exception as error:  # Best effort, like the other backends.
            logger.debug("Groundbox sandbox shutdown failed: %s", error)

    async def running(self) -> bool:
        try:
            sandbox = await asyncio.wait_for(
                self._call(self._gb.get_sandbox, self.state.sandbox_id),
                timeout=self.state.timeouts.fast_op_s,
            )
        except Exception:
            return False
        return sandbox.get("status") == "running"

    async def _prepare_backend_workspace(self) -> None:
        root = sandbox_path_str(self.state.manifest.root)
        # The default image runs as `runner`, which cannot write outside its home (Manifest()'s
        # default root is /workspace): make such a root with sudo and give it to the user.
        script = (
            'mkdir -p -- "$1" 2>/dev/null || '
            '{ sudo -n mkdir -p -- "$1" && sudo -n chown "$(id -u):$(id -g)" -- "$1"; }'
        )
        try:
            result = await self._run_command(
                ["sh", "-c", script, "sh", root], env={}, timeout_s=self.state.timeouts.fast_op_s, cwd=None
            )
            if result.get("exit_code") != 0:
                stderr = base64.b64decode(result.get("stderr") or "").decode("utf-8", errors="replace")
                raise WorkspaceStartError(
                    path=posix_path_for_error(root),
                    context={
                        "backend": "groundbox",
                        "reason": "workspace_root_nonzero_exit",
                        "exit_code": result.get("exit_code"),
                        "output": stderr[-2000:],
                    },
                    message=f"failed to start session: making the workspace root {root} failed: {stderr.strip()}",
                )
        except WorkspaceStartError:
            raise
        except Exception as error:
            raise WorkspaceStartError(
                path=posix_path_for_error(root),
                context={**_error_context(error), "reason": "workspace_root_setup_failed"},
                cause=error,
                message=f"failed to start session: Groundbox workspace root setup failed: {error}",
            ) from error

    def _runtime_helpers(self) -> tuple[RuntimeHelperScript, ...]:
        return (RESOLVE_WORKSPACE_PATH_HELPER,)

    async def _validate_path_access(self, path: Path | str, *, for_write: bool = False) -> Path:
        return await self._validate_remote_path_access(path, for_write=for_write)

    # -- ports -----------------------------------------------------------------

    async def _resolve_exposed_port(self, port: int) -> ExposedPortEndpoint:
        try:
            preview = await self._call(
                self._gb.expose_port,
                self.state.sandbox_id,
                port,
                public=True if self.state.public_ports else None,
            )
        except Exception as error:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={**_error_context(error), "detail": "expose_port_failed"},
                cause=error,
            ) from error

        public = bool(preview.get("public"))
        url = preview.get("url") if public else (preview.get("share_url") or preview.get("url"))
        split = urlsplit(url) if isinstance(url, str) else None
        if split is None or not split.hostname:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={"backend": "groundbox", "detail": "invalid_preview_url"},
            )
        tls = split.scheme == "https"
        return ExposedPortEndpoint(
            host=split.hostname,
            port=split.port or (443 if tls else 80),
            tls=tls,
            query=split.query,
        )

    # -- exec ------------------------------------------------------------------

    async def _resolved_envs(self) -> dict[str, str]:
        # The sandbox's own env (options.env) is set at create time; the manifest's is per call.
        return dict(await self.state.manifest.environment.resolve())

    def _coerce_exec_timeout(self, timeout_s: float | None) -> float:
        if timeout_s is None:
            return float(self.state.timeouts.exec_timeout_unbounded_s)
        if timeout_s <= 0:
            return 0.001
        return float(timeout_s)

    async def _run_command(
        self, argv: list[str], *, env: dict[str, str], timeout_s: int, cwd: str | None
    ) -> dict[str, Any]:
        try:
            return await self._call(
                self._gb.run_command,
                self.state.sandbox_id,
                argv,
                cwd=cwd,
                env=env or None,
                timeout_s=timeout_s,
                encoding="base64",
            )
        except GroundboxError as error:
            # The workspace root does not exist yet (or was removed): run from the home folder.
            if cwd is not None and error.code == "bad_request" and "not a folder" in error.message:
                return await self._run_command(argv, env=env, timeout_s=timeout_s, cwd=None)
            raise

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        argv = [str(part) for part in command]
        env = await self._resolved_envs()
        cwd = sandbox_path_str(self.state.manifest.root)
        limit = self._coerce_exec_timeout(timeout)
        timeout_s = max(1, math.ceil(limit))
        if timeout_s > _MAX_SYNC_EXEC_S:
            return await self._exec_in_background(command, argv, env=env, cwd=cwd, limit=limit, timeout=timeout)
        try:
            result = await self._run_command(argv, env=env, timeout_s=timeout_s, cwd=cwd)
        except (asyncio.TimeoutError, TimeoutError) as error:
            raise ExecTimeoutError(command=command, timeout_s=timeout, cause=error) from error
        except Exception as error:
            raise _exec_transport_error(command, error) from error
        if result.get("timed_out"):
            raise ExecTimeoutError(command=command, timeout_s=timeout)
        return ExecResult(
            stdout=base64.b64decode(result.get("stdout") or ""),
            stderr=base64.b64decode(result.get("stderr") or ""),
            exit_code=int(result.get("exit_code") or 0),
        )

    async def _exec_in_background(
        self,
        command: tuple[str | Path, ...],
        argv: list[str],
        *,
        env: dict[str, str],
        cwd: str,
        limit: float,
        timeout: float | None,
    ) -> ExecResult:
        """A command longer than POST /exec allows: start it in the background and poll."""
        deadline = time.monotonic() + limit
        stdout, stderr = bytearray(), bytearray()
        exec_id: str | None = None
        try:
            try:
                started = await self._call(
                    self._gb.start_command,
                    self.state.sandbox_id,
                    argv,
                    cwd=cwd,
                    env=env or None,
                    timeout_s=max(1, math.ceil(limit)),
                )
            except GroundboxError as error:
                if not (error.code == "bad_request" and "not a folder" in error.message):
                    raise
                started = await self._call(
                    self._gb.start_command,
                    self.state.sandbox_id,
                    argv,
                    env=env or None,
                    timeout_s=max(1, math.ceil(limit)),
                )
            exec_id = started["id"]
            seq = 0
            while True:
                part = await self._call(
                    self._gb.get_command_output, self.state.sandbox_id, exec_id, seq=seq, encoding="base64"
                )
                seq = part.get("seq", seq)
                stdout += base64.b64decode(part.get("stdout") or "")
                stderr += base64.b64decode(part.get("stderr") or "")
                if part.get("done"):
                    if part.get("timed_out"):
                        raise ExecTimeoutError(command=command, timeout_s=timeout)
                    return ExecResult(
                        stdout=bytes(stdout), stderr=bytes(stderr), exit_code=int(part.get("exit_code") or 0)
                    )
                if time.monotonic() >= deadline:
                    raise ExecTimeoutError(command=command, timeout_s=timeout)
                if not part.get("more"):
                    await asyncio.sleep(self.state.timeouts.poll_s)
        except ExecTimeoutError:
            if exec_id is not None:
                try:
                    await self._call(self._gb.kill_command, self.state.sandbox_id, exec_id)
                except Exception:
                    pass
            raise
        except Exception as error:
            raise _exec_transport_error(command, error) from error

    # -- PTY -------------------------------------------------------------------

    def supports_pty(self) -> bool:
        return True

    def _stream_url(self) -> str:
        base = self._gb.base_url
        if base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
        return f"{base}/sandboxes/{self.state.sandbox_id}/exec/stream"

    async def _open_stream(self) -> Any:
        from websockets.asyncio.client import connect

        headers = {"User-Agent": "openai-agents-groundbox"}
        if self._gb.api_key:
            headers["Authorization"] = f"Bearer {self._gb.api_key}"
        return await connect(
            self._stream_url(),
            additional_headers=headers,
            max_size=None,
            open_timeout=self.state.timeouts.fast_op_s,
        )

    async def pty_exec_start(
        self,
        *command: str | Path,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: str | User | None = None,
        tty: bool = False,
        yield_time_s: float | None = None,
        max_output_tokens: int | None = None,
    ) -> PtyExecUpdate:
        argv = self._prepare_exec_command(*command, shell=shell, user=user)
        env = await self._resolved_envs()
        limit = self._coerce_exec_timeout(timeout)
        start: dict[str, Any] = {
            "type": "start",
            "args": argv,
            "cwd": sandbox_path_str(self.state.manifest.root),
            "timeout_s": max(1, math.ceil(limit)),
            # Resumable: the command outlives a dropped connection, and we reattach.
            "resumable": True,
        }
        if env:
            start["env"] = env
        if tty:
            start.update(
                tty=True,
                rows=24,
                cols=80,
                stdin=True,
                hangup_s=self.state.timeouts.pty_hangup_s,
            )

        entry = _GroundboxPtyEntry(tty=tty)
        registered = False
        try:
            entry.ws = await asyncio.wait_for(self._open_stream(), timeout=self.state.timeouts.fast_op_s)
            await entry.ws.send(json.dumps(start))
            entry.exec_id = await asyncio.wait_for(
                self._await_started(entry), timeout=self.state.timeouts.fast_op_s
            )
            entry.reader_task = asyncio.create_task(self._pty_reader(entry))
            async with self._pty_lock:
                process_id = allocate_pty_process_id(self._reserved_pty_process_ids)
                self._reserved_pty_process_ids.add(process_id)
                pruned = self._prune_pty_sessions_if_needed()
                self._pty_sessions[process_id] = entry
                process_count = len(self._pty_sessions)
                registered = True
        except asyncio.TimeoutError as error:
            if not registered:
                await asyncio.shield(self._terminate_pty_entry(entry))
            raise ExecTimeoutError(command=command, timeout_s=timeout, cause=error) from error
        except Exception as error:
            if not registered:
                await asyncio.shield(self._terminate_pty_entry(entry))
            raise _exec_transport_error(command, error) from error
        except BaseException:
            if not registered:
                await asyncio.shield(self._terminate_pty_entry(entry))
            raise

        if pruned is not None:
            await self._terminate_pty_entry(pruned)
        if process_count >= PTY_PROCESSES_WARNING:
            logger.warning("PTY process count reached warning threshold: %s active sessions", process_count)

        yield_time_ms = 10_000 if yield_time_s is None else int(yield_time_s * 1000)
        output, original_token_count, output_closed = await self._collect_pty_output(
            entry=entry,
            yield_time_ms=clamp_pty_yield_time_ms(yield_time_ms),
            max_output_tokens=max_output_tokens,
        )
        return await self._finalize_pty_update(
            process_id=process_id,
            entry=entry,
            output=output,
            original_token_count=original_token_count,
            output_closed=output_closed,
        )

    async def _await_started(self, entry: _GroundboxPtyEntry) -> str:
        while True:
            message = json.loads(await entry.ws.recv())
            kind = message.get("type")
            if kind == "started" and isinstance(message.get("exec_id"), str):
                return str(message["exec_id"])
            if kind == "error":
                raise GroundboxError(
                    str(message.get("code") or "internal"),
                    str(message.get("message") or "the command stream failed"),
                    200,
                    "GET",
                    f"/sandboxes/{self.state.sandbox_id}/exec/stream",
                )
            await self._handle_stream_message(entry, message)

    async def _handle_stream_message(self, entry: _GroundboxPtyEntry, message: dict[str, Any]) -> bool:
        """Apply one stream message to the entry. Returns True when the command has ended."""
        kind = message.get("type")
        if kind in ("stdout", "stderr"):
            if isinstance(message.get("seq"), int):
                entry.seq = message["seq"]
            data = message.get("data")
            if isinstance(data, str) and data:
                async with entry.output_lock:
                    entry.output_chunks.append(data.encode("utf-8", errors="replace"))
                entry.output_notify.set()
        elif kind == "skipped":
            if isinstance(message.get("seq"), int):
                entry.seq = message["seq"]
        elif kind == "exit":
            code = message.get("exit_code")
            entry.exit_code = int(code) if isinstance(code, int) else None
            return True
        elif kind == "error":
            return True
        return False

    async def _pty_reader(self, entry: _GroundboxPtyEntry) -> None:
        reconnects = 0
        try:
            while True:
                try:
                    async for raw in entry.ws:
                        reconnects = 0
                        try:
                            message = json.loads(raw)
                        except ValueError:
                            continue
                        if await self._handle_stream_message(entry, message):
                            return
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logger.debug("Groundbox command stream dropped: %s", error)
                if entry.closing or entry.exec_id is None or reconnects >= 5:
                    return
                # The command is resumable: reattach and read on from the last sequence number.
                await asyncio.sleep(min(0.5 * 2**reconnects, 10.0))
                reconnects += 1
                try:
                    entry.ws = await asyncio.wait_for(self._open_stream(), timeout=self.state.timeouts.fast_op_s)
                    await entry.ws.send(json.dumps({"type": "attach", "exec_id": entry.exec_id, "seq": entry.seq}))
                except Exception as error:
                    logger.debug("Groundbox command stream reattach failed: %s", error)
        finally:
            entry.output_closed.set()
            entry.output_notify.set()

    async def pty_write_stdin(
        self,
        *,
        session_id: int,
        chars: str,
        yield_time_s: float | None = None,
        max_output_tokens: int | None = None,
    ) -> PtyExecUpdate:
        async with self._pty_lock:
            entry = self._resolve_pty_session_entry(pty_processes=self._pty_sessions, session_id=session_id)

        if chars:
            if not entry.tty:
                raise RuntimeError("stdin is not available for this process")
            await asyncio.wait_for(
                entry.ws.send(json.dumps({"type": "stdin", "data": chars})),
                timeout=self.state.timeouts.fast_op_s,
            )
            await asyncio.sleep(0.1)

        yield_time_ms = 250 if yield_time_s is None else int(yield_time_s * 1000)
        output, original_token_count, output_closed = await self._collect_pty_output(
            entry=entry,
            yield_time_ms=resolve_pty_write_yield_time_ms(yield_time_ms=yield_time_ms, input_empty=chars == ""),
            max_output_tokens=max_output_tokens,
        )
        entry.last_used = time.monotonic()
        return await self._finalize_pty_update(
            process_id=session_id,
            entry=entry,
            output=output,
            original_token_count=original_token_count,
            output_closed=output_closed,
        )

    async def _finalize_pty_update(
        self,
        *,
        process_id: int,
        entry: _GroundboxPtyEntry,
        output: bytes,
        original_token_count: int | None,
        output_closed: bool,
    ) -> PtyExecUpdate:
        exit_code = entry.exit_code if output_closed else None
        live_process_id: int | None = process_id
        if output_closed:
            async with self._pty_lock:
                removed = self._pty_sessions.pop(process_id, None)
                self._reserved_pty_process_ids.discard(process_id)
            if removed is not None:
                await self._terminate_pty_entry(removed)
            live_process_id = None
        return PtyExecUpdate(
            process_id=live_process_id,
            output=output,
            exit_code=exit_code,
            original_token_count=original_token_count,
        )

    async def pty_terminate_all(self) -> None:
        async with self._pty_lock:
            entries = list(self._pty_sessions.values())
            self._pty_sessions.clear()
            self._reserved_pty_process_ids.clear()
        for entry in entries:
            await self._terminate_pty_entry(entry)

    async def _collect_pty_output(
        self,
        *,
        entry: _GroundboxPtyEntry,
        yield_time_ms: int,
        max_output_tokens: int | None,
    ) -> tuple[bytes, int | None, bool]:
        return await collect_pty_output(
            output_chunks=entry.output_chunks,
            output_lock=entry.output_lock,
            output_notify=entry.output_notify,
            is_done=entry.output_closed.is_set,
            yield_time_ms=yield_time_ms,
            max_output_tokens=max_output_tokens,
        )

    def _prune_pty_sessions_if_needed(self) -> _GroundboxPtyEntry | None:
        if len(self._pty_sessions) < PTY_PROCESSES_MAX:
            return None
        meta = [(pid, entry.last_used, entry.output_closed.is_set()) for pid, entry in self._pty_sessions.items()]
        pid = process_id_to_prune_from_meta(meta)
        if pid is None:
            return None
        self._reserved_pty_process_ids.discard(pid)
        return self._pty_sessions.pop(pid, None)

    async def _terminate_pty_entry(self, entry: _GroundboxPtyEntry) -> None:
        entry.closing = True
        ws = entry.ws
        try:
            if ws is not None and not entry.output_closed.is_set():
                # The command is resumable, so closing the socket alone would leave it running.
                await asyncio.wait_for(ws.send(json.dumps({"type": "kill"})), timeout=self.state.timeouts.cleanup_s)
        except Exception:
            pass
        try:
            if ws is not None:
                await asyncio.wait_for(ws.close(), timeout=self.state.timeouts.cleanup_s)
        except Exception:
            pass
        task = entry.reader_task
        entry.reader_task = None
        if task is not None and task is not asyncio.current_task():
            if not task.done():
                task.cancel()
            try:
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=self.state.timeouts.cleanup_s)
            except asyncio.TimeoutError:
                pass
        entry.output_closed.set()
        entry.output_notify.set()

    # -- files -----------------------------------------------------------------

    async def read(self, path: Path | str, *, user: str | User | None = None) -> io.IOBase:
        error_path = posix_path_as_path(coerce_posix_path(path))
        if user is not None:
            workspace_path = await self._check_read_with_exec(path, user=user)
        else:
            workspace_path = await self._validate_path_access(path)
        try:
            data = await self._call(self._gb.read_file, self.state.sandbox_id, sandbox_path_str(workspace_path))
        except Exception as error:
            if _is_not_found(error):
                raise WorkspaceReadNotFoundError(path=error_path, cause=error) from error
            raise WorkspaceArchiveReadError(
                path=error_path, context=_error_context(error), cause=error, retryable=_retryable(error)
            ) from error
        return io.BytesIO(data)

    async def write(
        self,
        path: Path | str,
        data: io.IOBase,
        *,
        user: str | User | None = None,
    ) -> None:
        error_path = posix_path_as_path(coerce_posix_path(path))
        if user is not None:
            await self._check_write_with_exec(path, user=user)
        payload = data.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, (bytes, bytearray)):
            raise WorkspaceWriteTypeError(path=error_path, actual_type=type(payload).__name__)
        workspace_path = await self._validate_path_access(path, for_write=True)
        try:
            await self._call(
                self._gb.write_file, self.state.sandbox_id, sandbox_path_str(workspace_path), bytes(payload)
            )
        except Exception as error:
            raise WorkspaceArchiveWriteError(
                path=workspace_path, context=_error_context(error), cause=error, retryable=_retryable(error)
            ) from error

    async def mkdir(
        self,
        path: Path | str,
        *,
        parents: bool = False,
        user: str | User | None = None,
    ) -> None:
        if user is not None or not parents:
            # Groundbox's mkdir always makes parents; without them, keep mkdir's own semantics.
            await super().mkdir(path, parents=parents, user=user)
            return
        workspace_path = await self._validate_path_access(path, for_write=True)
        if workspace_path == Path("/"):
            return
        try:
            await self._call(self._gb.make_dir, self.state.sandbox_id, sandbox_path_str(workspace_path))
        except Exception as error:
            raise WorkspaceArchiveWriteError(
                path=workspace_path,
                context={**_error_context(error), "reason": "mkdir_failed"},
                cause=error,
                retryable=_retryable(error),
            ) from error

    # -- workspace persistence -------------------------------------------------

    async def _sh(self, script: str, *, timeout: float) -> ExecResult:
        return await self._exec_internal("sh", "-c", script, timeout=timeout)

    async def persist_workspace(self) -> io.IOBase:
        root = self._workspace_root_path()
        if self.state.workspace_persistence == "snapshot" and not self._native_snapshot_requires_tar_fallback():
            return await self._persist_native_snapshot(root)

        tar_path = f"/tmp/openai-agents-persist-{self.state.session_id.hex}.tar"
        tar_cmd = " ".join(
            [
                "tar",
                *shell_tar_exclude_args(self._persist_workspace_skip_relpaths()),
                "-C",
                shlex.quote(root.as_posix()),
                "-cf",
                shlex.quote(tar_path),
                ".",
            ]
        )
        try:
            result = await self._sh(tar_cmd, timeout=self.state.timeouts.workspace_tar_s)
            if not result.ok():
                raise WorkspaceArchiveReadError(
                    path=root,
                    context={
                        "backend": "groundbox",
                        "reason": "tar_failed",
                        "exit_code": result.exit_code,
                        "output": result.stderr.decode("utf-8", errors="replace")[-2000:],
                    },
                    retryable=False,
                )
            raw = await self._call(self._gb.read_file, self.state.sandbox_id, tar_path)
        except WorkspaceArchiveReadError:
            raise
        except Exception as error:
            raise WorkspaceArchiveReadError(
                path=root, context=_error_context(error), cause=error, retryable=_retryable(error)
            ) from error
        finally:
            try:
                await self._call(self._gb.delete_file, self.state.sandbox_id, tar_path)
            except Exception:
                pass
        return io.BytesIO(raw)

    async def _persist_native_snapshot(self, root: Path) -> io.IOBase:
        try:
            snapshot = await self._call(
                self._gb.keep_snapshot,
                self.state.sandbox_id,
                label=f"openai-agents {self.state.session_id}",
                wait=True,
                wait_s=float(self.state.timeouts.workspace_tar_s),
                idempotency_key=str(uuid.uuid4()),
            )
        except Exception as error:
            raise WorkspaceArchiveReadError(
                path=root,
                context={**_error_context(error), "reason": "snapshot_failed"},
                cause=error,
                retryable=_retryable(error),
            ) from error
        snapshot_id = snapshot.get("id")
        if not isinstance(snapshot_id, str) or snapshot.get("status") not in (None, "ready"):
            raise WorkspaceArchiveReadError(
                path=root,
                context={
                    "backend": "groundbox",
                    "reason": "snapshot_not_ready",
                    "status": snapshot.get("status"),
                    "error": snapshot.get("error"),
                },
                retryable=snapshot.get("status") == "creating",
            )
        return io.BytesIO(_encode_snapshot_ref(snapshot_id))

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        root = self._workspace_root_path()
        payload = data.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, (bytes, bytearray)):
            raise WorkspaceWriteTypeError(path=root, actual_type=type(payload).__name__)
        payload = bytes(payload)

        snapshot_id = _decode_snapshot_ref(payload)
        if snapshot_id is not None:
            if snapshot_id == self.state.restored_from_snapshot_id:
                return  # The sandbox was already started from this snapshot.
            try:
                await self._replace_sandbox_from_snapshot(snapshot_id)
            except Exception as error:
                raise WorkspaceArchiveWriteError(
                    path=root,
                    context={**_error_context(error), "reason": "snapshot_restore_failed"},
                    cause=error,
                    retryable=_retryable(error),
                ) from error
            return

        try:
            validate_tar_bytes(payload, allow_external_symlink_targets=False)
        except UnsafeTarMemberError as error:
            raise WorkspaceArchiveWriteError(
                path=root,
                context={"reason": "unsafe_or_invalid_tar", "member": error.member, "detail": str(error)},
                cause=error,
            ) from error

        tar_path = f"/tmp/openai-agents-hydrate-{self.state.session_id.hex}.tar"
        try:
            await self.mkdir(root, parents=True)
            await self._call(self._gb.write_file, self.state.sandbox_id, tar_path, payload)
            result = await self._sh(
                f"tar -C {shlex.quote(root.as_posix())} -xf {shlex.quote(tar_path)}",
                timeout=self.state.timeouts.workspace_tar_s,
            )
            if not result.ok():
                raise WorkspaceArchiveWriteError(
                    path=root,
                    context={
                        "backend": "groundbox",
                        "reason": "tar_extract_failed",
                        "exit_code": result.exit_code,
                        "output": result.stderr.decode("utf-8", errors="replace")[-2000:],
                    },
                )
        except WorkspaceArchiveWriteError:
            raise
        except Exception as error:
            raise WorkspaceArchiveWriteError(
                path=root, context=_error_context(error), cause=error, retryable=_retryable(error)
            ) from error
        finally:
            try:
                await self._call(self._gb.delete_file, self.state.sandbox_id, tar_path)
            except Exception:
                pass

    async def _restore_snapshot_into_workspace_on_resume(self) -> None:
        # The SDK clears the workspace before hydrating. A Groundbox snapshot replaces the whole
        # sandbox instead, and the one resume() started from it must not be cleared.
        if self.state.workspace_persistence == "snapshot":
            snapshot_id = await _snapshot_ref_from(self.state.snapshot, dependencies=self.dependencies)
            if snapshot_id is not None:
                if snapshot_id != self.state.restored_from_snapshot_id:
                    root = self._workspace_root_path()
                    try:
                        await self._replace_sandbox_from_snapshot(snapshot_id)
                    except Exception as error:
                        raise WorkspaceArchiveWriteError(
                            path=root,
                            context={**_error_context(error), "reason": "snapshot_restore_failed"},
                            cause=error,
                            retryable=_retryable(error),
                        ) from error
                return
        await super()._restore_snapshot_into_workspace_on_resume()

    async def _replace_sandbox_from_snapshot(self, snapshot_id: str) -> None:
        """Start a sandbox from a kept snapshot, then delete the one this session had."""
        await self.pty_terminate_all()
        old_id = self.state.sandbox_id
        sandbox = await _create_sandbox(
            self._gb,
            _snapshot_create_spec(self.state.create_spec, snapshot_id),
            timeout_s=self.state.timeouts.create_s,
        )
        self.state.sandbox_id = sandbox["id"]
        self.state.restored_from_snapshot_id = snapshot_id
        if old_id and old_id != sandbox["id"]:
            try:
                await asyncio.to_thread(self._gb.delete_sandbox, old_id)
            except Exception as error:
                logger.debug("Deleting the replaced Groundbox sandbox failed: %s", error)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

# Fields that size the machine or pick its disk; a sandbox from a snapshot takes the snapshot's.
_SNAPSHOT_EXCLUDED_FIELDS = ("cpus", "memory_gb", "disk_gb", "template", "image")


def _snapshot_create_spec(create_spec: dict[str, Any], snapshot_id: str) -> dict[str, Any]:
    spec = {key: value for key, value in create_spec.items() if key not in _SNAPSHOT_EXCLUDED_FIELDS}
    spec["snapshot_id"] = snapshot_id
    return spec


async def _create_sandbox(gb: Groundbox, spec: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
    """Create a sandbox and wait until it runs. Deletes it again when it does not come up."""
    sandbox = await asyncio.to_thread(gb.create_sandbox, idempotency_key=str(uuid.uuid4()), **spec)
    if sandbox.get("status") in _LIVE_STATUSES:
        sandbox = await asyncio.to_thread(gb.wait_for_sandbox, sandbox["id"], timeout_s)
    if sandbox.get("status") != "running":
        try:
            await asyncio.to_thread(gb.delete_sandbox, sandbox["id"])
        except Exception:
            pass
        raise WorkspaceStartError(
            path=Path("/"),
            context={
                "backend": "groundbox",
                "reason": "sandbox_not_running",
                "sandbox_id": sandbox.get("id"),
                "status": sandbox.get("status"),
                "error": sandbox.get("error"),
            },
            message=(
                f"Groundbox sandbox {sandbox.get('id')} is {sandbox.get('status')}"
                + (f": {sandbox.get('error')}" if sandbox.get("error") else "")
            ),
        )
    return sandbox


class GroundboxSandboxClient(BaseSandboxClient[GroundboxSandboxClientOptions]):
    """Creates, resumes and deletes Groundbox sandboxes for the Agents SDK.

    ``api_key`` defaults to the ``GROUNDBOX_API_KEY`` environment variable and ``base_url`` to
    ``GROUNDBOX_API_URL``, then https://api.groundbox.dev/v1. Pass ``groundbox`` to reuse a
    configured ``groundbox.Groundbox`` client.
    """

    backend_id = "groundbox"
    _instrumentation: Instrumentation

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        groundbox: Groundbox | None = None,
        instrumentation: Instrumentation | None = None,
        dependencies: Dependencies | None = None,
    ) -> None:
        self._gb = groundbox if groundbox is not None else Groundbox(api_key=api_key, base_url=base_url)
        self._instrumentation = instrumentation if instrumentation is not None else Instrumentation()
        self._dependencies = dependencies

    @property
    def groundbox(self) -> Groundbox:
        """The underlying ``groundbox.Groundbox`` client, for calls this interface has no method for."""
        return self._gb

    @staticmethod
    def _create_spec(options: GroundboxSandboxClientOptions, session_id: uuid.UUID) -> dict[str, Any]:
        metadata = {"openai_agents_session": str(session_id), **(options.metadata or {})}
        spec: dict[str, Any] = {
            "cpus": options.cpus,
            "memory_gb": options.memory_gb,
            "disk_gb": options.disk_gb,
            "timeout_minutes": options.timeout_minutes,
            "on_timeout": options.on_timeout,
            "image": options.image,
            "template": options.template,
            "env": dict(options.env) if options.env else None,
            "metadata": metadata,
            "allow_hosts": list(options.allow_hosts) if options.allow_hosts is not None else None,
            "deny_hosts": list(options.deny_hosts) if options.deny_hosts is not None else None,
            "secrets": list(options.secrets) if options.secrets else None,
            "flex": options.flex,
            "idle_timeout_seconds": options.idle_timeout_seconds,
        }
        return {key: value for key, value in spec.items() if value is not None}

    @redact_mount_error_data
    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: GroundboxSandboxClientOptions,
    ) -> SandboxSession:
        if manifest is None:
            manifest = Manifest(root=DEFAULT_GROUNDBOX_WORKSPACE_ROOT)
        self._validate_manifest_for_create(manifest)

        timeouts_in = options.timeouts
        if isinstance(timeouts_in, GroundboxSandboxTimeouts):
            timeouts = timeouts_in
        elif timeouts_in is None:
            timeouts = GroundboxSandboxTimeouts()
        else:
            timeouts = GroundboxSandboxTimeouts.model_validate(timeouts_in)

        session_id = uuid.uuid4()
        create_spec = self._create_spec(options, session_id)
        sandbox = await _create_sandbox(self._gb, create_spec, timeout_s=timeouts.create_s)

        state = GroundboxSandboxSessionState(
            session_id=session_id,
            manifest=manifest,
            snapshot=resolve_snapshot(snapshot, str(session_id)),
            sandbox_id=sandbox["id"],
            create_spec=create_spec,
            pause_on_exit=options.pause_on_exit,
            workspace_persistence=options.workspace_persistence,
            exposed_ports=options.exposed_ports,
            public_ports=options.public_ports,
            timeouts=timeouts,
        )
        inner = GroundboxSandboxSession.from_state(state, groundbox=self._gb)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def delete(self, session: SandboxSession) -> SandboxSession:
        inner = session._inner
        if not isinstance(inner, GroundboxSandboxSession):
            raise TypeError("GroundboxSandboxClient.delete expects a GroundboxSandboxSession")
        try:
            await inner.shutdown()
        except Exception:
            pass
        return session

    async def _reattach(self, state: GroundboxSandboxSessionState) -> bool:
        """Bring the session's sandbox back if it still exists. Returns whether it runs now."""
        try:
            sandbox = await asyncio.to_thread(self._gb.get_sandbox, state.sandbox_id)
            status = sandbox.get("status")
            if status == "paused":
                await asyncio.to_thread(self._gb.resume_sandbox, state.sandbox_id)
            elif status in ("shut_down", "shutting_down"):
                if status == "shutting_down":
                    await asyncio.to_thread(self._gb.wait_for_sandbox, state.sandbox_id, 120.0)
                await asyncio.to_thread(self._gb.start_sandbox, state.sandbox_id)
            elif status not in ("running", *_LIVE_STATUSES):
                return False  # stopped (deleted, timed out) or failed: it cannot come back.
            sandbox = await asyncio.to_thread(self._gb.wait_for_sandbox, state.sandbox_id, float(state.timeouts.create_s))
            return sandbox.get("status") == "running"
        except Exception as error:
            logger.debug("Groundbox sandbox %s could not be reattached: %s", state.sandbox_id, error)
            return False

    @redact_mount_error_data
    async def resume(self, state: SandboxSessionState) -> SandboxSession:
        if not isinstance(state, GroundboxSandboxSessionState):
            raise TypeError("GroundboxSandboxClient.resume expects a GroundboxSandboxSessionState")
        state.assert_path_grants_rebound()

        reconnected = await self._reattach(state)
        if not reconnected:
            snapshot_id = None
            if state.workspace_persistence == "snapshot":
                snapshot_id = await _snapshot_ref_from(state.snapshot)
            if snapshot_id is not None:
                spec = _snapshot_create_spec(state.create_spec, snapshot_id)
                state.restored_from_snapshot_id = snapshot_id
            else:
                spec = dict(state.create_spec)
                state.restored_from_snapshot_id = None
            sandbox = await _create_sandbox(self._gb, spec, timeout_s=state.timeouts.create_s)
            state.sandbox_id = sandbox["id"]
            state.workspace_root_ready = False

        inner = GroundboxSandboxSession.from_state(state, groundbox=self._gb)
        inner._set_start_state_preserved(reconnected, system=reconnected)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return self._deserialize_session_state_payload(payload, GroundboxSandboxSessionState)


async def _snapshot_ref_from(snapshot: SnapshotBase, *, dependencies: Dependencies | None = None) -> str | None:
    """The kept snapshot id a persisted workspace snapshot names, or None (a tar, or nothing)."""
    try:
        if not await snapshot.restorable(dependencies=dependencies):
            return None
        restored = await snapshot.restore(dependencies=dependencies)
        try:
            raw = restored.read()
        finally:
            try:
                restored.close()
            except Exception:
                pass
    except Exception:
        return None
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, (bytes, bytearray)):
        return None
    return _decode_snapshot_ref(bytes(raw))


__all__ = [
    "DEFAULT_GROUNDBOX_WORKSPACE_ROOT",
    "GroundboxSandboxClient",
    "GroundboxSandboxClientOptions",
    "GroundboxSandboxSession",
    "GroundboxSandboxSessionState",
    "GroundboxSandboxTimeouts",
]
