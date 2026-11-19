"""Marks for standard-suite tests whose expectations predate deepagents 0.7.

langchain-tests 1.1.9's `SandboxIntegrationTests` still expects glob() to return paths
relative to the search root and write() to refuse an existing file. deepagents 0.7's
`BaseSandbox` (which this backend uses unchanged for glob and write) returns absolute
paths and overwrites, so these fail for every `BaseSandbox` backend, not just Groundbox.
"""

from __future__ import annotations

import pytest

_DEEPAGENTS_07_CONTRACT = {
    "test_glob": "deepagents 0.7 glob() returns absolute paths",
    "test_glob_basic_pattern": "deepagents 0.7 glob() returns absolute paths",
    "test_glob_with_character_class": "deepagents 0.7 glob() returns absolute paths",
    "test_glob_with_question_mark": "deepagents 0.7 glob() returns absolute paths",
    "test_glob_hidden_files_explicitly": "deepagents 0.7 glob() returns absolute paths",
    "test_glob_with_directories": "deepagents 0.7 glob() lists files, not folders",
    "test_write_existing_file_fails": "deepagents 0.7 write() overwrites an existing file",
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        reason = _DEEPAGENTS_07_CONTRACT.get(item.name)
        if reason and "Standard" in (item.cls.__name__ if item.cls else ""):
            item.add_marker(pytest.mark.xfail(reason=reason, strict=False))
