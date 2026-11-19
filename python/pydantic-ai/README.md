# pydantic-ai-groundbox

Run a [Pydantic AI](https://ai.pydantic.dev) agent's commands and file edits in a [Groundbox](https://groundbox.dev)
sandbox, a Firecracker microVM, instead of on your machine.

`GroundboxSandbox` is a capability that gives the run a Groundbox sandbox as its workspace, the way the official
`E2BSandbox` and `DaytonaSandbox` from pydantic-ai-harness do. `GroundboxSandboxBackend` is the Pydantic AI
`WorkspaceBackend` underneath (commands and files), for code that manages sandboxes itself. Both pass Pydantic AI's
workspace backend conformance suite against live Groundbox sandboxes.

## Install

```bash
pip install pydantic-ai-groundbox
# The examples use Coder from pydantic-ai-harness and an Anthropic model:
pip install "pydantic-ai-harness[coder,anthropic]"
```

Python 3.11 or newer, `pydantic-ai-slim` 2.68 or newer. Set `GROUNDBOX_API_KEY` to a key from
[app.groundbox.dev/keys](https://app.groundbox.dev/keys) (it needs the `sandboxes:read` and `sandboxes:write` scopes).

## Quick start

```python
from pydantic_ai import Agent
from pydantic_ai_groundbox import GroundboxSandbox
from pydantic_ai_harness.coder import Coder

agent = Agent('anthropic:claude-sonnet-4-5', capabilities=[GroundboxSandbox(), Coder()])
result = agent.run_sync('Clone https://github.com/pydantic/pydantic-ai and summarize how capabilities work.')
print(result.output)
```

`Coder`'s shell and file tools now run in the sandbox. Use `Shell()` and `FileSystem()` from pydantic-ai-harness for a
narrower agent, or your own tools through `ctx.workspace`:

```python
from pydantic_ai import Agent, RunContext
from pydantic_ai_groundbox import GroundboxSandbox

agent = Agent('anthropic:claude-sonnet-4-5', capabilities=[GroundboxSandbox(cpus=4, working_dir='/home/runner/app')])


@agent.tool
async def run_python(ctx: RunContext, code: str) -> str:
    """Run a Python snippet in the sandbox."""
    result = await ctx.workspace.run(['python3', '-c', code], timeout=30)
    return result.stdout + result.stderr
```

## Continue, reattach, clean up

A run without a workspace ref creates a sandbox. A follow-up run given the message history finds the ref there and
works in the same sandbox; so does a run given the ref as `workspace=`:

```python
import asyncio

sandbox = GroundboxSandbox()
agent = Agent('anthropic:claude-sonnet-4-5', capabilities=[sandbox, Coder()])

result = agent.run_sync('Set up a FastAPI app in /home/runner/app.')
followup = agent.run_sync('Add a /health route.', message_history=result.all_messages())

ref = result.workspace.ref  # WorkspaceRef(provider='groundbox', id='sbx_...'); store it
later = agent.run_sync('Run the tests.', workspace=ref)

asyncio.run(sandbox.destroy(ref))  # delete it: billing stops, the disk is gone
```

Attaching resumes a paused sandbox and starts a stopped one. A sandbox that was deleted, or ended at its timeout,
raises `WorkspaceUnavailableError` instead of starting over in an empty one. The backend never deletes a sandbox:
call `destroy(ref)` (or `pydantic_ai_groundbox.delete_sandbox(id)`), or let its timeout end it.

## Options

| Field | What it does |
| --- | --- |
| `cpus`, `memory_gb`, `disk_gb` | Size of a new sandbox. Defaults: 2 vCPU, 2 GB per vCPU, 20 GB. |
| `timeout_minutes`, `on_timeout` | Lifetime of a new sandbox (60 minutes by default) and what happens then: `'delete'` (default), `'stop'` (keep the disk) or `'pause'` (keep memory too). `stop` and `pause` need the paid plan. |
| `idle_timeout_seconds` | Pause the sandbox when idle; the next command or file call wakes it. |
| `template`, `image` | Start from a Groundbox template, or straight from a Docker image such as `python:3.12-slim` (commands then run as root). |
| `allow_hosts`, `deny_hosts` | Egress rules: host names, `*.wildcards`, IPv4 addresses and networks. |
| `secrets` | Team secrets given to every command as variables; the values never pass through your app or the model. |
| `metadata` | Labels, for `list_sandboxes(metadata=...)`. |
| `create_options` | Any other `Groundbox.create_sandbox` argument: `snapshot_id`, `flex`, `volumes`, `env_from_secrets`, ... |
| `working_dir` | Where commands start and relative paths resolve; made on a new sandbox. Default: `/home/runner`. |
| `env` | Variables every command gets, on new and attached sandboxes. |
| `client` | A configured `groundbox.Groundbox` (another key or base URL) instead of `GROUNDBOX_API_KEY`. |
| `authority`, `path_authority` | The authority of the sandbox's files, as for `E2BSandbox`. |

`GroundboxSandbox` works in agent specs (`Agent.from_spec(..., custom_capability_types=[GroundboxSandbox])`) with
every field but `client` and `create_options`.

## How it behaves

- Commands run as `runner` (passwordless `sudo`) on Groundbox's image, as root on a Docker `image`. Each command runs in
  the background in the sandbox and its output is read until the command exits, so a background child holding
  stdout open does not hold up the result.
- On a timeout or cancellation the command and everything it started are killed; detached processes (`setsid`,
  `nohup ... &`) keep running after a command returns, also for a later run that attaches by ref.
- Output past 10 MiB stops the command and raises `WorkspaceOutputLimitError`; redirect large output to a file.
- File calls use Groundbox's file API. Writes go through symlinks, reading a FIFO raises `OSError` instead of waiting,
  and listings include looping and dangling symlinks.
- Errors map onto Pydantic AI's: a rejected key, a spend cap, a missing scope or a deleted sandbox is
  `WorkspaceUnavailableError`; rate limits are waited out for up to a minute; network errors and 5xx propagate so
  durable engines can retry. Sandbox creation sends an idempotency key, so a retried create never starts a second
  sandbox.
- The `groundbox` SDK is synchronous; its calls run in worker threads, so the backend works under asyncio and Trio.

## Limits

- One command costs two or more API round trips (start, then read output), about 0.6 s for a trivial command from
  the US west coast; file reads and writes about 0.5 s.
- The file API reads and writes with root privileges, so Unix permissions do not keep file tools out of a path.
  Commands do run as `runner`.
- There is no native `realpath`; `Workspace.realpath` uses the sandbox's shell, like other backends without it.
- Commands get stdin at end-of-file; interactive input, terminals (`tty`), previews, desktops, fork and save points
  are in the Groundbox API (`backend.client`) but not in the workspace protocol.
- A Groundbox sandbox is Linux on x86-64 only.

## Tests

```bash
pip install -e . pytest anyio trio "pydantic-ai-harness[coder]"
pytest -m "not live"            # unit tests and the conformance suite over an on-host fake
GROUNDBOX_API_KEY=gbx_... pytest -m live   # the conformance suite and agent runs on real sandboxes
```

The live tests start small sandboxes (1 vCPU, 20-minute timeout) and delete every one they start.

## License

MIT. See [LICENSE](LICENSE).
