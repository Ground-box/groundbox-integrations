from __future__ import annotations

import importlib.metadata

import pytest

pytest.importorskip("deepagents_code", reason="deepagents-code needs Python 3.14+")

from deepagents_code.integrations.sandbox_provider import (
    SandboxNotFoundError,
    SandboxProvider,
)

from langchain_groundbox.provider import GroundboxProvider
from langchain_groundbox.sandbox import GroundboxSandbox
from tests.fakes import FakeGroundbox


def make_provider(
    env: dict[str, str] | None = None,
) -> tuple[GroundboxProvider, list[FakeGroundbox]]:
    values = {"GROUNDBOX_API_KEY": "gbx_test", **(env or {})}
    clients: list[FakeGroundbox] = []
    shared = FakeGroundbox()

    def factory(api_key: str, base_url: str | None) -> FakeGroundbox:
        shared.api_key, shared.base_url = api_key, base_url
        clients.append(shared)
        return shared

    provider = GroundboxProvider(resolve_env_var=values.get, client_factory=factory)
    return provider, clients


def test_is_a_sandbox_provider() -> None:
    assert issubclass(GroundboxProvider, SandboxProvider)


def test_metadata() -> None:
    meta = GroundboxProvider(resolve_env_var=lambda _name: None).metadata
    assert meta.name == "groundbox"
    assert meta.working_dir == "/home/runner"
    assert meta.supports_sandbox_id is True
    assert meta.supports_snapshot_name is True
    assert meta.backend_module == "langchain_groundbox"
    assert meta.install is not None
    assert meta.install.command(in_app=False) == "dcode install langchain-groundbox --package"


def test_metadata_workdir_follows_image_and_override() -> None:
    image = GroundboxProvider(resolve_env_var={"GROUNDBOX_IMAGE": "python:3.12"}.get)
    assert image.metadata.working_dir == "/root"
    custom = GroundboxProvider(resolve_env_var={"GROUNDBOX_WORKDIR": "/work"}.get)
    assert custom.metadata.working_dir == "/work"


def test_constructing_needs_no_credentials() -> None:
    GroundboxProvider(resolve_env_var=lambda _name: None)


def test_missing_api_key() -> None:
    provider = GroundboxProvider(resolve_env_var=lambda _name: None)
    with pytest.raises(ValueError, match="GROUNDBOX_API_KEY"):
        provider.get_or_create()


def test_create_defaults() -> None:
    provider, clients = make_provider({"GROUNDBOX_API_URL": "https://example.test/v1"})
    backend = provider.get_or_create()

    assert isinstance(backend, GroundboxSandbox)
    fake = clients[0]
    assert fake.api_key == "gbx_test"
    assert fake.base_url == "https://example.test/v1"
    (_, kwargs) = fake.called("create_sandbox")[0]
    assert kwargs["metadata"] == {"source": "deepagents"}
    assert "idempotency_key" in kwargs
    assert "cpus" not in kwargs and "template" not in kwargs and "image" not in kwargs
    assert not fake.called("wait_for_sandbox")


def test_create_with_settings_from_env_and_kwargs() -> None:
    provider, clients = make_provider(
        {"GROUNDBOX_CPUS": "4", "GROUNDBOX_SANDBOX_TIMEOUT_MINUTES": "120"}
    )
    provider.get_or_create(
        template="py-tools",
        memory_gb=8,
        env={"A": "1"},
        metadata={"run": "7"},
        allow_hosts=["pypi.org"],
        command_timeout="60",
    )
    (_, kwargs) = clients[0].called("create_sandbox")[0]
    assert kwargs["cpus"] == 4
    assert kwargs["memory_gb"] == 8
    assert kwargs["timeout_minutes"] == 120
    assert kwargs["template"] == "py-tools"
    assert kwargs["env"] == {"A": "1"}
    assert kwargs["metadata"] == {"source": "deepagents", "run": "7"}
    assert kwargs["allow_hosts"] == ["pypi.org"]


def test_image_runs_in_root() -> None:
    provider, clients = make_provider()
    backend = provider.get_or_create(image="python:3.12-slim")
    backend.execute("true")
    assert clients[0].called("create_sandbox")[0][1]["image"] == "python:3.12-slim"
    assert clients[0].called("run_command")[0][1]["cwd"] == "/root"


def test_snapshot_name_is_template_or_kept_snapshot() -> None:
    provider, clients = make_provider({"GROUNDBOX_CPUS": "4"})
    provider.get_or_create(snapshot="my-template:3")
    provider.get_or_create(snapshot="snap_abc")
    first, second = (kw for _, kw in clients[0].called("create_sandbox"))
    assert first["template"] == "my-template:3"
    assert second["snapshot_id"] == "snap_abc"
    assert "cpus" not in second and "template" not in second


def test_snapshot_conflicts_with_image() -> None:
    provider, _ = make_provider()
    with pytest.raises(ValueError, match="snapshot"):
        provider.get_or_create(snapshot="snap_abc", image="alpine")


def test_custom_workdir_is_made() -> None:
    provider, clients = make_provider()
    backend = provider.get_or_create(workdir="/home/runner/project")
    assert clients[0].called("make_dir")[0][0][1] == "/home/runner/project"
    backend.execute("ls")
    assert clients[0].called("run_command")[0][1]["cwd"] == "/home/runner/project"


def test_queued_sandbox_is_waited_for() -> None:
    provider, _clients = make_provider()
    clients_fake = FakeGroundbox()
    clients_fake.create_status = "queued"
    provider._client_factory = lambda **_: clients_fake  # type: ignore[assignment]
    provider.get_or_create(timeout=42)
    assert clients_fake.called("wait_for_sandbox")[0][1]["timeout_s"] == 42


def test_failed_start_deletes_and_raises() -> None:
    provider, _ = make_provider()
    fake = FakeGroundbox()
    fake.create_status = "queued"
    fake.wait_status = "failed"
    provider._client_factory = lambda **_: fake  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="did not start"):
        provider.get_or_create()
    assert fake.called("delete_sandbox")


def test_attach_running_paused_and_stopped() -> None:
    provider, _clients = make_provider()
    fake = FakeGroundbox()
    provider._client_factory = lambda **_: fake  # type: ignore[assignment]
    running = fake.create_sandbox()["id"]
    paused = fake.create_sandbox()["id"]
    fake.sandboxes[paused]["status"] = "paused"
    shut = fake.create_sandbox()["id"]
    fake.sandboxes[shut]["status"] = "shut_down"

    assert provider.get_or_create(sandbox_id=running).id == running
    assert provider.get_or_create(sandbox_id=paused).id == paused
    assert provider.get_or_create(sandbox_id=shut).id == shut
    assert fake.called("resume_sandbox")[0][0] == (paused,)
    assert fake.called("start_sandbox")[0][0] == (shut,)
    assert len(fake.called("create_sandbox")) == 3  # Only the setup above.


def test_attach_missing_or_ended_raises_not_found() -> None:
    provider, _ = make_provider()
    fake = FakeGroundbox()
    provider._client_factory = lambda **_: fake  # type: ignore[assignment]
    with pytest.raises(SandboxNotFoundError):
        provider.get_or_create(sandbox_id="sbx_nope")
    ended = fake.create_sandbox()["id"]
    fake.delete_sandbox(ended)
    with pytest.raises(SandboxNotFoundError, match="ended"):
        provider.get_or_create(sandbox_id=ended)


def test_delete() -> None:
    provider, clients = make_provider()
    backend = provider.get_or_create()
    provider.delete(sandbox_id=backend.id)
    assert clients[0].sandboxes[backend.id]["status"] == "stopped"
    with pytest.raises(SandboxNotFoundError):
        provider.delete(sandbox_id="sbx_nope")


def test_unsupported_kwargs() -> None:
    provider, _ = make_provider()
    with pytest.raises(TypeError, match="bogus"):
        provider.get_or_create(bogus=1)
    with pytest.raises(TypeError, match="bogus"):
        provider.delete(sandbox_id="sbx_x", bogus=1)


async def test_async_wrappers() -> None:
    provider, clients = make_provider()
    backend = await provider.aget_or_create()
    await provider.adelete(sandbox_id=backend.id)
    assert clients[0].sandboxes[backend.id]["status"] == "stopped"


def test_entry_point_is_registered() -> None:
    entries = importlib.metadata.entry_points(group="deepagents_code.sandbox_providers")
    groundbox = [entry for entry in entries if entry.name == "groundbox"]
    if not groundbox:
        pytest.skip("package not installed; run `pip install -e .` to register the entry point")
    assert groundbox[0].load() is GroundboxProvider


def test_dcode_registry_finds_provider() -> None:
    from deepagents_code.integrations.sandbox_config import SandboxConfig
    from deepagents_code.integrations.sandbox_registry import SandboxRegistry

    registry = SandboxRegistry(config=SandboxConfig())
    if "groundbox" not in registry.available_providers():
        pytest.skip("package not installed; run `pip install -e .` to register the entry point")
    assert isinstance(registry.create_provider("groundbox"), GroundboxProvider)
    meta = registry.get_metadata("groundbox")
    assert meta is not None and meta.supports_snapshot_name
