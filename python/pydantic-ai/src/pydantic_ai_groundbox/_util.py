"""Argument checks and cancellation helpers shared by the backend and the capability.

`command_argv` and `stop_shielded` follow the private helpers of the same names in
pydantic-ai-harness (MIT), so this backend treats commands and cancellation the way the
official E2B and Daytona backends do.
"""

from __future__ import annotations

import asyncio
import math
import posixpath
from collections.abc import Awaitable, Callable, Mapping

import anyio
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import Authority
from pydantic_ai.workspaces import WorkspaceCommand

try:  # Optional: Trio depends on it, so without it Trio cannot be running.
    import sniffio as _sniffio
except ModuleNotFoundError:  # pragma: no cover
    _sniffio = None


def running_on_asyncio() -> bool:
    """Whether the caller runs on asyncio rather than Trio."""
    if _sniffio is None:  # pragma: no cover
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        return True
    try:
        return _sniffio.current_async_library() == 'asyncio'
    except _sniffio.AsyncLibraryNotFoundError:  # pragma: no cover
        return False


# asyncio holds only weak references to tasks, so a detached stop needs a strong one until it ends.
_pending_stops: set[asyncio.Task[None]] = set()


async def stop_shielded(stop: Callable[[], Awaitable[object]], *, grace: float) -> None:
    """Run `stop` to completion (bounded by `grace`) even while the caller is being cancelled.

    A failure inside `stop` is swallowed: it must never replace the timeout or cancellation that
    made the caller stop the command.
    """

    async def best_effort() -> None:
        try:
            await stop()
        except Exception:
            pass

    if running_on_asyncio():

        async def bounded() -> None:
            with anyio.move_on_after(grace, shield=True):
                await best_effort()

        # A native `Task.cancel()` bypasses AnyIO shields, so the stop runs in a task of its own that
        # the caller's cancellation cannot reach.
        child = asyncio.create_task(bounded())
        _pending_stops.add(child)
        child.add_done_callback(_pending_stops.discard)
        try:
            with anyio.move_on_after(grace + 0.5, shield=True):
                await asyncio.shield(child)
        except asyncio.CancelledError:
            # Re-raised from a step that resumed normally, so AnyIO scopes do not claim it as their own.
            with anyio.CancelScope(shield=True):
                while True:
                    try:
                        await asyncio.sleep(0)
                    except asyncio.CancelledError:
                        continue
                    break
            raise
    else:
        with anyio.move_on_after(grace, shield=True):
            await best_effort()


def command_argv(command: WorkspaceCommand, shell: bool) -> list[str]:
    """The argv that runs `command`, with the same `shell` rules as Pydantic AI's local backend."""
    if isinstance(command, str):
        if not shell:
            raise TypeError('a string command requires shell=True; pass an argv sequence otherwise')
        return ['/bin/sh', '-c', command]
    if isinstance(command, bytes):
        raise TypeError('a bytes command is not supported; pass a string or argv sequence')
    if shell:
        raise TypeError('an argv sequence cannot be combined with shell=True; pass a single command string')
    if not command:
        raise TypeError('an argv sequence needs at least the program to run')
    for argument in command:
        if type(argument) is not str:
            raise TypeError('argv elements must be strings')
        if '\x00' in argument:
            raise ValueError('argv elements must not contain NUL bytes')
    return list(command)


def check_timeout(timeout: float | None) -> None:
    """Raise `ValueError` unless a command `timeout` is a positive finite number or `None`."""
    if timeout is not None and (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))  # pyright: ignore[reportUnnecessaryIsInstance]
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError(f'timeout must be a positive finite number or None, got {timeout!r}.')


def check_env_types(env: Mapping[str, str] | None) -> None:
    """Raise `TypeError` unless `env` is `None` or maps strings to strings."""
    if env is not None and any(type(key) is not str or type(value) is not str for key, value in env.items()):
        raise TypeError('env keys and values must be strings')


def absolute_path(name: str, value: str | None) -> str | None:
    """`value`, checked to be an absolute POSIX path, or `None`."""
    if value is not None and not posixpath.isabs(value):
        raise ValueError(f'{name} must be an absolute path in the sandbox or None, got {value!r}.')
    return value


def check_positive_int(name: str, value: int | None) -> None:
    """Raise `UserError` unless `value` is `None` or an integer of at least 1 (`bool` is not a count)."""
    if value is not None and (type(value) is not int or value < 1):
        raise UserError(f'{name} must be an integer of at least 1 or None, got {value!r}.')


def check_path_authority(path_authority: Mapping[str, Authority] | None) -> None:
    """Raise `UserError` for a `path_authority` key starting with `~`, which a sandbox does not expand."""
    for path in path_authority or ():
        if path.startswith('~'):
            raise UserError(
                f'`path_authority` paths are paths in the sandbox, so `~` is not expanded: got {path!r}. '
                'Pass an absolute path, or one relative to the working directory.'
            )
