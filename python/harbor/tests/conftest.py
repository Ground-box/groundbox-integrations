"""A fake Groundbox client and helpers to build a GroundboxSandbox around it."""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import pytest

from groundbox import GroundboxError
from harbor.models.task.config import EnvironmentConfig, NetworkMode, NetworkPolicy
from harbor.models.trial.paths import TrialPaths

from groundbox_harbor import GroundboxSandbox


def err(code: str, status: int, message: str = "") -> GroundboxError:
    return GroundboxError(code, message or code, status, "POST", "/x")


class FakeGroundbox:
    """Records every call; answers like the Groundbox API does."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.templates: dict[str, dict[str, Any]] = {}
        self.files: dict[str, bytes] = {}
        self.ids = itertools.count(1)
        self.exec_results: list[dict[str, Any]] = []
        self.output_pages: list[dict[str, Any]] = []
        self.errors: dict[str, list[Exception]] = {}
        self.build_status = "ready"
        self.sandbox_status = "running"

    def _record(self, _method: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((_method, args, kwargs))
        queue = self.errors.get(_method)
        if queue:
            raise queue.pop(0)

    def named(self, name: str) -> list[tuple[tuple, dict]]:
        return [(a, k) for n, a, k in self.calls if n == name]

    # sandboxes
    def create_sandbox(self, **kwargs: Any) -> dict[str, Any]:
        self._record("create_sandbox", **kwargs)
        return {"id": f"sbx_{next(self.ids)}", "status": self.sandbox_status}

    def get_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        self._record("get_sandbox", sandbox_id)
        return {"id": sandbox_id, "status": "running"}

    def update_sandbox(self, sandbox_id: str, **kwargs: Any) -> dict[str, Any]:
        self._record("update_sandbox", sandbox_id, **kwargs)
        return {"id": sandbox_id}

    def delete_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        self._record("delete_sandbox", sandbox_id)
        return {"ok": True}

    # commands
    def run_command(self, sandbox_id: str, cmd: Any, **kwargs: Any) -> dict[str, Any]:
        self._record("run_command", sandbox_id, cmd, **kwargs)
        if self.exec_results:
            return self.exec_results.pop(0)
        return {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}

    def start_command(self, sandbox_id: str, cmd: Any, **kwargs: Any) -> dict[str, Any]:
        self._record("start_command", sandbox_id, cmd, **kwargs)
        return {"id": "ex_1"}

    def get_command_output(self, sandbox_id: str, exec_id: str, seq: int = 0) -> dict[str, Any]:
        self._record("get_command_output", sandbox_id, exec_id, seq=seq)
        if self.output_pages:
            return self.output_pages.pop(0)
        return {"stdout": "", "stderr": "", "seq": seq, "more": False, "done": True, "exit_code": 0}

    def kill_command(self, sandbox_id: str, exec_id: str) -> dict[str, Any]:
        self._record("kill_command", sandbox_id, exec_id)
        return {}

    # files
    def write_file(self, sandbox_id: str, path: str, content: bytes) -> dict[str, Any]:
        self._record("write_file", sandbox_id, path, len(content))
        self.files[path] = bytes(content)
        return {}

    def read_file(self, sandbox_id: str, path: str) -> bytes:
        self._record("read_file", sandbox_id, path)
        return self.files[path]

    def stat_file(self, sandbox_id: str, path: str) -> dict[str, Any]:
        self._record("stat_file", sandbox_id, path)
        if path not in self.files:
            raise err("not_found", 404, f"No such file or folder: {path}")
        return {"type": "file", "size_bytes": len(self.files[path])}

    # templates
    def get_template(self, name: str) -> dict[str, Any]:
        self._record("get_template", name)
        if name not in self.templates:
            raise err("not_found", 404)
        return self.templates[name]

    def create_template(self, name: str, **spec: Any) -> dict[str, Any]:
        self._record("create_template", name=name, **spec)
        self.templates[name] = {
            "name": name,
            "status": "building",
            "version": None,
            "created_at": "2026-11-19T00:00:00Z",
            "latest_build": {"version": 1, "status": "queued"},
        }
        return self.templates[name]

    def build_template(self, name: str, **spec: Any) -> dict[str, Any]:
        self._record("build_template", name, **spec)
        return {"version": 2, "status": "queued"}

    def get_template_logs(self, name: str, version: int, after: int = -1, wait_s: int = 0) -> dict[str, Any]:
        self._record("get_template_logs", name, version, after=after)
        self.templates.setdefault(name, {"name": name})["status"] = self.build_status
        return {
            "lines": [{"seq": 0, "text": "building"}],
            "next": 0,
            "done": True,
            "status": self.build_status,
        }

    def list_templates(self, limit: int = 100) -> dict[str, Any]:
        self._record("list_templates")
        return {"data": list(self.templates.values())}

    def delete_template(self, name: str) -> dict[str, Any]:
        self._record("delete_template", name)
        self.templates.pop(name, None)
        return {"ok": True}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGroundbox:
    client = FakeGroundbox()
    monkeypatch.setattr(GroundboxSandbox, "_client_factory", lambda: client)
    monkeypatch.setattr(GroundboxSandbox, "_poll_initial_sec", 0.0)
    monkeypatch.setattr(GroundboxSandbox, "_poll_max_sec", 0.0)
    monkeypatch.setattr(GroundboxSandbox, "_build_poll_sec", 0.0)
    monkeypatch.setenv("GROUNDBOX_API_KEY", "gbx_fake")
    return client


@pytest.fixture
def make_env(tmp_path: Path, fake: FakeGroundbox):
    def _make(
        *,
        dockerfile: str | None = "FROM ubuntu:24.04\nWORKDIR /app\n",
        docker_image: str | None = None,
        files: dict[str, str] | None = None,
        network_policy: NetworkPolicy | None = None,
        cpus: float | None = 1,
        memory_mb: int | None = 2048,
        storage_mb: int | None = 10240,
        name: str = "env",
        **kwargs: Any,
    ) -> GroundboxSandbox:
        root = tmp_path / name
        env_dir = root / "environment"
        env_dir.mkdir(parents=True, exist_ok=True)
        if dockerfile is not None:
            (env_dir / "Dockerfile").write_text(dockerfile)
        for rel, text in (files or {}).items():
            path = env_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        trial_paths = TrialPaths(trial_dir=root / "trial")
        trial_paths.mkdir()
        config = EnvironmentConfig(
            docker_image=docker_image,
            cpus=cpus,
            memory_mb=memory_mb,
            storage_mb=storage_mb,
        )
        return GroundboxSandbox(
            environment_dir=env_dir,
            environment_name="hello",
            session_id="hello__abc__env",
            trial_paths=trial_paths,
            task_env_config=config,
            network_policy=network_policy or NetworkPolicy(network_mode=NetworkMode.PUBLIC),
            **kwargs,
        )

    return _make
