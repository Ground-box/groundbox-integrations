"""The Groundbox client the sandbox uses, and calling it from async code.

The `groundbox` SDK is synchronous (standard library only), so each call runs in
a worker thread. A command can hold its thread for as long as it runs, so the
sandbox uses its own, larger thread limit instead of anyio's default of 40.
"""

from __future__ import annotations

import os
import random
import re
from collections.abc import Callable
from functools import partial
from typing import Any, TypeVar

import anyio
import anyio.to_thread
from groundbox import Groundbox, GroundboxError

T = TypeVar("T")

# Seconds an SDK call waits for an answer (a command's own timeout is added to it).
REQUEST_TIMEOUT = 300.0

_client: Groundbox | None = None
_limiter: anyio.CapacityLimiter | None = None


def api_key_present() -> bool:
    return bool(os.environ.get("GROUNDBOX_API_KEY")) or _client is not None


def get_client() -> Groundbox:
    """The shared client: GROUNDBOX_API_KEY, and GROUNDBOX_API_URL when set."""
    global _client
    if _client is None:
        if not os.environ.get("GROUNDBOX_API_KEY"):
            raise RuntimeError(
                "GROUNDBOX_API_KEY is not set. Create a key at https://app.groundbox.dev/keys "
                "and export GROUNDBOX_API_KEY=gbx_..."
            )
        _client = Groundbox(timeout=REQUEST_TIMEOUT)
    return _client


def set_client(client: Any) -> None:
    """Use this client (a `Groundbox`, or a fake in tests) instead of making one."""
    global _client
    _client = client


def _thread_limiter() -> anyio.CapacityLimiter:
    global _limiter
    if _limiter is None:
        _limiter = anyio.CapacityLimiter(int(os.environ.get("INSPECT_GROUNDBOX_THREADS", "256")))
    return _limiter


async def call(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a blocking SDK call in a worker thread."""
    return await anyio.to_thread.run_sync(partial(fn, *args, **kwargs), limiter=_thread_limiter())


def is_transient(error: BaseException) -> bool:
    """An error worth trying again: no answer, a rate limit, or the API briefly unable to serve."""
    if not isinstance(error, GroundboxError):
        return False
    return error.code in ("network", "rate_limited", "no_capacity") or error.status in (429, 502, 503, 504)


def retry_delay(error: GroundboxError, attempt: int) -> float:
    """Seconds to wait before trying again: what a rate limit asks for, else exponential backoff."""
    if error.code == "rate_limited" or error.status == 429:
        hint = (error.details or {}).get("retry_after")
        match = re.search(r"in (\d+) seconds?", error.message or "")
        if hint is None and match:
            hint = match.group(1)
        try:
            return min(30.0, float(hint)) + random.random()
        except (TypeError, ValueError):
            pass
    return min(10.0, 0.5 * 2**attempt) + random.random() * 0.25


async def retry(fn: Callable[..., T], *args: Any, attempts: int = 6, **kwargs: Any) -> T:
    """`call` again after a transient error (no answer, a rate limit, a 502/503/504)."""
    for attempt in range(attempts):
        try:
            return await call(fn, *args, **kwargs)
        except GroundboxError as error:
            if attempt == attempts - 1 or not is_transient(error):
                raise
            await anyio.sleep(retry_delay(error, attempt))
    raise AssertionError("unreachable")
