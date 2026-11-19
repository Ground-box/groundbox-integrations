# langchain-groundbox

[Groundbox](https://groundbox.dev) sandboxes for [Deep Agents](https://github.com/langchain-ai/deepagents):
a sandbox backend for `deepagents`, and a sandbox provider for Deep Agents Code (`dcode`).

Each sandbox is a Firecracker microVM running Ubuntu 24.04 on dedicated cores, as the user
`runner` (with sudo), with `python3`, `bash` and `git` installed.

## Install

With Deep Agents Code:

```bash
dcode install langchain-groundbox --package
```

With plain `deepagents` (or to test against an editable `dcode`):

```bash
pip install langchain-groundbox
```

Set your API key (create one at https://app.groundbox.dev/keys):

```bash
export GROUNDBOX_API_KEY=gbx_...
```

## Use it in dcode

```bash
dcode --sandbox groundbox
```

The provider registers itself under the `deepagents_code.sandbox_providers` entry point as
`groundbox`. dcode starts a sandbox, runs the agent's commands and file edits in it, and
deletes it when the session ends.

```bash
dcode --sandbox groundbox --sandbox-setup ./setup.sh           # run a script in the sandbox first
dcode --sandbox groundbox --sandbox-id sbx_...                   # attach to a sandbox you started; it is not deleted
dcode --sandbox groundbox --sandbox-snapshot-name my-template    # start from a Groundbox template
dcode --sandbox groundbox --sandbox-snapshot-name snap_...       # start from a kept snapshot
```

To make it the default, in `~/.deepagents/config.toml`:

```toml
[sandboxes]
default = "groundbox"
```

### Settings

Each is read from the environment, also with a `DEEPAGENTS_CODE_` prefix (which wins):

| Variable | What it does | Default |
| --- | --- | --- |
| `GROUNDBOX_API_KEY` | API key (required) | |
| `GROUNDBOX_API_URL` | API base | `https://api.groundbox.dev/v1` |
| `GROUNDBOX_TEMPLATE` | Template (id, `name` or `name:version`) for new sandboxes | your team's default template, else Groundbox's image |
| `GROUNDBOX_IMAGE` | Docker image for new sandboxes, e.g. `python:3.12-slim` (commands run as root, in `/root`) | |
| `GROUNDBOX_CPUS` | vCPUs, 1 to 16 | 2 |
| `GROUNDBOX_MEMORY_GB` | Memory in GB | 2 per vCPU |
| `GROUNDBOX_DISK_GB` | Minimum disk in GB | 20 |
| `GROUNDBOX_SANDBOX_TIMEOUT_MINUTES` | Sandbox lifetime | 60 |
| `GROUNDBOX_WORKDIR` | Folder the agent works in | `/home/runner` (`/root` with an image) |

The same settings, and a few more (`env`, `metadata`, `allow_hosts`, `deny_hosts`, `secrets`,
`idle_timeout_seconds`, `command_timeout`), are keyword arguments of
`GroundboxProvider.get_or_create()`. To pass them from dcode's config, declare the provider
there with its class path:

```toml
[sandboxes.providers.groundbox]
class_path = "langchain_groundbox.provider:GroundboxProvider"
working_dir = "/home/runner"
supports_snapshot_name = true

[sandboxes.providers.groundbox.params]
cpus = 4
timeout_minutes = 180
allow_hosts = ["github.com", "*.githubusercontent.com", "pypi.org", "files.pythonhosted.org"]
```

## Use it with `deepagents`

```python
from deepagents import create_deep_agent
from groundbox import Groundbox
from langchain_groundbox import GroundboxSandbox

gb = Groundbox()  # reads GROUNDBOX_API_KEY
sandbox = gb.create_sandbox(cpus=2, timeout_minutes=60)
try:
    backend = GroundboxSandbox(client=gb, sandbox_id=sandbox["id"])
    agent = create_deep_agent(
        model="anthropic:claude-sonnet-4-5",
        backend=backend,
        system_prompt="You work in a Linux sandbox. Your home is /home/runner.",
    )
    agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "Write and run a Python script that prints the first 10 primes.",
                }
            ]
        }
    )
finally:
    gb.delete_sandbox(sandbox["id"])
```

`GroundboxSandbox(client, sandbox_id, workdir="/home/runner", timeout=1800, env=None)`:
`timeout` is the default per-command timeout in seconds (0 for none).

## How it maps

| Deep Agents | Groundbox |
| --- | --- |
| `execute(command, timeout=)` | `POST /exec` with `bash -c` (or `sh -c` when the image has no bash), in `workdir`. Over 60 minutes, or `timeout=0`: a background command whose output is read until it ends |
| `upload_files` / `download_files` | the file API (`PUT` / `GET /files`); parent folders are made |
| `read`, `write`, `edit`, `ls`, `glob`, `grep` | Deep Agents' own `BaseSandbox` helpers over `execute()` (they need `python3`) |
| `get_or_create(sandbox_id=...)` | `GET /sandboxes/{id}`; a paused sandbox is resumed, a shut-down one started |
| `get_or_create(snapshot=...)` | `snap_...` starts from a kept snapshot, anything else names a template |
| `delete` | `DELETE /sandboxes/{id}` |

## Limits

- The free trial allows 2 sandboxes at a time, 2 vCPU, 4 GB and 60 minutes each. Kept snapshots,
  templates and stop/start need the paid plan.
- The file API acts as root, so `download_files` can read a file the `runner` user cannot
  (for example one with mode `000`), where other sandboxes answer `permission_denied`. Files
  it writes belong to `runner`.
- Commands and files are synchronous calls; the async methods (`aexecute`, `aget_or_create`, ...)
  run them in a thread, as Deep Agents' defaults do.
- Output is not cut by the backend (`truncated` is always `False`); a background command keeps its
  last 32 MB of output.
- `--sandbox-snapshot-name` does not build a missing template: make it first
  (https://groundbox.dev/docs/templates).
- A call the API refuses for now (`rate_limited`, `no_capacity`) is tried up to four more times,
  waiting as long as the API asks (at most 30 seconds each); other errors come back as the command's output
  (exit code 1) or as a file error.
- The provider module imports `deepagents_code`, which needs Python 3.14. `GroundboxSandbox` alone
  works on Python 3.11+.

## Development

```bash
pip install -e . pytest pytest-asyncio langchain-tests deepagents-code   # deepagents-code: Python 3.14
pytest tests/unit_tests
GROUNDBOX_API_KEY=gbx_... pytest tests/integration_tests   # creates and deletes real sandboxes
```

The live tests run LangChain's standard `SandboxIntegrationTests` suite. Seven of its tests are
marked expected-to-fail: langchain-tests 1.1.9 still expects relative `glob()` paths and a
`write()` that refuses existing files, which deepagents 0.7's `BaseSandbox` changed.

MIT licensed; see [LICENSE](LICENSE).
