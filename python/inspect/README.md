# inspect-groundbox-sandbox

[Groundbox](https://groundbox.dev) sandboxes for [Inspect AI](https://inspect.aisi.org.uk):
`sandbox="groundbox"` gives every sample its own Linux VM (a Firecracker microVM with
dedicated cores), started in about a second. It is an Inspect sandbox extension, built like
the Daytona, E2B and Modal sandboxes in
[inspect_sandboxes](https://github.com/meridianlabs-ai/inspect_sandboxes).

## Install

```
pip install inspect-groundbox-sandbox
export GROUNDBOX_API_KEY=gbx_...   # https://app.groundbox.dev/keys
```

Installing the package registers the `groundbox` sandbox with Inspect through Inspect's
extension entry point; there is nothing to import.

## Use it

```python
from inspect_ai import Task, task
from inspect_ai.agent import react
from inspect_ai.dataset import Sample
from inspect_ai.scorer import includes
from inspect_ai.tool import bash, python


@task
def ctf() -> Task:
    return Task(
        dataset=[Sample(input="Find the flag in /challenge.", target="flag{...}")],
        solver=react(tools=[bash(timeout=180), python(timeout=180)]),
        scorer=includes(),
        sandbox="groundbox",
    )
```

```
inspect eval ctf.py --model anthropic/claude-sonnet-4-5
```

With no config, a sample runs on Groundbox's own image: Ubuntu 24.04 with Docker, Node,
Python, Go, Rust and Java, where commands run as `runner` (who has sudo) in `/home/runner`.

### Configure it

Pass a `GroundboxSandboxEnvironmentConfig` (or a dict of its fields), a Dockerfile or a
compose file:

```python
from inspect_groundbox_sandbox import GroundboxSandboxEnvironmentConfig

sandbox=("groundbox", GroundboxSandboxEnvironmentConfig(
    image="python:3.12-slim",      # any public image, or one in your Groundbox registry
    cpus=4, memory_gb=8,
    allow_hosts=["pypi.org", "files.pythonhosted.org"],   # [] blocks all egress
    timeout_minutes=120,
))

sandbox=("groundbox", "Dockerfile")      # built once into a Groundbox template
sandbox=("groundbox", "compose.yaml")    # one service; see below
```

| Field | What it does |
| --- | --- |
| `image` | Start from a Docker image. Commands run as root. |
| `template` | Start from a Groundbox template (`name`, `name:version` or `tpl_...`). |
| `dockerfile` | A Dockerfile, built once into a template named by its content. |
| `snapshot_id` | Start from a kept Groundbox snapshot (memory, disk and processes). |
| `cpus`, `memory_gb`, `disk_gb` | Size: 1 to 16 vCPUs, up to 4 GB per vCPU. Default 2 vCPU, 4 GB, 20 GB. |
| `timeout_minutes` | How long a sandbox may live. Default 60. |
| `flex` | Half price; starts when a machine has spare room, so it may queue. |
| `env` | Environment variables for every command. |
| `allow_hosts`, `deny_hosts` | Egress allowlist or deny list (names, `*.wildcards`, IPv4, CIDR). |
| `secrets`, `env_from_secrets` | Groundbox team secrets as environment variables. |
| `user`, `working_dir` | Who `exec()` runs as, and where, when the call does not say. |
| `ports` | Ports `connection()` gives an HTTPS preview URL (paid plan). |
| `metadata` | Extra metadata pairs on each sandbox. |

### Compose files

A compose file with one service maps onto one sandbox:

- `image` starts from that image; `build` builds the Dockerfile into a template.
- `environment`, `user`, `working_dir`, `cpus` / `mem_limit` / `deploy.resources` carry over.
- `network_mode: none` becomes `allow_hosts: []`: no egress, and no DNS.
- `ports` become previews in `connection()`.
- An `x-groundbox:` block (top level or on the service) takes any config field above and wins.

```yaml
services:
  default:
    build: .
    network_mode: none
x-groundbox:
  cpus: 4
  timeout_minutes: 120
```

## How it behaves

- `exec()` runs through Groundbox's exec API: argv with no shell, stdin, env, cwd, timeout.
  A timeout raises `TimeoutError` and kills the command and its children.
- `exec(user=...)` and the files' `user=` switch users with `runuser`, `sudo` or `su`, each
  launched by absolute path. Sandboxes from Groundbox's image use `sudo`; Debian or Ubuntu
  images use `runuser`; Alpine uses `su`. An image with none of them can only run as its
  default user (`SandboxUserUnsupportedError` otherwise).
- `read_file()` and `write_file()` act as the default user (or the one given), so file
  permissions hold and new files belong to that user. Files above 1 MB move through
  Groundbox's files API, in 16 MB parts.
- A Dockerfile's final `USER` and `WORKDIR` are applied, as Docker would (Groundbox itself runs
  template commands as root in `/root`).
- `connection()` gives `groundbox sandbox shell <id>` (the [CLI](https://groundbox.dev/docs/cli)).
- Each sandbox carries metadata `created_by=inspect-ai`, `inspect_run_id`, `task` and `name`.
  `task_cleanup` deletes this run's sandboxes, including any whose create answer was lost.
- A team at its sandbox limit (`429`) waits and retries, up to `INSPECT_GROUNDBOX_START_TIMEOUT`
  seconds (default 1800). Use `--max-sandboxes` to stay under your team's limit.
- Inspect's injected sandbox tools (used by `bash_session`, `text_editor` and `exec_remote`)
  install and run; the live tests check `exec_remote` on Groundbox's image.

Cleanup by hand:

```
inspect sandbox cleanup groundbox            # every live sandbox Inspect made
inspect sandbox cleanup groundbox sbx_...    # one
```

`inspect eval --no-sandbox-cleanup` keeps them and prints their ids; open one with
`groundbox sandbox shell sbx_...`.

## Limits

- **Multi-service compose is not supported yet.** A compose file must have one service.
  (Daytona, E2B and Modal run several services with Docker inside one sandbox; Groundbox's image
  has Docker, so this can come later.)
- **Dockerfiles build with no build context**: `COPY` and `ADD` of local files fail. Push the
  image to a registry and use `image`, or write the files with `Sample.files`.
- A single `exec()` runs for at most 60 minutes.
- Groundbox keeps the last 4 MB of each output stream of a command; the beginning of longer
  output is dropped (Inspect allows this; write large output to a file and `read_file()` it).
- For `image`, the image's own `USER` and `WORKDIR` are not applied (commands run as root in
  `/root`); set `user` and `working_dir`.
- Private images on other registries are not supported; push them to the Groundbox registry.
- Templates, previews, stop/start and volumes are on the paid plan. The free trial runs 2
  sandboxes at a time, for up to 60 minutes each.
- A team keeps up to 20 templates; each distinct Dockerfile (and size) makes one named
  `inspect-<hash>`. Delete old ones with `groundbox templates rm`.

## Settings

| Variable | Default | |
| --- | --- | --- |
| `GROUNDBOX_API_KEY` | | Required. |
| `GROUNDBOX_API_URL` | `https://api.groundbox.dev/v1` | |
| `INSPECT_GROUNDBOX_START_TIMEOUT` | `1800` | Seconds a sample waits for its sandbox to start. |
| `INSPECT_GROUNDBOX_THREADS` | `256` | Worker threads for Groundbox calls (the SDK is synchronous). |

## Tests

```
pip install -e ".[dev]"
pytest                                   # unit tests, with a fake client
GROUNDBOX_API_KEY=gbx_... pytest -m live # Inspect's sandbox conformance checks and evals, on real sandboxes
```

`tests/test_live_self_check.py` runs Inspect's own portable sandbox checks
(`inspect_ai.util._sandbox.self_check`) on Groundbox's image and on `python:3.12-slim`.
`examples/hello_groundbox.py` has two evals that need no LLM key:

```
inspect eval examples/hello_groundbox.py --model mockllm/model
```

## License

MIT. See [LICENSE](../../LICENSE).
