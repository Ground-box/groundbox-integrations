# groundbox-harbor

Run [Harbor](https://github.com/harbor-framework/harbor) jobs (Terminal-Bench and any other
Harbor task) in [Groundbox](https://groundbox.dev) sandboxes: each trial gets its own
Firecracker microVM. This is a Harbor custom environment (`BaseEnvironment`), so it works
with the `harbor` CLI as it is, with no change to Harbor.

## Install

```bash
pip install groundbox-harbor        # or: uv tool install harbor --with groundbox-harbor
export GROUNDBOX_API_KEY=gbx_...    # https://app.groundbox.dev/keys
```

Install it in the same Python environment as Harbor. It needs Python 3.12 or newer and
Harbor 0.26 or newer.

## Run

```bash
harbor run -d terminal-bench@2.0 -a terminus-2 -m anthropic/claude-sonnet-4-5 \
  -e groundbox_harbor:GroundboxSandbox -n 8
```

The oracle agent needs no model key, which makes it a quick check that everything works:

```bash
harbor run -p examples/tasks/hello-world -a oracle -e groundbox_harbor:GroundboxSandbox
```

In a job config:

```json
{
  "environment": {
    "import_path": "groundbox_harbor:GroundboxSandbox",
    "kwargs": { "sandbox_timeout_minutes": 120 }
  }
}
```

Or from Python, with `EnvironmentConfig(import_path="groundbox_harbor:GroundboxSandbox")`.

## How a task becomes a sandbox

- **`docker_image` in task.toml** (every Terminal-Bench 2.0 task has one): the sandbox starts
  straight from that image. The first sandbox from an image waits a minute or so while
  Groundbox turns it into a disk; later ones start in about a second.
- **`environment/Dockerfile`** (or `--force-build`): the Dockerfile is built once as a
  Groundbox template named `harbor-<hash>`, and every trial with the same Dockerfile and
  size starts from it. Building is free. `COPY`/`ADD` of local files work: the
  `environment/` folder is written into the Dockerfile as a first build stage (see Limits).
- **`--ek template=<name>`**: start from a Groundbox template you made yourself, ignoring the
  task's image and Dockerfile.

Commands run as the Dockerfile's last `USER` (root when it sets none), in its `WORKDIR`
(`/` when none), with the image's `ENV`, as `docker exec` would. Harbor's agent and verifier
users are applied with `su`.

## Options

Pass them with `--ek name=value` (or `environment.kwargs`):

| Option | Default | What it does |
| --- | --- | --- |
| `template` | none | Start from this Groundbox template (name, id or `name:version`). |
| `image_user` | Dockerfile `USER`, else root | User for commands when Harbor names none. |
| `sandbox_timeout_minutes` | 60 | Sandbox lifetime; renewed while the trial runs, so a sandbox left by a dead Harbor process ends within this long. |
| `renew_interval_sec` | 600 | How often the lifetime is renewed; 0 turns renewal off. |
| `flex` | false | Half-price sandboxes that start when a machine has spare room (may queue). |
| `allow_memory_overcommit` | true | `vm.overcommit_memory=1` at start, as on a Docker host. |
| `create_retry_sec` | 900 | How long to retry a create Groundbox refuses for a concurrency limit (429). |
| `evict_templates` | true | At the team's template limit, delete the oldest `harbor-*` template not in use and retry. |

Task resources map to the sandbox: `cpus` to vCPUs (whole cores, 1 to 16), `memory_mb` to
GB (at most 4 GB per vCPU, so more memory raises the vCPU count unless CPU is a hard limit),
`storage_mb` to the disk size.

## What it supports

| Harbor feature | Groundbox |
| --- | --- |
| exec, cwd, env, user, timeout | yes (long commands run in the background and are polled) |
| upload/download of files and folders | yes (tar for folders; large files in chunks) |
| `docker_image` tasks, Dockerfile tasks | yes |
| `network_mode = "no-network"` | yes (`allow_hosts: []`) |
| `allowlist` network mode | yes: host names, `*.wildcards`, IPv4 addresses and CIDRs; no IPv6 |
| switching network policy between phases | yes (egress is changed on the running sandbox) |
| resource limits | cpus and memory are both reserved and a hard limit |
| GPUs, TPUs, Windows tasks | no |
| docker-compose (multi-container) tasks | no |
| stream mode / SSH, host mounts | no |

## Limits

- A Dockerfile build has no build context on Groundbox's side, and Groundbox takes a
  Dockerfile of at most 64 KiB. Local `COPY`/`ADD` therefore work when the `environment/`
  folder, gzipped and base64-encoded, fits in that (a few dozen KB). For a bigger context,
  push the image (for example with `groundbox build --push` to `registry.groundbox.dev`) and
  set `docker_image`, or pass `--ek template=...`. `.dockerignore` is not applied.
- The first build stage on busybox needs Docker Hub; Dockerfiles that set their own
  `# syntax=` older than 1.4 cannot use heredocs.
- With only `docker_image` (no Dockerfile in `environment/`), the image's own `WORKDIR` and
  `USER` are not known: commands start in `/` as root. Set `workdir` in task.toml or
  `--ek image_user=...` when the image needs them.
- Images must be `linux/amd64` and public (or in your Groundbox registry).
- A team keeps 20 templates; `harbor-*` templates past that are evicted oldest first.
- Each sandbox costs $0.04 per vCPU-hour while it runs. Sandboxes are deleted at the end
  of each trial.

## Tests

```bash
pip install -e '.[test]'
pytest                                   # unit tests, with a fake Groundbox client
GROUNDBOX_API_KEY=gbx_... pytest -m live # also starts real sandboxes
```

## License

MIT; see [LICENSE](../../LICENSE) at the root of this repository.
