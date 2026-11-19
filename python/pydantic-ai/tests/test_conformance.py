"""Pydantic AI's workspace backend conformance suite, run against `GroundboxSandboxBackend`.

`TestFakeGroundboxBackend` runs on POSIX hosts over `FakeGroundbox`, where commands and file calls
act on a temporary host directory. `TestLiveGroundboxBackend` runs the same rules against real
Groundbox sandboxes when `GROUNDBOX_API_KEY` is set, and deletes every sandbox it starts.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from pydantic_ai.workspaces import WorkspaceBackend, WorkspaceRef
from pydantic_ai.workspaces.conformance import WorkspaceBackendSuite

from pydantic_ai_groundbox import GroundboxSandboxBackend, delete_sandbox

from .conftest import LIVE, LIVE_METADATA
from .fake_groundbox import FakeGroundbox

# Small and short-lived, so a sandbox a crashed run leaves behind deletes itself.
LIVE_OPTIONS = {'cpus': 1, 'timeout_minutes': 20, 'metadata': LIVE_METADATA}


@pytest.mark.skipif(os.name != 'posix', reason='the fake runs commands on a POSIX host')
class TestFakeGroundboxBackend(WorkspaceBackendSuite):
    @pytest.fixture
    def backend(self, fake: FakeGroundbox) -> GroundboxSandboxBackend:
        return GroundboxSandboxBackend(client=fake)  # type: ignore[arg-type]

    @pytest.fixture
    def fresh_backend(self, fake: FakeGroundbox) -> Callable[[], WorkspaceBackend]:
        return lambda: GroundboxSandboxBackend(client=fake)  # type: ignore[arg-type]

    @pytest.fixture
    def attach_backend(self, fake: FakeGroundbox) -> Callable[[WorkspaceRef], WorkspaceBackend]:
        return lambda ref: GroundboxSandboxBackend(client=fake, ref=ref)  # type: ignore[arg-type]

    @pytest.fixture
    def destroy_environment(self, fake: FakeGroundbox) -> Callable[[WorkspaceBackend], Awaitable[None]]:
        async def destroy(backend: WorkspaceBackend) -> None:
            assert backend.ref is not None
            await delete_sandbox(backend.ref.id, client=fake)  # type: ignore[arg-type]

        return destroy


async def _delete(backend: WorkspaceBackend) -> None:
    assert backend.ref is not None
    await delete_sandbox(backend.ref.id)


@pytest.mark.live
@pytest.mark.skipif(not LIVE, reason='set GROUNDBOX_API_KEY to run against the Groundbox API')
class TestLiveGroundboxBackend(WorkspaceBackendSuite):
    # Class-scoped so the rules share one sandbox; destructive rules get sandboxes of their own.
    @pytest.fixture(scope='class')
    @classmethod
    async def backend(cls) -> AsyncIterator[GroundboxSandboxBackend]:
        backend = GroundboxSandboxBackend(create_options=LIVE_OPTIONS)
        try:
            await backend.working_dir()
            yield backend
        finally:
            if backend.ref is not None:
                await delete_sandbox(backend.ref.id)

    @pytest.fixture
    async def fresh_backend(self) -> AsyncIterator[Callable[[], WorkspaceBackend]]:
        created: list[GroundboxSandboxBackend] = []

        def create() -> WorkspaceBackend:
            backend = GroundboxSandboxBackend(create_options=LIVE_OPTIONS)
            created.append(backend)
            return backend

        yield create
        for backend in created:
            if backend.ref is not None:
                await delete_sandbox(backend.ref.id)

    @pytest.fixture
    def attach_backend(self) -> Callable[[WorkspaceRef], WorkspaceBackend]:
        return lambda ref: GroundboxSandboxBackend(ref=ref)

    @pytest.fixture
    def destroy_environment(self) -> Callable[[WorkspaceBackend], Awaitable[None]]:
        return _delete

    @pytest.fixture
    def filesystem_honors_shell_permissions(self) -> bool:
        # Groundbox's file API reads and writes with root privileges, like E2B's.
        return False
