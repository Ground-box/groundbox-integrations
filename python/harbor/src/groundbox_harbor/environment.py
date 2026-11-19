"""Harbor environment that runs each trial in a Groundbox sandbox.

Use it with ``harbor run -e groundbox_harbor:GroundboxSandbox``. Each trial gets
its own Firecracker microVM, started from the task's ``docker_image`` or from
a Groundbox template built from its ``environment/Dockerfile``.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import shlex
import stat
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar, TypeVar, override

from pydantic import Field, model_validator

from harbor.environments.base import (
    BaseEnvironment,
    ExecResult,
    SandboxBuildFailedError,
)
from harbor.environments.capabilities import (
    EnvironmentCapabilities,
    EnvironmentResourceCapabilities,
)
from harbor.environments.definition import (
    effective_exec_cwd,
    parse_dockerfile_workdir,
    require_agent_environment_definition,
    should_upload_environment_dir,
    should_use_prebuilt_docker_image,
)
from harbor.environments.options import EnvironmentOptions
from harbor.environments.tar_transfer import (
    extract_dir_from_file,
    pack_dir_to_bytes,
    remote_pack_command,
    remote_unpack_command,
    tar_pack_succeeded,
)
from harbor.models.task.config import NetworkMode, NetworkPolicy
from harbor.models.trial.config import ResourceMode

from groundbox import Groundbox, GroundboxError

from groundbox_harbor._dockerfile import (
    ContextTooLargeError,
    parse_dockerfile_user,
    prepare_dockerfile,
)

T = TypeVar("T")

# Bump when the template recipe changes, so old templates are not reused.
TEMPLATE_RECIPE_VERSION = "g1"
# Groundbox: memory per vCPU is at most 4 GB, and 1 to 16 vCPUs.
MAX_MEMORY_GB_PER_CPU = 4
MAX_CPUS = 16
# One file upload request is at most 32 MiB; one file download at most 100 MiB.
UPLOAD_CHUNK_BYTES = 24 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 64 * 1024 * 1024
# Commands that may run longer than this go through a background command that
# is polled, so a dropped connection never loses the result.
FOREGROUND_EXEC_MAX_SEC = 300
_TRANSFER_TIMEOUT_SEC = 600
_SHORT_TIMEOUT_SEC = 60
_TRANSIENT_CODES = frozenset({"network", "bad_response", "no_capacity", "rate_limited"})
_TRANSIENT_STATUSES = frozenset({502, 503, 504})
# Refusals that mean a command never started, so sending it again is safe.
_NOT_STARTED_CODES = frozenset({"no_capacity", "rate_limited"})
_RETRY_IN = re.compile(r"(\d+) seconds?")


class GroundboxOptions(EnvironmentOptions):
    """User-facing kwargs (``--ek name=value``) for :class:`GroundboxSandbox`."""

    template: str | None = Field(
        default=None,
        description=(
            "An existing Groundbox template (name, id or name:version) to start "
            "sandboxes from instead of the task's docker_image or Dockerfile."
        ),
    )
    image_user: str | None = Field(
        default=None,
        description=(
            "User to run commands as when Harbor names none. Defaults to the "
            "Dockerfile's final USER, else root."
        ),
    )
    sandbox_timeout_minutes: int = Field(
        default=60,
        ge=1,
        le=43200,
        description=(
            "Sandbox lifetime without renewal. It is renewed while the trial runs, "
            "so a sandbox left behind by a dead Harbor process ends within this long."
        ),
    )
    renew_interval_sec: float = Field(
        default=600.0,
        ge=0,
        description="Seconds between lifetime renewals; 0 turns renewal off.",
    )
    flex: bool = Field(
        default=False,
        description="Half-price sandboxes that start when a box has spare room.",
    )
    allow_memory_overcommit: bool = Field(
        default=True,
        description=(
            "Set vm.overcommit_memory=1 at start, so large allocations that are "
            "never fully touched succeed as on a Docker host."
        ),
    )
    create_retry_sec: float = Field(
        default=900.0,
        ge=0,
        description=(
            "How long to keep retrying a create that Groundbox refuses for a "
            "concurrency limit (429)."
        ),
    )
    evict_templates: bool = Field(
        default=True,
        description=(
            "When the team is at its template limit, delete the least recently "
            "created harbor-* template this process is not using and try again."
        ),
    )
    @model_validator(mode="after")
    def _check_renewal(self) -> GroundboxOptions:
        if (
            self.renew_interval_sec
            and self.renew_interval_sec >= self.sandbox_timeout_minutes * 60
        ):
            raise ValueError(
                "renew_interval_sec must be 0 or shorter than sandbox_timeout_minutes"
            )
        return self


class _TemplateLocks:
    """One build at a time per template name in this process."""

    locks: ClassVar[dict[str, asyncio.Lock]] = {}
    in_use: ClassVar[dict[str, int]] = {}
    force_built: ClassVar[set[str]] = set()

    @classmethod
    def lock(cls, name: str) -> asyncio.Lock:
        return cls.locks.setdefault(name, asyncio.Lock())


def _backoff(exc: GroundboxError, attempt: int, cap: float) -> float:
    """Seconds to wait before trying again: the API's own hint when it gives one."""
    match = _RETRY_IN.search(exc.message or "")
    if match:
        return float(match.group(1)) + 1.0
    return float(min(2**attempt, cap))


def _not_started(exc: BaseException) -> bool:
    return isinstance(exc, GroundboxError) and exc.code in _NOT_STARTED_CODES


def _is_transient(exc: BaseException) -> bool:
    return isinstance(exc, GroundboxError) and (
        exc.code in _TRANSIENT_CODES or exc.status in _TRANSIENT_STATUSES
    )


def _is_template_limit(exc: BaseException) -> bool:
    if not isinstance(exc, GroundboxError) or exc.code == "concurrency_limit":
        return False
    text = f"{exc.code} {exc.message}".lower()
    return "template" in text and ("limit" in text or "up to" in text)


class GroundboxSandbox(BaseEnvironment):
    """A Harbor environment on Groundbox sandboxes (Firecracker microVMs)."""

    options_model = GroundboxOptions
    options: GroundboxOptions

    # A prebuilt template and image user describe the agent's image, so a
    # separate verifier environment with its own definition should not get them.
    agent_only_kwargs = frozenset({"template", "image_user"})

    # Seams for tests.
    _client_factory: ClassVar[Callable[..., Any]] = Groundbox
    _poll_initial_sec: ClassVar[float] = 0.25
    _poll_max_sec: ClassVar[float] = 3.0
    _build_poll_sec: ClassVar[float] = 3.0

    @classmethod
    @override
    def preflight(cls, kwargs: dict[str, Any] | None = None) -> None:
        super().preflight(kwargs)
        if not os.environ.get("GROUNDBOX_API_KEY"):
            raise SystemExit(
                "Groundbox requires GROUNDBOX_API_KEY to be set. Create a key at "
                "https://app.groundbox.dev/keys and export it, then try again."
            )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # GROUNDBOX_API_KEY (and GROUNDBOX_API_URL) come from the Harbor process.
        self._client = type(self)._client_factory()
        self._sandbox_id: str | None = None
        self._renew_task: asyncio.Task[None] | None = None
        self._shell = "/bin/sh"
        self._image_path: str | None = None
        self._template_ref: str | None = None
        self._template_name: str | None = None
        dockerfile = self._dockerfile_path
        if self.options.template is not None:
            self._workdir = None
            self._dockerfile_user = None
        else:
            self._workdir = parse_dockerfile_workdir(dockerfile)
            self._dockerfile_user = parse_dockerfile_user(dockerfile)

    # ── identity and capabilities ───────────────────────────────────────

    @staticmethod
    @override
    def type() -> str:
        return "groundbox"

    @classmethod
    @override
    def resource_capabilities(cls) -> EnvironmentResourceCapabilities:
        # A microVM's vCPUs and memory are both reserved and a hard ceiling.
        return EnvironmentResourceCapabilities(
            cpu_limit=True,
            cpu_request=True,
            memory_limit=True,
            memory_request=True,
        )

    @property
    @override
    def capabilities(self) -> EnvironmentCapabilities:
        return EnvironmentCapabilities(
            disable_internet=True,
            network_allowlist=True,
            network_allowlist_hostnames=True,
            network_allowlist_wildcard_hostnames=True,
            network_allowlist_ipv4_addresses=True,
            network_allowlist_ipv4_cidrs=True,
            network_allowlist_ipv6_addresses=False,
            network_allowlist_ipv6_cidrs=False,
            dynamic_network_policy=True,
        )

    @property
    def _dockerfile_path(self) -> Path:
        return self.environment_dir / "Dockerfile"

    @property
    def sandbox_id(self) -> str | None:
        return self._sandbox_id

    @property
    def default_exec_user(self) -> str:
        """User for commands when Harbor names none, as ``docker exec`` would."""
        return self.options.image_user or self._dockerfile_user or "root"

    @override
    def _validate_definition(self) -> None:
        if self.options.template is not None:
            return
        require_agent_environment_definition(
            self.environment_dir, docker_image=self.task_env_config.docker_image
        )
        if not self.task_env_config.docker_image and not self._dockerfile_path.exists():
            raise ValueError(
                "Groundbox runs one container per trial: docker-compose task "
                "environments are not supported. Add environment/Dockerfile or "
                "set docker_image."
            )

    # ── sizes ───────────────────────────────────────────────────────────

    def _size(self) -> dict[str, int]:
        size: dict[str, int] = {}
        cpus = self._effective_cpus
        memory_mb = self._effective_memory_mb
        if cpus is not None:
            size["cpus"] = max(1, int(cpus))
        if memory_mb is not None:
            memory_gb = max(1, math.ceil(memory_mb / 1024))
            needed_cpus = math.ceil(memory_gb / MAX_MEMORY_GB_PER_CPU)
            if needed_cpus > size.get("cpus", 2):
                if self._resource_mode("cpu") in (
                    ResourceMode.LIMIT,
                    ResourceMode.GUARANTEE,
                ):
                    raise ValueError(
                        f"Groundbox gives at most {MAX_MEMORY_GB_PER_CPU} GB per vCPU: "
                        f"{memory_gb} GB needs {needed_cpus} vCPUs, more than the "
                        f"CPU limit of {size.get('cpus')}."
                    )
                self.logger.info(
                    f"Groundbox gives at most {MAX_MEMORY_GB_PER_CPU} GB per vCPU; "
                    f"using {needed_cpus} vCPUs for {memory_gb} GB of memory."
                )
                size["cpus"] = needed_cpus
            size["memory_gb"] = memory_gb
        if size.get("cpus", 0) > MAX_CPUS:
            raise ValueError(f"Groundbox sandboxes have at most {MAX_CPUS} vCPUs.")
        storage_mb = self._effective_storage_mb
        if storage_mb is not None:
            size["disk_gb"] = max(1, math.ceil(storage_mb / 1024))
        return size

    # ── network ─────────────────────────────────────────────────────────

    @staticmethod
    def _allow_hosts(policy: NetworkPolicy) -> list[str] | None:
        if policy.network_mode == NetworkMode.PUBLIC:
            return None
        if policy.network_mode == NetworkMode.NO_NETWORK:
            return []
        return list(policy.allowed_hosts)

    @override
    async def _apply_network_policy(self, network_policy: NetworkPolicy) -> None:
        sandbox_id = self._require_sandbox()
        await self._call(
            self._client.update_sandbox,
            sandbox_id,
            allow_hosts=self._allow_hosts(network_policy),
        )

    # ── SDK plumbing ────────────────────────────────────────────────────

    async def _call(self, fn: Callable[..., T], *args: Any, retries: int = 4, **kwargs: Any) -> T:
        """Run a blocking SDK call in a thread, retrying transient failures.

        Only use it for calls that are safe to repeat.
        """
        attempt = 0
        while True:
            try:
                return await asyncio.to_thread(fn, *args, **kwargs)
            except GroundboxError as exc:
                if attempt >= retries or not _is_transient(exc):
                    raise
                attempt += 1
                await asyncio.sleep(_backoff(exc, attempt, 10))

    def _require_sandbox(self) -> str:
        if self._sandbox_id is None:
            raise RuntimeError("Sandbox not found. Please start the environment first.")
        return self._sandbox_id

    def _metadata(self) -> dict[str, str]:
        metadata = {
            "created_by": "harbor",
            "harbor_environment": self.environment_name[:200],
            "harbor_session_id": self.session_id[:200],
            "harbor_trial": self.trial_paths.trial_dir.name[:200],
        }
        if self.context_id is not None:
            metadata["harbor_context_id"] = str(self.context_id)
        job_id = getattr(self, "job_id", None)
        if job_id is not None:
            metadata["harbor_job_id"] = str(job_id)
        return metadata

    async def _retry_limited(self, fn: Callable[[], T], what: str) -> T:
        """Retry ``fn`` while Groundbox answers 429, up to ``create_retry_sec``."""
        deadline = time.monotonic() + self.options.create_retry_sec
        delay = 5.0
        while True:
            try:
                return await asyncio.to_thread(fn)
            except GroundboxError as exc:
                if exc.status != 429 or time.monotonic() + delay > deadline:
                    raise
                wait = max(delay, _backoff(exc, 0, 0))
                self.logger.info(f"Groundbox refused {what} for now ({exc.message}); retrying in {wait:.0f}s")
                await asyncio.sleep(wait)
                delay = min(delay * 1.5, 30.0)

    # ── templates (Dockerfile builds) ───────────────────────────────────

    def _template_name_for(self, size: dict[str, int]) -> str:
        digest = hashlib.sha256(
            "\0".join(
                [
                    self.environment_id,
                    self.task_env_config.docker_image or "",
                    str(size.get("cpus")),
                    str(size.get("memory_gb")),
                    str(size.get("disk_gb")),
                    TEMPLATE_RECIPE_VERSION,
                ]
            ).encode()
        ).hexdigest()
        return f"harbor-{digest[:32]}"

    async def _ensure_template(self, force_build: bool, size: dict[str, int]) -> str:
        """Build (or reuse) the template for this Dockerfile and return its ref."""
        try:
            prepared = prepare_dockerfile(self._dockerfile_path)
        except ContextTooLargeError as exc:
            raise SandboxBuildFailedError(str(exc)) from exc
        if prepared.inlined_context:
            self.logger.debug(
                f"Inlined the {prepared.context_bytes}-byte build context into the Dockerfile"
            )
        name = self._template_name_for(size)
        self._template_name = name
        spec: dict[str, Any] = {"dockerfile": prepared.text, **size}
        async with _TemplateLocks.lock(name):
            existing = await self._get_template(name)
            rebuild = force_build and name not in _TemplateLocks.force_built
            if existing is None:
                self.logger.info(f"Building Groundbox template {name}")
                created = await self._create_template(name, spec)
                version = (created.get("latest_build") or {}).get("version") or 1
            elif rebuild or existing.get("status") == "failed":
                self.logger.info(f"Rebuilding Groundbox template {name}")
                build = await self._retry_limited(
                    lambda: self._client.build_template(name, **spec), "a template build"
                )
                version = build.get("version") or (build.get("latest_build") or {}).get("version")
            else:
                version = existing.get("version") or (existing.get("latest_build") or {}).get("version")
                if existing.get("status") == "ready":
                    return f"{name}:{version}" if version else name
            if force_build:
                _TemplateLocks.force_built.add(name)
            await self._wait_for_template(name, version)
            return f"{name}:{version}" if version else name

    async def _get_template(self, name: str) -> dict[str, Any] | None:
        try:
            return await self._call(self._client.get_template, name)
        except GroundboxError as exc:
            if exc.status == 404 or exc.code == "not_found":
                return None
            raise

    async def _create_template(self, name: str, spec: dict[str, Any]) -> dict[str, Any]:
        evicted = 0
        while True:
            try:
                return await self._retry_limited(
                    lambda: self._client.create_template(name=name, **spec),
                    "a template build",
                )
            except GroundboxError as exc:
                if exc.code == "conflict":
                    # Another process made it first: wait for theirs.
                    existing = await self._get_template(name)
                    if existing is not None:
                        return existing
                    raise
                if _is_template_limit(exc) and self.options.evict_templates and evicted < 3:
                    if await self._evict_one_template(keep=name):
                        evicted += 1
                        continue
                raise

    async def _evict_one_template(self, keep: str) -> bool:
        listing = await self._call(self._client.list_templates, limit=100)
        candidates = [
            t
            for t in listing.get("data", [])
            if str(t.get("name", "")).startswith("harbor-")
            and t.get("name") != keep
            and not _TemplateLocks.in_use.get(t.get("name"), 0)
            and t.get("status") != "building"
        ]
        if not candidates:
            return False
        oldest = min(candidates, key=lambda t: str(t.get("created_at") or ""))
        self.logger.info(
            f"Groundbox template limit reached; deleting old template {oldest['name']}"
        )
        await self._call(self._client.delete_template, oldest["name"])
        return True

    async def _wait_for_template(self, name: str, version: int | None) -> None:
        after = -1
        tail: list[str] = []
        status = None
        if version is not None:
            while True:
                page = await self._call(
                    self._client.get_template_logs, name, int(version), after=after, wait_s=20
                )
                for line in page.get("lines", []):
                    text = str(line.get("text", ""))
                    self.logger.debug(f"[groundbox build] {text}")
                    tail = (tail + [text])[-40:]
                after = page.get("next", after)
                if page.get("done"):
                    status = page.get("status")
                    break
        else:
            while True:
                template = await self._get_template(name)
                status = template.get("status") if template else "failed"
                if status != "building":
                    break
                await asyncio.sleep(self._build_poll_sec)
        if status != "ready":
            log = "\n".join(tail)
            raise SandboxBuildFailedError(
                f"Groundbox template {name} build ended {status!r}.\n{log}"
            )

    # ── lifecycle ───────────────────────────────────────────────────────

    @override
    async def start(self, force_build: bool) -> None:
        size = self._size()
        create: dict[str, Any] = {
            **size,
            "timeout_minutes": self.options.sandbox_timeout_minutes,
            "env": self._startup_env() or None,
            "metadata": self._metadata(),
            "allow_hosts": self._allow_hosts(self.network_policy),
            "flex": True if self.options.flex else None,
        }
        if self.options.template is not None:
            if force_build:
                self.logger.warning(
                    "template is set, so force_build is ignored; the sandbox starts "
                    f"from {self.options.template!r} as built."
                )
            create["template"] = self.options.template
            self._template_ref = self.options.template
        elif should_use_prebuilt_docker_image(
            self.environment_dir,
            docker_image=self.task_env_config.docker_image,
            force_build=force_build,
        ):
            create["image"] = self.task_env_config.docker_image
        else:
            self._template_ref = await self._ensure_template(force_build, size)
            create["template"] = self._template_ref

        if self._template_name:
            _TemplateLocks.in_use[self._template_name] = (
                _TemplateLocks.in_use.get(self._template_name, 0) + 1
            )
        await self._create_sandbox(create)

        if self.options.renew_interval_sec > 0:
            self._renew_task = asyncio.create_task(self._renew_loop())
        try:
            await self._prepare_sandbox()
            await self.ensure_dirs(self._mount_targets(writable_only=True))
            if self.task_env_config.workdir:
                await self._run_root(
                    f"mkdir -p {shlex.quote(self.task_env_config.workdir)}",
                    _SHORT_TIMEOUT_SEC,
                )
            await self._upload_environment_dir_after_start()
        except BaseException:
            if self._renew_task is not None:
                self._renew_task.cancel()
                self._renew_task = None
            raise

    async def _create_sandbox(self, create: dict[str, Any]) -> None:
        # None means "leave it out"; an empty allow_hosts list means no network.
        body = {k: v for k, v in create.items() if v is not None}

        def _create() -> dict[str, Any]:
            # One key per attempt that Groundbox refused (429); the same key for
            # retries after a dropped connection, so they never start two sandboxes.
            key = str(uuid.uuid4())
            attempt = 0
            while True:
                try:
                    return self._client.create_sandbox(idempotency_key=key, **body)
                except GroundboxError as exc:
                    # A 429 is a refusal: the caller retries it with a new key.
                    if attempt >= 2 or exc.status == 429 or not _is_transient(exc):
                        raise
                    attempt += 1
                    time.sleep(2**attempt)

        try:
            sandbox = await self._retry_limited(_create, "a new sandbox")
        except GroundboxError as exc:
            if exc.status == 404 and "image" in body:
                raise SandboxBuildFailedError(
                    f"Groundbox could not find image {body['image']!r}: {exc.message}"
                ) from exc
            raise
        self._sandbox_id = sandbox["id"]
        self.logger.debug(f"Groundbox sandbox {self._sandbox_id} is {sandbox.get('status')}")
        if sandbox.get("status") != "running":
            # The first sandbox from an image waits while the image becomes a disk.
            sandbox = await self._wait_running(
                self._sandbox_id,
                max(60.0, float(self.task_env_config.build_timeout_sec or 1800)),
            )
        if sandbox.get("status") != "running":
            sandbox_id = self._sandbox_id
            await self._delete_quietly()
            reason = sandbox.get("error") or sandbox.get("status")
            raise SandboxBuildFailedError(
                f"Groundbox sandbox {sandbox_id} did not start: {reason}"
            )

    async def _wait_running(self, sandbox_id: str, timeout_sec: float) -> dict[str, Any]:
        """Poll until the sandbox leaves queued/starting (gently: keys are rate limited)."""
        deadline = time.monotonic() + timeout_sec
        while True:
            sandbox = await self._call(self._client.get_sandbox, sandbox_id, retries=8)
            if sandbox.get("status") not in ("queued", "starting", "resuming"):
                return sandbox
            if time.monotonic() >= deadline:
                return sandbox
            await asyncio.sleep(self._build_poll_sec)

    async def _prepare_sandbox(self) -> None:
        """Find the shell, record PATH, and allow memory overcommit."""
        overcommit = (
            "sysctl -qw vm.overcommit_memory=1 2>/dev/null || "
            "echo 1 > /proc/sys/vm/overcommit_memory 2>/dev/null || true; "
            if self.options.allow_memory_overcommit
            else ""
        )
        # The VM's /dev is a fresh devtmpfs without the links Docker adds, so bash
        # process substitution (<(...)) and /dev/stdin would fail; /run/lock is
        # missing for the same reason.
        dev_links = (
            "for l in fd:/proc/self/fd stdin:/proc/self/fd/0 stdout:/proc/self/fd/1 "
            "stderr:/proc/self/fd/2; do [ -e /dev/${l%%:*} ] || "
            "ln -s ${l#*:} /dev/${l%%:*} 2>/dev/null; done; "
            "mkdir -p /run/lock 2>/dev/null; "
        )
        result = await self._run_root(
            f"{dev_links}{overcommit}command -v bash || echo; printf '%s\\n' \"$PATH\"",
            _SHORT_TIMEOUT_SEC,
        )
        lines = (result.stdout or "").splitlines()
        bash = lines[0].strip() if lines else ""
        self._shell = bash or "/bin/sh"
        self._image_path = lines[1].strip() if len(lines) > 1 else None

    async def _renew_loop(self) -> None:
        while True:
            await asyncio.sleep(self.options.renew_interval_sec)
            sandbox_id = self._sandbox_id
            if sandbox_id is None:
                return
            try:
                await self._call(
                    self._client.update_sandbox,
                    sandbox_id,
                    timeout_minutes=self.options.sandbox_timeout_minutes,
                )
            except Exception as exc:  # renewal is best effort
                self.logger.warning(f"Groundbox sandbox timeout renewal failed: {exc}")

    async def _delete_quietly(self) -> None:
        sandbox_id = self._sandbox_id
        self._sandbox_id = None
        if sandbox_id is None:
            return
        try:
            await self._call(self._client.delete_sandbox, sandbox_id)
        except GroundboxError as exc:
            if exc.status != 404:
                self.logger.warning(
                    f"Failed to delete Groundbox sandbox {sandbox_id} ({self.session_id}): "
                    f"{exc}. It ends at its {self.options.sandbox_timeout_minutes}-minute timeout."
                )

    @override
    async def stop(self, delete: bool) -> None:
        """Delete the sandbox. Sandboxes are per trial, so ``delete=False`` changes nothing."""
        if not delete:
            self.logger.debug("Groundbox sandboxes are deleted at stop regardless of delete=False.")
        if self._renew_task is not None:
            self._renew_task.cancel()
            self._renew_task = None
        if self._template_name and _TemplateLocks.in_use.get(self._template_name):
            _TemplateLocks.in_use[self._template_name] -= 1
        await self._delete_quietly()

    @override
    async def attach(self) -> None:
        sandbox_id = self._require_sandbox()
        os.execvp(
            "npx", ["npx", "-y", "@groundbox/cli@latest", "sandbox", "shell", sandbox_id]
        )

    # ── commands ────────────────────────────────────────────────────────

    def _argv(self, command: str, cwd: str | None, user: str, env: dict[str, str] | None) -> list[str]:
        inner = command if not cwd else f"cd {shlex.quote(cwd)} && {command}"
        if user in ("root", "0"):
            return [self._shell, "-c", inner]
        # su resets PATH (and HOME, USER, LOGNAME, SHELL) for the target user, which
        # docker exec -u does not do for PATH: put the image's PATH back, then the
        # variables Harbor asked for.
        exports = {}
        if self._image_path:
            exports["PATH"] = self._image_path
        if env:
            exports.update(env)
        if exports:
            prefix = "export " + " ".join(f"{k}={shlex.quote(v)}" for k, v in exports.items())
            inner = f"{prefix}; {inner}"
        if user.isdigit():
            return [
                "sh",
                "-c",
                f'exec su "$(getent passwd {user} | cut -d: -f1)" -s {shlex.quote(self._shell)} -c "$0"',
                inner,
            ]
        return ["su", user, "-s", self._shell, "-c", inner]

    @override
    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        """Run ``command`` in the sandbox.

        ``user`` (or Harbor's default user) is applied with ``su``; with neither,
        commands run as :attr:`default_exec_user`. A command over its timeout
        raises ``RuntimeError``, as the Docker environment does.
        """
        sandbox_id = self._require_sandbox()
        resolved = self._resolve_user(user)
        run_as = str(resolved) if resolved is not None else self.default_exec_user
        merged_env = self._merge_env(env)
        # Docker starts in the image's WORKDIR, "/" when it sets none; Groundbox
        # would start in the user's home.
        workdir = (
            effective_exec_cwd(cwd, self.task_env_config.workdir, self._workdir) or "/"
        )
        argv = self._argv(command, workdir, run_as, merged_env)
        return await self._run(sandbox_id, argv, merged_env, timeout_sec)

    async def _run(
        self,
        sandbox_id: str,
        argv: list[str],
        env: dict[str, str] | None,
        timeout_sec: int | None,
    ) -> ExecResult:
        if timeout_sec is not None and timeout_sec <= FOREGROUND_EXEC_MAX_SEC:
            result = await self._dispatch(
                self._client.run_command,
                sandbox_id,
                argv,
                env=env or None,
                timeout_s=max(1, int(timeout_sec)),
            )
        else:
            result = await self._run_background(sandbox_id, argv, env, timeout_sec)
        if result.get("timed_out"):
            raise RuntimeError(f"Command timed out after {timeout_sec} seconds")
        return ExecResult(
            stdout=result.get("stdout", ""),
            stderr=result.get("stderr", ""),
            return_code=int(result.get("exit_code", -1)),
        )

    async def _dispatch(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Send a command, retrying only refusals that prove it never started.

        A dropped connection is not retried: the command may be running already.
        """
        attempt = 0
        while True:
            try:
                return await asyncio.to_thread(fn, *args, **kwargs)
            except GroundboxError as exc:
                if attempt >= 8 or not _not_started(exc):
                    raise
                attempt += 1
                await asyncio.sleep(_backoff(exc, attempt, 15))

    async def _run_background(
        self,
        sandbox_id: str,
        argv: list[str],
        env: dict[str, str] | None,
        timeout_sec: int | None,
    ) -> dict[str, Any]:
        started = await self._dispatch(
            self._client.start_command,
            sandbox_id,
            argv,
            env=env or None,
            timeout_s=int(timeout_sec) if timeout_sec else None,
        )
        exec_id = started["id"]
        stdout: list[str] = []
        stderr: list[str] = []
        seq = 0
        delay = self._poll_initial_sec
        try:
            while True:
                part = await self._call(
                    self._client.get_command_output, sandbox_id, exec_id, seq=seq, retries=5
                )
                seq = part.get("seq", seq)
                if part.get("stdout"):
                    stdout.append(part["stdout"])
                if part.get("stderr"):
                    stderr.append(part["stderr"])
                if part.get("done"):
                    return {
                        "stdout": "".join(stdout),
                        "stderr": "".join(stderr),
                        "exit_code": part.get("exit_code", -1),
                        "timed_out": part.get("timed_out", False),
                    }
                if part.get("more"):
                    continue
                await asyncio.sleep(delay)
                delay = min(delay * 1.5, self._poll_max_sec)
        except asyncio.CancelledError:
            # Harbor cancels the agent at its own timeout: end the command too.
            try:
                await asyncio.shield(
                    asyncio.to_thread(self._client.kill_command, sandbox_id, exec_id)
                )
            except Exception:
                pass
            raise

    async def _run_root(self, command: str, timeout_sec: int) -> ExecResult:
        """Run a helper command as root in ``/``, without Harbor's env overlays."""
        sandbox_id = self._require_sandbox()
        return await self._run(sandbox_id, ["sh", "-c", f"cd / && {command}"], None, timeout_sec)

    # ── files ───────────────────────────────────────────────────────────

    async def _put_bytes(self, data: bytes, target_path: str) -> None:
        sandbox_id = self._require_sandbox()
        if len(data) <= UPLOAD_CHUNK_BYTES:
            await self._call(self._client.write_file, sandbox_id, target_path, data)
            return
        prefix = f"/tmp/.harbor-upload-{uuid.uuid4().hex}"
        parts = []
        try:
            for index, offset in enumerate(range(0, len(data), UPLOAD_CHUNK_BYTES)):
                part = f"{prefix}.{index:05d}"
                await self._call(
                    self._client.write_file,
                    sandbox_id,
                    part,
                    data[offset : offset + UPLOAD_CHUNK_BYTES],
                )
                parts.append(part)
            quoted = " ".join(shlex.quote(p) for p in parts)
            target = shlex.quote(target_path)
            result = await self._run_root(
                f'mkdir -p "$(dirname {target})" && cat {quoted} > {target}',
                _TRANSFER_TIMEOUT_SEC,
            )
            if result.return_code != 0:
                raise RuntimeError(f"Failed to assemble {target_path}: {result.stderr}")
        finally:
            if parts:
                await self._run_root(f"rm -f {prefix}.*", _SHORT_TIMEOUT_SEC)

    async def _get_bytes(self, source_path: str) -> bytes:
        sandbox_id = self._require_sandbox()
        info = await self._call(self._client.stat_file, sandbox_id, source_path)
        if int(info.get("size_bytes") or 0) <= DOWNLOAD_CHUNK_BYTES:
            return await self._call(self._client.read_file, sandbox_id, source_path)
        prefix = f"/tmp/.harbor-download-{uuid.uuid4().hex}."
        try:
            result = await self._run_root(
                f"split -b {DOWNLOAD_CHUNK_BYTES} -a 5 {shlex.quote(source_path)} {prefix}"
                f" && ls -1 {prefix}*",
                _TRANSFER_TIMEOUT_SEC,
            )
            if result.return_code != 0:
                raise RuntimeError(f"Failed to split {source_path}: {result.stderr}")
            chunks = []
            for part in sorted((result.stdout or "").split()):
                chunks.append(await self._call(self._client.read_file, sandbox_id, part))
            return b"".join(chunks)
        finally:
            await self._run_root(f"rm -f {prefix}*", _SHORT_TIMEOUT_SEC)

    @override
    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        """Copy a local file into the sandbox as root, keeping its permission bits."""
        source = Path(source_path)
        await self._put_bytes(await asyncio.to_thread(source.read_bytes), target_path)
        mode = stat.S_IMODE(source.stat().st_mode)
        result = await self._run_root(
            f"chmod {mode:o} {shlex.quote(target_path)}", _SHORT_TIMEOUT_SEC
        )
        if result.return_code != 0:
            raise RuntimeError(f"Failed to set permissions on {target_path}: {result.stderr}")

    @override
    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        """Copy a local directory into the sandbox through one gzipped tar.

        Extracted as root with ``--no-same-owner``, like ``docker cp``. Needs tar
        and gzip in the image.
        """
        source = Path(source_dir)
        if not source.is_dir():
            raise FileNotFoundError(f"Source directory {source_dir} does not exist")
        archive = await asyncio.to_thread(
            pack_dir_to_bytes, source, compress=True, include_root=False
        )
        remote = f"/tmp/.harbor-upload-{uuid.uuid4().hex}.tar.gz"
        try:
            await self._put_bytes(archive.getvalue(), remote)
            result = await self._run_root(
                remote_unpack_command(remote, target_dir, no_same_owner=True),
                _TRANSFER_TIMEOUT_SEC,
            )
            if result.return_code != 0:
                raise RuntimeError(
                    f"Failed to extract uploaded archive into {target_dir} "
                    f"(the image needs tar and gzip): {result.stderr}"
                )
        finally:
            await self._run_root(f"rm -f {shlex.quote(remote)}", _SHORT_TIMEOUT_SEC)

    @override
    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = await self._get_bytes(source_path)
        await asyncio.to_thread(target.write_bytes, data)

    @override
    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        """Copy a sandbox directory to the host through one gzipped tar."""
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        remote = f"/tmp/.harbor-download-{uuid.uuid4().hex}.tar.gz"
        try:
            result = await self._run_root(
                remote_pack_command(source_dir, remote), _TRANSFER_TIMEOUT_SEC
            )
            if not tar_pack_succeeded(
                result.return_code,
                stdout=result.stdout,
                stderr=result.stderr,
                logger=self.logger,
            ):
                raise RuntimeError(
                    f"Failed to archive {source_dir} in the sandbox "
                    f"(the image needs tar and gzip): {result.stderr}"
                )
            data = await self._get_bytes(remote)
            with tempfile.TemporaryDirectory() as tmp:
                archive_path = Path(tmp) / "download.tar.gz"
                archive_path.write_bytes(data)
                await asyncio.to_thread(extract_dir_from_file, archive_path, target)
        finally:
            await self._run_root(f"rm -f {shlex.quote(remote)}", _SHORT_TIMEOUT_SEC)

    async def _path_type(self, path: str) -> str | None:
        sandbox_id = self._require_sandbox()
        try:
            info = await self._call(self._client.stat_file, sandbox_id, path)
        except GroundboxError as exc:
            if exc.status == 404 or exc.code == "not_found":
                return None
            raise
        return info.get("type")

    @override
    async def is_dir(self, path: str, user: str | int | None = None) -> bool:
        return await self._path_type(path) == "dir"

    @override
    async def is_file(self, path: str, user: str | int | None = None) -> bool:
        return await self._path_type(path) == "file"

    @override
    async def _upload_environment_dir_after_start(self) -> None:
        if self.options.template is None or self.task_env_config.docker_image:
            await super()._upload_environment_dir_after_start()
            return
        # A caller-supplied template is a prebuilt image: loose environment files
        # still need the same upload as for docker_image.
        if not self._enable_environment_dir_upload or not should_upload_environment_dir(
            self.environment_dir, docker_image=self.options.template
        ):
            return
        workdir = self.task_env_config.workdir
        if not workdir:
            result = await self.exec("pwd")
            workdir = (result.stdout or "/").strip()
        await self.upload_dir(self.environment_dir, workdir)
