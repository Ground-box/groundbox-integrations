"""End to end on real Groundbox sandboxes: Inspect evals, a Dockerfile, egress rules, Inspect's injected tools.

Needs GROUNDBOX_API_KEY. Run with: pytest -m live tests/test_live_eval.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from inspect_ai import eval as inspect_eval
from inspect_ai.util._sandbox.events import SandboxEnvironmentProxy

from inspect_groundbox_sandbox import GroundboxSandboxEnvironment, _client
from inspect_groundbox_sandbox._config import resolve_spec

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("GROUNDBOX_API_KEY"), reason="GROUNDBOX_API_KEY is not set"),
]

EXAMPLE = Path(__file__).parent.parent / "examples" / "hello_groundbox.py"


def test_example_evals(tmp_path: Path) -> None:
    logs = inspect_eval(
        [f"{EXAMPLE}@hello", f"{EXAMPLE}@agent_bash"], model="mockllm/model", log_dir=str(tmp_path), display="none"
    )
    for log in logs:
        assert log.status == "success", log.error
        assert log.results is not None
        assert log.results.scores[0].metrics["accuracy"].value == 1.0, log.eval.task


async def test_dockerfile_compose_user_workdir_and_no_network(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(
        "FROM python:3.12-slim\nRUN useradd -m agent\nUSER agent\nWORKDIR /home/agent/work\n"
    )
    compose = tmp_path / "compose.yaml"
    compose.write_text("services:\n  default:\n    build: .\n    network_mode: none\n")
    task = "live-dockerfile"
    await GroundboxSandboxEnvironment.task_init(task, str(compose))
    envs = await GroundboxSandboxEnvironment.sample_init(task, str(compose), {})
    env = envs["default"]
    try:
        result = await env.exec(["sh", "-c", "whoami; pwd"])
        assert result.stdout.split() == ["agent", "/home/agent/work"]
        assert (await env.exec(["whoami"], user="root")).stdout.strip() == "root"

        await env.write_file("notes/a.txt", "written by inspect")
        owner = await env.exec(["stat", "-c", "%U %n", "/home/agent/work/notes/a.txt"])
        assert owner.stdout.strip() == "agent /home/agent/work/notes/a.txt"
        assert await env.read_file("notes/a.txt") == "written by inspect"

        network = await env.exec(
            ["python3", "-c", "import socket; socket.create_connection(('example.com', 80), timeout=5)"], timeout=30
        )
        assert not network.success
    finally:
        await GroundboxSandboxEnvironment.sample_cleanup(task, str(compose), envs, False)
        await GroundboxSandboxEnvironment.task_cleanup(task, str(compose), True)
        name = resolve_spec(str(compose)).template_name()
        try:
            _client.get_client().delete_template(name)
        except Exception:
            pass


async def test_exec_remote_uses_injected_tools() -> None:
    task = "live-exec-remote"
    await GroundboxSandboxEnvironment.task_init(task, None)
    envs = await GroundboxSandboxEnvironment.sample_init(task, None, {})
    # Inside an eval, Inspect hands tools the sandbox wrapped in its event proxy.
    env = SandboxEnvironmentProxy(envs["default"])
    try:
        result = await env.exec_remote(["sh", "-c", "echo out; echo err >&2; exit 3"], stream=False)
        assert (result.returncode, result.stdout, result.stderr) == (3, "out\n", "err\n")
    finally:
        await GroundboxSandboxEnvironment.sample_cleanup(task, None, envs, False)
        await GroundboxSandboxEnvironment.task_cleanup(task, None, True)
