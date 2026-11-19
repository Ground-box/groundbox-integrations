"""Unit tests against a fake Groundbox API (see fakes.py). No network, no API key."""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import pytest
from agents import RunConfig, Runner
from agents.sandbox import LocalSnapshotSpec, Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Shell
from agents.sandbox.entries import File
from agents.sandbox.errors import (
    ExecTimeoutError,
    ExecTransportError,
    WorkspaceReadNotFoundError,
    WorkspaceStartError,
)
from agents.sandbox.session import BaseSandboxClientOptions
from agents.testing import ScriptedModel, assistant_message, function_call
from fakes import FakeGroundbox, FakeStream, _error

from openai_agents_groundbox import (
    GroundboxSandboxClient,
    GroundboxSandboxClientOptions,
    GroundboxSandboxSession,
    GroundboxSandboxSessionState,
)
from openai_agents_groundbox.sandbox import _decode_snapshot_ref, _encode_snapshot_ref


@pytest.fixture
def gb(tmp_path: Path) -> FakeGroundbox:
    home = tmp_path / "home"
    home.mkdir()
    return FakeGroundbox(home)


@pytest.fixture
def client(gb: FakeGroundbox, monkeypatch: pytest.MonkeyPatch) -> GroundboxSandboxClient:
    streams: list[FakeStream] = []

    async def open_stream(self: GroundboxSandboxSession) -> FakeStream:
        stream = FakeStream(gb)
        streams.append(stream)
        return stream

    monkeypatch.setattr(GroundboxSandboxSession, "_open_stream", open_stream)
    instance = GroundboxSandboxClient(groundbox=gb)  # type: ignore[arg-type]
    instance.streams = streams  # type: ignore[attr-defined]
    return instance


@pytest.fixture
def manifest(tmp_path: Path) -> Manifest:
    return Manifest(root=str(tmp_path / "workspace"), entries={"notes.md": File(content=b"ship friday\n")})


def inner(session) -> GroundboxSandboxSession:  # type: ignore[no-untyped-def]
    return session._inner


# -- options and state ---------------------------------------------------------


def test_options_parse_from_dict() -> None:
    options = BaseSandboxClientOptions.parse({"type": "groundbox", "cpus": 4, "image": "python:3.12-slim"})
    assert isinstance(options, GroundboxSandboxClientOptions)
    assert options.cpus == 4
    assert options.model_dump()["type"] == "groundbox"


async def test_create_maps_options_and_state_round_trips(client, gb, manifest) -> None:
    options = GroundboxSandboxClientOptions(
        cpus=2,
        memory_gb=4,
        timeout_minutes=30,
        on_timeout="pause",
        env={"A": "1"},
        metadata={"team": "x"},
        allow_hosts=["github.com"],
        flex=True,
        exposed_ports=(3000,),
    )
    session = await client.create(manifest=manifest, options=options)
    (_, spec), = gb.called("create_sandbox")
    assert spec["idempotency_key"]
    assert {k: spec[k] for k in ("cpus", "memory_gb", "timeout_minutes", "on_timeout", "env", "allow_hosts", "flex")} == {
        "cpus": 2,
        "memory_gb": 4,
        "timeout_minutes": 30,
        "on_timeout": "pause",
        "env": {"A": "1"},
        "allow_hosts": ["github.com"],
        "flex": True,
    }
    assert spec["metadata"]["team"] == "x"
    assert spec["metadata"]["openai_agents_session"] == str(session.state.session_id)
    assert "image" not in spec and "disk_gb" not in spec

    payload = client.serialize_session_state(session.state)
    assert payload["type"] == "groundbox"
    assert "gbx_" not in json.dumps(payload)
    restored = client.deserialize_session_state(json.loads(json.dumps(payload)))
    assert isinstance(restored, GroundboxSandboxSessionState)
    assert restored.sandbox_id == session.state.sandbox_id
    assert restored.exposed_ports == (3000,)


async def test_create_that_does_not_run_is_deleted(client, gb, manifest) -> None:
    gb.create_status = "failed"
    with pytest.raises(WorkspaceStartError, match="the image does not exist"):
        await client.create(manifest=manifest, options=GroundboxSandboxClientOptions(image="nope:latest"))
    assert gb.called("delete_sandbox")


# -- exec and files ------------------------------------------------------------


async def test_start_exec_and_files(client, gb, manifest) -> None:
    session = await client.create(manifest=manifest, options=GroundboxSandboxClientOptions())
    async with session:
        result = await session.exec("pwd && cat notes.md && echo err >&2 && exit 3")
        assert result.exit_code == 3
        assert result.stdout.decode().splitlines() == [manifest.root, "ship friday"]
        assert result.stderr == b"err\n"

        await session.write(Path("out/data.bin"), io.BytesIO(b"\x00\xff"))
        assert (await session.read(Path("out/data.bin"))).read() == b"\x00\xff"
        names = {entry.path for entry in await session.ls(Path("."))}
        assert any(name.endswith("notes.md") for name in names)
        await session.mkdir(Path("a/b"), parents=True)
        assert gb.called("make_dir")
        await session.rm(Path("a"), recursive=True)
        assert not (Path(manifest.root) / "a").exists()

        with pytest.raises(WorkspaceReadNotFoundError):
            await inner(session).read(Path("missing.txt"))


async def test_exec_timeout_and_transport_errors(client, gb, manifest) -> None:
    session = await client.create(manifest=manifest, options=GroundboxSandboxClientOptions())
    async with session:
        with pytest.raises(ExecTimeoutError):
            await inner(session).exec("sleep 5", timeout=1)
        gb.fail_next_exec = _error("internal", "the box is gone", 503)
        with pytest.raises(ExecTransportError) as raised:
            await inner(session).exec("true")
        assert raised.value.retryable is True
        assert raised.value.context["http_status"] == 503


async def test_exec_falls_back_when_the_root_is_gone(client, gb, manifest) -> None:
    session = await client.create(manifest=manifest, options=GroundboxSandboxClientOptions())
    result = await inner(session)._exec_internal("pwd")  # before start(): the root does not exist yet
    assert result.ok()
    assert result.stdout.decode().strip() == str(gb.home)


async def test_long_exec_runs_in_the_background(client, gb, manifest) -> None:
    session = await client.create(manifest=manifest, options=GroundboxSandboxClientOptions(timeouts={"poll_s": 0.05}))
    async with session:
        result = await inner(session).exec("echo long", timeout=7200)
    assert result.stdout == b"long\n"
    assert gb.called("start_command")


# -- persistence and resume ----------------------------------------------------


async def test_tar_snapshot_restores_into_a_new_sandbox(client, gb, manifest, tmp_path) -> None:
    session = await client.create(
        manifest=manifest,
        snapshot=LocalSnapshotSpec(base_path=tmp_path / "snapshots"),
        options=GroundboxSandboxClientOptions(),
    )
    await session.start()
    await session.write(Path("kept.txt"), io.BytesIO(b"kept\n"))
    await session.stop()
    await session.shutdown()
    first_id = session.state.sandbox_id
    assert gb.sandboxes[first_id]["status"] == "stopped"

    # The workspace lives on this machine in the fake, so wipe it to prove the restore.
    shutil.rmtree(manifest.root)

    resumed = await client.resume(client.deserialize_session_state(client.serialize_session_state(session.state)))
    assert resumed.state.sandbox_id != first_id
    await resumed.start()
    assert (await resumed.read(Path("kept.txt"))).read() == b"kept\n"
    await resumed.aclose()


@pytest.mark.parametrize(
    ("status", "expected_call"),
    [("running", None), ("paused", "resume_sandbox"), ("shut_down", "start_sandbox")],
)
async def test_resume_reattaches_to_the_same_sandbox(client, gb, manifest, status, expected_call) -> None:
    session = await client.create(manifest=manifest, options=GroundboxSandboxClientOptions())
    gb.sandboxes[session.state.sandbox_id]["status"] = status
    resumed = await client.resume(session.state)
    assert resumed.state.sandbox_id == session.state.sandbox_id
    assert inner(resumed)._workspace_state_preserved_on_start()
    if expected_call:
        assert gb.called(expected_call)
    assert len(gb.called("create_sandbox")) == 1


async def test_shutdown_deletes_or_pauses(client, gb, manifest) -> None:
    session = await client.create(manifest=manifest, options=GroundboxSandboxClientOptions())
    await client.delete(session)
    assert gb.sandboxes[session.state.sandbox_id]["status"] == "stopped"

    paused = await client.create(manifest=manifest, options=GroundboxSandboxClientOptions(pause_on_exit=True))
    await client.delete(paused)
    assert gb.sandboxes[paused.state.sandbox_id]["status"] == "paused"


async def test_native_snapshot_persistence(client, gb, manifest, tmp_path) -> None:
    session = await client.create(
        manifest=manifest,
        snapshot=LocalSnapshotSpec(base_path=tmp_path / "snapshots"),
        options=GroundboxSandboxClientOptions(cpus=4, image="python:3.12-slim", workspace_persistence="snapshot"),
    )
    await session.start()
    await session.write(Path("model.txt"), io.BytesIO(b"trained\n"))
    await session.stop()
    await session.shutdown()
    (args, _), = gb.called("keep_snapshot")
    assert args == (session.state.sandbox_id,)
    snapshot_id = next(iter(gb.snapshots))

    resumed = await client.resume(session.state)
    spec = gb.called("create_sandbox")[-1][1]
    assert spec["snapshot_id"] == snapshot_id
    assert "cpus" not in spec and "image" not in spec
    assert resumed.state.restored_from_snapshot_id == snapshot_id
    creates = len(gb.called("create_sandbox"))
    await resumed.start()  # hydrate sees the same snapshot: no second replacement
    assert len(gb.called("create_sandbox")) == creates
    # The sandbox started from the snapshot is not cleared before use (the fake shares one disk).
    assert (await resumed.read(Path("model.txt"))).read() == b"trained\n"
    await resumed.aclose()


def test_snapshot_ref_round_trip() -> None:
    assert _decode_snapshot_ref(_encode_snapshot_ref("snap_1")) == "snap_1"
    assert _decode_snapshot_ref(b"\x1f\x8b not json") is None
    assert _decode_snapshot_ref(json.dumps({"snapshot_id": "x"}).encode()) is None


# -- ports ---------------------------------------------------------------------


async def test_exposed_ports(client, gb, manifest) -> None:
    private = await client.create(manifest=manifest, options=GroundboxSandboxClientOptions(exposed_ports=(3000,)))
    endpoint = await private.resolve_exposed_port(3000)
    assert (endpoint.host, endpoint.port, endpoint.tls, endpoint.query) == (
        "abc123-3000.groundbox.page",
        443,
        True,
        "gb_token=gbp_secret",
    )
    public = await client.create(
        manifest=manifest, options=GroundboxSandboxClientOptions(exposed_ports=(3000,), public_ports=True)
    )
    endpoint = await public.resolve_exposed_port(3000)
    assert endpoint.query == ""
    assert gb.called("expose_port")[-1][1] == {"public": True}


# -- PTY -----------------------------------------------------------------------


async def test_pty_with_stdin(client, gb, manifest) -> None:
    session = await client.create(manifest=manifest, options=GroundboxSandboxClientOptions())
    async with session:
        update = await session.pty_exec_start("cat", tty=True, yield_time_s=0.3)
        assert update.process_id is not None and update.exit_code is None
        update = await session.pty_write_stdin(session_id=update.process_id, chars="hello\n", yield_time_s=0.5)
        assert b"hello" in update.output
        stream = client.streams[-1]
        start = stream.sent[0]
        assert start["tty"] is True and start["stdin"] is True and start["resumable"] is True
        assert start["cwd"] == manifest.root
        await session.pty_terminate_all()
        assert stream.sent[-1] == {"type": "kill"}


async def test_pty_without_tty_finishes_and_refuses_stdin(client, gb, manifest) -> None:
    session = await client.create(manifest=manifest, options=GroundboxSandboxClientOptions())
    async with session:
        update = await session.pty_exec_start("echo a; sleep 0.5; echo b", yield_time_s=0.1)
        assert update.process_id is not None
        with pytest.raises(RuntimeError, match="stdin is not available"):
            await session.pty_write_stdin(session_id=update.process_id, chars="x")
        update = await session.pty_write_stdin(session_id=update.process_id, chars="", yield_time_s=2)
        assert update.process_id is None and update.exit_code == 0
        assert b"b" in update.output


# -- the Runner, end to end with a scripted model -------------------------------


async def test_runner_with_scripted_model(client, gb, manifest) -> None:
    model = ScriptedModel(
        [
            [function_call("exec_command", {"cmd": "cat notes.md"}, call_id="call-1")],
            [assistant_message("Friday.")],
        ]
    )
    agent = SandboxAgent(name="Groundbox test", model=model, default_manifest=manifest, capabilities=[Shell()])
    result = await Runner.run(
        agent,
        "When do we ship?",
        run_config=RunConfig(
            tracing_disabled=True,
            sandbox=SandboxRunConfig(client=client, options=GroundboxSandboxClientOptions()),
        ),
    )
    model.assert_complete()
    outputs = [str(item.output) for item in result.new_items if item.type == "tool_call_output_item"]
    assert any("ship friday" in output for output in outputs), outputs
    assert result.final_output == "Friday."
    # The runner owned the session: the sandbox is deleted afterwards.
    assert [s["status"] for s in gb.sandboxes.values()] == ["stopped"]
