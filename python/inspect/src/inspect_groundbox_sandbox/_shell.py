"""The small shell programs the sandbox runs, and running a command as another user.

Groundbox runs a command as the sandbox's own user (`runner` on Groundbox's
image, root on a Docker image or template). To run as someone else, the command
is wrapped in `runuser`, `su` or `sudo`, each launched by absolute path so the
image's PATH never picks the wrapper.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from inspect_ai.util import SandboxUserUnsupportedError

PINNED_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
SHELL = "/bin/sh"
ENV = "/usr/bin/env"

# Prints the default user's name, uid and folder, then the user-switching tools there are.
PROBE_SCRIPT = (
    "id -un 2>/dev/null || echo ''; id -u; pwd; "
    "for p in /usr/sbin/runuser /sbin/runuser /usr/bin/sudo /usr/sbin/sudo /bin/su /usr/bin/su; do "
    '[ -x "$p" ] && echo "$p"; done; true'
)

MARK = "inspect-groundbox:"

# $1 file, $2 the largest size to print inline. Checks as the reading user, then
# prints the size on stderr and the content on stdout when it is small enough.
READ_SCRIPT = f"""PATH={PINNED_PATH}
f=$1
if [ ! -e "$f" ]; then echo "{MARK} ENOENT" >&2; exit 2; fi
if [ -d "$f" ]; then echo "{MARK} EISDIR" >&2; exit 21; fi
if [ ! -r "$f" ]; then echo "{MARK} EACCES" >&2; exit 13; fi
n=$(wc -c < "$f") || exit 13
n=$(echo $n)
echo "{MARK} SIZE $n" >&2
if [ "$n" -le "$2" ]; then exec cat -- "$f"; fi
exit 0
"""

# $1 file, $2 "-" to write stdin, or a file in the sandbox to copy from.
WRITE_SCRIPT = f"""PATH={PINNED_PATH}
f=$1
if [ -d "$f" ]; then echo "{MARK} EISDIR" >&2; exit 21; fi
d=$(dirname -- "$f")
if [ ! -d "$d" ]; then mkdir -p -- "$d" || exit 1; fi
if [ "$2" = "-" ]; then cat > "$f"; else cat -- "$2" > "$f"; fi
"""

# $1 file to read stdin from; the rest is the command.
STDIN_FROM_FILE_SCRIPT = 'f=$1; shift; exec "$@" < "$f"'

# $1 folder: joins part-* into data.
JOIN_PARTS_SCRIPT = f'PATH={PINNED_PATH}; cd "$1" && cat part-* > data && rm -f part-*'


@dataclass(frozen=True)
class Identity:
    """Who a command runs as when the sandbox is not asked to switch users."""

    name: str
    uid: int
    home: str
    runuser: str | None = None
    sudo: str | None = None
    su: str | None = None

    @classmethod
    def parse(cls, stdout: str) -> Identity:
        lines = stdout.splitlines()
        if len(lines) < 3:
            raise RuntimeError(f"Could not read the sandbox's user: {stdout!r}")
        tools = lines[3:]

        def first(*names: str) -> str | None:
            return next((t for t in tools if t in names), None)

        return cls(
            name=lines[0].strip(),
            uid=int(lines[1].strip()),
            home=lines[2].strip(),
            runuser=first("/usr/sbin/runuser", "/sbin/runuser"),
            sudo=first("/usr/bin/sudo", "/usr/sbin/sudo"),
            su=first("/bin/su", "/usr/bin/su"),
        )

    def is_self(self, user: str | None) -> bool:
        return user is None or user == self.name or user == str(self.uid)


def wrap_for_user(cmd: list[str], user: str | None, env: dict[str, str] | None, identity: Identity) -> list[str]:
    """`cmd` as `user`, or `cmd` itself when that is who runs it anyway.

    `env` is applied after the switch with /usr/bin/env, since sudo and su reset
    the environment.
    """
    if identity.is_self(user):
        return cmd
    assert user is not None
    tail = [ENV, *(f"{k}={v}" for k, v in (env or {}).items()), *cmd]
    numeric = re.fullmatch(r"\d+", user) is not None
    if identity.uid == 0:
        if identity.runuser and not numeric:
            return [identity.runuser, "-u", user, "--", *tail]
        if identity.sudo:
            return [identity.sudo, "-n", "-H", "-u", f"#{user}" if numeric else user, "--", *tail]
        if identity.su and not numeric:
            # One quoted command line and no further arguments: BusyBox su
            # would read a later "-c" in the command's own arguments as its own.
            return [identity.su, "-s", SHELL, user, "-c", "exec " + shlex.join(tail)]
    elif identity.sudo:
        return [identity.sudo, "-n", "-H", "-u", f"#{user}" if numeric else user, "--", *tail]
    raise SandboxUserUnsupportedError(
        f"Cannot run as {user!r} in this Groundbox sandbox: it runs as {identity.name or identity.uid} "
        "and has no runuser, sudo or su to switch users."
    )


def marker(stderr: str, name: str) -> str | None:
    """The value after `inspect-groundbox: NAME` in a script's stderr."""
    for line in stderr.splitlines():
        if line.startswith(f"{MARK} {name}"):
            return line[len(MARK) + len(name) + 1 :].strip()
    return None


def strip_markers(stderr: str) -> str:
    return "\n".join(line for line in stderr.splitlines() if not line.startswith(MARK))
