"""A fake Groundbox client: records calls and answers from scripted handlers."""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any

from groundbox import GroundboxError


def exec_answer(exit_code: int = 0, stdout: bytes = b"", stderr: bytes = b"", timed_out: bool = False) -> dict:
    """What POST /exec answers with encoding=base64."""
    return {
        "exit_code": exit_code,
        "stdout": base64.b64encode(stdout).decode(),
        "stderr": base64.b64encode(stderr).decode(),
        "duration_ms": 1,
        "timed_out": timed_out,
    }


def api_error(code: str, status: int, message: str = "") -> GroundboxError:
    return GroundboxError(code, message or code, status, "POST", "/test")


PROBE_RUNNER = b"runner\n1001\n/home/runner\n/usr/bin/sudo\n"
PROBE_ROOT = b"root\n0\n/root\n/usr/sbin/runuser\n/bin/su\n"


class FakeGroundbox:
    def __init__(self, probe: bytes = PROBE_RUNNER) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.probe = probe
        self.create_errors: list[GroundboxError] = []
        self.create_status = "running"
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.files: dict[str, bytes] = {}
        self.listed: list[dict] = []
        # (cmd, kwargs) -> answer dict; None falls through to the default.
        self.exec_handler: Callable[[list[str], dict], dict | None] = lambda cmd, kw: None
        self.exec_error: GroundboxError | None = None

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def calls_to(self, name: str) -> list[tuple[tuple, dict]]:
        return [(a, k) for n, a, k in self.calls if n == name]

    # sandboxes
    def create_sandbox(self, **kwargs: Any) -> dict:
        self._record("create_sandbox", **kwargs)
        if self.create_errors:
            raise self.create_errors.pop(0)
        sandbox_id = f"sbx_fake{len(self.created)}"
        self.created.append(sandbox_id)
        return {
            "id": sandbox_id,
            "status": self.create_status,
            "error": "image not found" if self.create_status == "failed" else None,
        }

    def wait_for_sandbox(self, sandbox_id: str, timeout_s: float = 0) -> dict:
        self._record("wait_for_sandbox", sandbox_id)
        return {"id": sandbox_id, "status": self.create_status, "error": "image not found"}

    def delete_sandbox(self, sandbox_id: str) -> dict:
        self._record("delete_sandbox", sandbox_id)
        self.deleted.append(sandbox_id)
        return {"id": sandbox_id, "status": "stopped"}

    def list_sandboxes(self, **kwargs: Any) -> dict:
        self._record("list_sandboxes", **kwargs)
        return {"data": self.listed}

    # commands
    def run_command(self, sandbox_id: str, cmd: Any, **kwargs: Any) -> dict:
        self._record("run_command", sandbox_id, cmd, **kwargs)
        if self.exec_error is not None:
            raise self.exec_error
        answer = self.exec_handler(cmd, kwargs)
        if answer is not None:
            return answer
        if cmd[:2] == ["/bin/sh", "-c"] and "id -un" in cmd[2]:
            return exec_answer(stdout=self.probe)
        return exec_answer()

    # files
    def write_file(self, sandbox_id: str, path: str, content: bytes) -> dict:
        self._record("write_file", sandbox_id, path, len(content))
        self.files[path] = bytes(content)
        return {"path": path, "size_bytes": len(content)}

    def read_file(self, sandbox_id: str, path: str) -> bytes:
        self._record("read_file", sandbox_id, path)
        if path not in self.files:
            raise api_error("not_found", 404, f"No such file or folder: {path}")
        return self.files[path]

    def delete_file(self, sandbox_id: str, path: str, recursive: bool = False) -> dict:
        self._record("delete_file", sandbox_id, path, recursive=recursive)
        return {"deleted": True}

    def expose_port(self, sandbox_id: str, port: int, public: bool | None = None) -> dict:
        self._record("expose_port", sandbox_id, port)
        return {"url": f"https://abc-{port}.groundbox.page"}

    # templates
    def get_template(self, name: str) -> dict:
        self._record("get_template", name)
        raise api_error("not_found", 404, f"No template {name}")

    def create_template(self, name: str, **kwargs: Any) -> dict:
        self._record("create_template", name, **kwargs)
        return {"name": name, "status": "building", "version": None, "latest_build": {"version": 1, "status": "queued"}}

    def follow_template_logs(self, name: str, version: int):
        self._record("follow_template_logs", name, version)
        yield {"seq": 0, "text": "step 1"}
        return {"status": "ready", "next": 1}
