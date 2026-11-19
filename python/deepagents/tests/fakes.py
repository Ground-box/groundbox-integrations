"""An in-memory stand-in for `groundbox.Groundbox`, enough for the unit tests."""

from __future__ import annotations

import itertools
from typing import Any

from groundbox import GroundboxError


def _error(code: str, message: str, status: int) -> GroundboxError:
    return GroundboxError(code, message, status, "GET", "/fake")


class FakeGroundbox:
    """Records calls and answers like the Groundbox API does."""

    _ids = itertools.count(1)

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.sandboxes: dict[str, dict[str, Any]] = {}
        self.files: dict[str, dict[str, bytes]] = {}
        self.dirs: dict[str, set[str]] = {}
        self.calls: list[tuple[str, tuple, dict]] = []
        self.create_status = "running"
        self.wait_status = "running"
        self.exec_results: list[dict[str, Any]] = []
        self.output_parts: list[dict[str, Any]] = []
        self.write_error: GroundboxError | None = None

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def called(self, name: str) -> list[tuple[tuple, dict]]:
        return [(args, kwargs) for call, args, kwargs in self.calls if call == name]

    def _get(self, sandbox_id: str) -> dict[str, Any]:
        if sandbox_id not in self.sandboxes:
            raise _error("not_found", f"No sandbox {sandbox_id}", 404)
        return self.sandboxes[sandbox_id]

    # Sandboxes -------------------------------------------------------------

    def create_sandbox(self, **kwargs: Any) -> dict[str, Any]:
        self._record("create_sandbox", **kwargs)
        sandbox_id = f"sbx_fake{next(self._ids)}"
        sandbox = {"id": sandbox_id, "status": self.create_status, **kwargs}
        self.sandboxes[sandbox_id] = sandbox
        self.files[sandbox_id] = {}
        self.dirs[sandbox_id] = {"/", "/home", "/home/runner", "/tmp"}
        return dict(sandbox)

    def get_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        self._record("get_sandbox", sandbox_id)
        return dict(self._get(sandbox_id))

    def wait_for_sandbox(self, sandbox_id: str, timeout_s: float = 1800.0) -> dict[str, Any]:
        self._record("wait_for_sandbox", sandbox_id, timeout_s=timeout_s)
        sandbox = self._get(sandbox_id)
        sandbox["status"] = self.wait_status
        return dict(sandbox)

    def delete_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        self._record("delete_sandbox", sandbox_id)
        sandbox = self._get(sandbox_id)
        sandbox["status"] = "stopped"
        sandbox["stop_reason"] = "deleted"
        return dict(sandbox)

    def resume_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        self._record("resume_sandbox", sandbox_id)
        self._get(sandbox_id)["status"] = "running"
        return dict(self._get(sandbox_id))

    def start_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        self._record("start_sandbox", sandbox_id)
        self._get(sandbox_id)["status"] = "running"
        return dict(self._get(sandbox_id))

    # Commands --------------------------------------------------------------

    def run_command(self, sandbox_id: str, cmd: Any, **kwargs: Any) -> dict[str, Any]:
        self._record("run_command", sandbox_id, cmd, **kwargs)
        self._get(sandbox_id)
        if self.exec_results:
            return self.exec_results.pop(0)
        return {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}

    def start_command(self, sandbox_id: str, cmd: Any, **kwargs: Any) -> dict[str, Any]:
        self._record("start_command", sandbox_id, cmd, **kwargs)
        return {"id": "ex_fake1"}

    def get_command_output(self, sandbox_id: str, exec_id: str, seq: int = 0) -> dict[str, Any]:
        self._record("get_command_output", sandbox_id, exec_id, seq=seq)
        return self.output_parts.pop(0)

    def kill_command(self, sandbox_id: str, exec_id: str) -> dict[str, Any]:
        self._record("kill_command", sandbox_id, exec_id)
        return {}

    # Files -----------------------------------------------------------------

    def write_file(self, sandbox_id: str, path: str, content: bytes) -> dict[str, Any]:
        self._record("write_file", sandbox_id, path)
        if self.write_error is not None:
            raise self.write_error
        if not path.startswith("/"):
            raise _error("bad_request", "Invalid query: path: Use an absolute path", 400)
        if path in self.dirs[sandbox_id]:
            raise _error("bad_request", f"{path} is a folder", 400)
        self.files[sandbox_id][path] = bytes(content)
        return {"path": path}

    def read_file(self, sandbox_id: str, path: str) -> bytes:
        self._record("read_file", sandbox_id, path)
        if not path.startswith("/"):
            raise _error("bad_request", "Invalid query: path: Use an absolute path", 400)
        if path in self.dirs[sandbox_id]:
            raise _error("bad_request", f"{path} is a folder", 400)
        if path not in self.files[sandbox_id]:
            raise _error("not_found", f"No such file or folder: {path}", 404)
        return self.files[sandbox_id][path]

    def make_dir(self, sandbox_id: str, path: str) -> dict[str, Any]:
        self._record("make_dir", sandbox_id, path)
        self.dirs[sandbox_id].add(path)
        return {"created": True}
