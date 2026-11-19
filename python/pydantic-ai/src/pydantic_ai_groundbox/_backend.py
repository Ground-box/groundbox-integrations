"""A Groundbox sandbox behind Pydantic AI's `WorkspaceBackend` protocol.

Behaviour of the Groundbox API this relies on, checked against api.groundbox.dev with the
`groundbox` Python SDK 0.4.2 on 2026-11-19:

- `POST /sandboxes` with an `Idempotency-Key` returns the same sandbox when retried, so a create
  whose answer is lost, or that two callers race on, never starts a second (billed) sandbox.
- Commands run as the image's user (`runner`, with passwordless sudo, on Groundbox's own image;
  root on a Docker `image`). An `args` list runs with no shell, stdin is at EOF, and a program that
  does not exist exits 127.
- `POST /sandboxes/{id}/commands` starts a command and answers at once with its id. Its output is
  read with `GET .../output?seq=`, byte for byte with `encoding=base64`; the sandbox keeps the last
  32 MB, and `skipped_bytes` says when some was dropped. `POST .../kill` ends the command and
  everything it started. `POST /exec`, by contrast, cuts each stream at about 4 MB without saying
  so, so it is only used for this backend's own short commands.
- The file API takes absolute paths. `stat` and listings describe a symlink itself, an upload
  replaces a symlink instead of writing through it, and reading a FIFO waits for a writer. The file
  API reads and writes with root privileges (an upload is owned by `runner`).
- A call on a sandbox that is not running answers `409 conflict` with the sandbox's `status` in
  `details`. A deleted sandbox reads `stopped`; only an id that never existed answers `404`.
"""

from __future__ import annotations

import base64
import errno
import logging
import math
import posixpath
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from functools import partial
from typing import Any, NoReturn, TypeVar

import anyio
import anyio.lowlevel
import anyio.to_thread
from pydantic_ai.workspaces import (
    CommandResult,
    FileEntry,
    SupportsCommands,
    SupportsFilesystem,
    Workspace,
    WorkspaceBackend,
    WorkspaceCommand,
    WorkspaceError,
    WorkspaceOutputLimitError,
    WorkspaceRef,
    WorkspaceTimeoutError,
    WorkspaceUnavailableError,
)

from ._util import absolute_path, check_env_types, check_timeout, command_argv, stop_shielded

try:
    from groundbox import Groundbox, GroundboxError, UpgradeRequiredError
except ImportError as _import_error:  # pragma: no cover
    raise ImportError('Install `pydantic-ai-groundbox` (it depends on the `groundbox` SDK).') from _import_error

__all__ = ('PROVIDER', 'GroundboxSandboxBackend', 'delete_sandbox')

logger = logging.getLogger(__name__)

_T = TypeVar('_T')

PROVIDER = 'groundbox'
"""The `WorkspaceRef.provider` of a Groundbox sandbox."""

_MISSING_KEY_MESSAGE = (
    'No Groundbox API key found. Set GROUNDBOX_API_KEY (create one at https://app.groundbox.dev/keys), '
    'or pass a configured `groundbox.Groundbox` as `client=`.'
)
_AUTH_MESSAGE = (
    'Groundbox rejected the API key. Set a valid GROUNDBOX_API_KEY, '
    'or pass a configured `groundbox.Groundbox` as `client=`.'
)

# How long a new sandbox may stay queued or starting. The first sandbox from a Docker `image` waits
# while the image is turned into a disk, which can take a few minutes; a flex sandbox waits for room.
DEFAULT_START_TIMEOUT = 1800.0
_STATUS_POLL = 1.0

# Bounds the backend's own short commands (`pwd -P`, following symlinks).
_INTERNAL_EXEC_TIMEOUT = 30

# The API's longest command timeout (30 days): a command run with `timeout=None` gets it as a backstop.
_MAX_COMMAND_TIMEOUT_S = 2_592_000
# The client enforces `timeout=` itself; the server's own timeout is a backstop a little later.
_SERVER_TIMEOUT_MARGIN_S = 5
# Between output reads while a command runs: from the first delay, doubling up to the last.
_POLL_FIRST = 0.05
_POLL_MAX = 0.5
# Time for a stop to reach the API when a command is cancelled or times out.
_STOP_GRACE = 15.0

# Total seconds one call may wait out `429 rate_limited` answers before the 429 propagates.
_RATE_LIMIT_BUDGET = 60.0

# A command's output is held in memory; past this it is stopped, like E2B's and Daytona's backends.
_MAX_OUTPUT_BYTES = 10 * 1024 * 1024
_OUTPUT_PREVIEW_CHARS = 64 * 1024

# Statuses a sandbox passes through on its way to `running`.
_TRANSIENT = frozenset({'queued', 'starting', 'resuming', 'pausing', 'shutting_down'})
# Statuses a sandbox never comes back from.
_ENDED = frozenset({'stopped', 'stopping', 'failed'})

# The file API's own words (and Node's errno names, which it passes on) for path failures, most specific first.
_PATH_ERRORS: tuple[tuple[str, type[OSError]], ...] = (
    ('is there already and is not a folder', FileExistsError),
    ('eexist', NotADirectoryError),  # an upload under a file: `mkdir` of the parent found the file
    ('enotdir', NotADirectoryError),
    ('not a directory', NotADirectoryError),
    ('not a folder', NotADirectoryError),
    ('eisdir', IsADirectoryError),
    ('is a folder', IsADirectoryError),
    ('is a directory', IsADirectoryError),
    ('eacces', PermissionError),
    ('permission denied', PermissionError),
    ('file exists', FileExistsError),
    ('no such file', FileNotFoundError),
    ('enoent', FileNotFoundError),
)

# Prints one line per path: `d` for a directory, `f SIZE` for a regular file, `o` for anything else
# that exists (a FIFO, a socket, a device) and `x` for nothing, following symlinks. `stat -L -c %s`
# works in GNU coreutils and BusyBox; every path is absolute, so none reads as an option.
_FOLLOW_SCRIPT = (
    'for p; do if [ -d "$p" ]; then echo d; elif [ -f "$p" ]; then echo "f $(stat -L -c %s "$p" 2>/dev/null)"; '
    'elif [ -e "$p" ]; then echo o; else echo x; fi; done'
)


def _details(error: GroundboxError) -> dict[str, Any]:
    details = error.details
    return details if isinstance(details, dict) else {}


def _path_error(error: GroundboxError, path: str) -> OSError | None:
    """The builtin error a failed path operation describes, or `None` if it describes none."""
    text = f'{error.message} {_details(error).get("reason", "")}'.lower()
    for marker, builtin in _PATH_ERRORS:
        if marker in text:
            return builtin(f'{error.message} (in the Groundbox sandbox: {path!r})')
    return None


def _unavailable_message(sandbox_id: str, status: str, reason: str | None = None) -> str:
    why = f' ({reason})' if reason else ''
    return (
        f'The Groundbox sandbox {sandbox_id!r} is {status}{why} and cannot be used any more. '
        "Pass `workspace='new'` to start a fresh sandbox."
    )


def _not_running_message(sandbox_id: str, status: str) -> str:
    return (
        f'The Groundbox sandbox {sandbox_id!r} is {status}, not running. A later run that attaches to it '
        "by its ref resumes or starts it again; pass `workspace='new'` to start a fresh sandbox."
    )


def _translate(error: GroundboxError, context: str, *, sandbox_id: str | None, path: str | None) -> Exception:
    """Map one SDK error onto the protocol's typed failures, or return it unchanged to propagate.

    A rejected key, a spend cap, a missing scope and a sandbox that is gone end the run
    (`WorkspaceUnavailableError`). Path failures become the builtin `OSError`s. Rate limits, server
    errors and unreachable networks come back unchanged so durable engines can retry them.
    """
    status, code = error.status, error.code
    if isinstance(error, UpgradeRequiredError):
        return WorkspaceUnavailableError(f'{context}: {error.message} Upgrade the SDK with `{error.upgrade}`.')
    if status == 401:
        return WorkspaceUnavailableError(_AUTH_MESSAGE)
    if status in (402, 403):
        # A spend cap, the free trial's limits, a test key or a key without the scope: no retry fixes it.
        return WorkspaceUnavailableError(f'{context}: {error.message}')
    if status == 404 and path is not None and error.message.startswith('No such file'):
        return FileNotFoundError(f'No such file or directory in the Groundbox sandbox: {path!r}')
    if status == 404 and sandbox_id is not None:
        return WorkspaceUnavailableError(_unavailable_message(sandbox_id, 'not found'))
    if status == 409 and sandbox_id is not None and isinstance(state := _details(error).get('status'), str):
        if state != 'running':
            if state in _ENDED:
                return WorkspaceUnavailableError(
                    _unavailable_message(sandbox_id, state, _details(error).get('stop_reason'))
                )
            return WorkspaceUnavailableError(_not_running_message(sandbox_id, state))
    if path is not None and (status in (400, 409) or code == 'internal'):
        if (path_error := _path_error(error, path)) is not None:
            return path_error
    if status == 400 and 'cwd ' in error.message and 'not a folder' in error.message:
        return WorkspaceError(f'{context}: {error.message}. Create the working directory, or pass one that exists.')
    if 400 <= status < 500 and status != 429:
        return WorkspaceError(f'{context}: {error.message}')
    return error


def _retry_after(error: GroundboxError) -> float:
    """Seconds a `429` asks the client to wait: its `details`, else "Try again in N seconds", else 2."""
    details = _details(error)
    for key in ('retry_after_s', 'retry_after'):
        if isinstance(value := details.get(key), (int, float)) and value > 0:
            return float(value)
    if match := re.search(r'in (\d+(?:\.\d+)?) seconds?', error.message):
        return float(match.group(1))
    return 2.0


def _decode(text: str) -> bytes:
    return base64.b64decode(text) if text else b''


class GroundboxSandboxBackend(WorkspaceBackend, SupportsCommands, SupportsFilesystem):
    """A [Groundbox](https://groundbox.dev) sandbox as a Pydantic AI `WorkspaceBackend`.

    Commands and file operations run in a Firecracker microVM, not on the host.

    Building one does no I/O. The first operation creates a sandbox, or attaches to the one `ref`
    names, resuming it if it is paused and starting it if it was stopped. The backend never deletes
    the sandbox: whoever holds the ref decides when to, with `delete_sandbox` or
    `GroundboxSandbox.destroy`.

    Each command runs in the background in the sandbox (`POST /sandboxes/{id}/commands`) and its
    output is read until it exits, so output is never cut short and a timeout or cancellation kills
    the command and everything it started. The SDK is synchronous; its calls run in worker threads,
    so the backend works under asyncio and Trio.

    Args:
        client: A `groundbox.Groundbox` to call the API with; one reading `GROUNDBOX_API_KEY` (and
            `GROUNDBOX_API_URL`) from the environment when `None`.
        ref: The sandbox to attach to on first use.
        working_dir: Absolute directory commands start in and relative paths resolve against. It is
            made on a sandbox this backend creates. `None` uses the sandbox's home directory
            (`/home/runner` on Groundbox's image).
        env: Variables every command gets, on a created or an attached sandbox; a command's own `env`
            is layered on top. Nothing is read from the host environment.
        start_timeout: Seconds a new or attached sandbox may take to reach `running`.
        create_options: Arguments for `Groundbox.create_sandbox` (`cpus`, `memory_gb`, `disk_gb`,
            `timeout_minutes`, `on_timeout`, `template`, `image`, `snapshot_id`, `allow_hosts`,
            `deny_hosts`, `secrets`, `env_from_secrets`, `metadata`, `idle_timeout_seconds`, `flex`,
            `volumes`, ...). Used only when a sandbox is created.
    """

    def __init__(
        self,
        *,
        client: Groundbox | None = None,
        ref: WorkspaceRef | None = None,
        working_dir: str | None = None,
        env: Mapping[str, str] | None = None,
        start_timeout: float = DEFAULT_START_TIMEOUT,
        create_options: Mapping[str, Any] | None = None,
    ) -> None:
        if ref is not None and ref.provider != PROVIDER:
            raise ValueError(f'unsupported workspace provider {ref.provider!r}; expected {PROVIDER!r}')
        check_env_types(env)
        if create_options is not None:
            if 'idempotency_key' in create_options:
                raise ValueError(
                    '`idempotency_key` is set by the backend so a retried create never starts two sandboxes'
                )
            if 'env' in create_options:
                raise ValueError('pass environment variables as `env`, not in `create_options`')
        if isinstance(start_timeout, bool) or not isinstance(start_timeout, (int, float)) or not start_timeout > 0:  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError(f'start_timeout must be a positive number of seconds, got {start_timeout!r}.')
        self._client = client
        self._ref = ref
        self._working_dir = absolute_path('working_dir', working_dir)
        self._env = dict(env) if env else {}
        self._start_timeout = float(start_timeout)
        self._create_options = dict(create_options or {})
        # One key for every create this backend sends: a retry, or a create that outlived a cancelled
        # caller, gets the sandbox the first one started.
        self._idempotency_key = f'pydantic-ai-{uuid.uuid4().hex}'
        self._lock = anyio.Lock()
        self._ready = False
        self._working_dir_pending = False
        self._resolved_working_dir: str | None = None

    # -- identity and acquisition -------------------------------------------------------------

    @property
    def ref(self) -> WorkspaceRef | None:
        """The sandbox's identity: `None` until one is created, then set for good."""
        return self._ref

    @property
    def client(self) -> Groundbox:
        """The `groundbox.Groundbox` this backend calls, for API features the workspace does not cover."""
        if self._client is None:
            self._client = Groundbox()
        return self._client

    async def get_sandbox(self) -> dict[str, Any]:
        """The sandbox as the API describes it (`GET /sandboxes/{id}`), creating or attaching first."""
        sandbox_id = await self._sandbox_id()
        return await self._api(self.client.get_sandbox, sandbox_id, context='Could not read the sandbox')

    async def _call(self, fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        """Run one blocking SDK call in a worker thread.

        Not abandoned on cancellation: the call finishes and only then is the cancellation raised,
        so a create or a command start is never left half-known.
        """
        if not self.client.api_key:
            raise WorkspaceUnavailableError(_MISSING_KEY_MESSAGE)
        waited = 0.0
        while True:
            try:
                return await anyio.to_thread.run_sync(partial(fn, *args, **kwargs))
            except GroundboxError as error:
                # A rate-limited request was not carried out, so it is safe to send again. Polling a
                # command must not fail a run over a burst; past the budget the 429 propagates.
                if error.status != 429 or waited >= _RATE_LIMIT_BUDGET:
                    raise
                delay = min(_retry_after(error), _RATE_LIMIT_BUDGET - waited)
                waited += delay
                await anyio.sleep(delay)

    async def _api(
        self,
        fn: Callable[..., _T],
        *args: Any,
        context: str,
        sandbox_id: str | None = None,
        path: str | None = None,
        **kwargs: Any,
    ) -> _T:
        """`_call`, with the SDK's errors translated onto the protocol's."""
        try:
            return await self._call(fn, *args, **kwargs)
        except GroundboxError as error:
            translated = _translate(error, context, sandbox_id=sandbox_id or self._sandbox_id_or_none(), path=path)
            if translated is error:
                raise
            raise translated from error

    def _sandbox_id_or_none(self) -> str | None:
        return self._ref.id if self._ref is not None else None

    async def _sandbox_id(self) -> str:
        """Create or attach on first use, and return the sandbox's id."""
        if self._ready and not self._working_dir_pending:
            assert self._ref is not None
            return self._ref.id
        async with self._lock:
            if not self._ready:
                if self._ref is None:
                    sandbox = await self._create()
                else:
                    sandbox = await self._api(
                        self.client.get_sandbox,
                        self._ref.id,
                        context=f'Could not attach to Groundbox sandbox {self._ref.id!r}',
                    )
                await self._until_running(sandbox)
                self._ready = True
            assert self._ref is not None
            if self._working_dir_pending:
                assert self._working_dir is not None
                await self._api(
                    self.client.make_dir,
                    self._ref.id,
                    self._working_dir,
                    context='Could not create working_dir',
                    path=self._working_dir,
                )
                self._working_dir_pending = False
        await anyio.lowlevel.checkpoint_if_cancelled()
        return self._ref.id

    async def _create(self) -> dict[str, Any]:
        """Start a sandbox, recording its ref as soon as the API answers, even if the caller was cancelled."""
        options = dict(self._create_options)
        if self._env:
            options['env'] = dict(self._env)

        def create() -> dict[str, Any]:
            sandbox = self.client.create_sandbox(idempotency_key=self._idempotency_key, **options)
            # Recorded here, in the worker thread, so the ref survives a caller cancelled meanwhile.
            self._ref = WorkspaceRef(provider=PROVIDER, id=sandbox['id'])
            self._working_dir_pending = self._working_dir is not None
            logger.info('Created Groundbox sandbox %s', sandbox['id'])
            return sandbox

        context = 'Could not start a Groundbox sandbox'
        try:
            return await self._call(create)
        except GroundboxError as error:
            translated = _translate(error, context, sandbox_id=None, path=None)
            if isinstance(translated, WorkspaceUnavailableError):
                raise translated from error
            if 400 <= error.status < 500 and error.status != 429:
                # Refused (an unknown template or image, a size over the plan's limits, a bad
                # argument): retrying the same request cannot succeed.
                raise WorkspaceUnavailableError(f'{context}: {error}') from error
            raise

    async def _until_running(self, sandbox: dict[str, Any]) -> None:
        """Wait for `sandbox` to run: resume it if paused, start it if it was shut down.

        A sandbox that has ended (deleted, failed, out of credit) raises `WorkspaceUnavailableError`;
        nothing is created in its place.
        """
        sandbox_id: str = sandbox['id']
        context = f'Could not bring Groundbox sandbox {sandbox_id!r} up'
        deadline = anyio.current_time() + self._start_timeout
        while True:
            status = sandbox.get('status')
            if status == 'running':
                return
            if status in _ENDED:
                reason = sandbox.get('error') or sandbox.get('stop_reason')
                raise WorkspaceUnavailableError(_unavailable_message(sandbox_id, str(status), reason))
            if anyio.current_time() >= deadline:
                # Transient, like any slow service: durable engines may retry, and the ref is kept.
                raise TimeoutError(
                    f'Groundbox sandbox {sandbox_id!r} was still {status} after {self._start_timeout:g} seconds.'
                )
            if status == 'paused':
                sandbox = await self._wake(self.client.resume_sandbox, sandbox_id, context)
                continue
            if status == 'shut_down':
                sandbox = await self._wake(self.client.start_sandbox, sandbox_id, context)
                continue
            await anyio.sleep(_STATUS_POLL)
            sandbox = await self._api(self.client.get_sandbox, sandbox_id, context=context, sandbox_id=sandbox_id)

    async def _wake(self, fn: Callable[[str], dict[str, Any]], sandbox_id: str, context: str) -> dict[str, Any]:
        try:
            return await self._call(fn, sandbox_id)
        except GroundboxError as error:
            if error.status == 409:
                # Someone else resumed or started it meanwhile: look again.
                return await self._api(self.client.get_sandbox, sandbox_id, context=context, sandbox_id=sandbox_id)
            translated = _translate(error, context, sandbox_id=sandbox_id, path=None)
            if translated is error:
                raise
            raise translated from error

    # -- commands -----------------------------------------------------------------------------

    async def working_dir(self) -> str:
        """The canonical absolute directory commands start in, resolved once with `pwd -P`."""
        if self._resolved_working_dir is None:
            result = await self._exec(['pwd', '-P'], context='Could not determine the working directory')
            printed = result.stdout.removesuffix('\n')
            if result.exit_code != 0 or not posixpath.isabs(printed):
                raise WorkspaceError(
                    f'Could not determine the working directory of Groundbox sandbox {self._sandbox_id_or_none()}: '
                    f'`pwd -P` exited {result.exit_code} and printed {result.stdout!r}{result.stderr!r}.'
                )
            self._resolved_working_dir = printed
        return self._resolved_working_dir

    async def _exec(self, argv: list[str], *, context: str) -> CommandResult:
        """One of the backend's own short commands, through `POST /exec` (one round trip)."""
        sandbox_id = await self._sandbox_id()
        answer = await self._api(
            self.client.run_command,
            sandbox_id,
            argv,
            cwd=self._working_dir,
            env=self._env or None,
            timeout_s=_INTERNAL_EXEC_TIMEOUT,
            context=context,
        )
        if answer.get('timed_out'):
            raise WorkspaceTimeoutError(f'{context}: the sandbox did not answer within {_INTERNAL_EXEC_TIMEOUT}s')
        return CommandResult(exit_code=answer['exit_code'], stdout=answer['stdout'], stderr=answer['stderr'])

    async def run(
        self,
        command: WorkspaceCommand,
        *,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        """Run a command to completion; on timeout, cancellation or too much output, kill it and its children."""
        argv = command_argv(command, shell)
        check_timeout(timeout)
        check_env_types(env)
        # Acquiring the sandbox has its own bound; the timeout is the command's alone.
        sandbox_id = await self._sandbox_id()
        context = 'Command could not run in the Groundbox sandbox'
        server_timeout = (
            _MAX_COMMAND_TIMEOUT_S
            if timeout is None
            else min(_MAX_COMMAND_TIMEOUT_S, math.ceil(timeout) + _SERVER_TIMEOUT_MARGIN_S)
        )
        started: dict[str, str] = {}

        def start() -> None:
            started['attempted'] = ''
            try:
                answer = self.client.start_command(
                    sandbox_id,
                    argv,
                    cwd=self._working_dir,
                    env={**self._env, **(env or {})} or None,
                    timeout_s=server_timeout,
                )
                # Recorded in the worker thread, so a cancelled caller can still kill what it started.
                started['id'] = answer['id']
            finally:
                started['returned'] = ''

        stdout, stderr = bytearray(), bytearray()
        part: dict[str, Any] = {}
        try:
            with anyio.move_on_after(timeout) as deadline:
                await self._api(start, context=context, sandbox_id=sandbox_id)
                exec_id = started['id']
                seq, delay = 0, 0.0
                while True:
                    part = await self._api(
                        self.client.get_command_output,
                        sandbox_id,
                        exec_id,
                        seq=seq,
                        encoding='base64',
                        context='Could not read the command output (the command may still be running)',
                        sandbox_id=sandbox_id,
                    )
                    stdout += _decode(part.get('stdout') or '')
                    stderr += _decode(part.get('stderr') or '')
                    if part.get('skipped_bytes') or len(stdout) + len(stderr) > _MAX_OUTPUT_BYTES:
                        raise WorkspaceOutputLimitError(
                            'Groundbox command output exceeded the 10 MiB limit; '
                            'redirect it to a file and read part of it instead',
                            limit=_MAX_OUTPUT_BYTES,
                            stdout=stdout.decode(errors='replace')[:_OUTPUT_PREVIEW_CHARS],
                            stderr=stderr.decode(errors='replace')[:_OUTPUT_PREVIEW_CHARS],
                        )
                    seq = part.get('seq', seq)
                    if part.get('done'):
                        break
                    if part.get('more'):
                        delay = 0.0
                    else:
                        delay = min(_POLL_MAX, delay * 2 or _POLL_FIRST)
                        await anyio.sleep(delay)
            if deadline.cancelled_caught or part.get('timed_out'):
                raise WorkspaceTimeoutError(
                    f'Command timed out after {timeout:g} seconds'
                    if timeout is not None
                    else "Command reached the sandbox's longest command timeout",
                    stdout=stdout.decode(errors='replace'),
                    stderr=stderr.decode(errors='replace'),
                )
        except BaseException:
            if not part.get('done'):
                await stop_shielded(partial(self._kill, sandbox_id, started), grace=_STOP_GRACE)
            raise
        exit_code = part.get('exit_code')
        if exit_code is None:
            # The command is `lost`: its processes were killed under it, as when the sandbox stops.
            await self._raise_if_gone(sandbox_id)
            raise WorkspaceError('The command ended without an exit code: its process was killed outside the command.')
        return CommandResult(
            exit_code=exit_code, stdout=stdout.decode(errors='replace'), stderr=stderr.decode(errors='replace')
        )

    async def _kill(self, sandbox_id: str, started: dict[str, str]) -> None:
        # A start still in flight records its id when it returns; wait for it rather than leak the command.
        while 'attempted' in started and 'returned' not in started:
            await anyio.sleep(0.05)
        if 'id' not in started:
            return
        try:
            await self._call(self.client.kill_command, sandbox_id, started['id'])
        except GroundboxError as error:
            # 409: the sandbox stopped, or the command ended on its own meanwhile; either way it is not running.
            if error.status != 409:
                logger.warning(
                    'Could not kill Groundbox command %s in sandbox %s: %s', started['id'], sandbox_id, error
                )

    async def _raise_if_gone(self, sandbox_id: str) -> None:
        sandbox = await self._api(
            self.client.get_sandbox, sandbox_id, context='Could not read the sandbox', sandbox_id=sandbox_id
        )
        status = str(sandbox.get('status'))
        if status in _ENDED:
            raise WorkspaceUnavailableError(_unavailable_message(sandbox_id, status, sandbox.get('stop_reason')))
        if status != 'running':
            raise WorkspaceUnavailableError(_not_running_message(sandbox_id, status))

    # -- files --------------------------------------------------------------------------------

    async def _file_api(self, fn: Callable[..., _T], path: str, *args: Any, context: str, **kwargs: Any) -> _T:
        sandbox_id = await self._sandbox_id()
        return await self._api(fn, sandbox_id, path, *args, context=context, sandbox_id=sandbox_id, path=path, **kwargs)

    async def _follow(self, paths: Sequence[str]) -> list[tuple[str, int | None]]:
        """What each path is once symlinks are followed: `('d'|'f'|'o'|'x', size)`, in one round trip."""
        result = await self._exec(['sh', '-c', _FOLLOW_SCRIPT, 'sh', *paths], context='Could not follow symlinks')
        lines = result.stdout.splitlines()
        if result.exit_code != 0 or len(lines) != len(paths):
            raise WorkspaceError(f'Could not follow symlinks: {result.stderr.strip() or result.stdout!r}')
        kinds: list[tuple[str, int | None]] = []
        for line in lines:
            kind, _, size = line.partition(' ')
            kinds.append((kind, int(size) if size.strip().isdigit() else None))
        return kinds

    async def _stat_raw(self, path: str) -> dict[str, Any]:
        if path == '/':
            # The file API names files and folders only; `/` is always a directory.
            await self._sandbox_id()
            return {'name': '/', 'path': '/', 'type': 'dir'}
        return await self._file_api(self.client.stat_file, path, context=f'Could not stat {path!r}')

    async def stat(self, path: str) -> FileEntry:
        info = await self._stat_raw(path)
        kind = info.get('type')
        name = posixpath.basename(path.rstrip('/')) or '/'
        if kind == 'symlink':
            followed, size = (await self._follow([path]))[0]
            if followed == 'x':
                # A dangling or looping link: nothing is there to describe.
                raise FileNotFoundError(f'Broken symlink in the Groundbox sandbox: {path!r}')
            return FileEntry(name=name, path=path, is_dir=followed == 'd', size=size if followed == 'f' else None)
        is_dir = kind == 'dir'
        size = info.get('size_bytes') if kind == 'file' else None
        return FileEntry(name=name, path=path, is_dir=is_dir, size=size)

    async def exists(self, path: str) -> bool:
        try:
            await self.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            return False
        return True

    async def list_dir(self, path: str) -> Sequence[FileEntry]:
        listing = await self._file_api(self.client.list_files, path, context=f'Could not list {path!r}')
        entries: list[dict[str, Any]] = listing.get('entries', [])
        links = [entry['path'] for entry in entries if entry.get('type') == 'symlink']
        followed = dict(zip(links, await self._follow(links), strict=True)) if links else {}
        result: list[FileEntry] = []
        for entry in entries:
            kind = entry.get('type')
            if kind == 'symlink':
                target, size = followed[entry['path']]
                is_dir, size = target == 'd', size if target == 'f' else None
            else:
                is_dir, size = kind == 'dir', entry.get('size_bytes') if kind == 'file' else None
            result.append(FileEntry(name=entry['name'], path=entry['path'], is_dir=is_dir, size=size))
        return result

    async def read_bytes(self, path: str) -> bytes:
        # The file API's read of a FIFO waits for a writer, so look at what the path is first.
        info = await self._stat_raw(path)
        kind = info.get('type')
        if kind == 'symlink':
            kind = {'d': 'dir', 'f': 'file', 'o': 'other'}.get((await self._follow([path]))[0][0])
            if kind is None:
                raise FileNotFoundError(f'Broken symlink in the Groundbox sandbox: {path!r}')
        if kind == 'dir':
            raise IsADirectoryError(f'Is a directory in the Groundbox sandbox: {path!r}')
        if kind != 'file':
            raise OSError(errno.EINVAL, 'Not a regular file (a FIFO, socket or device) in the Groundbox sandbox', path)
        return await self._file_api(self.client.read_file, path, context=f'Could not read {path!r}')

    async def write_bytes(self, path: str, data: bytes) -> None:
        target = path
        try:
            info: dict[str, Any] | None = await self._stat_raw(path)
        except FileNotFoundError:
            info = None
        if info is not None and info.get('type') == 'dir':
            raise IsADirectoryError(f'Is a directory in the Groundbox sandbox: {path!r}')
        if info is not None and info.get('type') == 'symlink':
            # An upload replaces a symlink; the protocol writes through it, to the file it names.
            target = await Workspace(self).realpath(path)
        await self._file_api(self.client.write_file, target, bytes(data), context=f'Could not write {path!r}')

    async def make_dir(self, path: str) -> None:
        if path == '/':
            await self._sandbox_id()
            return
        await self._file_api(self.client.make_dir, path, context=f'Could not create directory {path!r}')

    async def remove(self, path: str) -> None:
        # The removal is recursive, so refuse the working directory and its ancestors, like the
        # built-in backends. The leaf stays unresolved: removing a link removes only the link.
        root = await self.working_dir()
        parent = await Workspace(self).realpath(posixpath.dirname(path))
        target = posixpath.normpath(posixpath.join(parent, posixpath.basename(path)))
        if root == target or root.startswith(target.rstrip('/') + '/'):
            raise ValueError('cannot remove the workspace root or its ancestor')
        await self._stat_raw(path)  # A missing path is `FileNotFoundError`; a dangling symlink is still there.
        answer = await self._file_api(
            self.client.delete_file, path, recursive=True, context=f'Could not remove {path!r}'
        )
        if not answer.get('deleted', True):
            raise FileNotFoundError(f'No such file or directory in the Groundbox sandbox: {path!r}')


def _raise_translated(error: GroundboxError, context: str, sandbox_id: str) -> NoReturn:
    translated = _translate(error, context, sandbox_id=sandbox_id, path=None)
    if translated is error:
        raise error
    raise translated from error


async def delete_sandbox(sandbox_id: str, *, client: Groundbox | None = None) -> None:
    """Delete a sandbox by id: billing stops and its disk is gone. One already deleted returns quietly."""
    client = client or Groundbox()
    if not client.api_key:
        raise WorkspaceUnavailableError(_MISSING_KEY_MESSAGE)
    try:
        await anyio.to_thread.run_sync(client.delete_sandbox, sandbox_id)
    except GroundboxError as error:
        if error.status == 404:
            return
        _raise_translated(error, f'Could not delete Groundbox sandbox {sandbox_id!r}', sandbox_id)
