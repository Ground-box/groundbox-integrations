# @groundbox/sandcastle

A [Groundbox](https://groundbox.dev) sandbox provider for [Sandcastle](https://github.com/mattpocock/sandcastle). Each run gets its own Groundbox sandbox, a Firecracker microVM with dedicated cores. Sandcastle copies your repo in, the agent works there, and its commits come back to a branch on your machine.

`groundbox()` is an isolated provider, like Sandcastle's own `e2b()`, `modal()` and `vercel()`. It works with `run()`, `createSandbox()` and `interactive()`.

## Install

```bash
npm install @ai-hero/sandcastle @groundbox/sandcastle
```

Node 22 or later.

## API key

Create a key at https://app.groundbox.dev/keys and set it in your shell:

```bash
export GROUNDBOX_API_KEY=gbx_...
```

Or pass `apiKey` to `groundbox()`. Don't put the key in `.sandcastle/.env`: Sandcastle forwards the keys declared there into the sandbox, where the agent can read them.

No account yet? `npx -y @groundbox/cli signup --name my-agent` gets a key with $1 of free sandbox use and no card.

## Use it

```typescript
import { run, claudeCode } from "@ai-hero/sandcastle";
import { groundbox } from "@groundbox/sandcastle";

await run({
  agent: claudeCode("claude-opus-4-8"),
  sandbox: groundbox(),
  prompt: "Fix issue #42 in this repo.",
  hooks: {
    sandbox: {
      onSandboxReady: [
        // The default image has no agent CLI: install the one you use.
        { command: "npm install -g @anthropic-ai/claude-code", sudo: true, timeoutMs: 300_000 },
        { command: "npm install", timeoutMs: 300_000 },
      ],
    },
  },
});
```

Put the agent's own login (`ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` and so on) in `.sandcastle/.env` as usual; Sandcastle passes it into the sandbox.

`createSandbox()` keeps one sandbox for several runs:

```typescript
import { createSandbox, claudeCode } from "@ai-hero/sandcastle";
import { groundbox } from "@groundbox/sandcastle";

await using sandbox = await createSandbox({
  branch: "agent/fix-42",
  sandbox: groundbox({ cpus: 4, memory_gb: 8 }),
});
await sandbox.run({ agent: claudeCode("claude-opus-4-8"), prompt: "Fix issue #42." });
const tests = await sandbox.exec("npm test");
```

## Options

Every `create_sandbox` option of [`@groundbox/sdk`](https://www.npmjs.com/package/@groundbox/sdk) passes through, so a sandbox can be sized, started from an image or a template, and fenced in:

```typescript
groundbox({
  cpus: 4,                     // vCPUs (memory up to 4 GB per vCPU is included)
  memory_gb: 8,
  disk_gb: 20,
  image: "node:22-bookworm",   // start from a public Docker image (runs as root)
  template: "my-template",     // or from a Groundbox template you built
  allow_hosts: ["github.com", "registry.npmjs.org", "api.anthropic.com"], // egress allowlist
  secrets: ["GITHUB_TOKEN"],   // team secrets, set as env vars in the sandbox
  metadata: { project: "alpha" },
  timeout_minutes: 60,         // the sandbox's whole lifetime (default 60)

  // Provider options
  apiKey: process.env.GROUNDBOX_API_KEY,
  env: { MY_VAR: "value" },    // merged at launch, like docker()'s env
  maxOutputTailChars: 65_536,  // output kept per stream in ExecResult when streaming
  commandTimeoutSeconds: 3600, // one command's limit (default: timeout_minutes)
});
```

## How it maps

| Sandcastle | Groundbox |
| --- | --- |
| `create()` | `create_sandbox`, then `wait_for_sandbox` if it is still coming up. Makes `$HOME/workspace`. |
| `worktreePath` | `/home/runner/workspace` on the default image, `/root/workspace` from a Docker image |
| `exec` with `onLine` | an exec stream: stdout reaches `onLine` line by line while the command runs. Resumable, so a dropped connection doesn't end the agent. |
| `exec` with `stdin` | written to the command's stdin, which is then closed |
| `exec` with `sudo: true` | the image's passwordless `sudo` (the default image runs as `runner`); nothing to do from a Docker image, where commands already run as root |
| `interactiveExec` | the same stream on a terminal (`tty`) when your stdin is a TTY, with raw keys and window resizes passed through |
| `copyIn` | `write_file` for a file (mode kept), `upload_files` (one tar.gz) for a directory, symlinks and empty folders included |
| `copyFileOut` | `read_file` |
| `close()` | `delete_sandbox` |
| `sandboxId` | the Groundbox sandbox id (`sbx_...`), for `groundbox sandbox exec` or the dashboard |

The default image is Ubuntu 24.04 with git, Node 22 and npm. From a Docker image, the provider sets `IS_SANDBOX=1`, which Claude Code needs before it skips its permission checks as root; the image needs `sh` and `git`.

## Limits

- **No agent CLI in the default image.** Install it in a `sandbox.onSandboxReady` hook (Claude Code installs in about 7 seconds), use a Docker image that has it, or build a Groundbox template with it once (`npm: ["@anthropic-ai/claude-code"]`) for a warm start.
- **Lifetime.** A sandbox lives for `timeout_minutes` (default 60) and is then deleted by Groundbox, so the timeout must cover setup, the agent run and syncing commits back. It is also the backstop when the host process dies without `close()`: there is no background reaper.
- **Free trial.** $1 of use, 2 sandboxes at a time, up to 2 vCPU and 4 GB, 60 minutes each. Templates, volumes and snapshots need the paid plan.
- **`interactive()`** is implemented on Groundbox's terminal stream and was tested on a real pseudo-terminal (keys in, output out, exit code back), but not yet with a logged-in agent's full TUI.
- **Git identity on the host.** Sandcastle needs `git config user.name` and `user.email` on the machine that calls `run()`. On a bare CI machine without them, a run finishes but brings back no commits.
- Groundbox features Sandcastle has no place for (pause and resume, fork, save points, previews) aren't used. Reach them with `@groundbox/sdk` and the handle's `sandboxId`.

## Tests

```bash
npm test                                  # unit tests on a fake Groundbox, incl. a full Sandcastle run()
GROUNDBOX_API_KEY=gbx_... npm test        # plus live tests against the real API
```

The live tests start real sandboxes (and delete them): the handle contract, `run()` and `createSandbox()` with a scripted agent that commits, and a run from a Docker image.

## License

MIT. See [LICENSE](./LICENSE).
