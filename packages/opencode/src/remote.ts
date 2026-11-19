/** Shell scripts run in the sandbox, and the env file the remote OpenCode server reads. */

export const STATE_DIR = ".groundbox-opencode"

export function shq(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`
}

const SUDO = `if [ "$(id -u)" = 0 ]; then SUDO=; else SUDO="sudo -n"; fi`

/** Makes the project folder (it may mirror a host path like /Users/...) owned by the sandbox user. Prints $HOME. */
export function prepareDirScript(projectPath: string): string {
  return [
    "set -e",
    SUDO,
    `$SUDO mkdir -p ${shq(projectPath)}`,
    `$SUDO chown "$(id -u):$(id -g)" ${shq(projectPath)}`,
    `mkdir -p "$HOME/${STATE_DIR}"`,
    `chmod 700 "$HOME/${STATE_DIR}"`,
    `printf '%s' "$HOME"`,
  ].join("\n")
}

/**
 * Runs the user's prepare commands, commits the uploaded snapshot (so moving a session back
 * home carries only the agent's changes), marks the project id, and installs OpenCode.
 * Prints the path of the opencode binary on its last line.
 */
export function setupScript(params: {
  projectPath: string
  branch: string
  projectID: string
  prepareCommands: string[]
  opencodeVersion: string
}): string {
  const version = params.opencodeVersion
  const pkg = version === "latest" ? "opencode-ai@latest" : `opencode-ai@${version}`
  const installFlag = version === "latest" ? "" : ` --version ${shq(version)}`
  return [
    "set -e",
    SUDO,
    `cd ${shq(params.projectPath)}`,
    ...params.prepareCommands.map((c) => c.trim()).filter(Boolean).map((c) => `(${c}) >&2`),
    `if [ ! -d .git ]; then git init -q -b ${shq(params.branch)} && git add -A && git -c user.name=opencode -c user.email=opencode@groundbox.local commit -q --allow-empty -m 'Workspace snapshot'; fi`,
    `printf '%s' ${shq(params.projectID)} > .git/opencode`,
    `oc_bin() { command -v opencode 2>/dev/null || { [ -x "$HOME/.opencode/bin/opencode" ] && echo "$HOME/.opencode/bin/opencode"; }; }`,
    `BIN=$(oc_bin || true)`,
    `if [ -z "$BIN" ] || [ ${shq(version)} = latest ] || [ "$("$BIN" --version 2>/dev/null)" != ${shq(version)} ]; then`,
    `  if command -v npm >/dev/null 2>&1; then $SUDO npm install -g --no-fund --no-audit ${shq(pkg)} >&2`,
    `  elif command -v curl >/dev/null 2>&1 && command -v bash >/dev/null 2>&1; then curl -fsSL https://opencode.ai/install | bash -s --${installFlag} >&2`,
    `  else echo "OpenCode cannot be installed: the sandbox has neither npm nor curl and bash" >&2; exit 3; fi`,
    `  hash -r 2>/dev/null || true`,
    `  BIN=$(oc_bin)`,
    `fi`,
    `echo "$BIN"`,
  ].join("\n")
}

/** The launcher the server runs from. It reads the env file each start, so a stopped sandbox can start again. */
export function serveScript(params: { home: string; projectPath: string; bin: string; port: number }): string {
  return [
    "#!/bin/sh",
    "set -a",
    `. ${shq(`${params.home}/${STATE_DIR}/env`)}`,
    "set +a",
    `cd ${shq(params.projectPath)}`,
    `exec ${shq(params.bin)} serve --hostname 0.0.0.0 --port ${params.port}`,
    "",
  ].join("\n")
}

/**
 * Starts (or restarts) the server in the background. The command returns at once, so the
 * sandbox can still pause when idle; the server pauses with it and goes on when it wakes.
 */
export function startServerScript(): string {
  return [
    `D="$HOME/${STATE_DIR}"`,
    `chmod 600 "$D/env"`,
    `if [ -f "$D/server.pid" ] && kill -0 "$(cat "$D/server.pid")" 2>/dev/null; then kill "$(cat "$D/server.pid")" || true; sleep 1; fi`,
    `nohup sh "$D/serve.sh" > "$D/server.log" 2>&1 < /dev/null &`,
    `echo $! > "$D/server.pid"`,
  ].join("\n")
}

export function serverLogTailScript(): string {
  return `tail -n 40 "$HOME/${STATE_DIR}/server.log" 2>/dev/null || true`
}

/** The env file for the remote server. Values are single-quoted, so nothing in them is expanded. */
export function renderEnvFile(env: Record<string, string | undefined>): string {
  const lines = ["# Written by @groundbox/opencode. Regenerated on every workspace create."]
  for (const [key, value] of Object.entries(env)) {
    if (value === undefined || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) continue
    lines.push(`${key}=${shq(value)}`)
  }
  return lines.join("\n") + "\n"
}
