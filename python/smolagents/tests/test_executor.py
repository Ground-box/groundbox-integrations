from __future__ import annotations

import pytest
from conftest import LocalGroundbox, ScriptedModel
from smolagents import CodeAgent
from smolagents.default_tools import FinalAnswerTool
from smolagents.tools import Tool
from smolagents.utils import AgentError

from smolagents_groundbox import GroundboxExecutor


class AddTool(Tool):
    name = "add"
    description = "Adds two integers."
    inputs = {
        "a": {"type": "integer", "description": "first"},
        "b": {"type": "integer", "description": "second"},
    }
    output_type = "integer"

    def forward(self, a: int, b: int) -> int:
        return a + b


@pytest.fixture
def executor(local_client, logger):
    ex = GroundboxExecutor(logger=logger, client=local_client, timeout_seconds=5)
    yield ex
    ex.cleanup()


def test_creates_sandbox_with_defaults_and_overrides(local_client, logger):
    ex = GroundboxExecutor(logger=logger, client=local_client, create_kwargs={"cpus": 2, "timeout_minutes": 10})
    assert local_client.created_with["cpus"] == 2
    assert local_client.created_with["timeout_minutes"] == 10
    assert local_client.created_with["metadata"] == {"created_by": "smolagents-groundbox"}
    ex.cleanup()
    assert local_client.deleted == ["sbx_local"]


def test_state_is_kept_between_calls(executor):
    assert executor("x = 41").output is None
    out = executor("x += 1\nprint('x is', x)\nx")
    assert out.output == "42"
    assert out.logs == "x is 42\n"
    assert out.is_final_answer is False
    executor("def double(n):\n    return n * 2")
    assert executor("double(x)").output == "84"


def test_captures_subprocess_output(executor):
    out = executor("import subprocess\nsubprocess.run(['echo', 'from a child'])\nprint('after')")
    assert out.logs == "from a child\nafter\n"


def test_error_raises_agent_error_and_keeps_state(executor):
    executor("y = 'kept'")
    with pytest.raises(AgentError) as info:
        executor("print('before')\n1 / 0")
    message = str(info.value)
    assert "before" in message
    assert "Executing code yielded an error:\nZeroDivisionError" in message
    assert "1 / 0" in message  # the traceback shows the line
    assert executor("y").output == "'kept'"


def test_syntax_error(executor):
    with pytest.raises(AgentError, match="SyntaxError"):
        executor("def broken(:")


def test_sys_exit_does_not_end_the_kernel(executor):
    executor("z = 5")
    with pytest.raises(AgentError, match="SystemExit"):
        executor("import sys\nsys.exit(3)")
    assert executor("z").output == "5"


def test_timeout_interrupts_and_keeps_state(local_client, logger):
    ex = GroundboxExecutor(logger=logger, client=local_client, timeout_seconds=1)
    try:
        ex("w = 'still here'")
        with pytest.raises(AgentError, match="ExecutionTimeout"):
            ex("import time\ntime.sleep(10)")
        assert ex("w").output == "'still here'"
    finally:
        ex.cleanup()


def test_kernel_crash_is_restarted(executor):
    executor("v = 1")
    with pytest.raises(AgentError, match="was restarted"):
        executor("import os\nos._exit(1)")
    with pytest.raises(AgentError, match="NameError"):
        executor("v")
    assert executor("1 + 1").output == "2"


def test_send_variables(executor):
    executor.send_variables({"numbers": [1, 2, 3], "name": "box", "pair": (1, "a")})
    assert executor("sum(numbers), name, pair").output == "(6, 'box', (1, 'a'))"


def test_final_answer_and_tools(executor):
    executor.send_tools({"add": AddTool(), "final_answer": FinalAnswerTool()})
    assert executor("add(2, 3)").output == "5"
    out = executor("total = add(20, 22)\nprint('done')\nfinal_answer({'total': total, 'items': (1, 2)})")
    assert out.is_final_answer is True
    assert out.output == {"total": 42, "items": (1, 2)}
    assert out.logs == "done\n"


def test_attached_sandbox_is_not_deleted(local_client, logger):
    ex = GroundboxExecutor(logger=logger, client=local_client, sandbox_id="sbx_mine")
    assert ex("2 * 21").output == "42"
    ex.cleanup()
    assert local_client.deleted == []
    assert ("kill_command", "ex_0") in local_client.calls
    assert not any(call[0] == "create_sandbox" for call in local_client.calls)


def test_failed_sandbox_is_cleaned_up(logger):
    client = LocalGroundbox(status="failed")
    try:
        with pytest.raises(RuntimeError, match="image not found"):
            GroundboxExecutor(logger=logger, client=client)
        assert client.deleted == ["sbx_local"]
    finally:
        client.close()


def test_install_packages_retries_without_break_system_packages(local_client, logger):
    ex = GroundboxExecutor(logger=logger, client=local_client)
    pip_calls = []

    def fake_run(sandbox_id, cmd, stdin=None, timeout_s=None):
        pip_calls.append(cmd)
        if "--break-system-packages" in cmd:
            return {"exit_code": 2, "stdout": "", "stderr": "no such option: --break-system-packages"}
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    real_run = local_client.run_command
    local_client.run_command = fake_run
    try:
        assert ex.install_packages(["requests"]) == ["requests"]
        assert len(pip_calls) == 2 and "--break-system-packages" not in pip_calls[1]
    finally:
        local_client.run_command = real_run
        ex.cleanup()


def test_code_agent_end_to_end_with_scripted_model(local_client, logger):
    model = ScriptedModel(
        [
            "base = add(40, 1)\nprint('base', base)",
            "final_answer(base + 1)",
        ]
    )
    ex = GroundboxExecutor(logger=logger, client=local_client)
    with CodeAgent(tools=[AddTool()], model=model, executor=ex, verbosity_level=-1) as agent:
        assert agent.run("What is 40 + 1 + 1?") == 42
        observations = [getattr(step, "observations", None) for step in agent.memory.steps]
        assert any(o and "base 41" in o for o in observations)
    assert local_client.deleted == ["sbx_local"]
