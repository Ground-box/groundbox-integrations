from __future__ import annotations

from pathlib import Path

import pytest
from inspect_ai.util import SandboxUserUnsupportedError

from inspect_groundbox_sandbox import GroundboxSandboxEnvironment, GroundboxSandboxEnvironmentConfig
from inspect_groundbox_sandbox._config import dockerfile_user_and_workdir, resolve_spec
from inspect_groundbox_sandbox._shell import Identity, wrap_for_user


def test_registered_as_groundbox() -> None:
    from inspect_ai.util._sandbox.registry import registry_find_sandboxenv

    assert registry_find_sandboxenv("groundbox") is GroundboxSandboxEnvironment


def test_no_config_is_groundbox_image() -> None:
    spec = resolve_spec(None)
    assert spec.create == {} and spec.dockerfile is None and spec.user is None


def test_config_model_maps_to_create() -> None:
    spec = resolve_spec(
        GroundboxSandboxEnvironmentConfig(
            image="python:3.12-slim", cpus=4, allow_hosts=["pypi.org"], env={"A": "1"}, user="nobody", working_dir="/w"
        )
    )
    assert spec.create == {"image": "python:3.12-slim", "cpus": 4, "allow_hosts": ["pypi.org"], "env": {"A": "1"}}
    assert spec.user == "nobody" and spec.working_dir == "/w"


def test_config_from_dict_and_deserialize() -> None:
    assert resolve_spec({"template": "evals", "flex": True}).create == {"template": "evals", "flex": True}
    config = GroundboxSandboxEnvironment.config_deserialize({"cpus": 2})
    assert isinstance(config, GroundboxSandboxEnvironmentConfig) and config.cpus == 2


def test_config_rejects_two_sources_and_unknown_fields() -> None:
    with pytest.raises(ValueError, match="only one"):
        resolve_spec(GroundboxSandboxEnvironmentConfig(image="a", template="b"))
    with pytest.raises(ValueError):
        GroundboxSandboxEnvironmentConfig(imagee="typo")  # type: ignore[call-arg]


def test_dockerfile_user_and_workdir_from_final_stage() -> None:
    text = """
FROM golang AS build
USER builder
WORKDIR /src
FROM debian:12
RUN useradd agent \\
    && true
USER agent:agent
WORKDIR /home/agent
WORKDIR work
"""
    assert dockerfile_user_and_workdir(text) == ("agent", "/home/agent/work")
    assert dockerfile_user_and_workdir("FROM alpine\n") == (None, None)


def test_dockerfile_path_builds_template(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.12-slim\nUSER nobody\nWORKDIR /app\n")
    spec = resolve_spec(str(dockerfile))
    assert spec.dockerfile is not None and spec.user == "nobody" and spec.working_dir == "/app"
    name = spec.template_name()
    assert name is not None and name.startswith("inspect-") and name == resolve_spec(str(dockerfile)).template_name()


def test_compose_single_service(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yaml"
    compose.write_text(
        """
services:
  default:
    image: python:3.12-slim
    environment:
      - FOO=bar
    cpus: 1.5
    mem_limit: 3g
    network_mode: none
    user: "nobody"
    working_dir: /work
    ports:
      - "8080:80"
      - "53:53/udp"
x-groundbox:
  timeout_minutes: 120
  env:
    EXTRA: "1"
"""
    )
    spec = resolve_spec(str(compose))
    assert spec.create == {
        "image": "python:3.12-slim",
        "cpus": 2,
        "memory_gb": 3,
        "allow_hosts": [],
        "timeout_minutes": 120,
        "env": {"FOO": "bar", "EXTRA": "1"},
    }
    assert spec.user == "nobody" and spec.working_dir == "/work" and spec.ports == [80]


def test_compose_build_and_template_override(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM alpine\nWORKDIR /srv\n")
    compose = tmp_path / "compose.yaml"
    compose.write_text("services:\n  default:\n    build: .\n")
    spec = resolve_spec(str(compose))
    assert spec.dockerfile is not None and spec.working_dir == "/srv"

    compose.write_text("services:\n  default:\n    build: .\nx-groundbox:\n  template: prebuilt\n")
    spec = resolve_spec(str(compose))
    assert spec.dockerfile is None and spec.create["template"] == "prebuilt"


def test_compose_multi_service_is_refused(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yaml"
    compose.write_text("services:\n  default:\n    image: a\n  db:\n    image: b\n")
    with pytest.raises(ValueError, match="one service"):
        resolve_spec(str(compose))


def test_identity_parse() -> None:
    identity = Identity.parse("runner\n1001\n/home/runner\n/usr/bin/sudo\n/bin/su\n")
    assert identity == Identity("runner", 1001, "/home/runner", runuser=None, sudo="/usr/bin/sudo", su="/bin/su")
    assert identity.is_self(None) and identity.is_self("runner") and identity.is_self("1001")
    assert not identity.is_self("root")


def test_wrap_for_user() -> None:
    runner = Identity("runner", 1001, "/home/runner", sudo="/usr/bin/sudo")
    assert wrap_for_user(["whoami"], None, None, runner) == ["whoami"]
    assert wrap_for_user(["whoami"], "runner", None, runner) == ["whoami"]
    assert wrap_for_user(["whoami"], "root", {"A": "1"}, runner) == [
        "/usr/bin/sudo", "-n", "-H", "-u", "root", "--", "/usr/bin/env", "A=1", "whoami",
    ]  # fmt: skip
    assert wrap_for_user(["id"], "1000", None, runner)[4] == "#1000"

    root = Identity("root", 0, "/root", runuser="/usr/sbin/runuser", su="/bin/su")
    assert wrap_for_user(["id"], "nobody", None, root) == [
        "/usr/sbin/runuser",
        "-u",
        "nobody",
        "--",
        "/usr/bin/env",
        "id",
    ]
    alpine = Identity("root", 0, "/root", su="/bin/su")
    # BusyBox su reads a later "-c" as its own option, so it gets one quoted line and no arguments.
    assert wrap_for_user(["sh", "-c", "echo $1", "x", "a b"], "nobody", None, alpine) == [
        "/bin/su", "-s", "/bin/sh", "nobody", "-c", "exec /usr/bin/env sh -c 'echo $1' x 'a b'",
    ]  # fmt: skip

    bare = Identity("app", 1000, "/app")
    with pytest.raises(SandboxUserUnsupportedError):
        wrap_for_user(["id"], "root", None, bare)
