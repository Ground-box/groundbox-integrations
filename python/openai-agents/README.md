# openai-agents-groundbox

Run [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) sandbox agents on
[Groundbox](https://groundbox.dev): a Linux microVM (Firecracker) with its own cores for each
agent session. `GroundboxSandboxClient` is a sandbox client in the same shape as the SDK's own
E2B, Modal and Daytona extensions; pass it to `SandboxRunConfig`.

## Install

```sh
pip install openai-agents-groundbox
```

Set `GROUNDBOX_API_KEY` (create one at https://app.groundbox.dev/keys, or let an agent sign
itself up for a free trial: `npx -y @groundbox/cli signup --name my-agent`).

Tested with `openai-agents` 0.25.0 and `groundbox` 0.4.2, Python 3.10 or newer.

## Use it

```python
import asyncio

from agents import Runner
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Shell
from agents.sandbox.entries import File

from openai_agents_groundbox import GroundboxSandboxClient, GroundboxSandboxClientOptions

agent = SandboxAgent(
    name="Workspace assistant",
    instructions="Answer questions about the workspace. Inspect the files first.",
    default_manifest=Manifest(entries={"notes.md": File(content=b"# Notes\n\nShip on Friday.\n")}),
    capabilities=[Shell()],
)

run_config = RunConfig(
    sandbox=SandboxRunConfig(
        client=GroundboxSandboxClient(),  # reads GROUNDBOX_API_KEY
        options=GroundboxSandboxClientOptions(cpus=2, timeout_minutes=30),
    ),
)

result = asyncio.run(Runner.run(agent, "When do we ship?", run_config=run_config))
print(result.final_output)
```

The runner creates the sandbox, writes the manifest into the workspace
(`/home/runner/workspace` by default), runs the agent's shell and file tools in it, and
deletes the sandbox when the run ends.

To keep one sandbox across several runs, create the session yourself and pass it in:

```python
client = GroundboxSandboxClient()
session = await client.create(manifest=agent.default_manifest, options=GroundboxSandboxClientOptions())
async with session:
    await Runner.run(agent, "First question", run_config=RunConfig(sandbox=SandboxRunConfig(session=session)))
    await Runner.run(agent, "Second question", run_config=RunConfig(sandbox=SandboxRunConfig(session=session)))
await client.delete(session)
```

`examples/run_agent.py` is a complete script (it needs `OPENAI_API_KEY` too; `--scripted`
runs it with a scripted model instead, no model key).

## Options

`GroundboxSandboxClientOptions` maps to `POST /v1/sandboxes`. Everything is optional:

| Option | What it does |
| --- | --- |
| `cpus`, `memory_gb`, `disk_gb` | Size: 1 to 16 vCPUs, up to 4 GB per vCPU (default 2 vCPUs, 2 GB per vCPU, 20 GB disk) |
| `timeout_minutes`, `on_timeout` | When the sandbox ends (default 60 minutes) and what that does: `delete`, `stop` or `pause` |
| `image` | Start from a Docker image, e.g. `python:3.12-slim` (commands run as root) |
| `template` | Start from a Groundbox template |
| `env`, `secrets` | Environment variables, and team secrets given as variables |
| `metadata` | Labels on the sandbox (the session id is added as `openai_agents_session`) |
| `allow_hosts`, `deny_hosts` | Egress allowlist or deny list |
| `flex` | Half price, starts when a machine has spare room |
| `idle_timeout_seconds` | Pause the sandbox when idle; the next call wakes it |
| `pause_on_exit` | Pause instead of delete when the session shuts down, so `client.resume(state)` reattaches |
| `workspace_persistence` | `"tar"` (default) or `"snapshot"`, see below |
| `exposed_ports`, `public_ports` | Ports `session.resolve_exposed_port(port)` gives an HTTPS address |
| `timeouts` | `GroundboxSandboxTimeouts` for exec, create, tar and PTY hang-up |

The client takes `api_key` and `base_url` (or `GROUNDBOX_API_KEY` / `GROUNDBOX_API_URL`), or a
ready `groundbox=Groundbox(...)` client. `client.groundbox` is that client, for anything this
interface does not cover (fork, save points, egress changes while running).

## What works

- Commands (`exec`), with the manifest's environment, the workspace root as the working
  directory, timeouts, and `user=` (run through `sudo -u`; the default image's `runner` user
  has sudo). A command with no timeout gets an hour (`GroundboxSandboxTimeouts.exec_timeout_unbounded_s`);
  commands allowed longer than an hour run in the background and are polled.
- A workspace root outside the user's home (the SDK's default `Manifest()` root is
  `/workspace`) is made with sudo and given to the `runner` user.
- PTY sessions for the `exec_command` / `write_stdin` tools, over Groundbox's command stream.
  `tty=True` gives a terminal that takes input; a long command without a tty keeps running
  and is read on later. Commands are resumable, so a dropped connection reattaches.
- Files: read, write, mkdir through the Groundbox files API; ls, rm, mv and `apply_patch`
  through the SDK's shell fallbacks. Paths are confined to the workspace like the other
  remote backends.
- Persistence on `stop()`: `"tar"` archives the workspace root and restores it into a new
  sandbox on resume. `"snapshot"` keeps a Groundbox snapshot of the whole sandbox (memory,
  disk and processes) and starts the resumed sandbox from it (paid plan). Each `stop()` keeps
  a new snapshot, labelled `openai-agents <session id>`; the package does not delete old ones.
  Snapshots unused for a month expire, a team keeps up to 50, and
  `client.groundbox.delete_snapshot(id)` deletes one.
- Resume: `client.resume(state)` reattaches to the same sandbox when it still exists (running,
  paused, or stopped with its disk kept), otherwise it makes a new one from the saved options
  and restores the workspace snapshot.
- Exposed ports through Groundbox previews (paid plan). Private ports resolve to a share link
  whose token is in `ExposedPortEndpoint.query` (valid 24 hours); `public_ports=True` makes them
  public.

## Limits

- Cloud bucket mounts (S3, R2, GCS, Azure) have no Groundbox mount strategy and are untested.
- PTY output arrives as text: bytes that are not UTF-8 are replaced. Non-PTY `exec` returns
  output byte for byte.
- The first sandbox from a Docker `image` waits while the image is turned into a disk (up to
  a few minutes); `GroundboxSandboxTimeouts.create_s` bounds that wait.
- The free trial allows 2 sandboxes at a time, 2 vCPUs, 4 GB and 60 minutes each; `stop`/`pause`
  on timeout, kept snapshots and previews need the paid plan.
- The package uses the SDK's underscored session hooks, so it pins `openai-agents` to the 0.25
  series.

## Tests

```sh
pip install -e ".[test]"
pytest -m "not live"                     # unit tests, with a fake Groundbox API
GROUNDBOX_API_KEY=gbx_... pytest -m live  # against the real API: creates and deletes sandboxes
```

The unit tests run their commands with the local shell (GNU tools; they are run on Linux).
The live tests need no model key: the Runner test uses the SDK's `ScriptedModel`, so it
drives the real `Runner` → `SandboxRunConfig` → Groundbox path without calling a model.

## License

MIT. See the repository's [LICENSE](../../LICENSE).
