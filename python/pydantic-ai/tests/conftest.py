from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from .fake_groundbox import FakeGroundbox

LIVE = bool(os.environ.get('GROUNDBOX_API_KEY'))

# Every live sandbox carries this session's tag, so the session can delete whatever a failed test left.
RUN_TAG = uuid.uuid4().hex[:12]
LIVE_METADATA = {'purpose': 'pydantic-ai-groundbox-tests', 'test_run': RUN_TAG}


@pytest.fixture(scope='session', autouse=True)
def _delete_leftover_live_sandboxes() -> Iterator[None]:
    yield
    if not LIVE:
        return
    from groundbox import Groundbox

    client = Groundbox()
    for sandbox in client.list_sandboxes(live=True, metadata={'test_run': RUN_TAG}).get('data', []):
        client.delete_sandbox(sandbox['id'])


@pytest.fixture(scope='session')
def anyio_backend() -> str:
    return 'asyncio'


@pytest.fixture
def fake(tmp_path: Path) -> FakeGroundbox:
    # Resolved, so the working directory `pwd -P` prints matches it behind a symlinked temp dir.
    return FakeGroundbox(tmp_path.resolve())


@pytest.fixture(autouse=True)
def _fast_polls(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker('live') is None:
        from pydantic_ai_groundbox import _backend

        monkeypatch.setattr(_backend, '_STATUS_POLL', 0.0)
