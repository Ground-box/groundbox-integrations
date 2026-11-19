"""A fake Groundbox API for unit tests.

`FakeGroundbox` has the methods of `groundbox.Groundbox` that the client uses. Commands run
for real on the local machine (Linux or macOS) with `subprocess`, and files are real local
files, so the SDK's own shell helpers, tar persistence and path checks are exercised. Tests
give the manifest a temporary root. `FakeStream` is the command stream WebSocket, backed by an
asyncio subprocess.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from groundbox import GroundboxError


def _error(code: str, message: str, status: int, path: str = "/") -> GroundboxError:
    return GroundboxError(code, message, status, "POST", path)


class FakeGroundbox:
    base_url = "https://api.groundbox.test/v1"
    api_key = "gbx_test"

    def __init__(self, home: Path) -> None:
        self.home = home
        self.sandboxes: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.create_status = "running"
        self.fail_next_exec: GroundboxError | None = None
        self.commands: dict[str, dict[str, Any]] = {}
        self.snapshots: dict[str, str] = {}
        self._count = 0

    def _log(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def called(self, name: str) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
        return [(args, kwargs) for call, args, kwargs in self.calls if call == name]

    def _next_id(self, prefix: str) -> str:
        self._count += 1
        return f"{prefix}_{self._count:04d}"

    # -- sandboxes ---------------------------------------------------------

    def create_sandbox(self, idempotency_key: str | None = None, **spec: Any) -> dict[str, Any]:
        self._log("create_sandbox", idempotency_key=idempotency_key, **spec)
        sandbox_id = self._next_id("sbx")
        sandbox = {"id": sandbox_id, "status": self.create_status, "error": None, **spec}
        if self.create_status == "failed":
            sandbox["error"] = "the image does not exist"
        self.sandboxes[sandbox_id] = sandbox
        return dict(sandbox)

    def get_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        self._log("get_sandbox", sandbox_id)
        if sandbox_id not in self.sandboxes:
            raise _error("not_found", f"No sandbox {sandbox_id}", 404)
        return dict(self.sandboxes[sandbox_id])

    def wait_for_sandbox(self, sandbox_id: str, timeout_s: float = 1800.0, poll_s: float = 1.0) -> dict[str, Any]:
        self._log("wait_for_sandbox", sandbox_id)
        sandbox = self.sandboxes[sandbox_id]
        if sandbox["status"] in ("queued", "starting", "resuming"):
            sandbox["status"] = "running"
        if sandbox["status"] == "shutting_down":
            sandbox["status"] = "shut_down"
        return dict(sandbox)

    def delete_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        self._log("delete_sandbox", sandbox_id)
        self.sandboxes[sandbox_id]["status"] = "stopped"
        return dict(self.sandboxes[sandbox_id])

    def pause_sandbox(self, sandbox_id: str, wake_on_request: bool | None = None) -> dict[str, Any]:
        self._log("pause_sandbox", sandbox_id)
        self.sandboxes[sandbox_id]["status"] = "paused"
        return dict(self.sandboxes[sandbox_id])

    def resume_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        self._log("resume_sandbox", sandbox_id)
        self.sandboxes[sandbox_id]["status"] = "resuming"
        return dict(self.sandboxes[sandbox_id])

    def start_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        self._log("start_sandbox", sandbox_id)
        self.sandboxes[sandbox_id]["status"] = "starting"
        return dict(self.sandboxes[sandbox_id])

    def keep_snapshot(self, sandbox_id: str, **kwargs: Any) -> dict[str, Any]:
        self._log("keep_snapshot", sandbox_id, **kwargs)
        snapshot_id = self._next_id("snap")
        self.snapshots[snapshot_id] = sandbox_id
        return {"id": snapshot_id, "status": "ready", "sandbox_id": sandbox_id}

    def expose_port(self, sandbox_id: str, port: int, public: bool | None = None) -> dict[str, Any]:
        self._log("expose_port", sandbox_id, port, public=public)
        url = f"https://abc123-{port}.groundbox.page/"
        return {"port": port, "public": bool(public), "url": url, "share_url": f"{url}?gb_token=gbp_secret"}

    # -- commands ----------------------------------------------------------

    def _check_cwd(self, cwd: str | None) -> str:
        if cwd is not None and not Path(cwd).is_dir():
            raise _error("bad_request", f"cwd {cwd} is not a folder", 400)
        return cwd or str(self.home)

    def run_command(
        self,
        sandbox_id: str,
        cmd: list[str] | str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_s: int | None = None,
        stdin: Any = None,
        encoding: str | None = None,
    ) -> dict[str, Any]:
        self._log("run_command", sandbox_id, cmd, cwd=cwd, env=env, timeout_s=timeout_s)
        if self.fail_next_exec is not None:
            error, self.fail_next_exec = self.fail_next_exec, None
            raise error
        argv = ["sh", "-c", cmd] if isinstance(cmd, str) else list(cmd)
        argv = self._local_argv(argv)
        directory = self._check_cwd(cwd)
        try:
            done = subprocess.run(
                argv,
                cwd=directory,
                env={**os.environ, **(env or {})},
                capture_output=True,
                timeout=timeout_s,
            )
            out, err, code, timed_out = done.stdout, done.stderr, done.returncode, False
        except subprocess.TimeoutExpired as expired:
            out, err, code, timed_out = expired.stdout or b"", expired.stderr or b"", 137, True
        encode = (lambda b: base64.b64encode(b).decode()) if encoding == "base64" else (lambda b: b.decode())
        return {"exit_code": code, "stdout": encode(out), "stderr": encode(err), "timed_out": timed_out}

    @staticmethod
    def _local_argv(argv: list[str]) -> list[str]:
        # `sudo -u <user> --` (the SDK's user= prefix) runs as the current user here.
        if argv[:2] == ["sudo", "-u"] and len(argv) > 3 and argv[3] == "--":
            return argv[4:]
        return argv

    def start_command(
        self,
        sandbox_id: str,
        cmd: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_s: int | None = None,
        stdin: bool | None = None,
    ) -> dict[str, Any]:
        self._log("start_command", sandbox_id, cmd, cwd=cwd, timeout_s=timeout_s)
        directory = self._check_cwd(cwd)
        exec_id = self._next_id("ex")
        record: dict[str, Any] = {"stdout": b"", "stderr": b"", "done": False, "exit_code": None}
        self.commands[exec_id] = record

        def run() -> None:
            done = subprocess.run(
                self._local_argv(list(cmd)),
                cwd=directory,
                env={**os.environ, **(env or {})},
                capture_output=True,
            )
            record.update(stdout=done.stdout, stderr=done.stderr, exit_code=done.returncode, done=True)

        threading.Thread(target=run, daemon=True).start()
        return {"id": exec_id, "state": "running"}

    def get_command_output(self, sandbox_id: str, exec_id: str, seq: int = 0, encoding: str | None = None) -> dict[str, Any]:
        record = self.commands[exec_id]
        if not record["done"]:
            return {"stdout": "", "stderr": "", "seq": seq, "more": False, "done": False}
        if seq:
            return {"stdout": "", "stderr": "", "seq": seq, "more": False, "done": True, "exit_code": record["exit_code"]}
        return {
            "stdout": base64.b64encode(record["stdout"]).decode(),
            "stderr": base64.b64encode(record["stderr"]).decode(),
            "seq": 1,
            "more": False,
            "done": True,
            "exit_code": record["exit_code"],
            "timed_out": False,
        }

    def kill_command(self, sandbox_id: str, exec_id: str, signal: str | None = None) -> dict[str, Any]:
        self._log("kill_command", sandbox_id, exec_id)
        return {"id": exec_id}

    # -- files -------------------------------------------------------------

    def read_file(self, sandbox_id: str, path: str) -> bytes:
        self._log("read_file", sandbox_id, path)
        target = Path(path)
        if target.is_dir():
            raise _error("bad_request", f"{path} is a folder", 400)
        if not target.exists():
            raise _error("not_found", f"No such file or folder: {path}", 404)
        return target.read_bytes()

    def write_file(self, sandbox_id: str, path: str, content: bytes | str) -> dict[str, Any]:
        self._log("write_file", sandbox_id, path)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode() if isinstance(content, str) else content)
        return {"path": path}

    def make_dir(self, sandbox_id: str, path: str) -> dict[str, Any]:
        self._log("make_dir", sandbox_id, path)
        Path(path).mkdir(parents=True, exist_ok=True)
        return {"created": True}

    def delete_file(self, sandbox_id: str, path: str, recursive: bool = False) -> dict[str, Any]:
        self._log("delete_file", sandbox_id, path)
        target = Path(path)
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        return {"deleted": True}


class FakeStream:
    """The `/exec/stream` WebSocket, running the command as a local subprocess."""

    def __init__(self, gb: FakeGroundbox) -> None:
        self.gb = gb
        self.sent: list[dict[str, Any]] = []
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.process: asyncio.subprocess.Process | None = None
        self.closed = False
        self._seq = 0

    async def send(self, raw: str) -> None:
        message = json.loads(raw)
        self.sent.append(message)
        kind = message["type"]
        if kind == "start":
            await self._start(message)
        elif kind == "stdin" and self.process is not None and self.process.stdin is not None:
            self.process.stdin.write(message["data"].encode())
            await self.process.stdin.drain()
        elif kind == "kill" and self.process is not None and self.process.returncode is None:
            self.process.kill()

    async def _start(self, message: dict[str, Any]) -> None:
        self.process = await asyncio.create_subprocess_exec(
            *FakeGroundbox._local_argv(message["args"]),
            cwd=self.gb._check_cwd(message.get("cwd")),
            env={**os.environ, **(message.get("env") or {})},
            stdin=asyncio.subprocess.PIPE if message.get("stdin") else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await self.inbox.put(json.dumps({"type": "started", "exec_id": "ex_stream"}))
        started = time.monotonic()

        async def pump(stream: asyncio.StreamReader, kind: str) -> None:
            while chunk := await stream.read(4096):
                self._seq += len(chunk)
                await self.inbox.put(json.dumps({"type": kind, "data": chunk.decode(), "seq": self._seq}))

        async def finish() -> None:
            assert self.process is not None and self.process.stdout and self.process.stderr
            await asyncio.gather(pump(self.process.stdout, "stdout"), pump(self.process.stderr, "stderr"))
            code = await self.process.wait()
            duration = int((time.monotonic() - started) * 1000)
            await self.inbox.put(json.dumps({"type": "exit", "exit_code": 137 if code < 0 else code, "duration_ms": duration}))
            await self.inbox.put(None)

        self._task = asyncio.create_task(finish())

    async def recv(self) -> str:
        item = await self.inbox.get()
        if item is None:
            raise ConnectionError("closed")
        return item

    def __aiter__(self) -> FakeStream:
        return self

    async def __anext__(self) -> str:
        item = await self.inbox.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self) -> None:
        self.closed = True
        await self.inbox.put(None)
