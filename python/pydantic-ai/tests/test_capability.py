"""`GroundboxSandbox`: configuration, specs, and whole agent runs driven by a scripted model."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.agent.spec import AgentSpec, CapabilitySpec
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.workspaces import AuthorityWorkspace, WorkspaceRef

from pydantic_ai_groundbox import GroundboxSandbox, GroundboxSandboxBackend

from .fake_groundbox import FakeGroundbox

pytestmark = [pytest.mark.anyio, pytest.mark.skipif(os.name != 'posix', reason='the fake runs on a POSIX host')]


def test_get_workspace_builds_a_backend_without_io(fake: FakeGroundbox) -> None:
    capability = GroundboxSandbox(client=fake, cpus=1, image='python:3.12-slim', allow_hosts=['pypi.org'])  # type: ignore[arg-type]
    backend = capability.get_workspace(None, ref=None)  # type: ignore[arg-type]
    assert isinstance(backend, GroundboxSandboxBackend) and backend.ref is None
    assert backend._create_options == {'cpus': 1, 'image': 'python:3.12-slim', 'allow_hosts': ['pypi.org']}
    ref = WorkspaceRef(provider='groundbox', id='sbx_1')
    attached = capability.get_workspace(None, ref=ref)  # type: ignore[arg-type]
    assert isinstance(attached, GroundboxSandboxBackend) and attached.ref == ref
    assert capability.get_workspace(None, ref=WorkspaceRef(provider='e2b', id='x')) is None  # type: ignore[arg-type]
    assert fake.calls == []


def test_named_fields_win_over_create_options(fake: FakeGroundbox) -> None:
    capability = GroundboxSandbox(client=fake, cpus=4, create_options={'cpus': 1, 'flex': True})  # type: ignore[arg-type]
    assert capability._create_options() == {'cpus': 4, 'flex': True}


def test_authority_wraps_the_workspace(fake: FakeGroundbox) -> None:
    capability = GroundboxSandbox(client=fake, authority='none')  # type: ignore[arg-type]
    assert isinstance(capability.get_workspace(None, ref=None), AuthorityWorkspace)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    'kwargs',
    [
        {'cpus': 0},
        {'timeout_minutes': True},
        {'template': 't', 'image': 'python:3.12'},
        {'working_dir': 'relative'},
        {'env': {'A': 1}},
        {'path_authority': {'~/x': 'none'}},
        {'create_options': {'idempotency_key': 'k'}},
    ],
)
def test_invalid_configuration(kwargs: dict[str, Any]) -> None:
    with pytest.raises(UserError):
        GroundboxSandbox(**kwargs)


def test_from_spec() -> None:
    spec = AgentSpec(
        model='test',
        capabilities=[
            CapabilitySpec(name='GroundboxSandbox', arguments={'cpus': 2, 'working_dir': '/home/runner/app'})
        ],
    )
    agent = Agent.from_spec(spec, custom_capability_types=[GroundboxSandbox])
    (capability,) = [c for c in agent.root_capability.capabilities if isinstance(c, GroundboxSandbox)]  # type: ignore[attr-defined]
    assert capability.cpus == 2 and capability.working_dir == '/home/runner/app'


async def test_destroy(fake: FakeGroundbox) -> None:
    capability = GroundboxSandbox(client=fake)  # type: ignore[arg-type]
    backend = capability.get_workspace(None, ref=None)  # type: ignore[arg-type]
    assert isinstance(backend, GroundboxSandboxBackend)
    await backend.working_dir()
    assert backend.ref is not None
    await capability.destroy(backend.ref)
    assert fake.sandboxes[backend.ref.id].status == 'stopped'
    with pytest.raises(ValueError):
        await capability.destroy(WorkspaceRef(provider='e2b', id='x'))


def scripted(*calls: tuple[str, dict[str, Any]]) -> FunctionModel:
    """A model that makes `calls` one per step, then answers with the last tool result; no LLM involved."""

    def next_step(messages: list[ModelMessage]) -> tuple[str, dict[str, Any]] | str:
        # Only this run's steps count: a follow-up run sees the earlier run's messages too.
        start = max(
            i for i, message in enumerate(messages) if any(isinstance(p, UserPromptPart) for p in message.parts)
        )
        returns = [part for message in messages[start:] for part in message.parts if isinstance(part, ToolReturnPart)]
        if len(returns) < len(calls):
            return calls[len(returns)]
        return str(returns[-1].content)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        step = next_step(messages)
        if isinstance(step, str):
            return ModelResponse(parts=[TextPart(step)])
        return ModelResponse(parts=[ToolCallPart(step[0], step[1])])

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        step = next_step(messages)
        if isinstance(step, str):
            yield step
        else:
            yield {0: DeltaToolCall(name=step[0], json_args=json.dumps(step[1]))}

    return FunctionModel(respond, stream_function=stream)


async def test_agent_run_with_shell_and_file_tools(fake: FakeGroundbox) -> None:
    try:
        from pydantic_ai_harness.file_system import FileSystem
    except ImportError:  # pydantic-ai-harness before 0.69 named the module `filesystem`
        from pydantic_ai_harness.filesystem import FileSystem  # type: ignore[no-redef]
    from pydantic_ai_harness.shell import Shell

    agent = Agent(
        scripted(
            ('write_file', {'path': 'hello.py', 'content': 'print("hello from the sandbox")\n'}),
            ('run_command', {'command': 'python3 hello.py'}),
        ),
        capabilities=[GroundboxSandbox(client=fake), Shell(), FileSystem()],  # type: ignore[arg-type]
    )
    result = await agent.run('Write hello.py and run it.')
    assert 'hello from the sandbox' in result.output
    assert result.workspace is not None and result.workspace.ref is not None
    assert result.workspace.ref.provider == 'groundbox'

    followup = await Agent(
        scripted(('run_command', {'command': 'cat hello.py'})),
        capabilities=[GroundboxSandbox(client=fake), Shell()],  # type: ignore[arg-type]
    ).run('Show hello.py.', message_history=result.all_messages())
    assert 'hello from the sandbox' in followup.output
    assert len(fake.called('create_sandbox')) == 1  # the follow-up attached to the same sandbox
