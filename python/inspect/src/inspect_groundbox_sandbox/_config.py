"""Configuration for the Groundbox sandbox: the config model, and how a
Dockerfile or a Compose file maps onto a Groundbox sandbox."""

from __future__ import annotations

import hashlib
import math
import posixpath
import re
from dataclasses import dataclass, field
from logging import getLogger
from pathlib import Path
from typing import Any

from inspect_ai.util import (
    ComposeConfig,
    ComposeService,
    is_compose_yaml,
    is_dockerfile,
    parse_compose_yaml,
    warn_once,
)
from pydantic import BaseModel, ConfigDict

logger = getLogger(__name__)

EXTENSION_KEY = "x-groundbox"


class GroundboxSandboxEnvironmentConfig(BaseModel):
    """Settings for a Groundbox sandbox, for `sandbox=("groundbox", config)`.

    Every field is optional. With none set, a sample gets a sandbox from
    Groundbox's own image (Ubuntu 24.04 with Docker, Node, Python, Go, Rust
    and Java), where commands run as the user `runner`, who has sudo.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    image: str | None = None
    """A public Docker image (or one in your Groundbox registry) to start from. Commands run as root."""

    template: str | None = None
    """A Groundbox template, by id, name or `name:version`."""

    dockerfile: str | None = None
    """Path to a Dockerfile. It is built once into a Groundbox template (with no build context)."""

    snapshot_id: str | None = None
    """A kept Groundbox snapshot (`snap_...`) to start each sample from."""

    cpus: int | None = None
    """vCPUs, 1 to 16 (Groundbox's default is 2)."""

    memory_gb: int | None = None
    """Memory in GB (Groundbox's default is 2 per vCPU, up to 4 per vCPU)."""

    disk_gb: int | None = None
    """Least disk size in GB (Groundbox's default is 20)."""

    timeout_minutes: int | None = None
    """How long a sandbox may live (Groundbox's default is 60 minutes)."""

    flex: bool | None = None
    """Half price; the sandbox starts when a machine has spare room, so it may wait in a queue."""

    env: dict[str, str] | None = None
    """Environment variables for every command."""

    metadata: dict[str, str] | None = None
    """Extra metadata pairs on each sandbox (Inspect adds its own run and task pairs)."""

    allow_hosts: list[str] | None = None
    """Egress allowlist: the sandbox reaches these hosts and nothing else. `[]` blocks all egress."""

    deny_hosts: list[str] | None = None
    """Egress deny list: the sandbox reaches everything but these."""

    secrets: list[str] | None = None
    """Groundbox team secrets given to every command as variables of the same name."""

    env_from_secrets: dict[str, str] | None = None
    """`{"VAR": "SECRET_NAME"}`: a team secret under another variable name."""

    user: str | None = None
    """The user `exec()` runs as when no user is given (default: the sandbox's own user)."""

    working_dir: str | None = None
    """The folder `exec()` runs in, and relative paths resolve against (created if missing)."""

    ports: list[int] | None = None
    """Ports to give an HTTPS preview URL in `connection()` (paid plan)."""


@dataclass
class SandboxSpec:
    """What one sample's sandbox is made from, resolved from the task's config."""

    create: dict[str, Any] = field(default_factory=dict)
    """Keyword arguments for `Groundbox.create_sandbox`."""

    dockerfile: str | None = None
    """Dockerfile text to build into a template first (sets `create["template"]`)."""

    user: str | None = None
    working_dir: str | None = None
    ports: list[int] = field(default_factory=list)

    def template_name(self) -> str | None:
        """The template a Dockerfile is built into: named by its content and size, so it is built once."""
        if self.dockerfile is None:
            return None
        key = "\0".join(
            [
                self.dockerfile,
                str(self.create.get("cpus")),
                str(self.create.get("memory_gb")),
                str(self.create.get("disk_gb")),
            ]
        )
        return "inspect-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


_CREATE_FIELDS = (
    "image",
    "template",
    "snapshot_id",
    "cpus",
    "memory_gb",
    "disk_gb",
    "timeout_minutes",
    "flex",
    "env",
    "metadata",
    "allow_hosts",
    "deny_hosts",
    "secrets",
    "env_from_secrets",
)


def resolve_spec(config: BaseModel | str | None) -> SandboxSpec:
    """Turn the task's sandbox config into a `SandboxSpec`.

    Accepts nothing, a `GroundboxSandboxEnvironmentConfig`, a dict of its fields,
    a Dockerfile path, a Compose file path or a `ComposeConfig`.
    """
    if config is None:
        return SandboxSpec()
    if isinstance(config, dict):
        config = GroundboxSandboxEnvironmentConfig(**config)
    if isinstance(config, GroundboxSandboxEnvironmentConfig):
        return _from_config(config, Path.cwd())
    if isinstance(config, ComposeConfig):
        return _from_compose(config, None)
    if isinstance(config, str):
        if is_dockerfile(config):
            return _from_dockerfile_path(Path(config), SandboxSpec())
        if is_compose_yaml(config):
            return _from_compose(parse_compose_yaml(config, multiple_services=True), config)
    raise ValueError(
        f"Unrecognized Groundbox sandbox config: {config!r}. Expected None, a "
        "GroundboxSandboxEnvironmentConfig, a Dockerfile, a compose file "
        "(*.yaml/*.yml) or a ComposeConfig."
    )


def _from_config(config: GroundboxSandboxEnvironmentConfig, base_dir: Path) -> SandboxSpec:
    sources = [n for n in ("image", "template", "dockerfile", "snapshot_id") if getattr(config, n)]
    if len(sources) > 1:
        raise ValueError(f"Set only one of image, template, dockerfile and snapshot_id (got {', '.join(sources)}).")
    spec = SandboxSpec(
        create={name: getattr(config, name) for name in _CREATE_FIELDS if getattr(config, name) is not None},
        user=config.user,
        working_dir=config.working_dir,
        ports=list(config.ports or []),
    )
    if config.dockerfile:
        path = Path(config.dockerfile)
        spec = _from_dockerfile_path(path if path.is_absolute() else base_dir / path, spec)
    return spec


def _from_dockerfile_path(path: Path, spec: SandboxSpec) -> SandboxSpec:
    if not path.exists():
        raise FileNotFoundError(f"Dockerfile not found: {path}")
    text = path.read_text(encoding="utf-8")
    _warn_build_context(text, path)
    user, working_dir = dockerfile_user_and_workdir(text)
    spec.dockerfile = text
    spec.user = spec.user or user
    spec.working_dir = spec.working_dir or working_dir
    return spec


def _warn_build_context(text: str, path: Path) -> None:
    for line in _instructions(text):
        word, _, rest = line.partition(" ")
        if word.upper() in ("COPY", "ADD") and "--from" not in rest:
            warn_once(
                logger,
                f"{path}: Groundbox builds a Dockerfile with no build context, so "
                f"'{line[:60]}' fails if it copies local files. Bake those files "
                "into an image you push to a registry, or write them with "
                "Sample.files / sandbox().write_file() instead.",
            )
            return


def _instructions(text: str) -> list[str]:
    """Dockerfile instructions with line continuations joined and comments dropped."""
    lines: list[str] = []
    current = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not current and (not stripped or stripped.startswith("#")):
            continue
        if stripped.endswith("\\"):
            current += stripped[:-1] + " "
            continue
        current += stripped
        lines.append(current.strip())
        current = ""
    if current.strip():
        lines.append(current.strip())
    return lines


def dockerfile_user_and_workdir(text: str) -> tuple[str | None, str | None]:
    """The final stage's USER and WORKDIR.

    Groundbox runs a template's commands as root in /root and does not apply
    these two, so the sandbox applies them itself, as Docker would.
    """
    user: str | None = None
    working_dir: str | None = None
    for line in _instructions(text):
        word, _, rest = line.partition(" ")
        word = word.upper()
        rest = rest.strip()
        if word == "FROM":
            user, working_dir = None, None
        elif word == "USER" and rest:
            user = _unquote(rest).split(":", 1)[0]
        elif word == "WORKDIR" and rest:
            path = _unquote(rest)
            working_dir = path if posixpath.isabs(path) else posixpath.join(working_dir or "/", path)
    return user, working_dir


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _from_compose(compose: ComposeConfig, compose_path: str | None) -> SandboxSpec:
    if len(compose.services) > 1:
        raise ValueError(
            f"The Groundbox sandbox runs one service per sample; this compose config has "
            f"{len(compose.services)} ({', '.join(compose.services)}). Multi-service compose "
            "is not supported yet."
        )
    _, service = find_default_service(compose)
    compose_dir = Path(compose_path).parent if compose_path else Path.cwd()

    ext: dict[str, Any] = {}
    ext.update(_extension(compose.extensions))
    ext.update(_extension(service.extensions))

    spec = SandboxSpec()
    if ext.get("template") or ext.get("image") or ext.get("snapshot_id"):
        pass
    elif service.build is not None:
        spec = _from_dockerfile_path(_dockerfile_path(service.build, compose_dir), spec)
    elif service.image:
        spec.create["image"] = service.image
    # No image and no build: Groundbox's own image.

    env: dict[str, str] = {}
    if service.environment:
        env.update(_parse_environment(service.environment))

    cpus = _service_cpus(service)
    if cpus is not None:
        spec.create["cpus"] = cpus
    memory_gb = _service_memory_gb(service)
    if memory_gb is not None:
        spec.create["memory_gb"] = memory_gb
    if service.network_mode == "none":
        spec.create["allow_hosts"] = []
    if service.user:
        spec.user = str(service.user).split(":", 1)[0]
    if service.working_dir:
        spec.working_dir = service.working_dir
    spec.ports = _service_ports(service)

    # x-groundbox wins over what the service says.
    if ext:
        overrides = GroundboxSandboxEnvironmentConfig(**ext)
        from_ext = _from_config(overrides, compose_dir)
        if from_ext.dockerfile is not None or any(k in from_ext.create for k in ("image", "template", "snapshot_id")):
            spec.dockerfile = from_ext.dockerfile
            for key in ("image", "template", "snapshot_id"):
                spec.create.pop(key, None)
        spec.create.update({k: v for k, v in from_ext.create.items() if k != "env"})
        env.update(from_ext.create.get("env") or {})
        spec.user = from_ext.user or spec.user
        spec.working_dir = from_ext.working_dir or spec.working_dir
        spec.ports = from_ext.ports or spec.ports
    if env:
        spec.create["env"] = env
    return spec


def _extension(extensions: dict[str, Any] | None) -> dict[str, Any]:
    raw = (extensions or {}).get(EXTENSION_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def find_default_service(config: ComposeConfig) -> tuple[str, ComposeService]:
    """`x-default: true`, else a service named `default` or `main`, else the first."""
    for name, service in config.services.items():
        if service.x_default:
            return name, service
    for name in ("default", "main"):
        if name in config.services:
            return name, config.services[name]
    name = next(iter(config.services))
    return name, config.services[name]


def _dockerfile_path(build: Any, compose_dir: Path) -> Path:
    if isinstance(build, str):
        return compose_dir / build / "Dockerfile"
    context = getattr(build, "context", None) or "."
    dockerfile = getattr(build, "dockerfile", None) or "Dockerfile"
    return compose_dir / context / dockerfile


def _parse_environment(environment: Any) -> dict[str, str]:
    if isinstance(environment, list):
        pairs = (item.split("=", 1) for item in environment if "=" in item)
        return {key: value for key, value in pairs}
    return {key: str(value) for key, value in environment.items() if value is not None}


def _service_cpus(service: ComposeService) -> int | None:
    value: Any = None
    resources = service.deploy.resources if service.deploy and service.deploy.resources else None
    if resources and resources.limits and resources.limits.cpus:
        value = resources.limits.cpus
    elif resources and resources.reservations and resources.reservations.cpus:
        value = resources.reservations.cpus
    elif service.cpus:
        value = service.cpus
    return max(1, math.ceil(float(value))) if value else None


def _service_memory_gb(service: ComposeService) -> int | None:
    value: Any = None
    resources = service.deploy.resources if service.deploy and service.deploy.resources else None
    if resources and resources.limits and resources.limits.memory:
        value = resources.limits.memory
    elif resources and resources.reservations and resources.reservations.memory:
        value = resources.reservations.memory
    elif service.mem_limit:
        value = service.mem_limit
    return max(1, math.ceil(parse_memory_mib(str(value)) / 1024)) if value else None


def parse_memory_mib(value: str) -> float:
    """`512m`, `1g`, `1.5gb`, `1024k` or bytes, in MiB."""
    match = re.match(r"^(\d+(?:\.\d+)?)\s*([kmgt]?)i?b?$", value.lower().strip())
    if not match:
        raise ValueError(f"Invalid memory size: {value!r} (expected e.g. 512m or 2g)")
    number, unit = float(match.group(1)), match.group(2)
    return number * {"": 1 / (1024 * 1024), "k": 1 / 1024, "m": 1, "g": 1024, "t": 1024 * 1024}[unit]


def _service_ports(service: ComposeService) -> list[int]:
    ports: list[int] = []
    for entry in service.ports or []:
        if isinstance(entry, dict):
            target, protocol = entry.get("target"), str(entry.get("protocol") or "tcp")
        else:
            text = str(entry)
            text, _, protocol = text.partition("/")
            target = text.rsplit(":", 1)[-1]
            protocol = protocol or "tcp"
        try:
            port = int(str(target))
        except (TypeError, ValueError):
            warn_once(logger, f"Groundbox previews skip the port entry {entry!r} (a range or unparseable).")
            continue
        if protocol != "tcp":
            warn_once(logger, f"Groundbox previews are HTTPS only; skipping the {protocol} port {entry!r}.")
            continue
        if port not in ports:
            ports.append(port)
    return ports
