# @groundbox/mastra

[Groundbox](https://groundbox.dev) sandboxes for [Mastra](https://mastra.ai) workspaces. Each sandbox
is a Firecracker microVM with its own cores: Ubuntu 24.04 with git, Node, Python and Docker, a
20 GB disk, and user `runner` with passwordless sudo. It plugs into Mastra's `WorkspaceSandbox`
interface the same way `@mastra/e2b` and `@mastra/daytona` do, so an agent gets
`mastra_workspace_execute_command`, background processes, file tools and, with a desktop,
computer-use tools that all run in the VM.

## Install

```bash
npm install @groundbox/mastra @mastra/core
```

Node 22.13 or newer. Set `GROUNDBOX_API_KEY` (create a key at https://app.groundbox.dev/keys), or
pass `apiKey`.

## Use it in a workspace

```typescript
import { Agent } from '@mastra/core/agent';
import { Workspace } from '@mastra/core/workspace';
import { GroundboxSandbox } from '@groundbox/mastra';

const sandbox = new GroundboxSandbox({
  cpus: 2,
  timeoutMinutes: 60, // lifetime of the VM; the default action at the end is delete
});

const workspace = new Workspace({
  sandbox,
  filesystem: sandbox.filesystem, // optional: file tools on the VM's own disk
});

const agent = new Agent({
  id: 'coder',
  name: 'coder',
  instructions: 'You are a careful engineer. Use the workspace to run code.',
  model: 'openai/gpt-4.1',
  workspace,
});

const result = await agent.generate('Write a Python script that prints the first 20 primes, then run it.');
console.log(result.text);

await workspace.destroy(); // deletes the VM
```

Without an agent:

```typescript
const sandbox = new GroundboxSandbox();
await sandbox.start();
const { stdout } = await sandbox.executeCommand('python3', ['-c', 'print(6 * 7)']);
await sandbox.destroy();
```

## What maps to what

| Mastra | Groundbox |
| --- | --- |
| `start()` | Finds the sandbox for this `id` (metadata `mastra-sandbox-id`) and resumes or starts it; otherwise creates one. Reports `created` or `connected`. |
| `stop()` | Pauses the VM: memory, processes and disk are kept, only storage is billed. A sandbox with volumes is shut down instead (disk kept). |
| `destroy()` | Deletes the VM. Billing stops. |
| `executeCommand()` | One `POST /exec` call. With `onStdout`/`onStderr` or an `abortSignal` (as Mastra's tools pass), a background command followed over the exec stream. |
| `processes` | Background commands (`ex_...` ids): spawn, stdin, close stdin, kill, list, and `get(id)` to pick up a command started elsewhere. Output survives a dropped connection. |
| `filesystem` | Groundbox's files API (read, write, list, stat, mkdir, delete); copy, move and append run as one `sh` command in the VM. |
| `writeFiles()` | One tar.gz upload, file modes kept. |
| `networking.getPortUrl(port)` | A Groundbox preview, `https://<slug>-<port>.groundbox.page`. |
| `computer` | With `desktop: true`: screenshots, mouse, keyboard and a live-view link for a Linux desktop with Chrome. |
| `snapshot()` / checkpoints | A kept Groundbox snapshot (memory, disk and processes) labelled with `checkpointName`; the next start for that name boots from it. |
| `clone()` | A sibling sandbox with the same settings; `idleTimeoutMinutes` becomes Groundbox's idle pause. |
| `groundboxSandboxProvider` | Registers the sandbox with the Mastra editor: `new MastraEditor({ sandboxes: [groundboxSandboxProvider] })`. |

`sandbox.groundbox` is the `@groundbox/sdk` client and `sandbox.sandboxId` the VM's id, for
anything this package does not wrap (fork, save points, egress changes while it runs).

## Options

| Option | Default | |
| --- | --- | --- |
| `id` | random | Logical id. A later start with the same id, in any process, reconnects to the same VM. |
| `sandboxId` | | Reattach to this `sbx_...` first. |
| `apiKey`, `baseUrl` | `GROUNDBOX_API_KEY` | |
| `timeout` | `300000` | Default command timeout in ms. |
| `cpus`, `memoryGb`, `diskGb` | 2, 2 per vCPU, 20 | Up to 16 vCPUs and 4 GB per vCPU. |
| `image` | | A Docker image (`python:3.12-slim`, or one in your Groundbox registry). Commands run as root. |
| `template`, `snapshotId` | | Start from a Groundbox template or kept snapshot. |
| `env` | | Set on the VM and kept as Mastra's env overlay (`setEnv` changes it later). |
| `secrets`, `envFromSecrets` | | Team secrets as environment variables. |
| `allowHosts`, `denyHosts` | | Egress rules: names, `*.wildcards`, IPv4 addresses and networks. |
| `volumes` | | `[{ volume: 'data', path: '/data', readOnly?: boolean }]` |
| `timeoutMinutes`, `onTimeout` | 60, `delete` | Lifetime of the VM, and `delete`, `stop` or `pause` at the end. |
| `idleTimeoutSeconds` | off | Pause when idle; the next request wakes it. |
| `flex` | `false` | Half price; starts when a machine has spare room. |
| `desktop` | `false` | `true` or `{ width, height, startUrl, recording }`. |
| `publicPreviews` | `false` | Previews open to anyone. Private ones need a signed-in member of your team. |
| `checkpointName`, `seedCheckpointName` | the `id` | Names for `snapshot()` checkpoints. |
| `workingDirectory` | `/home/runner` | `/root` (or the image's `HOME`) on a Docker image. |
| `instructions` | | A string, or a function of the default instructions. |

## Limits

- Not supported: `mount()` of S3, GCS or Azure filesystems (the FUSE mounts `@mastra/e2b` and
  `@mastra/daytona` do), and `FactorySandbox` for Mastra Code's factory. A Groundbox volume
  (`volumes`) is the way to keep data across sandboxes.
- Commands run as the image's user (`runner`, or root on `image`); there is no per-command user.
  Use `sudo` on the default image.
- `filesystem` does not confine paths: the agent reaches whatever the sandbox user can. A
  no-clobber `writeFile` checks, then writes (not atomic); copy and move with `overwrite: false`
  are checked inside one shell command.
- Previews, kept snapshots (checkpoints), volumes, templates and `onTimeout: 'stop' | 'pause'`
  are on the paid plan. The free trial gives $1 of use, 2 sandboxes at a time, up to 2 vCPUs and
  4 GB, 60 minutes each.
- A single `executeCommand` without callbacks can run for up to 60 minutes; with callbacks or a
  longer `timeout`, as long as the sandbox lives.

## Develop

```bash
npm ci
npm test                                   # unit tests, no network
GROUNDBOX_API_KEY=gbx_... npm run test:live  # creates real sandboxes and deletes them
npm run build
```

The live tests drive Mastra's own `Workspace`, its workspace tools and an `Agent` loop with a
scripted model (no LLM key needed) against real sandboxes.

## License

MIT. See [LICENSE](./LICENSE).
