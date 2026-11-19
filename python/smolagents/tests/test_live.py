"""Against Groundbox itself. Runs only with GROUNDBOX_API_KEY set; creates and deletes one sandbox per test."""

from __future__ import annotations

import os

import pytest
from conftest import ScriptedModel
from smolagents import CodeAgent
from smolagents.default_tools import FinalAnswerTool
from smolagents.tools import Tool
from smolagents.utils import AgentError

from smolagents_groundbox import GroundboxExecutor

pytestmark = pytest.mark.skipif(not os.environ.get("GROUNDBOX_API_KEY"), reason="GROUNDBOX_API_KEY is not set")


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


def _assert_gone(executor):
    status = executor.client.get_sandbox(executor.sandbox_id).get("status")
    assert status not in ("running", "queued", "starting", "paused"), status


def test_live_executor(logger):
    ex = GroundboxExecutor(
        additional_imports=["pillow"],
        logger=logger,
        timeout_seconds=5,
        create_kwargs={"cpus": 1, "timeout_minutes": 15, "metadata": {"test": "smolagents-groundbox"}},
    )
    try:
        # State between calls.
        ex("import platform\ncount = 1")
        out = ex("count += 1\nprint('running on', platform.system())\ncount")
        assert out.output == "2"
        assert out.logs == "running on Linux\n"

        # Errors, with state kept.
        with pytest.raises(AgentError, match="ZeroDivisionError"):
            ex("1 / 0")
        assert ex("count").output == "2"

        # A step past timeout_seconds is interrupted, state kept.
        with pytest.raises(AgentError, match="ExecutionTimeout"):
            ex("import time\ntime.sleep(30)")
        assert ex("count").output == "2"

        # A package from additional_imports, and an image result.
        img = ex("from PIL import Image\nImage.new('RGB', (4, 3), 'red')").output
        assert img.size == (4, 3)

        # Variables, tools and final_answer, as CodeAgent sends them.
        ex.send_variables({"numbers": [1, 2, 3]})
        ex.send_tools({"add": AddTool(), "final_answer": FinalAnswerTool()})
        out = ex("final_answer({'sum': add(sum(numbers), 36), 'count': count})")
        assert out.is_final_answer is True
        assert out.output == {"sum": 42, "count": 2}
    finally:
        ex.cleanup()
    _assert_gone(ex)


def test_live_code_agent_with_scripted_model(logger):
    model = ScriptedModel(
        [
            "import os\nwho = os.popen('id -un').read().strip()\nprint('user', who)\nbase = add(40, 1)",
            "final_answer(f'{who}:{base + 1}')",
        ]
    )
    ex = GroundboxExecutor(logger=logger, create_kwargs={"cpus": 1, "timeout_minutes": 15})
    with CodeAgent(tools=[AddTool()], model=model, executor=ex, verbosity_level=-1) as agent:
        assert agent.run("Add one to forty, then one more.") == "runner:42"
    _assert_gone(ex)
