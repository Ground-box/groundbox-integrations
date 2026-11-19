"""A stand-in for `groundbox.Groundbox` that runs sandboxes on the host, for tests without the API.

Commands run as host processes and the file API acts on host paths, so `host_root` plays the
sandbox's home directory. Each method copies the real API's observable behaviour, including its
quirks: `stat` describes a symlink itself, an upload replaces a symlink, errors carry the API's
codes, statuses and messages, and a call on a sandbox that is not running answers `409` with its
status in `details`. POSIX hosts only.
"""

from __future__ import annotations

import base64
import os
import shutil
import signal
import stat as stat_module
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from groundbox import GroundboxError

Json = dict[str, Any]


def _error(status: int, code: str, message: str, details: Json | None = None) -> GroundboxError:
    return GroundboxError(code, message, status, 'FAKE', '/fake', None, details)


@dataclass
class _Command:
    process: subprocess.Popen[bytes] | None
    started: float
    timeout_s: float
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    exit_code: int | None = None
    timed_out: bool = False
    readers: list[threading.Thread] = field(default_factory=list)


@dataclass
class _Sandbox:
    id: str
    status: str
    options: Json
    statuses: list[str] = field(default_factory=list)
    stop_reason: str | None = None
    commands: dict[str, _Command] = field(default_factory=dict)

    def describe(self) -> Json:
        return {'id': self.id, 'status': self.status, 'stop_reason': self.stop_reason, **self.options}


class FakeGroundbox:
    """The subset of `groundbox.Groundbox` the backend calls."""

    def __init__(self, host_root: Path, *, api_key: str | None = 'gbx_fake') -> None:
        self.api_key = api_key
        self.host_root = host_root
        self.sandboxes: dict[str, _Sandbox] = {}
        self.idempotency: dict[str, str] = {}
        self.calls: list[tuple[str, tuple[Any, ...], Json]] = []
        # A failure (or a callable raising one) a method raises on its next call(s), for error tests.
        self.fail: dict[str, list[GroundboxError | Callable[[], None]]] = {}
        # Statuses a new sandbox reports on its first reads, before `running`.
        self.create_statuses: list[str] = []
        self.create_delay = 0.0
        self.lock = threading.Lock()

    # -- plumbing -----------------------------------------------------------------------------

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        with self.lock:
            self.calls.append((name, args, kwargs))
            queue = self.fail.get(name)
            failure = queue.pop(0) if queue else None
        if isinstance(failure, GroundboxError):
            raise failure
        if failure is not None:
            failure()

    def called(self, name: str) -> list[tuple[tuple[Any, ...], Json]]:
        return [(args, kwargs) for called, args, kwargs in self.calls if called == name]

    def _sandbox(self, sandbox_id: str) -> _Sandbox:
        sandbox = self.sandboxes.get(sandbox_id)
        if sandbox is None:
            raise _error(404, 'not_found', f'No sandbox {sandbox_id}')
        return sandbox

    def _running(self, sandbox_id: str) -> _Sandbox:
        sandbox = self._sandbox(sandbox_id)
        if sandbox.status != 'running':
            details: Json = {'status': sandbox.status, 'stop_reason': sandbox.stop_reason}
            raise _error(409, 'conflict', f'Sandbox {sandbox_id} is {sandbox.status}, not running', details)
        return sandbox

    @staticmethod
    def _absolute(path: str) -> None:
        if not path.startswith('/'):
            raise _error(400, 'bad_request', 'Invalid query: path: Use an absolute path')

    # -- sandboxes ----------------------------------------------------------------------------

    def create_sandbox(self, idempotency_key: str | None = None, **options: Any) -> Json:
        self._record('create_sandbox', idempotency_key=idempotency_key, **options)
        if self.create_delay:
            time.sleep(self.create_delay)
        with self.lock:
            if idempotency_key is not None and idempotency_key in self.idempotency:
                return self.sandboxes[self.idempotency[idempotency_key]].describe()
            sandbox_id = f'sbx_{uuid.uuid4().hex[:16]}'
            statuses = list(self.create_statuses)
            sandbox = _Sandbox(
                id=sandbox_id, status=statuses.pop(0) if statuses else 'running', options=options, statuses=statuses
            )
            self.sandboxes[sandbox_id] = sandbox
            if idempotency_key is not None:
                self.idempotency[idempotency_key] = sandbox_id
        return sandbox.describe()

    def get_sandbox(self, sandbox_id: str) -> Json:
        self._record('get_sandbox', sandbox_id)
        sandbox = self._sandbox(sandbox_id)
        if sandbox.statuses:
            sandbox.status = sandbox.statuses.pop(0)
        elif sandbox.status in ('queued', 'starting', 'resuming'):
            sandbox.status = 'running'
        return sandbox.describe()

    def resume_sandbox(self, sandbox_id: str) -> Json:
        self._record('resume_sandbox', sandbox_id)
        sandbox = self._sandbox(sandbox_id)
        if sandbox.status != 'paused':
            raise _error(
                409, 'conflict', f'Sandbox {sandbox_id} is {sandbox.status}, not paused', {'status': sandbox.status}
            )
        sandbox.status = 'resuming'
        return sandbox.describe()

    def start_sandbox(self, sandbox_id: str) -> Json:
        self._record('start_sandbox', sandbox_id)
        sandbox = self._sandbox(sandbox_id)
        if sandbox.status != 'shut_down':
            raise _error(409, 'conflict', f'Sandbox {sandbox_id} is {sandbox.status}', {'status': sandbox.status})
        sandbox.status = 'starting'
        return sandbox.describe()

    def pause_sandbox(self, sandbox_id: str) -> Json:
        self._record('pause_sandbox', sandbox_id)
        self._running(sandbox_id).status = 'paused'
        return self._sandbox(sandbox_id).describe()

    def stop_sandbox(self, sandbox_id: str) -> Json:
        self._record('stop_sandbox', sandbox_id)
        sandbox = self._running(sandbox_id)
        self._kill_all(sandbox)
        sandbox.status = 'shut_down'
        return sandbox.describe()

    def delete_sandbox(self, sandbox_id: str) -> Json:
        self._record('delete_sandbox', sandbox_id)
        sandbox = self._sandbox(sandbox_id)
        self._kill_all(sandbox)
        sandbox.status, sandbox.stop_reason = 'stopped', 'deleted'
        return sandbox.describe()

    def _kill_all(self, sandbox: _Sandbox) -> None:
        for command in sandbox.commands.values():
            if command.process is not None and command.process.poll() is None:
                try:
                    os.killpg(command.process.pid, signal.SIGKILL)
                except ProcessLookupError:  # pragma: no cover
                    pass
                command.process.wait()

    # -- commands -----------------------------------------------------------------------------

    def _env(self, env: Json | None) -> Json:
        return {
            'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
            'HOME': str(self.host_root),
            'LC_ALL': 'C',
            **(env or {}),
        }

    def _cwd(self, cwd: str | None) -> str:
        cwd = cwd or str(self.host_root)
        if not os.path.isdir(cwd):
            raise _error(400, 'bad_request', f'cwd {cwd} is not a folder')
        return cwd

    def run_command(
        self,
        sandbox_id: str,
        cmd: str | list[str],
        cwd: str | None = None,
        env: Json | None = None,
        timeout_s: int | None = None,
        stdin: str | bytes | None = None,
        encoding: str | None = None,
    ) -> Json:
        self._record('run_command', sandbox_id, cmd, cwd=cwd, env=env, timeout_s=timeout_s)
        self._running(sandbox_id)
        argv = ['/bin/sh', '-c', cmd] if isinstance(cmd, str) else list(cmd)
        try:
            done = subprocess.run(
                argv,
                cwd=self._cwd(cwd),
                env=self._env(env),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout_s or 60,
            )
        except FileNotFoundError:
            return {
                'exit_code': 127,
                'stdout': '',
                'stderr': f'Could not start the command: spawn {argv[0]} ENOENT\n',
                'timed_out': False,
            }
        except subprocess.TimeoutExpired:
            return {'exit_code': 137, 'stdout': '', 'stderr': '', 'timed_out': True}
        out, err = done.stdout, done.stderr
        if encoding == 'base64':
            return {
                'exit_code': done.returncode,
                'stdout': base64.b64encode(out).decode(),
                'stderr': base64.b64encode(err).decode(),
                'timed_out': False,
            }
        return {
            'exit_code': done.returncode,
            'stdout': out.decode(errors='replace'),
            'stderr': err.decode(errors='replace'),
            'timed_out': False,
        }

    def start_command(
        self,
        sandbox_id: str,
        cmd: str | list[str],
        cwd: str | None = None,
        env: Json | None = None,
        timeout_s: int | None = None,
        stdin: bool | None = None,
    ) -> Json:
        self._record('start_command', sandbox_id, cmd, cwd=cwd, env=env, timeout_s=timeout_s)
        sandbox = self._running(sandbox_id)
        argv = ['/bin/sh', '-c', cmd] if isinstance(cmd, str) else list(cmd)
        exec_id = f'ex_{uuid.uuid4().hex[:20]}'
        try:
            process = subprocess.Popen(
                argv,
                cwd=self._cwd(cwd),
                env=self._env(env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError:
            command = _Command(process=None, started=time.monotonic(), timeout_s=timeout_s or 3600, exit_code=127)
            command.stderr += f'Could not start the command: spawn {argv[0]} ENOENT\n'.encode()
            sandbox.commands[exec_id] = command
            return {'id': exec_id, 'state': 'exited'}
        command = _Command(process=process, started=time.monotonic(), timeout_s=timeout_s or 3600)

        def pump(stream: Any, sink: bytearray) -> None:
            for chunk in iter(lambda: stream.read1(65536), b''):
                with self.lock:
                    sink.extend(chunk)

        for stream, sink in ((process.stdout, command.stdout), (process.stderr, command.stderr)):
            reader = threading.Thread(target=pump, args=(stream, sink), daemon=True)
            reader.start()
            command.readers.append(reader)
        sandbox.commands[exec_id] = command
        return {'id': exec_id, 'state': 'running'}

    def get_command_output(self, sandbox_id: str, exec_id: str, seq: int = 0, encoding: str | None = None) -> Json:
        self._record('get_command_output', sandbox_id, exec_id, seq=seq, encoding=encoding)
        sandbox = self._running(sandbox_id)
        command = sandbox.commands[exec_id]
        process = command.process
        if process is not None and command.exit_code is None:
            code = process.poll()
            if code is None and time.monotonic() - command.started > command.timeout_s:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                command.timed_out, code = True, 137
            if code is not None:
                # Like the sandbox: done when the command itself exits, with what it wrote by then.
                for reader in command.readers:
                    reader.join(timeout=0.2)
                command.exit_code = 128 - code if code < 0 else code
        with self.lock:
            # `seq` counts bytes of both streams; the fake hands out everything new at once.
            out_seen, err_seen = (seq >> 32), seq & 0xFFFFFFFF
            out, err = bytes(command.stdout[out_seen:]), bytes(command.stderr[err_seen:])
            next_seq = (len(command.stdout) << 32) | len(command.stderr)
        done = command.exit_code is not None
        return {
            'stdout': base64.b64encode(out).decode(),
            'stderr': base64.b64encode(err).decode(),
            'seq': next_seq,
            'skipped_bytes': 0,
            'more': False,
            'done': done,
            'exit_code': command.exit_code,
            'timed_out': command.timed_out,
        }

    def kill_command(self, sandbox_id: str, exec_id: str, signal_name: str | None = None) -> Json:
        self._record('kill_command', sandbox_id, exec_id)
        sandbox = self._running(sandbox_id)
        command = sandbox.commands[exec_id]
        if command.process is not None and command.process.poll() is None:
            os.killpg(command.process.pid, signal.SIGKILL)
            command.process.wait()  # Reaped, as the sandbox's own supervisor would, so no zombie answers `kill -0`.
        return {'id': exec_id}

    # -- files --------------------------------------------------------------------------------

    @staticmethod
    def _entry(path: str) -> Json:
        info = os.lstat(path)
        if stat_module.S_ISLNK(info.st_mode):
            kind = 'symlink'
        elif stat_module.S_ISDIR(info.st_mode):
            kind = 'dir'
        elif stat_module.S_ISREG(info.st_mode):
            kind = 'file'
        else:
            kind = 'other'
        return {
            'name': os.path.basename(path.rstrip('/')) or '/',
            'path': path,
            'type': kind,
            'size_bytes': info.st_size,
        }

    def stat_file(self, sandbox_id: str, path: str) -> Json:
        self._record('stat_file', sandbox_id, path)
        self._running(sandbox_id)
        self._absolute(path)
        try:
            return self._entry(path)
        except (FileNotFoundError, NotADirectoryError):
            raise _error(404, 'not_found', f'No such file or folder: {path}') from None

    def list_files(self, sandbox_id: str, dir: str) -> Json:  # noqa: A002
        self._record('list_files', sandbox_id, dir)
        self._running(sandbox_id)
        self._absolute(dir)
        if not os.path.lexists(dir):
            raise _error(404, 'not_found', f'No such file or folder: {dir}')
        if not os.path.isdir(dir):
            raise _error(400, 'bad_request', f'Not a folder: {dir}')
        return {'dir': dir, 'entries': [self._entry(os.path.join(dir, name)) for name in sorted(os.listdir(dir))]}

    def read_file(self, sandbox_id: str, path: str) -> bytes:
        self._record('read_file', sandbox_id, path)
        self._running(sandbox_id)
        self._absolute(path)
        if os.path.isdir(path):
            raise _error(400, 'bad_request', f'{path} is a folder')
        try:
            return Path(path).read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            raise _error(404, 'not_found', f'No such file or folder: {path}') from None

    def write_file(self, sandbox_id: str, path: str, content: str | bytes) -> Json:
        self._record('write_file', sandbox_id, path)
        self._running(sandbox_id)
        self._absolute(path)
        data = content.encode() if isinstance(content, str) else bytes(content)
        if os.path.isdir(path) and not os.path.islink(path):
            raise _error(400, 'bad_request', f'{path} is a folder')
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except (FileExistsError, NotADirectoryError):
            reason = f"EEXIST: file already exists, mkdir '{os.path.dirname(path)}'"
            raise _error(502, 'internal', 'The sandbox could not do that', {'reason': reason}) from None
        if os.path.islink(path):
            os.unlink(path)  # Like the API: an upload replaces a symlink.
        Path(path).write_bytes(data)
        return {'path': path, 'size_bytes': len(data)}

    def make_dir(self, sandbox_id: str, path: str) -> Json:
        self._record('make_dir', sandbox_id, path)
        self._running(sandbox_id)
        self._absolute(path)
        if os.path.lexists(path) and not os.path.isdir(path):
            raise _error(409, 'conflict', f'{path} is there already and is not a folder')
        existed = os.path.isdir(path)
        try:
            os.makedirs(path, exist_ok=True)
        except (FileExistsError, NotADirectoryError):
            raise _error(400, 'bad_request', f'Cannot make {path}: Not a directory') from None
        except PermissionError:
            raise _error(400, 'bad_request', f'Cannot make {path}: Permission denied') from None
        return {'path': path, 'created': not existed}

    def delete_file(self, sandbox_id: str, path: str, recursive: bool = False) -> Json:
        self._record('delete_file', sandbox_id, path, recursive=recursive)
        self._running(sandbox_id)
        self._absolute(path)
        if not os.path.lexists(path):
            return {'path': path, 'deleted': False}
        if os.path.islink(path) or not os.path.isdir(path):
            os.unlink(path)
        elif recursive:
            shutil.rmtree(path)
        elif os.listdir(path):
            raise _error(409, 'conflict', f'{path} is a folder with files in it. Pass recursive=true to delete it')
        else:
            os.rmdir(path)
        return {'path': path, 'deleted': True}
