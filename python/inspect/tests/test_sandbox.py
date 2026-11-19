"""The sandbox against a fake Groundbox client: no network, no key."""

from __future__ import annotations

import base64
from collections.abc import Iterator

import pytest
from inspect_ai.util import OutputLimitExceededError, SandboxUnavailableError

from inspect_groundbox_sandbox import GroundboxSandboxEnvironment, GroundboxSandboxEnvironmentConfig, _client, _sandbox

from .fakes import PROBE_ROOT, FakeGroundbox, api_error, exec_answer


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGroundbox]:
    client = FakeGroundbox()
    _client.set_client(client)
    monkeypatch.setattr(_sandbox, "START_TIMEOUT_SECONDS", 5.0)

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(_sandbox.anyio, "sleep", no_sleep)
    monkeypatch.setattr(_client.anyio, "sleep", no_sleep)
    yield client
    _client.set_client(None)


async def make_env(config: object = None) -> GroundboxSandboxEnvironment:
    await GroundboxSandboxEnvironment.task_init("task", config)  # type: ignore[arg-type]
    envs = await GroundboxSandboxEnvironment.sample_init("task", config, {"__sample_id__": "s1"})  # type: ignore[arg-type]
    return envs["default"].as_type(GroundboxSandboxEnvironment)


async def test_sample_init_tags_sandbox(fake: FakeGroundbox) -> None:
    env = await make_env(GroundboxSandboxEnvironmentConfig(cpus=4, metadata={"team": "evals"}))
    ((_, kwargs),) = fake.calls_to("create_sandbox")
    assert kwargs["cpus"] == 4
    metadata = kwargs["metadata"]
    assert metadata["created_by"] == "inspect-ai" and metadata["team"] == "evals" and metadata["task"] == "task"
    assert metadata["name"].startswith("inspect-task-s1-") and metadata["inspect_run_id"]
    assert kwargs["idempotency_key"]
    assert env.sandbox_id == "sbx_fake0"


async def test_create_waits_for_room_with_new_keys_and_retries_lost_answers_with_same_key(fake: FakeGroundbox) -> None:
    fake.create_errors = [api_error("concurrency_limit", 429), api_error("network", 0)]
    await make_env()
    keys = [kwargs["idempotency_key"] for _, kwargs in fake.calls_to("create_sandbox")]
    assert len(keys) == 3
    assert keys[0] != keys[1]  # refused: a new request
    assert keys[1] == keys[2]  # no answer: the same request again


async def test_create_gives_up_on_a_hard_error(fake: FakeGroundbox) -> None:
    fake.create_errors = [api_error("bad_request", 400, "cpus must be 1 to 16")]
    with pytest.raises(Exception, match="cpus"):
        await make_env()


async def test_failed_start_is_deleted_and_raised(fake: FakeGroundbox) -> None:
    fake.create_status = "failed"
    with pytest.raises(RuntimeError, match="image not found"):
        await make_env(GroundboxSandboxEnvironmentConfig(image="nope:latest"))
    assert fake.deleted == ["sbx_fake0"]


async def test_exec_decodes_output_and_passes_options(fake: FakeGroundbox) -> None:
    env = await make_env()
    fake.exec_handler = lambda cmd, kw: exec_answer(3, "café\n".encode(), b"\xff oops") if cmd == ["ls"] else None
    result = await env.exec(["ls"], cwd="/tmp", env={"A": "1"}, timeout=5, input="hi")
    assert (result.returncode, result.success, result.stdout, result.stderr) == (3, False, "café\n", "� oops")
    _, kwargs = fake.calls_to("run_command")[-1]
    assert kwargs == {"cwd": "/tmp", "env": {"A": "1"}, "timeout_s": 5, "stdin": "hi", "encoding": "base64"}


async def test_exec_relative_cwd_and_user(fake: FakeGroundbox) -> None:
    env = await make_env()
    await env.exec(["whoami"], cwd="sub", user="root")
    args, kwargs = fake.calls_to("run_command")[-1]
    assert kwargs["cwd"] == "/home/runner/sub"
    assert args[1][:6] == ["/usr/bin/sudo", "-n", "-H", "-u", "root", "--"]


async def test_exec_timeout_raises(fake: FakeGroundbox) -> None:
    env = await make_env()
    fake.exec_handler = lambda cmd, kw: exec_answer(137, b"partial", timed_out=True)
    with pytest.raises(TimeoutError) as info:
        await env.exec(["sleep", "10"], timeout=1)
    assert info.value.truncated_output == "partial"


async def test_exec_not_executable_is_permission_error(fake: FakeGroundbox) -> None:
    env = await make_env()
    fake.exec_handler = lambda cmd, kw: exec_answer(
        127, stderr=b"Could not start the command: spawn /etc/passwd EACCES\n"
    )
    with pytest.raises(PermissionError):
        await env.exec(["/etc/passwd"])
    fake.exec_handler = lambda cmd, kw: exec_answer(127, stderr=b"Could not start the command: spawn nope ENOENT\n")
    assert (await env.exec(["nope"])).returncode == 127


async def test_exec_on_stopped_sandbox_is_unavailable(fake: FakeGroundbox) -> None:
    env = await make_env()
    fake.exec_error = api_error("conflict", 409, "The sandbox is not running")
    with pytest.raises(SandboxUnavailableError):
        await env.exec(["true"])


async def test_exec_missing_cwd_is_a_failed_result(fake: FakeGroundbox) -> None:
    env = await make_env()
    fake.exec_error = api_error("bad_request", 400, "cwd /nope is not a folder")
    result = await env.exec(["ls"], cwd="/nope")
    assert not result.success and "not a folder" in result.stderr


async def test_exec_large_stdin_and_command_go_up_as_files(fake: FakeGroundbox) -> None:
    env = await make_env()
    big = b"x" * (_sandbox.INLINE_STDIN_BYTES + 1)
    await env.exec(["wc", "-c"], input=big)
    args, kwargs = fake.calls_to("run_command")[-1]
    assert kwargs["stdin"] is None
    assert args[1][:3] == ["/bin/sh", "-c", _sandbox.STDIN_FROM_FILE_SCRIPT]
    uploaded = args[1][4]
    assert fake.files[uploaded] == big
    assert fake.calls_to("delete_file")[-1][0][1] == uploaded.rsplit("/", 1)[0]

    long_args = ["printf", "%s"] + ["y" * 65536] * 4
    await env.exec(long_args)
    args, _ = fake.calls_to("run_command")[-1]
    assert args[1][0] == "/bin/sh" and fake.files[args[1][1]].startswith(b"exec printf %s yyyy")


async def test_exec_output_over_limit(fake: FakeGroundbox, monkeypatch: pytest.MonkeyPatch) -> None:
    env = await make_env()
    monkeypatch.setattr(_sandbox.SandboxEnvironmentLimits, "MAX_EXEC_OUTPUT_SIZE", 4)
    fake.exec_handler = lambda cmd, kw: exec_answer(0, b"0123456789")
    with pytest.raises(OutputLimitExceededError):
        await env.exec(["cat", "big"])


def read_answer(size: int, content: bytes = b"", code: int = 0, mark: str = "") -> dict:
    stderr = f"inspect-groundbox: {mark}\n" if mark else f"inspect-groundbox: SIZE {size}\n"
    return exec_answer(code, content, stderr.encode())


async def test_read_file(fake: FakeGroundbox) -> None:
    env = await make_env()
    fake.exec_handler = lambda cmd, kw: read_answer(5, b"hi\r\n!") if "inspect-read" in cmd else None
    assert await env.read_file("notes.txt") == "hi\r\n!"
    args, _ = fake.calls_to("run_command")[-1]
    assert args[1][4] == "/home/runner/notes.txt"
    assert await env.read_file("/x", text=False) == b"hi\r\n!"


@pytest.mark.parametrize(
    ("code", "mark", "error"),
    [(2, "ENOENT", FileNotFoundError), (21, "EISDIR", IsADirectoryError), (13, "EACCES", PermissionError)],
)
async def test_read_file_errors(fake: FakeGroundbox, code: int, mark: str, error: type[Exception]) -> None:
    env = await make_env()
    fake.exec_handler = lambda cmd, kw: read_answer(0, code=code, mark=mark) if "inspect-read" in cmd else None
    with pytest.raises(error) as info:
        await env.read_file("/data/f.txt")
    assert "/data/f.txt" in str(info.value)


async def test_read_large_file_uses_files_api_and_limit(fake: FakeGroundbox, monkeypatch: pytest.MonkeyPatch) -> None:
    env = await make_env()
    size = _sandbox.INLINE_READ_BYTES + 10
    fake.files["/big"] = b"z" * size
    fake.exec_handler = lambda cmd, kw: read_answer(size) if "inspect-read" in cmd else None
    assert len(await env.read_file("/big", text=False)) == size
    monkeypatch.setattr(_sandbox.SandboxEnvironmentLimits, "MAX_READ_FILE_SIZE", 100)
    with pytest.raises(OutputLimitExceededError):
        await env.read_file("/big")


async def test_write_file_small_and_large(fake: FakeGroundbox) -> None:
    env = await make_env()
    await env.write_file("out/a.txt", "hello")
    args, kwargs = fake.calls_to("run_command")[-1]
    assert args[1][3:] == ["inspect-write", "/home/runner/out/a.txt", "-"] and kwargs["stdin"] == b"hello"

    big = b"q" * (_sandbox.UPLOAD_CHUNK_BYTES + 5)
    await env.write_file("/tmp/big.bin", big)
    parts = [a[1] for a, _ in fake.calls_to("write_file")]
    assert len(parts) == 2 and parts[0].endswith("part-00000")
    args, _ = fake.calls_to("run_command")[-1]
    assert args[1][3] == "inspect-write" and args[1][5].endswith("/data")


async def test_write_file_as_user_and_errors(fake: FakeGroundbox) -> None:
    env = await make_env()
    await env.write_file("/srv/x", "1", user="root")
    args, _ = fake.calls_to("run_command")[-1]
    assert args[1][:5] == ["/usr/bin/sudo", "-n", "-H", "-u", "root"]

    fake.exec_handler = lambda cmd, kw: exec_answer(2, stderr=b"sh: 1: cannot create /etc/x: Permission denied\n")
    with pytest.raises(PermissionError):
        await env.write_file("/etc/x", "1")
    fake.exec_handler = lambda cmd, kw: exec_answer(21, stderr=b"inspect-groundbox: EISDIR\n")
    with pytest.raises(IsADirectoryError):
        await env.write_file("/etc", "1")


async def test_root_image_defaults_and_configured_user(fake: FakeGroundbox) -> None:
    fake.probe = PROBE_ROOT
    env = await make_env(GroundboxSandboxEnvironmentConfig(image="debian", user="agent", working_dir="/work"))
    workdir_call = fake.calls_to("run_command")[1][0][1]
    assert workdir_call[:3] == [
        "/bin/sh",
        "-c",
        'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; mkdir -p -- "$1" && chown -- "$2" "$1"',
    ]
    await env.exec(["id"])
    args, kwargs = fake.calls_to("run_command")[-1]
    assert args[1] == ["/usr/sbin/runuser", "-u", "agent", "--", "/usr/bin/env", "id"] and kwargs["cwd"] == "/work"


async def test_cleanup(fake: FakeGroundbox, capsys: pytest.CaptureFixture[str]) -> None:
    env = await make_env()
    await GroundboxSandboxEnvironment.sample_cleanup("task", None, {"default": env}, interrupted=True)
    assert fake.deleted == []  # interrupted: left for task_cleanup
    fake.listed = [{"id": "sbx_orphan"}, {"id": env.sandbox_id}]
    await GroundboxSandboxEnvironment.task_cleanup("task", None, cleanup=True)
    assert fake.deleted == [env.sandbox_id, "sbx_orphan"]
    ((_, kwargs),) = fake.calls_to("list_sandboxes")
    assert kwargs["live"] is True and set(kwargs["metadata"]) == {"inspect_run_id"}

    env = await make_env()
    await GroundboxSandboxEnvironment.task_cleanup("task", None, cleanup=False)
    assert env.sandbox_id in capsys.readouterr().out

    await GroundboxSandboxEnvironment.cli_cleanup("sbx_given")
    assert fake.deleted[-1] == "sbx_given"


async def test_dockerfile_is_built_once(fake: FakeGroundbox, tmp_path) -> None:  # type: ignore[no-untyped-def]
    _sandbox._templates_ready.clear()
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.12-slim\nRUN pip install numpy\n")
    env = await make_env(str(dockerfile))
    await GroundboxSandboxEnvironment.sample_init("task", str(dockerfile), {})
    assert len(fake.calls_to("create_template")) == 1
    (name,), kwargs = fake.calls_to("create_template")[0]
    assert kwargs["dockerfile"].startswith("FROM python") and name.startswith("inspect-")
    assert all(k["template"] == name for _, k in fake.calls_to("create_sandbox"))
    assert env.sandbox_id


async def test_connection(fake: FakeGroundbox) -> None:
    env = await make_env(GroundboxSandboxEnvironmentConfig(ports=[8080]))
    connection = await env.connection()
    assert connection.type == "groundbox" and connection.command == f"groundbox sandbox shell {env.sandbox_id}"
    assert connection.ports and connection.ports[0].mappings[0].host_ip == "abc-8080.groundbox.page"


def test_base64_roundtrip_helper() -> None:
    assert base64.b64decode(exec_answer(0, b"\x00\xff")["stdout"]) == b"\x00\xff"


def test_rate_limit_waits_as_asked() -> None:
    error = api_error("rate_limited", 429, "Too many requests from this API key. Try again in 8 seconds")
    assert 8 <= _client.retry_delay(error, 0) < 9
    assert _client.retry_delay(api_error("network", 0), 0) < 1


async def test_exec_retries_rate_limit(fake: FakeGroundbox) -> None:
    env = await make_env()
    errors = [api_error("rate_limited", 429, "Try again in 1 seconds")]

    def handler(cmd: list[str], kw: dict) -> dict | None:
        if cmd == ["true"] and errors:
            raise errors.pop()
        return None

    fake.exec_handler = handler
    assert (await env.exec(["true"])).success
