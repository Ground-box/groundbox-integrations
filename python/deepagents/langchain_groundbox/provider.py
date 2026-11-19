"""Groundbox sandbox provider for Deep Agents Code (`dcode --sandbox groundbox`)."""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import Callable
from typing import Any, NoReturn

from deepagents_code.integrations.sandbox_provider import (
    SandboxInstallHint,
    SandboxNotFoundError,
    SandboxProvider,
    SandboxProviderMetadata,
)
from groundbox import Groundbox, GroundboxError

from langchain_groundbox.sandbox import (
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_WORKDIR,
    GroundboxSandbox,
    _with_retry,
)

PROVIDER_NAME = "groundbox"
PACKAGE_NAME = "langchain-groundbox"
IMAGE_WORKDIR = "/root"
"""Commands from a Docker `image` run as root, in `/root`."""

DEFAULT_STARTUP_TIMEOUT = 180
"""Seconds to wait for a queued sandbox to start (a new Docker image can take minutes)."""

SNAPSHOT_ID_PREFIX = "snap_"

EnvResolver = Callable[[str], str | None]
ClientFactory = Callable[..., Groundbox]

_STARTING = ("queued", "starting", "resuming")
_ENDED = ("stopped", "failed")


def _default_resolve_env_var(name: str) -> str | None:
    """Read `name`, preferring the `DEEPAGENTS_CODE_`-prefixed form as dcode does."""
    if not name.startswith("DEEPAGENTS_CODE_"):
        prefixed = f"DEEPAGENTS_CODE_{name}"
        if prefixed in os.environ:
            return os.environ[prefixed] or None
    return os.environ.get(name) or None


def _resolve_int(value: object, *, name: str, allow_zero: bool = False) -> int | None:
    if value is None or value == "":
        return None
    try:
        resolved = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        msg = f"{name} must be an integer"
        raise ValueError(msg) from exc
    if resolved < (0 if allow_zero else 1):
        msg = f"{name} must be {'non-negative' if allow_zero else 'positive'}"
        raise ValueError(msg)
    return resolved


def _raise_unsupported_kwargs(kwargs: dict[str, object]) -> NoReturn:
    msg = f"Received unsupported arguments: {', '.join(sorted(kwargs))}"
    raise TypeError(msg)


def _drop_none(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


class GroundboxProvider(SandboxProvider):
    """Create, reattach to and delete Groundbox sandboxes for Deep Agents Code.

    Settings come from keyword arguments (config `params`) first, then
    environment variables, each also read with a `DEEPAGENTS_CODE_` prefix:

    - `GROUNDBOX_API_KEY` (required) and `GROUNDBOX_API_URL`
    - `GROUNDBOX_TEMPLATE`, `GROUNDBOX_IMAGE`: what a new sandbox starts from
    - `GROUNDBOX_CPUS`, `GROUNDBOX_MEMORY_GB`, `GROUNDBOX_DISK_GB`
    - `GROUNDBOX_SANDBOX_TIMEOUT_MINUTES`: the sandbox's lifetime (default 60)
    - `GROUNDBOX_WORKDIR`: folder commands start in
    """

    def __init__(
        self,
        *,
        resolve_env_var: EnvResolver | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        """Make the provider without touching credentials.

        dcode may build a provider only to read its metadata, so the API key is
        read when a sandbox is created or deleted.

        Args:
            resolve_env_var: Environment lookup for settings.
            client_factory: Builds the Groundbox client from `api_key` and
                `base_url` (tests pass a fake).
        """
        self._resolve_env_var = resolve_env_var or _default_resolve_env_var
        self._client_factory: ClientFactory = client_factory or Groundbox

    @property
    def metadata(self) -> SandboxProviderMetadata:
        """What dcode shows and checks for this provider."""
        return SandboxProviderMetadata(
            name=PROVIDER_NAME,
            working_dir=self._default_workdir(),
            install=SandboxInstallHint(kind="package", name=PACKAGE_NAME),
            supports_sandbox_id=True,
            supports_snapshot_name=True,
            backend_module="langchain_groundbox",
        )

    def get_or_create(
        self,
        *,
        sandbox_id: str | None = None,
        timeout: int | str | None = None,
        snapshot: str | None = None,
        template: str | None = None,
        image: str | None = None,
        cpus: int | str | None = None,
        memory_gb: int | str | None = None,
        disk_gb: int | str | None = None,
        timeout_minutes: int | str | None = None,
        workdir: str | None = None,
        command_timeout: int | str | None = None,
        env: dict[str, str] | None = None,
        metadata: dict[str, str] | None = None,
        allow_hosts: list[str] | None = None,
        deny_hosts: list[str] | None = None,
        secrets: list[str] | None = None,
        idle_timeout_seconds: int | str | None = None,
        **kwargs: object,
    ) -> GroundboxSandbox:
        """Reattach to a sandbox by id, or start a new one.

        Args:
            sandbox_id: An existing sandbox (`sbx_...`) to attach to. A paused
                sandbox is resumed and a shut-down one started. dcode does not
                delete a sandbox it attached to by id.
            timeout: Seconds to wait for a queued sandbox to start (default 180).
            snapshot: From `--sandbox-snapshot-name`: a kept snapshot id
                (`snap_...`) or a template name (`name` or `name:version`).
            template: Template id or name for a new sandbox.
            image: Docker image for a new sandbox (commands then run as root).
            cpus: vCPUs, 1 to 16 (default 2).
            memory_gb: Memory in GB (default 2 per vCPU, up to 4 per vCPU).
            disk_gb: Minimum disk size in GB.
            timeout_minutes: Sandbox lifetime in minutes (default 60).
            workdir: Folder commands start in.
            command_timeout: Default command timeout in seconds (default 1800;
                0 for none).
            env: Environment variables for every command.
            metadata: Metadata pairs on the sandbox (`source=deepagents` is added).
            allow_hosts: Egress allowlist.
            deny_hosts: Egress denylist.
            secrets: Team secrets given to commands as variables.
            idle_timeout_seconds: Pause the sandbox after this long idle.
            **kwargs: Rejected.

        Returns:
            A `GroundboxSandbox` backend.

        Raises:
            SandboxNotFoundError: If `sandbox_id` does not exist or has ended.
            RuntimeError: If a new sandbox fails or does not start in time.
            TypeError: If unsupported arguments are passed.
            ValueError: If the API key is missing or a setting is invalid.
        """
        if kwargs:
            _raise_unsupported_kwargs(kwargs)

        client = self._client()
        startup_timeout = _resolve_int(timeout, name="timeout") or DEFAULT_STARTUP_TIMEOUT
        default_command_timeout = _resolve_int(
            command_timeout, name="command_timeout", allow_zero=True
        )
        if default_command_timeout is None:
            default_command_timeout = DEFAULT_COMMAND_TIMEOUT

        if sandbox_id is not None:
            sandbox = self._attach(client, sandbox_id, startup_timeout)
        else:
            sandbox = self._create(
                client,
                startup_timeout=startup_timeout,
                snapshot=snapshot,
                template=template,
                image=image,
                cpus=cpus,
                memory_gb=memory_gb,
                disk_gb=disk_gb,
                timeout_minutes=timeout_minutes,
                env=env,
                metadata=metadata,
                allow_hosts=allow_hosts,
                deny_hosts=deny_hosts,
                secrets=secrets,
                idle_timeout_seconds=idle_timeout_seconds,
            )

        resolved_workdir = workdir or self._default_workdir(image=image)
        backend = GroundboxSandbox(
            client=client,
            sandbox_id=sandbox["id"],
            workdir=resolved_workdir,
            timeout=default_command_timeout,
            env=env,
        )
        if resolved_workdir not in (DEFAULT_WORKDIR, IMAGE_WORKDIR):
            # Best effort: a command run in a missing folder reports it itself.
            with contextlib.suppress(GroundboxError):
                client.make_dir(sandbox["id"], resolved_workdir)
        return backend

    def delete(self, *, sandbox_id: str, **kwargs: object) -> None:
        """Delete a Groundbox sandbox. Billing stops; the disk is gone.

        Raises:
            SandboxNotFoundError: If `sandbox_id` does not exist.
            TypeError: If unsupported arguments are passed.
            ValueError: If the API key is missing.
        """
        if kwargs:
            _raise_unsupported_kwargs(kwargs)
        try:
            client = self._client()
            _with_retry(lambda: client.delete_sandbox(sandbox_id))
        except GroundboxError as exc:
            if exc.status == 404:
                raise SandboxNotFoundError(sandbox_id) from exc
            raise

    # ------------------------------------------------------------------

    def _client(self) -> Groundbox:
        api_key = self._resolve_env_var("GROUNDBOX_API_KEY")
        if not api_key:
            msg = (
                "No Groundbox API key found. Set GROUNDBOX_API_KEY (or "
                "DEEPAGENTS_CODE_GROUNDBOX_API_KEY). Create one at "
                "https://app.groundbox.dev/keys."
            )
            raise ValueError(msg)
        base_url = self._resolve_env_var("GROUNDBOX_API_URL")
        return self._client_factory(api_key=api_key, base_url=base_url)

    def _default_workdir(self, *, image: str | None = None) -> str:
        configured = self._resolve_env_var("GROUNDBOX_WORKDIR")
        if configured:
            return configured
        if image or self._resolve_env_var("GROUNDBOX_IMAGE"):
            return IMAGE_WORKDIR
        return DEFAULT_WORKDIR

    def _attach(self, client: Groundbox, sandbox_id: str, startup_timeout: int) -> dict:
        try:
            sandbox = _with_retry(lambda: client.get_sandbox(sandbox_id))
        except GroundboxError as exc:
            if exc.status == 404:
                raise SandboxNotFoundError(sandbox_id) from exc
            raise
        status = sandbox.get("status")
        if status in _ENDED:
            reason = sandbox.get("stop_reason") or sandbox.get("error") or status
            msg = f"Groundbox sandbox {sandbox_id} has ended ({reason})"
            raise SandboxNotFoundError(msg)
        if status == "paused":
            _with_retry(lambda: client.resume_sandbox(sandbox_id))
        elif status in ("shut_down", "shutting_down"):
            if status == "shutting_down":
                _with_retry(lambda: client.wait_for_sandbox(sandbox_id, timeout_s=startup_timeout))
            _with_retry(lambda: client.start_sandbox(sandbox_id))
        return self._wait_running(client, sandbox_id, startup_timeout, delete_on_failure=False)

    def _create(
        self,
        client: Groundbox,
        *,
        startup_timeout: int,
        snapshot: str | None,
        template: str | None,
        image: str | None,
        cpus: int | str | None,
        memory_gb: int | str | None,
        disk_gb: int | str | None,
        timeout_minutes: int | str | None,
        env: dict[str, str] | None,
        metadata: dict[str, str] | None,
        allow_hosts: list[str] | None,
        deny_hosts: list[str] | None,
        secrets: list[str] | None,
        idle_timeout_seconds: int | str | None,
    ) -> dict:
        env_var = self._resolve_env_var
        snapshot_id = None
        if snapshot:
            if template or image:
                msg = "snapshot cannot be combined with template or image"
                raise ValueError(msg)
            if snapshot.startswith(SNAPSHOT_ID_PREFIX):
                snapshot_id = snapshot
            else:
                template = snapshot
        if snapshot_id is None and not template and not image:
            template = env_var("GROUNDBOX_TEMPLATE")
            image = env_var("GROUNDBOX_IMAGE") if not template else None
        if template and image:
            msg = "template and image cannot be combined"
            raise ValueError(msg)

        sizes: dict[str, Any] = {}
        if snapshot_id is None:  # A snapshot keeps its own size.
            sizes = {
                "cpus": _resolve_int(cpus or env_var("GROUNDBOX_CPUS"), name="cpus"),
                "memory_gb": _resolve_int(
                    memory_gb or env_var("GROUNDBOX_MEMORY_GB"), name="memory_gb"
                ),
                "disk_gb": _resolve_int(disk_gb or env_var("GROUNDBOX_DISK_GB"), name="disk_gb"),
            }

        request = _drop_none(
            {
                **sizes,
                "timeout_minutes": _resolve_int(
                    timeout_minutes or env_var("GROUNDBOX_SANDBOX_TIMEOUT_MINUTES"),
                    name="timeout_minutes",
                ),
                "template": template,
                "image": image,
                "snapshot_id": snapshot_id,
                "env": env,
                "metadata": {"source": "deepagents", **(metadata or {})},
                "allow_hosts": allow_hosts,
                "deny_hosts": deny_hosts,
                "secrets": secrets,
                "idle_timeout_seconds": _resolve_int(
                    idle_timeout_seconds, name="idle_timeout_seconds"
                ),
                "idempotency_key": str(uuid.uuid4()),
            }
        )
        sandbox = _with_retry(lambda: client.create_sandbox(**request))
        if sandbox.get("status") == "running":
            return sandbox
        return self._wait_running(client, sandbox["id"], startup_timeout, delete_on_failure=True)

    @staticmethod
    def _wait_running(
        client: Groundbox, sandbox_id: str, startup_timeout: int, *, delete_on_failure: bool
    ) -> dict:
        sandbox = _with_retry(
            lambda: client.wait_for_sandbox(sandbox_id, timeout_s=startup_timeout)
        )
        status = sandbox.get("status")
        if status == "running":
            return sandbox
        if delete_on_failure:
            with contextlib.suppress(GroundboxError):
                client.delete_sandbox(sandbox_id)
        if status in _STARTING:
            msg = (
                f"Groundbox sandbox {sandbox_id} did not start within {startup_timeout} "
                f"seconds (still {status})"
            )
        else:
            detail = sandbox.get("error") or sandbox.get("stop_reason") or status
            msg = f"Groundbox sandbox {sandbox_id} did not start: {detail}"
        raise RuntimeError(msg)
