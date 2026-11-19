# @groundbox/opencode

Run [OpenCode](https://opencode.ai) sessions in [Groundbox](https://groundbox.dev) sandboxes.

The plugin adds a `groundbox` type to OpenCode's workspace API. OpenCode keeps running on your
machine. Each workspace is a Groundbox sandbox (a Firecracker microVM) running an OpenCode server
on a copy of your project. Start a session inside it, or move a local session into it with `/warp`.

## Install

Add the plugin to `opencode.json` (in the project, or `~/.config/opencode/opencode.json` for all
projects). The second element of the tuple holds options and can be left out.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": [["@groundbox/opencode", { "cpus": 2, "idleTimeoutSeconds": 600 }]]
}
```

Start OpenCode with your Groundbox API key and workspaces turned on:

```bash
export GROUNDBOX_API_KEY=gbx_...   # https://app.groundbox.dev/keys
OPENCODE_EXPERIMENTAL_WORKSPACES=true opencode
```

Needs OpenCode 1.16 or newer (`opencode-ai`), a macOS or Linux host, and the Groundbox paid plan:
the plugin reaches the sandbox through a [preview](https://groundbox.dev/docs/previews), and
previews are not in the free trial.

## Use

1. In OpenCode, run `/warp` and choose **Groundbox Sandbox**. The plugin starts a sandbox, copies
   your project in, installs the same OpenCode version you run locally, and starts its server.
   That takes about 10 to 20 seconds. The workspace is named after your branch, for example
   `groundbox-main`.
2. Send a prompt. The agent's commands, edits and tools run in the sandbox.
3. To bring a session home, run `/warp` and choose **None**. OpenCode offers to apply the
   sandbox's file changes to your project as a patch (your project must be a git repository).
4. Removing the workspace deletes its sandbox.

The same works without the TUI, through OpenCode's HTTP API:

```bash
# with `opencode serve` running on :4096 in the project
curl -X POST "localhost:4096/experimental/workspace?directory=$PWD" \
  -H 'content-type: application/json' -d '{"type":"groundbox","branch":null}'
# then create sessions with ?workspace=<id>
```

## How it works

- **Sandbox**: one per workspace, from Groundbox's default Ubuntu image (user `runner`, with
  sudo), a template, or a Docker image. Tagged with the metadata `opencode_workspace_id`, which is
  how the plugin finds it again after OpenCode restarts. No local state file.
- **Project copy**: in a git repository, git decides what is sent: tracked files plus untracked
  files that are not ignored, so `.gitignore`d files (`node_modules`, `.env`) stay on your
  machine. `.git` itself is not sent; the sandbox commits the copy as `Workspace snapshot`, so
  moving a session home carries only the agent's changes. The project lives at the same path as
  on your machine (on Windows: `/home/runner/project`), which is what lets a session move between
  the two.
- **Server**: `opencode serve` runs in the background in the sandbox with the environment OpenCode
  hands the workspace (your `opencode auth` credentials, the workspace id), plus anything in
  `forwardEnv` and `env`. They are written to `~/.groundbox-opencode/env` (mode 600) in the sandbox.
- **Connection**: the local OpenCode talks to the sandbox through a private Groundbox preview,
  `https://<slug>-4096.groundbox.page`. Every request carries the preview's share token and a
  random basic-auth password for the OpenCode server, made per workspace and kept in the
  workspace's `extra` field in your local OpenCode database. Share tokens last 24 hours; the
  plugin renews them before they run out.
- **Idle and timeout**: the sandbox pauses after 10 idle minutes (memory and processes kept,
  billed as storage) and the next request wakes it. It also pauses at its timeout (2 hours,
  counted from the last start or wake). When OpenCode next reaches a paused or stopped sandbox,
  the plugin resumes or starts it, and restarts the server after a stop. See Limits for why the
  timeout, not the idle pause, is what usually ends a session today.

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `cpus` | `2` | vCPUs (1 to 16). |
| `memoryGb` | Groundbox default | Memory, up to 4 GB per vCPU. |
| `diskGb` | Groundbox default (20) | Minimum disk size. |
| `template` | none | Start from a Groundbox template. |
| `image` | none | Start from a public Docker image (linux/amd64). Runs as root; needs `npm`, or `curl` and `bash`, to install OpenCode. |
| `timeoutMinutes` | `120` | Lifetime, counted from the last start or wake. At most 43200. While OpenCode is open, a paused sandbox is resumed on its next request, so this mostly bounds what an abandoned workspace costs. |
| `onTimeout` | `"pause"` | `"pause"`, `"stop"` (keeps the disk) or `"delete"`. |
| `idleTimeoutSeconds` | `600` | Pause after this many idle seconds; a request wakes it. `null` turns it off. |
| `projectPath` | your host path | Project folder in the sandbox. |
| `serverPort` | `4096` | Port of the OpenCode server in the sandbox. |
| `opencodeVersion` | your local version | OpenCode version installed in the sandbox. |
| `forwardEnv` | `[]` | Names of local environment variables to copy to the sandbox's OpenCode server, for example `["ANTHROPIC_API_KEY"]` if your model key comes from the environment rather than `opencode auth login`. |
| `env` | `{}` | Extra environment variables for the sandbox's OpenCode server. |
| `prepareCommands` | `[]` | Shell commands run in the project folder after the copy and before the server starts, for example `["npm ci"]`. |
| `allowHosts` | no limit | Egress allowlist for the sandbox. |
| `metadata` | `{}` | Extra sandbox metadata. |
| `uploadIgnore` | `.git`, `node_modules`, `dist`, `build`, ... | Skipped names when the project is not a git repository. |
| `maxFileBytes` | 50 MB | Larger files are not copied. |
| `readyTimeoutMs` | `90000` | How long to wait for the sandbox's OpenCode server. |

## Limits

- OpenCode's workspace API is experimental (`OPENCODE_EXPERIMENTAL_WORKSPACES=true`) and can
  change without notice.
- OpenCode 2 (`@opencode/cli`) has no workspace API for plugins, so this package is for
  OpenCode 1 only.
- Moving an existing local session into a workspace with `/warp` did not work in our tests with
  OpenCode 1.19.6: the warp itself succeeds, but the next command times out with "Timed out
  waiting for sync fence". Sessions started in the workspace work. See the PR for details.
- Idle pause does not fire once OpenCode has connected: OpenCode holds an event stream
  (`/global/event`) to the workspace, and a stream through a Groundbox preview keeps the sandbox
  active, even after OpenCode closes. Until that changes, the sandbox runs until its timeout
  (`timeoutMinutes`, 2 hours by default) and then pauses. Remove workspaces you are done with.
- The copy is one-way at create time. Changes you make locally later do not reach the sandbox;
  commits made inside the sandbox do not come home (the file changes do, through `/warp`).
- Needs the paid plan (previews). A deleted or expired sandbox makes the workspace unavailable:
  remove it and create a new one.
- Windows hosts: the project cannot live at the same path in the sandbox, so a new session
  started from a warped one may not open locally.

## Development

```bash
npm install
npm run typecheck
npm test                                  # unit tests, with a fake Groundbox
GROUNDBOX_API_KEY=gbx_... npm run test:live   # one real sandbox, deleted at the end
npm run build
```

To try a checkout in OpenCode, build it and put the folder's absolute path in place of
`@groundbox/opencode` in `opencode.json`.

## License

MIT. See [LICENSE](LICENSE).
