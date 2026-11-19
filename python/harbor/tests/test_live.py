"""Live test against Groundbox: runs only with GROUNDBOX_API_KEY set.

Starts real sandboxes (a few cents) and deletes them. Run with
``GROUNDBOX_API_KEY=gbx_... pytest -m live``.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from harbor.models.task.config import EnvironmentConfig, NetworkMode, NetworkPolicy
from harbor.models.trial.paths import TrialPaths

from groundbox_harbor import GroundboxSandbox

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("GROUNDBOX_API_KEY"), reason="GROUNDBOX_API_KEY is not set"
    ),
]


def _env(tmp_path: Path, dockerfile: str, files: dict[str, str] | None = None, **kwargs) -> GroundboxSandbox:
    env_dir = tmp_path / "environment"
    env_dir.mkdir()
    (env_dir / "Dockerfile").write_text(dockerfile)
    for rel, text in (files or {}).items():
        (env_dir / rel).write_text(text)
    trial_paths = TrialPaths(trial_dir=tmp_path / "trial")
    trial_paths.mkdir()
    return GroundboxSandbox(
        environment_dir=env_dir,
        environment_name="groundbox-harbor-live",
        session_id=f"live__{os.getpid()}__env",
        trial_paths=trial_paths,
        task_env_config=EnvironmentConfig(cpus=1, memory_mb=2048),
        network_policy=NetworkPolicy(network_mode=NetworkMode.PUBLIC),
        **kwargs,
    )


def test_dockerfile_with_copy_end_to_end(tmp_path: Path):
    dockerfile = (
        "FROM ubuntu:24.04\n"
        "RUN useradd -m agent\n"
        "WORKDIR /app\n"
        "COPY greet.sh /app/greet.sh\n"
        "RUN chmod +x /app/greet.sh\n"
    )
    env = _env(tmp_path, dockerfile, {"greet.sh": "#!/bin/bash\necho \"hello $1\"\n"})

    async def scenario() -> None:
        await env.start(force_build=False)
        try:
            result = await env.exec("./greet.sh groundbox && pwd && cat <(echo procsub)", timeout_sec=60)
            assert result.return_code == 0, result.stderr
            assert result.stdout.split() == ["hello", "groundbox", "/app", "procsub"]

            who = await env.exec("id -un; echo $HOME; echo $FOO", user="agent", env={"FOO": "bar"}, timeout_sec=60)
            assert who.stdout.split() == ["agent", "/home/agent", "bar"]

            # Background path (no timeout) and a non-zero exit.
            bg = await env.exec("echo out; echo err >&2; exit 7")
            assert (bg.stdout, bg.stderr, bg.return_code) == ("out\n", "err\n", 7)

            src = tmp_path / "upload"
            (src / "sub").mkdir(parents=True)
            (src / "sub" / "x.txt").write_text("x")
            script = src / "run.sh"
            script.write_text("#!/bin/sh\necho ran\n")
            script.chmod(0o755)
            await env.upload_dir(src, "/data")
            await env.upload_file(script, "/usr/local/bin/run-it")
            ran = await env.exec("run-it && cat /data/sub/x.txt", timeout_sec=60)
            assert ran.stdout == "ran\nx"
            assert await env.is_dir("/data/sub") and await env.is_file("/data/run.sh")

            out = tmp_path / "out"
            await env.download_dir("/data", out)
            assert (out / "sub" / "x.txt").read_text() == "x"
            await env.download_file("/app/greet.sh", out / "greet.sh")
            assert "hello" in (out / "greet.sh").read_text()

            with pytest.raises(RuntimeError, match="timed out"):
                await env.exec("sleep 30", timeout_sec=2)

            await env.set_network_policy(NetworkPolicy(network_mode=NetworkMode.NO_NETWORK))
            blocked = await env.exec(
                "timeout 5 bash -c 'echo > /dev/tcp/1.1.1.1/443' && echo open || echo blocked",
                timeout_sec=30,
            )
            assert blocked.stdout.strip() == "blocked"
        finally:
            await env.stop(delete=True)

    asyncio.run(scenario())
