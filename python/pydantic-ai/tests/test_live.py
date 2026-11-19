"""Whole agent runs against real Groundbox sandboxes, with a scripted model instead of an LLM.

Needs GROUNDBOX_API_KEY; every sandbox started here is deleted at the end.
"""

from __future__ import annotations

import pytest
from pydantic_ai import Agent

from pydantic_ai_groundbox import GroundboxSandbox

from .conftest import LIVE, LIVE_METADATA
from .test_capability import scripted

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.live,
    pytest.mark.skipif(not LIVE, reason='set GROUNDBOX_API_KEY to run against the Groundbox API'),
]


async def test_coder_runs_in_a_groundbox_sandbox_and_a_later_run_reattaches() -> None:
    from pydantic_ai_harness.coder import Coder

    sandbox = GroundboxSandbox(
        cpus=1,
        timeout_minutes=20,
        working_dir='/home/runner/project',
        metadata=LIVE_METADATA,
    )
    agent = Agent(
        scripted(
            (
                'write_file',
                {'path': 'hello.py', 'content': 'import platform\nprint("hello from", platform.system())\n'},
            ),
            ('shell', {'command': 'python3 hello.py && whoami && pwd'}),
        ),
        capabilities=[sandbox, Coder()],
    )
    result = await agent.run('Write hello.py and run it.')
    assert result.workspace is not None and result.workspace.ref is not None
    ref = result.workspace.ref
    try:
        assert ref.provider == 'groundbox'
        assert 'hello from Linux' in result.output
        assert 'runner' in result.output and '/home/runner/project' in result.output

        # The message history carries the ref: the follow-up works in the same sandbox.
        followup = await Agent(scripted(('read_file', {'path': 'hello.py'})), capabilities=[sandbox, Coder()]).run(
            'Show hello.py.', message_history=result.all_messages()
        )
        assert 'platform.system()' in followup.output

        # So does an explicit `workspace=ref`, as an application that stored the ref would pass it.
        later = await Agent(scripted(('shell', {'command': 'ls'})), capabilities=[sandbox, Coder()]).run(
            'List the files.', workspace=ref
        )
        assert 'hello.py' in later.output
        assert later.workspace is not None and later.workspace.ref == ref
    finally:
        await sandbox.destroy(ref)
