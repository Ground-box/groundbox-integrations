"""Unit tests for GroundboxSandbox against a fake Groundbox client."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from harbor.environments.base import SandboxBuildFailedError
from harbor.models.task.config import NetworkMode, NetworkPolicy
from harbor.models.trial.config import ResourceMode

from groundbox_harbor import GroundboxSandbox
from groundbox_harbor import environment as env_module

from .conftest import err


@pytest.fixture(autouse=True)
def _reset_template_state():
    env_module._TemplateLocks.locks.clear()
    env_module._TemplateLocks.in_use.clear()
    env_module._TemplateLocks.force_built.clear()
    yield


PREPARE = {"exit_code": 0, "stdout": "/usr/bin/bash\n/usr/local/bin:/usr/bin:/bin\n", "stderr": ""}


def _start(env: GroundboxSandbox, fake, force_build: bool = False) -> None:
    fake.exec_results.insert(0, dict(PREPARE))
    asyncio.run(env.start(force_build=force_build))


def test_type_and_capabilities(make_env):
    env = make_env()
    assert env.type() == "groundbox"
    caps = env.capabilities
    assert caps.disable_internet and caps.network_allowlist and caps.dynamic_network_policy
    assert caps.network_allowlist_wildcard_hostnames and caps.network_allowlist_ipv4_cidrs
    assert not caps.network_allowlist_ipv6_addresses
    assert not (caps.gpus or caps.windows or caps.docker_compose or caps.mounted)


def test_preflight_needs_key(monkeypatch):
    monkeypatch.delenv("GROUNDBOX_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        GroundboxSandbox.preflight({})
    monkeypatch.setenv("GROUNDBOX_API_KEY", "gbx_x")
    GroundboxSandbox.preflight({"sandbox_timeout_minutes": 30})


def test_unknown_option_is_rejected():
    with pytest.raises(ValueError, match="Unknown option"):
        GroundboxSandbox.parse_options({"nope": 1})


def test_compose_only_task_is_rejected(make_env, tmp_path):
    with pytest.raises(ValueError, match="docker-compose"):
        make_env(dockerfile=None, files={"docker-compose.yaml": "services: {}\n"})


def test_prebuilt_image_starts_from_image(make_env, fake):
    env = make_env(docker_image="alexgshaw/regex-log:20251031")
    _start(env, fake)
    (_, create), = fake.named("create_sandbox")
    assert create["image"] == "alexgshaw/regex-log:20251031"
    assert create["cpus"] == 1 and create["memory_gb"] == 2 and create["disk_gb"] == 10
    assert "allow_hosts" not in create  # public network
    assert create["metadata"]["harbor_session_id"] == "hello__abc__env"
    assert create["idempotency_key"]
    assert not fake.named("create_template")
    assert env.sandbox_id == "sbx_1"


def test_force_build_builds_template(make_env, fake):
    env = make_env(docker_image="alexgshaw/regex-log:20251031")
    _start(env, fake, force_build=True)
    assert fake.named("create_template")
    (_, create), = fake.named("create_sandbox")
    assert create["template"].startswith("harbor-") and create["template"].endswith(":1")
    assert "image" not in create


def test_dockerfile_template_is_built_once_and_reused(make_env, fake):
    first = make_env(name="a")
    _start(first, fake)
    (_, spec), = fake.named("create_template")
    assert spec["name"].startswith("harbor-")
    assert spec["dockerfile"] == "FROM ubuntu:24.04\nWORKDIR /app\n"
    assert spec["cpus"] == 1 and spec["memory_gb"] == 2 and spec["disk_gb"] == 10
    second = make_env(name="b")
    _start(second, fake)
    assert len(fake.named("create_template")) == 1
    templates = [k["template"] for _, k in fake.named("create_sandbox")]
    assert templates[0].split(":")[0] == templates[1].split(":")[0]


def test_failed_build_raises(make_env, fake):
    fake.build_status = "failed"
    env = make_env()
    with pytest.raises(SandboxBuildFailedError, match="failed"):
        _start(env, fake)
    assert not fake.named("create_sandbox")


def test_template_limit_evicts_oldest_unused_harbor_template(make_env, fake):
    fake.templates["harbor-old"] = {"name": "harbor-old", "status": "ready", "created_at": "2026-01-01"}
    fake.templates["mine"] = {"name": "mine", "status": "ready", "created_at": "2025-01-01"}
    fake.errors["create_template"] = [
        err("limit_reached", 409, "A team keeps up to 20 templates")
    ]
    env = make_env()
    _start(env, fake)
    assert [a for a, _ in fake.named("delete_template")] == [("harbor-old",)]
    assert "mine" in fake.templates


def test_create_retries_on_concurrency_limit(make_env, fake, monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(env_module.asyncio, "sleep", no_sleep)
    fake.errors["create_sandbox"] = [err("concurrency_limit", 429, "busy")]
    env = make_env(docker_image="ubuntu:24.04", dockerfile=None)
    _start(env, fake)
    keys = [k["idempotency_key"] for _, k in fake.named("create_sandbox")]
    assert len(keys) == 2 and keys[0] != keys[1]


def test_no_network_and_allowlist(make_env, fake):
    env = make_env(network_policy=NetworkPolicy(network_mode=NetworkMode.NO_NETWORK))
    _start(env, fake)
    assert fake.named("create_sandbox")[0][1]["allow_hosts"] == []

    env2 = make_env(
        name="allow",
        network_policy=NetworkPolicy(
            network_mode=NetworkMode.ALLOWLIST, allowed_hosts=["pypi.org", "*.github.com"]
        ),
    )
    _start(env2, fake)
    assert fake.named("create_sandbox")[1][1]["allow_hosts"] == ["pypi.org", "*.github.com"]
    asyncio.run(env2.set_network_policy(NetworkPolicy(network_mode=NetworkMode.PUBLIC)))
    (args, kwargs), = fake.named("update_sandbox")
    assert args == ("sbx_2",) and kwargs == {"allow_hosts": None}


def test_ipv6_allowlist_is_rejected(make_env):
    with pytest.raises(ValueError, match="IPv6"):
        make_env(
            network_policy=NetworkPolicy(
                network_mode=NetworkMode.ALLOWLIST, allowed_hosts=["2001:db8::1"]
            )
        )


def test_memory_over_four_gb_per_cpu_raises_cpus(make_env, fake):
    env = make_env(docker_image="ubuntu:24.04", cpus=1, memory_mb=8192)
    _start(env, fake)
    create = fake.named("create_sandbox")[0][1]
    assert create["cpus"] == 2 and create["memory_gb"] == 8


def test_memory_over_cpu_limit_is_an_error(make_env, fake):
    env = make_env(
        docker_image="ubuntu:24.04",
        cpus=1,
        memory_mb=8192,
        cpu_enforcement_policy=ResourceMode.LIMIT,
    )
    with pytest.raises(ValueError, match="per vCPU"):
        _start(env, fake)


def test_fractional_cpus_rejected(make_env):
    with pytest.raises(ValueError, match="whole CPU"):
        make_env(cpus=1.5)


def test_prepare_records_shell_and_path(make_env, fake):
    env = make_env()
    _start(env, fake)
    prepare = fake.named("run_command")[0][0][1]
    assert prepare[0] == "sh" and "fd:/proc/self/fd" in prepare[2]
    assert "overcommit_memory" in prepare[2]
    assert env._shell == "/usr/bin/bash"
    assert env._image_path == "/usr/local/bin:/usr/bin:/bin"


def _started(make_env, fake, **kwargs) -> GroundboxSandbox:
    env = make_env(**kwargs)
    _start(env, fake)
    fake.calls.clear()
    return env


def test_exec_as_root_in_workdir(make_env, fake):
    env = _started(make_env, fake)
    fake.exec_results.append({"exit_code": 3, "stdout": "out", "stderr": "err", "timed_out": False})
    result = asyncio.run(env.exec("echo hi", env={"A": "1"}, timeout_sec=30))
    assert (result.return_code, result.stdout, result.stderr) == (3, "out", "err")
    (args, kwargs), = fake.named("run_command")
    assert args[1] == ["/usr/bin/bash", "-c", "cd /app && echo hi"]
    assert kwargs["env"] == {"A": "1"} and kwargs["timeout_s"] == 30


def test_exec_defaults_to_root_dir_without_workdir(make_env, fake):
    env = _started(make_env, fake, dockerfile="FROM ubuntu:24.04\n")
    asyncio.run(env.exec("pwd", timeout_sec=5))
    assert fake.named("run_command")[0][0][1][-1] == "cd / && pwd"


def test_exec_as_other_user_uses_su_and_keeps_path(make_env, fake):
    env = _started(make_env, fake)
    asyncio.run(env.exec("whoami", user="agent", env={"K": "v w"}, timeout_sec=5))
    argv = fake.named("run_command")[0][0][1]
    assert argv[:4] == ["su", "agent", "-s", "/usr/bin/bash"]
    assert argv[5].startswith("export PATH=/usr/local/bin:/usr/bin:/bin K='v w'; cd /app && whoami")


def test_exec_numeric_uid(make_env, fake):
    env = _started(make_env, fake)
    asyncio.run(env.exec("id", user=1000, timeout_sec=5))
    argv = fake.named("run_command")[0][0][1]
    assert argv[0] == "sh" and "getent passwd 1000" in argv[2]


def test_dockerfile_user_is_the_default(make_env, fake):
    env = _started(make_env, fake, dockerfile="FROM ubuntu:24.04\nUSER agent:agent\n")
    assert env.default_exec_user == "agent"
    asyncio.run(env.exec("id", timeout_sec=5))
    assert fake.named("run_command")[0][0][1][:2] == ["su", "agent"]
    asyncio.run(env.exec("id", user="root", timeout_sec=5))
    assert fake.named("run_command")[1][0][1][0] == "/usr/bin/bash"


def test_exec_timeout_raises(make_env, fake):
    env = _started(make_env, fake)
    fake.exec_results.append({"exit_code": 137, "stdout": "", "stderr": "", "timed_out": True})
    with pytest.raises(RuntimeError, match="timed out"):
        asyncio.run(env.exec("sleep 99", timeout_sec=1))


def test_long_exec_runs_in_background_and_polls(make_env, fake):
    env = _started(make_env, fake)
    fake.output_pages = [
        {"stdout": "a", "stderr": "", "seq": 1, "more": True, "done": False},
        {"stdout": "", "stderr": "", "seq": 1, "more": False, "done": False},
        {"stdout": "b", "stderr": "e", "seq": 3, "more": False, "done": True, "exit_code": 0},
    ]
    result = asyncio.run(env.exec("make", timeout_sec=None))
    assert (result.stdout, result.stderr, result.return_code) == ("ab", "e", 0)
    assert not fake.named("run_command")
    seqs = [k["seq"] for _, k in fake.named("get_command_output")]
    assert seqs == [0, 1, 1]


def test_no_capacity_is_retried_before_the_command_starts(make_env, fake, monkeypatch):
    async def no_sleep(_):
        return None

    env = _started(make_env, fake)
    monkeypatch.setattr(env_module.asyncio, "sleep", no_sleep)
    fake.errors["run_command"] = [err("no_capacity", 503, "box not reachable")]
    result = asyncio.run(env.exec("true", timeout_sec=5))
    assert result.return_code == 0
    assert len(fake.named("run_command")) == 2


def test_rate_limit_waits_for_the_hinted_time(make_env, fake, monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    env = _started(make_env, fake)
    monkeypatch.setattr(env_module.asyncio, "sleep", fake_sleep)
    fake.errors["run_command"] = [
        err("rate_limited", 429, "Too many requests from this API key. Try again in 12 seconds")
    ]
    asyncio.run(env.exec("true", timeout_sec=5))
    assert slept == [13.0]


def test_queued_image_sandbox_is_waited_for(make_env, fake):
    fake.sandbox_status = "queued"
    env = make_env(docker_image="python:3.12-slim", dockerfile=None)
    _start(env, fake)
    assert fake.named("get_sandbox")


def test_dropped_connection_is_not_retried(make_env, fake):
    env = _started(make_env, fake)
    fake.errors["run_command"] = [err("network", 0, "connection reset")]
    with pytest.raises(Exception, match="connection reset"):
        asyncio.run(env.exec("true", timeout_sec=5))
    assert len(fake.named("run_command")) == 1


def test_upload_large_file_in_chunks(make_env, fake, tmp_path, monkeypatch):
    monkeypatch.setattr(env_module, "UPLOAD_CHUNK_BYTES", 10)
    env = _started(make_env, fake)
    source = tmp_path / "big.bin"
    source.write_bytes(b"x" * 25)
    asyncio.run(env.upload_file(source, "/data/big.bin"))
    parts = [a[1] for a, _ in fake.named("write_file")]
    assert len(parts) == 3 and all(p.startswith("/tmp/.harbor-upload-") for p in parts)
    commands = [a[1][2] for a, _ in fake.named("run_command") + fake.named("start_command")]
    assert any("cat " in c and "> /data/big.bin" in c for c in commands)
    assert any(c.startswith("cd / && chmod ") for c in commands)


def test_download_file(make_env, fake, tmp_path):
    env = _started(make_env, fake)
    fake.files["/logs/out.txt"] = b"hello"
    target = tmp_path / "out" / "out.txt"
    asyncio.run(env.download_file("/logs/out.txt", target))
    assert target.read_bytes() == b"hello"


def test_is_file_and_is_dir(make_env, fake):
    env = _started(make_env, fake)
    fake.files["/a"] = b""
    assert asyncio.run(env.is_file("/a"))
    assert not asyncio.run(env.is_dir("/a"))
    assert not asyncio.run(env.is_file("/missing"))


def test_stop_deletes_and_tolerates_missing(make_env, fake):
    env = _started(make_env, fake)
    fake.errors["delete_sandbox"] = [err("not_found", 404)]
    asyncio.run(env.stop(delete=True))
    assert fake.named("delete_sandbox") == [(("sbx_1",), {})]
    assert env.sandbox_id is None
    asyncio.run(env.stop(delete=True))  # already gone: no call
    assert len(fake.named("delete_sandbox")) == 1


def test_failed_start_deletes_the_sandbox(make_env, fake):
    env = make_env(docker_image="ubuntu:24.04", dockerfile=None)
    fake.errors["run_command"] = [err("bad_request", 400, "boom")]
    with pytest.raises(Exception, match="boom"):
        asyncio.run(env.start(force_build=False))
    assert fake.named("delete_sandbox") == [(("sbx_1",), {})]
    assert env.sandbox_id is None


def test_delete_retries_rate_limit(make_env, fake, monkeypatch):
    async def no_sleep(_):
        return None

    env = _started(make_env, fake)
    monkeypatch.setattr(env_module.asyncio, "sleep", no_sleep)
    fake.errors["delete_sandbox"] = [err("rate_limited", 429, "Try again in 2 seconds")]
    asyncio.run(env.stop(delete=True))
    assert len(fake.named("delete_sandbox")) == 2


def test_template_option_skips_build(make_env, fake):
    env = make_env(dockerfile=None, docker_image=None, template="my-template:3")
    _start(env, fake)
    assert not fake.named("create_template")
    assert fake.named("create_sandbox")[0][1]["template"] == "my-template:3"


def test_renew_interval_must_be_shorter_than_timeout():
    with pytest.raises(ValueError, match="renew_interval_sec"):
        GroundboxSandbox.parse_options({"sandbox_timeout_minutes": 5, "renew_interval_sec": 600})


