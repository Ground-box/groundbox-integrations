"""Live tests against the Groundbox API. They run when GROUNDBOX_API_KEY is set.

Each test creates real sandboxes (1 vCPU, short timeouts) and deletes them. A final sweep
deletes any sandbox still tagged with this run's id. No model API key is needed: the Runner
test uses the SDK's ScriptedModel.
"""

from __future__ import annotations

import asyncio
import io
import json
import urllib.request
import uuid
from pathlib import Path

import pytest
from agents import RunConfig, Runner
from agents.sandbox import LocalSnapshotSpec, Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Shell
from agents.sandbox.entries import File
from agents.testing import ScriptedModel, assistant_message, function_call
from groundbox import Groundbox

from openai_agents_groundbox import GroundboxSandboxClient, GroundboxSandboxClientOptions

pytestmark = pytest.mark.live

RUN_ID = uuid.uuid4().hex[:12]


def options(**overrides: object) -> GroundboxSandboxClientOptions:
    base: dict[str, object] = {"cpus": 1, "timeout_minutes": 15, "metadata": {"openai_agents_test": RUN_ID}}
    base.update(overrides)
    return GroundboxSandboxClientOptions(**base)  # type: ignore[arg-type]


def manifest() -> Manifest:
    return Manifest(entries={"notes.md": File(content=b"# Notes\n\nShip on Friday.\n")})


@pytest.fixture(scope="module", autouse=True)
def sweep() -> object:
    yield
    gb = Groundbox()
    for sandbox in gb.list_sandboxes(live=True, metadata={"openai_agents_test": RUN_ID}).get("data", []):
        gb.delete_sandbox(sandbox["id"])


async def read_text(session, path: str) -> str:  # type: ignore[no-untyped-def]
    return (await session.read(Path(path))).read().decode()


async def test_exec_files_pty_and_user() -> None:
    client = GroundboxSandboxClient()
    session = await client.create(manifest=manifest(), options=options(env={"BASE": "base"}))
    try:
        async with session:
            result = await session.exec("pwd; id -un; echo $BASE; cat notes.md")
            assert result.exit_code == 0, result.stderr
            assert result.stdout.decode().splitlines()[:3] == ["/workspace", "runner", "base"]
            assert "Ship on Friday." in result.stdout.decode()

            as_root = await session.exec("id -un", user="root")
            assert as_root.stdout.decode().strip() == "root"

            await session.write(Path("bin/data.bin"), io.BytesIO(bytes(range(256))))
            assert (await session.read(Path("bin/data.bin"))).read() == bytes(range(256))
            raw = await session.exec("head -c 4 bin/data.bin | od -An -tx1")
            assert raw.stdout.split() == [b"00", b"01", b"02", b"03"]

            failed = await session.exec("echo oops >&2; exit 7")
            assert (failed.exit_code, failed.stderr) == (7, b"oops\n")

            repl = await session.pty_exec_start("python3 -i -q", tty=True, yield_time_s=2)
            assert repl.process_id is not None
            answer = await session.pty_write_stdin(session_id=repl.process_id, chars="print(6 * 7)\n", yield_time_s=2)
            assert b"42" in answer.output

            slow = await session.pty_exec_start("echo start; sleep 3; echo end", yield_time_s=0.5)
            assert slow.process_id is not None and b"start" in slow.output
            done = await session.pty_write_stdin(session_id=slow.process_id, chars="", yield_time_s=6)
            assert done.exit_code == 0 and b"end" in done.output
    finally:
        await client.delete(session)
    assert Groundbox().get_sandbox(session.state.sandbox_id)["status"] == "stopped"


async def test_tar_stop_and_resume_into_a_new_sandbox(tmp_path: Path) -> None:
    client = GroundboxSandboxClient()
    session = await client.create(
        manifest=manifest(), snapshot=LocalSnapshotSpec(base_path=tmp_path), options=options()
    )
    await session.start()
    await session.write(Path("progress.txt"), io.BytesIO(b"step 3 of 5\n"))
    await session.stop()
    await session.shutdown()
    first_id = session.state.sandbox_id

    payload = json.loads(json.dumps(client.serialize_session_state(session.state)))
    resumed = await client.resume(client.deserialize_session_state(payload))
    try:
        assert resumed.state.sandbox_id != first_id
        await resumed.start()
        assert await read_text(resumed, "progress.txt") == "step 3 of 5\n"
        assert "Ship on Friday." in await read_text(resumed, "notes.md")
    finally:
        await resumed.aclose()
        await client.delete(resumed)


async def test_resume_reattaches_running_and_paused_sandboxes() -> None:
    client = GroundboxSandboxClient()
    session = await client.create(manifest=manifest(), options=options(pause_on_exit=True))
    try:
        await session.start()
        await session.write(Path("live.txt"), io.BytesIO(b"still here\n"))
        await session.exec("nohup sleep 600 >/dev/null 2>&1 &")

        # Running: a new process picks up the same sandbox from serialized state.
        running = await client.resume(client.deserialize_session_state(client.serialize_session_state(session.state)))
        assert running.state.sandbox_id == session.state.sandbox_id
        assert await read_text(running, "live.txt") == "still here\n"

        # Paused on exit (memory kept): resume wakes the same sandbox, processes included.
        await session.shutdown()
        assert Groundbox().get_sandbox(session.state.sandbox_id)["status"] == "paused"
        woken = await client.resume(session.state)
        await woken.start()
        assert woken.state.sandbox_id == session.state.sandbox_id
        assert await read_text(woken, "live.txt") == "still here\n"
        assert (await woken.exec("pgrep -x sleep")).ok()
    finally:
        Groundbox().delete_sandbox(session.state.sandbox_id)


async def test_native_snapshot_persistence(tmp_path: Path) -> None:
    client = GroundboxSandboxClient()
    session = await client.create(
        manifest=manifest(),
        snapshot=LocalSnapshotSpec(base_path=tmp_path),
        options=options(workspace_persistence="snapshot"),
    )
    await session.start()
    await session.write(Path("model.txt"), io.BytesIO(b"trained\n"))
    await session.stop()
    await session.shutdown()
    first_id = session.state.sandbox_id

    resumed = await client.resume(session.state)
    snapshot_id = resumed.state.restored_from_snapshot_id
    try:
        assert snapshot_id and resumed.state.sandbox_id != first_id
        await resumed.start()
        assert await read_text(resumed, "model.txt") == "trained\n"
    finally:
        await resumed.aclose()
        await client.delete(resumed)
        # Every stop() keeps a new snapshot (aclose() above did too): delete this session's.
        gb = Groundbox()
        label = f"openai-agents {session.state.session_id}"
        for kept in gb.list_snapshots(limit=100).get("data", []):
            if kept.get("label") == label:
                gb.delete_snapshot(kept["id"])


async def test_exposed_port() -> None:
    client = GroundboxSandboxClient()
    session = await client.create(manifest=manifest(), options=options(exposed_ports=(8000,)))
    try:
        async with session:
            await session.exec("nohup python3 -m http.server 8000 --bind 0.0.0.0 >/tmp/http.log 2>&1 &")
            endpoint = await session.resolve_exposed_port(8000)
            assert endpoint.tls and endpoint.query.startswith("gb_token=")
            url = endpoint.url_for("http")
            body = ""
            for _ in range(30):
                try:
                    body = await asyncio.to_thread(lambda: urllib.request.urlopen(url, timeout=10).read().decode())
                    break
                except Exception:
                    await asyncio.sleep(1)
            assert "notes.md" in body
    finally:
        await client.delete(session)


async def test_docker_image_runs_as_root() -> None:
    client = GroundboxSandboxClient()
    session = await client.create(manifest=manifest(), options=options(image="python:3.12-slim"))
    try:
        async with session:
            result = await session.exec("id -un; python -V; cat notes.md")
            lines = result.stdout.decode().splitlines()
            assert lines[0] == "root" and lines[1].startswith("Python 3.12")
    finally:
        await client.delete(session)


async def test_runner_end_to_end_with_scripted_model() -> None:
    model = ScriptedModel(
        [
            [function_call("exec_command", {"cmd": "cat notes.md && uname -sm"}, call_id="call-1")],
            [assistant_message("Friday.")],
        ]
    )
    agent = SandboxAgent(name="Groundbox live", model=model, default_manifest=manifest(), capabilities=[Shell()])
    result = await Runner.run(
        agent,
        "When do we ship?",
        run_config=RunConfig(
            tracing_disabled=True,
            sandbox=SandboxRunConfig(client=GroundboxSandboxClient(), options=options()),
        ),
    )
    model.assert_complete()
    outputs = [str(item.output) for item in result.new_items if item.type == "tool_call_output_item"]
    assert any("Ship on Friday." in output and "Linux x86_64" in output for output in outputs), outputs
    left = Groundbox().list_sandboxes(live=True, metadata={"openai_agents_test": RUN_ID}).get("data", [])
    # The runner owned the session, so it deleted the sandbox.
    assert left == []
