"""Live tests against the real Groundbox API. They create and delete real sandboxes.

Run with GROUNDBOX_API_KEY set; skipped without it.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

if not os.environ.get("GROUNDBOX_API_KEY"):
    pytest.skip("GROUNDBOX_API_KEY is not set", allow_module_level=True)

from groundbox import Groundbox
from langchain_tests.integration_tests import SandboxIntegrationTests

from langchain_groundbox import GroundboxSandbox

if TYPE_CHECKING:
    from collections.abc import Iterator

    from deepagents.backends.protocol import SandboxBackendProtocol

pytestmark = pytest.mark.live


class TestGroundboxSandboxStandard(SandboxIntegrationTests):
    """LangChain's standard sandbox suite, on one Groundbox sandbox."""

    @pytest.fixture(scope="class")
    def sandbox(self) -> Iterator[SandboxBackendProtocol]:
        client = Groundbox()
        created = client.create_sandbox(
            timeout_minutes=30, metadata={"source": "langchain-groundbox-tests"}
        )
        try:
            yield GroundboxSandbox(client=client, sandbox_id=created["id"])
        finally:
            client.delete_sandbox(created["id"])


def test_provider_lifecycle() -> None:
    """The dcode provider: create, run, files, reattach by id, delete."""
    pytest.importorskip("deepagents_code", reason="deepagents-code needs Python 3.14+")
    from deepagents_code.integrations.sandbox_provider import SandboxNotFoundError

    from langchain_groundbox.provider import GroundboxProvider

    provider = GroundboxProvider()
    backend = provider.get_or_create(timeout_minutes=10, metadata={"test": "lifecycle"})
    try:
        result = backend.execute("whoami && pwd && python3 -c 'print(6*7)'")
        assert result.exit_code == 0, result.output
        assert result.output.split() == ["runner", "/home/runner", "42"]

        assert backend.write("/home/runner/notes.txt", "one\ntwo\n").error is None
        assert backend.edit("/home/runner/notes.txt", "two", "three").error is None
        read = backend.read("/home/runner/notes.txt")
        assert read.error is None
        assert "three" in str(read.file_data)

        again = provider.get_or_create(sandbox_id=backend.id)
        assert again.execute("cat notes.txt").output == "one\nthree\n"

        timed = backend.execute("sleep 5", timeout=1)
        assert timed.exit_code == 124
    finally:
        provider.delete(sandbox_id=backend.id)

    with pytest.raises(SandboxNotFoundError):
        provider.get_or_create(sandbox_id=backend.id)
