"""Turn a task's Dockerfile into one Groundbox can build as a template.

A Groundbox template is built from the Dockerfile text alone: there is no build
context, so ``COPY`` and ``ADD`` of local files would fail. When a Dockerfile
copies local files, the whole ``environment/`` folder is packed into a
gzipped tar, written into the Dockerfile as base64 in a first build stage
(``harbor-groundbox-context``, on busybox), and each local ``COPY``/``ADD``
becomes ``COPY --from=harbor-groundbox-context /ctx/...``. Docker's own COPY
then decides file versus directory, globs, ``--chown`` and ``--chmod``, so the
result matches a normal ``docker build`` of the same folder.

Groundbox takes a Dockerfile of at most 64 KiB, so this only works for small
contexts; :class:`ContextTooLargeError` says so when it does not fit.
"""

from __future__ import annotations

import base64
import io
import json
import re
import shlex
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from dockerfile_parse import DockerfileParser

CONTEXT_STAGE = "harbor-groundbox-context"
CONTEXT_STAGE_IMAGE = "busybox:1.36.1"
CONTEXT_ROOT = "/ctx"
ADD_EXTRACT_ROOT = "/ctx-add"
# Groundbox refuses a template Dockerfile longer than this many characters.
MAX_DOCKERFILE_CHARS = 65536

_ARCHIVE_FLAGS = (
    (".tar.gz", "z"),
    (".tgz", "z"),
    (".tar.bz2", "j"),
    (".tbz2", "j"),
    (".tar.xz", "J"),
    (".txz", "J"),
    (".tar", ""),
)
_REMOTE_PREFIXES = ("http://", "https://", "git@", "git://", "ssh://")


class ContextTooLargeError(ValueError):
    """The rewritten Dockerfile is over Groundbox's 64 KiB limit."""


@dataclass
class PreparedDockerfile:
    text: str
    inlined_context: bool
    context_bytes: int = 0


def _split_flags(tokens: list[str]) -> tuple[list[str], list[str]]:
    flags: list[str] = []
    rest = list(tokens)
    while rest and rest[0].startswith("--"):
        flags.append(rest.pop(0))
    return flags, rest


def _parse_copy_value(value: str) -> tuple[list[str], list[str], bool] | None:
    """Flags, paths (sources then destination) and whether the JSON form was used."""
    try:
        tokens = shlex.split(value, posix=True)
    except ValueError:
        tokens = value.split()
    flags, rest = _split_flags(tokens)
    remainder = value.strip()
    # Drop the flags from the raw value to see whether the paths are JSON.
    for flag in flags:
        idx = remainder.find(flag)
        if idx != -1:
            remainder = remainder[idx + len(flag) :].lstrip()
    if remainder.startswith("["):
        try:
            paths = json.loads(remainder)
        except ValueError:
            return None
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            return None
        return flags, paths, True
    return flags, rest, False


def _flag_name(flag: str) -> str:
    return flag.split("=", 1)[0]


def _is_local_source(source: str) -> bool:
    return not source.startswith(_REMOTE_PREFIXES)


def _context_path(source: str) -> str:
    """The path inside the context stage for a COPY source."""
    rel = PurePosixPath("/" + source.lstrip("/"))
    # PurePosixPath keeps glob characters and "." as they are.
    text = str(rel)
    if source.rstrip("/") in ("", "."):
        return f"{CONTEXT_ROOT}/."
    return f"{CONTEXT_ROOT}{text}"


def _archive_flag(source: str) -> str | None:
    lower = source.lower()
    for suffix, flag in _ARCHIVE_FLAGS:
        if lower.endswith(suffix):
            return flag
    return None


def _pack_context(environment_dir: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", compresslevel=9) as tar:
        for path in sorted(environment_dir.rglob("*")):
            rel = path.relative_to(environment_dir).as_posix()
            info = tar.gettarinfo(str(path), arcname=rel)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            if info.isreg():
                with path.open("rb") as handle:
                    tar.addfile(info, handle)
            elif info.isdir() or info.issym():
                tar.addfile(info)
    return buffer.getvalue()


def _render_paths(paths: list[str], as_json: bool) -> str:
    if as_json:
        return json.dumps(paths)
    return " ".join(shlex.quote(p) if re.search(r"\s", p) else p for p in paths)


def prepare_dockerfile(dockerfile_path: Path) -> PreparedDockerfile:
    """Return Dockerfile text Groundbox can build with no build context.

    Raises :class:`ContextTooLargeError` when the result is over 64 KiB.
    """
    text = dockerfile_path.read_text()
    environment_dir = dockerfile_path.parent
    parser = DockerfileParser(fileobj=io.BytesIO(text.encode()))
    structure = parser.structure
    lines = text.splitlines(keepends=True)

    replacements: dict[int, tuple[int, str]] = {}
    add_extracts: list[tuple[str, str, str]] = []  # (source, extract dir, tar flag)
    first_from: int | None = None
    needs_context = False

    for instr in structure:
        name = instr["instruction"]
        if name == "FROM" and first_from is None:
            first_from = instr["startline"]
        if name not in ("COPY", "ADD"):
            continue
        parsed = _parse_copy_value(instr["value"])
        if parsed is None:
            continue
        flags, paths, as_json = parsed
        if len(paths) < 2:
            continue
        from_flags = [f for f in flags if _flag_name(f) == "--from"]
        if from_flags:
            # Stage indexes shift by one once the context stage is added first.
            new_flags = []
            for flag in flags:
                value = flag.split("=", 1)[1] if "=" in flag else ""
                if _flag_name(flag) == "--from" and value.isdigit():
                    flag = f"--from={int(value) + 1}"
                new_flags.append(flag)
            if new_flags != flags:
                body = " ".join(new_flags + [_render_paths(paths, as_json)])
                replacements[instr["startline"]] = (
                    instr["endline"],
                    f"{name} {body}\n",
                )
            continue
        sources, dest = paths[:-1], paths[-1]
        if not all(_is_local_source(s) for s in sources):
            if name == "ADD" and not any(_is_local_source(s) for s in sources):
                continue  # ADD from URLs or git needs no context.
            # A mix of remote and local sources: leave it, Docker would too.
            continue
        needs_context = True
        new_sources: list[str] = []
        for source in sources:
            flag = _archive_flag(source) if name == "ADD" else None
            if flag is not None:
                target = f"{ADD_EXTRACT_ROOT}/{len(add_extracts)}"
                add_extracts.append((source, target, flag))
                new_sources.append(f"{target}/.")
            else:
                new_sources.append(_context_path(source))
        kept_flags = [
            f for f in flags if _flag_name(f) in ("--chown", "--chmod", "--link")
        ]
        body = " ".join(
            [f"--from={CONTEXT_STAGE}", *kept_flags]
            + [_render_paths([*new_sources, dest], as_json)]
        )
        replacements[instr["startline"]] = (instr["endline"], f"COPY {body}\n")

    if not needs_context and not replacements:
        return PreparedDockerfile(text=text, inlined_context=False)

    out: list[str] = []
    context_size = 0
    i = 0
    while i < len(lines):
        if needs_context and i == first_from:
            archive = _pack_context(environment_dir)
            context_size = len(archive)
            encoded = base64.encodebytes(archive).decode()
            script = [
                "set -e",
                f"mkdir -p {CONTEXT_ROOT}",
                f"base64 -d <<'HARBOR_GROUNDBOX_B64' | tar -xzf - -C {CONTEXT_ROOT}",
                encoded.rstrip("\n"),
                "HARBOR_GROUNDBOX_B64",
            ]
            for source, target, flag in add_extracts:
                archive_path = _context_path(source)
                script.append(
                    f"mkdir -p {target} && tar -x{flag}f {shlex.quote(archive_path)} -C {target}"
                )
            out.append(f"FROM {CONTEXT_STAGE_IMAGE} AS {CONTEXT_STAGE}\n")
            out.append("RUN <<'HARBOR_GROUNDBOX_CONTEXT'\n")
            out.append("\n".join(script) + "\n")
            out.append("HARBOR_GROUNDBOX_CONTEXT\n\n")
        if i in replacements:
            end, replacement = replacements[i]
            out.append(replacement)
            i = end + 1
            continue
        out.append(lines[i])
        i += 1

    result = "".join(out)
    if len(result) > MAX_DOCKERFILE_CHARS:
        raise ContextTooLargeError(
            f"{dockerfile_path} copies local files, and with the environment folder "
            f"written into it ({context_size} bytes compressed) the Dockerfile is "
            f"{len(result)} characters, over Groundbox's {MAX_DOCKERFILE_CHARS}. "
            "Push the image to a registry (for example registry.groundbox.dev with "
            "`groundbox build --push`) and set docker_image in task.toml, or pass "
            "--ek template=<a Groundbox template built from it>."
        )
    return PreparedDockerfile(
        text=result, inlined_context=needs_context, context_bytes=context_size
    )


@dataclass
class _Stage:
    user: str | None = None


def parse_dockerfile_user(dockerfile_path: Path) -> str | None:
    """The ``USER`` the final stage runs as, or None when it sets none.

    ``FROM <earlier stage>`` keeps that stage's user, as Docker does. A
    ``user:group`` value gives the user part.
    """
    if not dockerfile_path.exists():
        return None
    parser = DockerfileParser(fileobj=io.BytesIO(dockerfile_path.read_bytes()))
    stages: dict[str, _Stage] = {}
    ordered: list[_Stage] = []
    current: _Stage | None = None
    for instr in parser.structure:
        name = instr["instruction"]
        if name == "FROM":
            parts = instr["value"].split()
            parts = [p for p in parts if not p.startswith("--")]
            base = parts[0] if parts else ""
            parent = stages.get(base.lower())
            if parent is None and base.isdigit() and int(base) < len(ordered):
                parent = ordered[int(base)]
            current = _Stage(user=parent.user if parent else None)
            ordered.append(current)
            if len(parts) >= 3 and parts[1].upper() == "AS":
                stages[parts[2].lower()] = current
        elif name == "USER" and current is not None:
            value = instr["value"].strip()
            current.user = value.split(":", 1)[0] if value else None
    return current.user if current else None
