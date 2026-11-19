"""Fakes for the Groundbox client.

LocalGroundbox stands in for a sandbox by running commands on this machine, with the
sandbox's /tmp/smolagents-groundbox folder mapped to a local temporary folder. It runs the
real kernel, so the unit tests cover the whole code path except the network.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

import pytest
from smolagents.models import ChatMessage, MessageRole, Model
from smolagents.monitoring import AgentLogger, LogLevel

from smolagents_groundbox.executor import KERNEL_DIR


class LocalGroundbox:
    def __init__(self, status: str = "running"):
        # Short path: Unix socket paths are limited to about 100 characters.
        self.root = tempfile.mkdtemp(prefix="gbx", dir="/tmp")
        self.status = status
        self.calls: list[tuple] = []
        self.processes: dict[str, subprocess.Popen] = {}
        self.deleted: list[str] = []
        self.created_with: dict | None = None

    def _map(self, value):
        if isinstance(value, str):
            return value.replace(KERNEL_DIR, self.root)
        return [self._map(v) for v in value]

    def create_sandbox(self, **kwargs):
        self.calls.append(("create_sandbox", kwargs))
        self.created_with = kwargs
        return {"id": "sbx_local", "status": self.status}

    def get_sandbox(self, sandbox_id):
        self.calls.append(("get_sandbox", sandbox_id))
        return {"id": sandbox_id, "status": self.status}

    def wait_for_sandbox(self, sandbox_id):
        self.calls.append(("wait_for_sandbox", sandbox_id))
        return {"id": sandbox_id, "status": self.status, "error": "image not found"}

    def write_file(self, sandbox_id, path, content):
        self.calls.append(("write_file", path))
        local = self._map(path)
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, "w", encoding="utf-8") as f:
            f.write(content)
        return {}

    def start_command(self, sandbox_id, cmd):
        self.calls.append(("start_command", cmd))
        args = [sys.executable if a == "python3" else a for a in self._map(cmd)]
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        exec_id = f"ex_{len(self.processes)}"
        self.processes[exec_id] = proc
        return {"id": exec_id}

    def run_command(self, sandbox_id, cmd, stdin=None, timeout_s=None):
        self.calls.append(("run_command", cmd))
        if cmd[1:3] == ["-m", "pip"]:
            return {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}
        args = [sys.executable if a == "python3" else a for a in self._map(cmd)]
        done = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=timeout_s)
        return {"exit_code": done.returncode, "stdout": done.stdout, "stderr": done.stderr, "timed_out": False}

    def get_command_output(self, sandbox_id, exec_id):
        return {"stdout": "", "stderr": ""}

    def kill_command(self, sandbox_id, exec_id):
        self.calls.append(("kill_command", exec_id))
        proc = self.processes.get(exec_id)
        if proc and proc.poll() is None:
            proc.kill()
            proc.wait()
        return {}

    def delete_sandbox(self, sandbox_id):
        self.calls.append(("delete_sandbox", sandbox_id))
        self.deleted.append(sandbox_id)
        self.close()
        return {}

    def close(self):
        for proc in self.processes.values():
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        shutil.rmtree(self.root, ignore_errors=True)


@pytest.fixture
def logger():
    return AgentLogger(level=LogLevel.OFF)


@pytest.fixture
def local_client():
    client = LocalGroundbox()
    yield client
    client.close()


class ScriptedModel(Model):
    """A model that answers with fixed code actions, for running CodeAgent with no LLM."""

    def __init__(self, actions: list[str]):
        super().__init__(model_id="scripted")
        self.actions = list(actions)

    def generate(self, messages, stop_sequences=None, response_format=None, tools_to_call_from=None, **kwargs):
        code = self.actions.pop(0)
        return ChatMessage(role=MessageRole.ASSISTANT, content=f"Thought: next step.\n<code>\n{code}\n</code>")
