"""Inspect's own sandbox conformance checks, run against real Groundbox sandboxes.

Needs GROUNDBOX_API_KEY. Run with: pytest -m live tests/test_live_self_check.py

Each config gets one sandbox, shared by every check (the checks clean up after
themselves), and deleted at the end.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
import pytest_asyncio
from inspect_ai.util import SandboxEnvironment

# Pull Inspect's portable checks into this module so pytest collects them.
from inspect_ai.util._sandbox.self_check import *  # noqa: F403

from inspect_groundbox_sandbox import GroundboxSandboxEnvironment, GroundboxSandboxEnvironmentConfig

pytestmark = [
    pytest.mark.live,
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.skipif(not os.environ.get("GROUNDBOX_API_KEY"), reason="GROUNDBOX_API_KEY is not set"),
]


@dataclass
class Case:
    id: str
    config: GroundboxSandboxEnvironmentConfig | None
    xfails: dict[str, str] = field(default_factory=dict)


ROOT_XFAILS = {
    name: "the default user is root, who is not stopped by file permissions"
    for name in (
        "test_read_file_not_allowed",
        "test_write_text_file_without_permissions",
        "test_write_binary_file_without_permissions",
    )
}

CASES = [
    # Groundbox's own image: commands run as `runner`, who has sudo.
    Case("default-image", None),
    # A Docker image: commands run as root, and root reads and writes past file modes.
    Case(
        "debian-image",
        GroundboxSandboxEnvironmentConfig(image="python:3.12-slim", cpus=1),
        xfails=ROOT_XFAILS,
    ),
    # Alpine: BusyBox, no runuser or sudo, so users switch with su.
    Case(
        "alpine-image",
        GroundboxSandboxEnvironmentConfig(image="alpine:3.20", cpus=1),
        xfails=ROOT_XFAILS,
    ),
]


@pytest_asyncio.fixture(scope="module", loop_scope="module", params=CASES, ids=lambda case: case.id)
async def _case_env(request: pytest.FixtureRequest) -> AsyncIterator[tuple[Case, SandboxEnvironment]]:
    case: Case = request.param
    task = f"self-check-{case.id}"
    await GroundboxSandboxEnvironment.task_init(task, case.config)
    envs = await GroundboxSandboxEnvironment.sample_init(task, case.config, {})
    try:
        yield case, envs["default"]
    finally:
        await GroundboxSandboxEnvironment.sample_cleanup(task, case.config, envs, False)
        await GroundboxSandboxEnvironment.task_cleanup(task, case.config, True)


@pytest.fixture
def sandbox_env(request: pytest.FixtureRequest, _case_env: tuple[Case, SandboxEnvironment]) -> SandboxEnvironment:
    case, env = _case_env
    reason = case.xfails.get(request.node.originalname)
    if reason:
        request.node.add_marker(pytest.mark.xfail(reason=reason, strict=True))
    return env
