"""GroundboxExecutor: run smolagents' CodeAgent code in a Groundbox sandbox."""

from __future__ import annotations

import base64
import json
import uuid
from importlib import resources
from io import BytesIO
from typing import Any, Optional

from smolagents.local_python_executor import CodeOutput
from smolagents.monitoring import AgentLogger, LogLevel
from smolagents.remote_executors import RemotePythonExecutor
from smolagents.utils import AgentError

__all__ = ["GroundboxExecutor"]

KERNEL_DIR = "/tmp/smolagents-groundbox"
DEFAULT_TIMEOUT_MINUTES = 60
# POST /exec waits for a command for up to 60 minutes.
MAX_EXEC_SECONDS = 3600


def _kernel_source() -> str:
    return resources.files("smolagents_groundbox").joinpath("_kernel.py").read_text(encoding="utf-8")


class GroundboxExecutor(RemotePythonExecutor):
    """
    Remote Python code executor in a Groundbox sandbox (a Firecracker microVM).

    Pass it to `CodeAgent(..., executor=GroundboxExecutor())`. Each code action runs in one
    long-lived Python process in the sandbox, so variables, imports and functions carry over
    from one step to the next, as with smolagents' built-in E2B, Modal, Docker and Blaxel
    executors.

    Args:
        additional_imports (`list[str]`, *optional*): Python packages to pip install in the sandbox
            before the first step.
        logger (`AgentLogger`, *optional*): Logger for output and errors. Defaults to a new
            `AgentLogger(level=LogLevel.INFO)`.
        allow_pickle (`bool`, default `False`): Whether to allow pickle serialization for objects that
            cannot be safely serialized to JSON (variables sent in and final answers sent back).
            **Security Warning:** pickle deserialization can execute arbitrary code. Leave it off unless you
            trust the sandbox and need custom types.
        api_key (`str`, *optional*): A Groundbox API key. Defaults to the `GROUNDBOX_API_KEY` environment variable.
        base_url (`str`, *optional*): The API's base URL. Defaults to `GROUNDBOX_API_URL`, then
            `https://api.groundbox.dev/v1`.
        create_kwargs (`dict`, *optional*): Keyword arguments for `Groundbox.create_sandbox`, such as
            `cpus`, `memory_gb`, `image="python:3.12-slim"`, `template`, `env`, `allow_hosts`, `secrets`
            or `timeout_minutes` (60 unless you set it).
        sandbox_id (`str`, *optional*): Use this running sandbox instead of creating one. It is left
            running on cleanup unless `delete_on_cleanup=True`.
        timeout_seconds (`int`, *optional*, default `300`): Longest one code action may run. Past it the
            code is interrupted with an `ExecutionTimeout` error and the namespace is kept. `None` waits
            for up to an hour.
        python (`str`, default `"python3"`): The Python interpreter in the sandbox.
        delete_on_cleanup (`bool`, *optional*): Delete the sandbox on `cleanup()`. Defaults to `True` for a
            sandbox this executor created and `False` for one passed as `sandbox_id`.
        client (`groundbox.Groundbox`, *optional*): A ready Groundbox client; `api_key` and `base_url` are
            then ignored.
    """

    def __init__(
        self,
        additional_imports: Optional[list[str]] = None,
        logger: Optional[AgentLogger] = None,
        allow_pickle: bool = False,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        create_kwargs: Optional[dict[str, Any]] = None,
        sandbox_id: Optional[str] = None,
        timeout_seconds: Optional[int] = 300,
        python: str = "python3",
        delete_on_cleanup: Optional[bool] = None,
        client: Any = None,
    ):
        super().__init__(additional_imports or [], logger or AgentLogger(level=LogLevel.INFO), allow_pickle)
        if client is None:
            try:
                from groundbox import Groundbox
            except ModuleNotFoundError:
                raise ModuleNotFoundError(
                    "Please install the Groundbox SDK to use GroundboxExecutor: `pip install groundbox`"
                )
            client = Groundbox(api_key=api_key, base_url=base_url)
        self.client = client
        self.timeout_seconds = timeout_seconds
        self.python = python
        self._cleaned_up = False
        self._kernel_command_id: Optional[str] = None
        self._kernel_path = f"{KERNEL_DIR}/kernel.py"
        self._socket_path = f"{KERNEL_DIR}/kernel-{uuid.uuid4().hex[:12]}.sock"
        self._owns_sandbox = sandbox_id is None
        self.delete_on_cleanup = self._owns_sandbox if delete_on_cleanup is None else delete_on_cleanup

        try:
            if sandbox_id is None:
                options: dict[str, Any] = {
                    "timeout_minutes": DEFAULT_TIMEOUT_MINUTES,
                    "metadata": {"created_by": "smolagents-groundbox"},
                    **(create_kwargs or {}),
                }
                sandbox = self.client.create_sandbox(**options)
                self.sandbox_id = sandbox["id"]
                self.logger.log(f"Started Groundbox sandbox {self.sandbox_id}", level=LogLevel.INFO)
            else:
                sandbox = self.client.get_sandbox(sandbox_id)
                self.sandbox_id = sandbox_id
            if sandbox.get("status") != "running":
                sandbox = self.client.wait_for_sandbox(self.sandbox_id)
            if sandbox.get("status") not in ("running", "paused"):
                # A paused sandbox wakes on the next command if it was paused with wake_on_request.
                raise RuntimeError(
                    f"Sandbox {self.sandbox_id} is {sandbox.get('status')}: {sandbox.get('error') or 'not running'}"
                )
            self._start_kernel()
            self.installed_packages = self.install_packages(self.additional_imports)
            self.logger.log("Groundbox is running", level=LogLevel.INFO)
        except Exception as e:
            self.cleanup()
            raise RuntimeError(f"Failed to initialize Groundbox sandbox: {e}") from e

    # ------------------------------------------------------------------
    # The kernel in the sandbox
    # ------------------------------------------------------------------

    def _start_kernel(self) -> None:
        self.client.write_file(self.sandbox_id, self._kernel_path, _kernel_source())
        command = self.client.start_command(
            self.sandbox_id, [self.python, "-u", self._kernel_path, "serve", self._socket_path]
        )
        self._kernel_command_id = command.get("id")
        answer = self._request({"ping": True}, exec_timeout=60)
        if not answer.get("pong"):
            raise RuntimeError(f"The Python kernel did not start in the sandbox: {self._kernel_failure()}")
        self.logger.log(f"Python {answer.get('python')} kernel ready", level=LogLevel.DEBUG)

    def _kernel_failure(self) -> str:
        """What the kernel printed before it ended, to explain a failed start."""
        if not self._kernel_command_id:
            return "no command id"
        try:
            out = self.client.get_command_output(self.sandbox_id, self._kernel_command_id)
            return (out.get("stderr") or out.get("stdout") or "no output").strip()[-2000:]
        except Exception as e:  # pragma: no cover - best effort
            return str(e)

    def _request(self, payload: dict[str, Any], exec_timeout: Optional[int] = None) -> dict[str, Any]:
        if exec_timeout is None:
            exec_timeout = MAX_EXEC_SECONDS
            if self.timeout_seconds:
                exec_timeout = min(MAX_EXEC_SECONDS, int(self.timeout_seconds) + 60)
        result = self.client.run_command(
            self.sandbox_id,
            [self.python, self._kernel_path, "run", self._socket_path],
            stdin=json.dumps(payload),
            timeout_s=exec_timeout,
        )
        stdout = (result.get("stdout") or "").strip()
        if result.get("exit_code") != 0 or not stdout:
            if result.get("timed_out"):
                raise AgentError(f"Code execution did not finish within {exec_timeout} seconds", self.logger)
            raise AgentError(
                f"Could not reach the Python kernel in the sandbox (exit code {result.get('exit_code')}):\n"
                f"{result.get('stderr') or stdout}",
                self.logger,
            )
        return json.loads(stdout.splitlines()[-1])

    def _restart_kernel(self) -> None:
        if self._kernel_command_id:
            try:
                self.client.kill_command(self.sandbox_id, self._kernel_command_id)
            except Exception:
                pass
        self._socket_path = f"{KERNEL_DIR}/kernel-{uuid.uuid4().hex[:12]}.sock"
        self._start_kernel()

    # ------------------------------------------------------------------
    # RemotePythonExecutor
    # ------------------------------------------------------------------

    def run_code_raise_errors(self, code: str) -> CodeOutput:
        """
        Execute Python code in the Groundbox sandbox and return the result.

        Args:
            code (`str`): Python code to execute.

        Returns:
            `CodeOutput`: Code output containing the result, logs, and whether it is the final answer.
        """
        answer = self._request({"code": code, "timeout_s": self.timeout_seconds})

        if answer.get("kernel_unavailable"):
            self._restart_kernel()
            raise AgentError(
                "The Python process in the sandbox ended (os._exit, a crash or out of memory) and was "
                "restarted: variables, imports and tools from earlier steps are gone.",
                self.logger,
            )

        logs = answer.get("logs") or ""

        if answer.get("final_answer") is not None:
            final_answer = self._deserialize_final_answer(answer["final_answer"], self.allow_pickle)
            return CodeOutput(output=final_answer, logs=logs, is_final_answer=True)

        error = answer.get("error")
        if error:
            error_message = (
                f"{logs}\n"
                f"Executing code yielded an error:\n"
                f"{error.get('name')}\n"
                f"{error.get('value')}\n"
                f"{error.get('traceback')}"
            )
            raise AgentError(error_message, self.logger)

        output = answer.get("output")
        if output is not None and answer.get("output_type") == "image/png":
            import PIL.Image

            output = PIL.Image.open(BytesIO(base64.b64decode(output)))
        return CodeOutput(output=output, logs=logs, is_final_answer=False)

    def install_packages(self, additional_imports: list[str]) -> list[str]:
        """Pip install packages in the sandbox. Returns the ones installed, or [] if pip failed."""
        if not additional_imports:
            return []
        self.logger.log(f"Installing packages: {', '.join(additional_imports)}", level=LogLevel.INFO)
        base = [self.python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check"]
        # Debian and Ubuntu mark the system Python as externally managed (PEP 668).
        result = self.client.run_command(
            self.sandbox_id, base + ["--break-system-packages", *additional_imports], timeout_s=900
        )
        if result.get("exit_code") != 0 and "--break-system-packages" in (result.get("stderr") or ""):
            # pip older than 23.0.1 does not know the flag.
            result = self.client.run_command(self.sandbox_id, base + list(additional_imports), timeout_s=900)
        if result.get("exit_code") != 0:
            self.logger.log_error(
                f"Failed to install packages (exit code {result.get('exit_code')}): {result.get('stderr')}"
            )
            return []
        self.logger.log(f"Successfully installed packages: {', '.join(additional_imports)}", level=LogLevel.INFO)
        return list(additional_imports)

    def cleanup(self):
        """Stop the kernel and delete the sandbox (if this executor created it)."""
        if self._cleaned_up:
            return
        self._cleaned_up = True
        sandbox_id = getattr(self, "sandbox_id", None)
        if sandbox_id is None:
            return
        try:
            if self.delete_on_cleanup:
                self.logger.log(f"Deleting Groundbox sandbox {sandbox_id}...", level=LogLevel.INFO)
                self.client.delete_sandbox(sandbox_id)
                self.logger.log("Sandbox cleanup completed", level=LogLevel.INFO)
            elif self._kernel_command_id:
                self.client.kill_command(sandbox_id, self._kernel_command_id)
        except Exception as e:
            self.logger.log_error(f"Error during cleanup: {e}")

    def delete(self):
        """Ensure cleanup on deletion."""
        self.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.cleanup()

    def __del__(self):
        try:
            self.cleanup()
        except Exception:
            pass
