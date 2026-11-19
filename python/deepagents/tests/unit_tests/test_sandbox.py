from __future__ import annotations

import pytest
from groundbox import GroundboxError

from langchain_groundbox import GroundboxSandbox
from langchain_groundbox.sandbox import TIMEOUT_EXIT_CODE
from tests.fakes import FakeGroundbox


@pytest.fixture
def fake() -> FakeGroundbox:
    return FakeGroundbox(api_key="gbx_test")


@pytest.fixture
def backend(fake: FakeGroundbox) -> GroundboxSandbox:
    sandbox = fake.create_sandbox()
    return GroundboxSandbox(client=fake, sandbox_id=sandbox["id"])  # type: ignore[arg-type]


def test_id(backend: GroundboxSandbox) -> None:
    assert backend.id.startswith("sbx_fake")


def test_execute_combines_output_and_passes_workdir(
    fake: FakeGroundbox, backend: GroundboxSandbox
) -> None:
    fake.exec_results.append({"exit_code": 3, "stdout": "out\n", "stderr": "err\n"})
    result = backend.execute("echo out; echo err >&2; exit 3", timeout=20)

    assert result.exit_code == 3
    assert result.output == "out\n\nerr\n"
    assert result.truncated is False
    (args, kwargs) = fake.called("run_command")[0]
    cmd = args[1]
    assert cmd[:2] == ["sh", "-c"] and "bash" in cmd[2]
    assert cmd[-1] == "echo out; echo err >&2; exit 3"
    assert kwargs["cwd"] == "/home/runner"
    assert kwargs["timeout_s"] == 20


def test_execute_uses_default_timeout(fake: FakeGroundbox, backend: GroundboxSandbox) -> None:
    backend.execute("true")
    assert fake.called("run_command")[0][1]["timeout_s"] == 30 * 60


def test_execute_reports_timeout(fake: FakeGroundbox, backend: GroundboxSandbox) -> None:
    fake.exec_results.append(
        {"exit_code": 137, "stdout": "partial", "stderr": "", "timed_out": True}
    )
    result = backend.execute("sleep 99", timeout=1)
    assert result.exit_code == TIMEOUT_EXIT_CODE
    assert "partial" in result.output
    assert "timed out after 1 seconds" in result.output


def test_execute_rejects_negative_timeout(backend: GroundboxSandbox) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        backend.execute("true", timeout=-1)


def test_execute_api_error_becomes_result(fake: FakeGroundbox, backend: GroundboxSandbox) -> None:
    def boom(*_args: object, **_kwargs: object) -> dict:
        raise GroundboxError("conflict", "The sandbox is not running", 409, "POST", "/exec")

    fake.run_command = boom  # type: ignore[method-assign]
    result = backend.execute("true")
    assert result.exit_code == 1
    assert "conflict" in result.output


def test_execute_retries_when_box_is_briefly_unreachable(
    fake: FakeGroundbox, backend: GroundboxSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("langchain_groundbox.sandbox.time.sleep", lambda _s: None)
    real = fake.run_command
    failures = [GroundboxError("no_capacity", "not reachable", 503, "POST", "/exec")]

    def flaky(*args: object, **kwargs: object) -> dict:
        if failures:
            raise failures.pop()
        return real(*args, **kwargs)  # type: ignore[arg-type]

    fake.run_command = flaky  # type: ignore[method-assign]
    fake.exec_results.append({"exit_code": 0, "stdout": "ok", "stderr": ""})
    assert backend.execute("true").output == "ok"


def test_rate_limit_waits_as_asked(
    fake: FakeGroundbox, backend: GroundboxSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    slept: list[float] = []
    monkeypatch.setattr("langchain_groundbox.sandbox.time.sleep", slept.append)
    failures = [
        GroundboxError(
            "rate_limited", "Too many requests. Try again in 3 seconds", 429, "GET", "/files"
        )
    ]
    real = fake.read_file

    def limited(*args: object) -> bytes:
        if failures:
            raise failures.pop()
        return real(*args)  # type: ignore[arg-type]

    fake.read_file = limited  # type: ignore[method-assign]
    backend.upload_files([("/tmp/x", b"x")])
    assert backend.download_files(["/tmp/x"])[0].content == b"x"
    assert slept == [3.0]


def test_other_errors_are_not_retried(
    fake: FakeGroundbox, backend: GroundboxSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("langchain_groundbox.sandbox.time.sleep", lambda _s: pytest.fail("slept"))
    assert backend.download_files(["/nope"])[0].error == "file_not_found"
    assert len(fake.called("read_file")) == 1


def test_execute_long_command_runs_in_background(
    fake: FakeGroundbox, backend: GroundboxSandbox
) -> None:
    fake.output_parts = [
        {"stdout": "a", "stderr": "", "seq": 1, "done": False, "more": True},
        {"stdout": "b", "stderr": "e", "seq": 2, "done": True, "exit_code": 0},
    ]
    result = backend.execute("train", timeout=2 * 3600)

    assert result.exit_code == 0
    assert result.output == "ab\ne"
    assert fake.called("start_command")[0][1]["timeout_s"] == 2 * 3600
    assert [kw["seq"] for _, kw in fake.called("get_command_output")] == [0, 1]
    assert not fake.called("run_command")


def test_execute_zero_timeout_has_no_limit(fake: FakeGroundbox, backend: GroundboxSandbox) -> None:
    fake.output_parts = [{"stdout": "", "stderr": "", "seq": 0, "done": True, "exit_code": 0}]
    backend.execute("serve", timeout=0)
    assert fake.called("start_command")[0][1]["timeout_s"] is None


def test_upload_and_download_roundtrip(backend: GroundboxSandbox) -> None:
    up = backend.upload_files([("/home/runner/a.bin", b"\x00\x01hi")])
    assert up[0].error is None
    down = backend.download_files(["/home/runner/a.bin"])
    assert down[0].content == b"\x00\x01hi"
    assert down[0].error is None


def test_download_errors(backend: GroundboxSandbox) -> None:
    results = backend.download_files(["/missing.txt", "/home/runner", "relative.txt"])
    assert [r.error for r in results] == ["file_not_found", "is_directory", "invalid_path"]
    assert all(r.content is None for r in results)


def test_upload_errors(backend: GroundboxSandbox) -> None:
    results = backend.upload_files([("relative.txt", b"x"), ("/home/runner", b"x")])
    assert [r.error for r in results] == ["invalid_path", "is_directory"]


def test_upload_server_error_is_diagnosed(fake: FakeGroundbox, backend: GroundboxSandbox) -> None:
    fake.write_error = GroundboxError(
        "internal", "The sandbox could not do that", 502, "PUT", "/files"
    )
    fake.exec_results.append({"exit_code": 0, "stdout": "file_not_found\n", "stderr": ""})
    results = backend.upload_files([("/home/runner/a.txt/x", b"x")])
    assert results[0].error == "file_not_found"


def test_write_uses_upload(fake: FakeGroundbox, backend: GroundboxSandbox) -> None:
    # write() first runs a preflight through execute(); the fake answers it with success.
    result = backend.write("/home/runner/new.txt", "hello")
    assert result.error is None
    assert fake.files[backend.id]["/home/runner/new.txt"] == b"hello"
