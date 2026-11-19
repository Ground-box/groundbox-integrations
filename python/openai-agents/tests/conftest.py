from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live: runs against the real Groundbox API (needs GROUNDBOX_API_KEY)")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    import os

    if os.environ.get("GROUNDBOX_API_KEY"):
        return
    skip = pytest.mark.skip(reason="GROUNDBOX_API_KEY is not set")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
