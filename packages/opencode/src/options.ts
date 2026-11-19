/** Plugin options, as given in the second element of the `opencode.json` plugin tuple. */
export type GroundboxWorkspaceOptions = {
  /** vCPUs for each workspace sandbox (1 to 16). Default 2. */
  cpus?: number
  /** GB of memory; up to 4 per vCPU. Default: Groundbox's default for `cpus`. */
  memoryGb?: number
  /** Minimum disk size in GB. Default: Groundbox's default (20). */
  diskGb?: number
  /** Start from a Groundbox template instead of the default image. */
  template?: string
  /** Start from a public Docker image (linux/amd64). Commands then run as root. Needs Node.js/npm or curl+bash. */
  image?: string
  /** Sandbox lifetime in minutes, counted from its last start or wake. Default 120. */
  timeoutMinutes?: number
  /** What the timeout does: "pause" (default, keeps memory), "stop" (keeps the disk) or "delete". */
  onTimeout?: "pause" | "stop" | "delete"
  /** Pause the sandbox after this many idle seconds; the next request wakes it. Default 600. `null` turns it off. */
  idleTimeoutSeconds?: number | null
  /** Project path inside the sandbox. Default: the same path as on this machine. */
  projectPath?: string
  /** Port of the OpenCode server in the sandbox. Default 4096. */
  serverPort?: number
  /** OpenCode version to install in the sandbox. Default: the version of the local OpenCode. */
  opencodeVersion?: string
  /** Names of local environment variables to copy into the remote OpenCode server (for example ANTHROPIC_API_KEY). */
  forwardEnv?: string[]
  /** Extra environment variables for the remote OpenCode server. */
  env?: Record<string, string>
  /** Shell commands run in the project folder after upload and before the server starts (for example `npm ci`). */
  prepareCommands?: string[]
  /** Hosts the sandbox may reach (egress allowlist). Default: no limit. */
  allowHosts?: string[]
  /** Extra sandbox metadata. */
  metadata?: Record<string, string>
  /** Paths (or path segments) not uploaded when the project is not a git repository. */
  uploadIgnore?: string[]
  /** Files larger than this are not uploaded. Default 50 MB. */
  maxFileBytes?: number
  /** How long to wait for the remote OpenCode server, in ms. Default 90000. */
  readyTimeoutMs?: number
}

export type ResolvedOptions = {
  cpus: number
  memoryGb?: number
  diskGb?: number
  template?: string
  image?: string
  timeoutMinutes: number
  onTimeout: "pause" | "stop" | "delete"
  idleTimeoutSeconds: number | null
  projectPath?: string
  serverPort: number
  opencodeVersion?: string
  forwardEnv: string[]
  env: Record<string, string>
  prepareCommands: string[]
  allowHosts?: string[]
  metadata: Record<string, string>
  uploadIgnore: string[]
  maxFileBytes: number
  readyTimeoutMs: number
}

export const DEFAULT_UPLOAD_IGNORE = [
  ".git",
  "node_modules",
  "dist",
  "build",
  ".next",
  ".turbo",
  ".cache",
  ".venv",
  "__pycache__",
  ".DS_Store",
]

const ENV_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/

export function resolveOptions(raw: unknown): ResolvedOptions {
  const o = (raw ?? {}) as Record<string, unknown>
  if (typeof o !== "object" || Array.isArray(o)) throw new Error("@groundbox/opencode: options must be an object")

  const num = (key: string, min: number, max: number): number | undefined => {
    const v = o[key]
    if (v === undefined) return undefined
    if (typeof v !== "number" || !Number.isFinite(v) || v < min || v > max) {
      throw new Error(`@groundbox/opencode: ${key} must be a number from ${min} to ${max}`)
    }
    return v
  }
  const str = (key: string): string | undefined => {
    const v = o[key]
    if (v === undefined) return undefined
    if (typeof v !== "string" || v.trim() === "") throw new Error(`@groundbox/opencode: ${key} must be a non-empty string`)
    return v.trim()
  }
  const strList = (key: string): string[] | undefined => {
    const v = o[key]
    if (v === undefined) return undefined
    if (!Array.isArray(v) || v.some((x) => typeof x !== "string")) {
      throw new Error(`@groundbox/opencode: ${key} must be a list of strings`)
    }
    return v as string[]
  }
  const strMap = (key: string): Record<string, string> | undefined => {
    const v = o[key]
    if (v === undefined) return undefined
    if (typeof v !== "object" || v === null || Array.isArray(v) || Object.values(v).some((x) => typeof x !== "string")) {
      throw new Error(`@groundbox/opencode: ${key} must be an object of strings`)
    }
    return v as Record<string, string>
  }

  const template = str("template")
  const image = str("image")
  if (template && image) throw new Error("@groundbox/opencode: set template or image, not both")

  const onTimeout = o.onTimeout ?? "pause"
  if (onTimeout !== "pause" && onTimeout !== "stop" && onTimeout !== "delete") {
    throw new Error('@groundbox/opencode: onTimeout must be "pause", "stop" or "delete"')
  }

  let idleTimeoutSeconds: number | null = 600
  if (o.idleTimeoutSeconds === null) idleTimeoutSeconds = null
  else if (o.idleTimeoutSeconds !== undefined) idleTimeoutSeconds = num("idleTimeoutSeconds", 30, 30 * 24 * 3600)!

  const projectPath = str("projectPath")
  if (projectPath && !projectPath.startsWith("/")) throw new Error("@groundbox/opencode: projectPath must be absolute")

  const forwardEnv = strList("forwardEnv") ?? []
  const env = strMap("env") ?? {}
  for (const name of [...forwardEnv, ...Object.keys(env)]) {
    if (!ENV_NAME.test(name)) throw new Error(`@groundbox/opencode: ${name} is not a valid environment variable name`)
  }

  return {
    cpus: num("cpus", 1, 16) ?? 2,
    memoryGb: num("memoryGb", 1, 64),
    diskGb: num("diskGb", 1, 2000),
    template,
    image,
    timeoutMinutes: num("timeoutMinutes", 1, 43_200) ?? 120,
    onTimeout,
    idleTimeoutSeconds,
    projectPath,
    serverPort: num("serverPort", 1024, 65_535) ?? 4096,
    opencodeVersion: str("opencodeVersion"),
    forwardEnv,
    env,
    prepareCommands: strList("prepareCommands") ?? [],
    allowHosts: strList("allowHosts"),
    metadata: strMap("metadata") ?? {},
    uploadIgnore: strList("uploadIgnore") ?? DEFAULT_UPLOAD_IGNORE,
    maxFileBytes: num("maxFileBytes", 1, 1024 ** 3) ?? 50 * 1024 * 1024,
    readyTimeoutMs: num("readyTimeoutMs", 1000, 30 * 60_000) ?? 90_000,
  }
}

/** Where the project lives in the sandbox: the host path when it can be mirrored on Linux. */
export function resolveProjectPath(options: ResolvedOptions, localDirectory: string, platform = process.platform): string {
  if (options.projectPath) return options.projectPath
  if (platform === "win32" || !localDirectory.startsWith("/")) return "/home/runner/project"
  return localDirectory
}
