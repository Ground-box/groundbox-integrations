"""Unit tests for turning a Dockerfile into a context-free Groundbox build."""

from __future__ import annotations

import base64
import io
import os
import re
import tarfile
from pathlib import Path

import pytest

from groundbox_harbor._dockerfile import (
    CONTEXT_STAGE,
    ContextTooLargeError,
    parse_dockerfile_user,
    prepare_dockerfile,
)


def _write(tmp_path: Path, dockerfile: str, files: dict[str, bytes | str] | None = None) -> Path:
    env = tmp_path / "environment"
    env.mkdir()
    (env / "Dockerfile").write_text(dockerfile)
    for rel, data in (files or {}).items():
        path = env / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
    return env / "Dockerfile"


def _archive_names(text: str) -> set[str]:
    match = re.search(r"<<'HARBOR_GROUNDBOX_B64'.*?\n(.*?)\nHARBOR_GROUNDBOX_B64", text, re.S)
    assert match, text
    data = base64.decodebytes(match.group(1).encode())
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = tar.getmembers()
        assert all(m.uid == 0 and m.gid == 0 for m in members)
        return {m.name for m in members}


def test_no_local_copy_is_unchanged(tmp_path):
    text = "FROM ubuntu:24.04\nRUN echo hi\nADD https://example.com/x.tgz /x\nCOPY --from=alpine /bin/sh /sh\n"
    prepared = prepare_dockerfile(_write(tmp_path, text))
    assert prepared.text == text and not prepared.inlined_context


def test_local_copy_uses_context_stage(tmp_path):
    text = (
        "# syntax=docker/dockerfile:1\n"
        "ARG BASE=ubuntu:24.04\n"
        "FROM ${BASE}\n"
        "WORKDIR /app\n"
        "COPY --chown=agent:agent \\\n    src/ /app/src/\n"
        "COPY [\"my file.txt\", \"/app/\"]\n"
        "COPY . /all\n"
        "RUN ls\n"
    )
    path = _write(tmp_path, text, {"src/a.py": "print(1)\n", "my file.txt": "x"})
    prepared = prepare_dockerfile(path)
    out = prepared.text
    assert prepared.inlined_context
    lines = out.splitlines()
    # Directives and global ARGs stay first; the context stage comes before the first FROM.
    assert lines[0] == "# syntax=docker/dockerfile:1"
    assert lines[1] == "ARG BASE=ubuntu:24.04"
    assert lines[2] == f"FROM busybox:1.36.1 AS {CONTEXT_STAGE}"
    assert f"COPY --from={CONTEXT_STAGE} --chown=agent:agent /ctx/src /app/src/" in out
    assert f'COPY --from={CONTEXT_STAGE} ["/ctx/my file.txt", "/app/"]' in out
    assert f"COPY --from={CONTEXT_STAGE} /ctx/. /all" in out
    assert "FROM ${BASE}" in out and "RUN ls" in out
    assert {"src/a.py", "my file.txt", "Dockerfile"} <= _archive_names(out)


def test_numeric_stage_references_shift(tmp_path):
    text = "FROM golang AS build\nRUN go build\nFROM ubuntu\nCOPY --from=0 /out /out\nCOPY run.sh /run.sh\n"
    out = prepare_dockerfile(_write(tmp_path, text, {"run.sh": "#!/bin/sh\n"})).text
    assert "COPY --from=1 /out /out" in out
    assert f"COPY --from={CONTEXT_STAGE} /ctx/run.sh /run.sh" in out


def test_add_local_archive_is_extracted(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("inner.txt")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"hi"))
    text = "FROM ubuntu\nADD data.tar.gz /opt/data\nADD notes.txt /opt/\n"
    out = prepare_dockerfile(_write(tmp_path, text, {"data.tar.gz": buf.getvalue(), "notes.txt": "n"})).text
    assert "mkdir -p /ctx-add/0 && tar -xzf /ctx/data.tar.gz -C /ctx-add/0" in out
    assert f"COPY --from={CONTEXT_STAGE} /ctx-add/0/. /opt/data" in out
    assert f"COPY --from={CONTEXT_STAGE} /ctx/notes.txt /opt/" in out


def test_large_context_is_refused(tmp_path):
    path = _write(tmp_path, "FROM ubuntu\nCOPY blob /blob\n", {"blob": os.urandom(80_000)})
    with pytest.raises(ContextTooLargeError, match="64|65536"):
        prepare_dockerfile(path)


def test_parse_dockerfile_user(tmp_path):
    assert parse_dockerfile_user(_write(tmp_path, "FROM ubuntu\nRUN true\n")) is None


def test_parse_dockerfile_user_follows_stages(tmp_path):
    text = (
        "FROM ubuntu AS base\nUSER 1000:1000\n"
        "FROM python AS other\n"
        "FROM base\nRUN id\n"
    )
    assert parse_dockerfile_user(_write(tmp_path, text)) == "1000"


def test_parse_dockerfile_user_last_wins(tmp_path):
    text = "FROM ubuntu\nUSER root\nRUN x\nUSER agent\n"
    assert parse_dockerfile_user(_write(tmp_path, text)) == "agent"
