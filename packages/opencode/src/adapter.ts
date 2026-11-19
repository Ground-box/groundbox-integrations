import { execFile } from "node:child_process"
import { randomBytes, randomUUID } from "node:crypto"
import { promisify } from "node:util"

import type { Groundbox } from "@groundbox/sdk"
import type { WorkspaceAdapter, WorkspaceInfo, WorkspaceTarget } from "@opencode-ai/plugin"

import { resolveProjectPath, type ResolvedOptions } from "./options.js"
import {
  STATE_DIR,
  prepareDirScript,
  renderEnvFile,
  serveScript,
  serverLogTailScript,
  setupScript,
  startServerScript,
} from "./remote.js"
import { batchFiles, planSnapshot, readBatch } from "./upload.js"

/** The part of the Groundbox SDK the adapter uses. Tests pass a fake. */
export type GroundboxClient = Pick<
  Groundbox,
  | "create_sandbox"
  | "wait_for_sandbox"
  | "get_sandbox"
  | "list_sandboxes"
  | "delete_sandbox"
  | "resume_sandbox"
  | "start_sandbox"
  | "run_command"
  | "write_file"
  | "upload_files"
  | "expose_port"
  | "delete_preview"
>

type Sandbox = Awaited<ReturnType<Groundbox["get_sandbox"]>>
type Preview = Awaited<ReturnType<Groundbox["expose_port"]>>

export type Logger = {
  info(message: string, extra?: Record<string, unknown>): Promise<void>
  warn(message: string, extra?: Record<string, unknown>): Promise<void>
  error(message: string, extra?: Record<string, unknown>): Promise<void>
}

/** What the workspace row keeps in `extra`. Stored locally by OpenCode, never sent to Groundbox as metadata. */
export type PersistedExtra = {
  groundbox: {
    v: 1
    runtimeVersion: string
    projectPath: string
    serverPort: number
    /** Basic-auth password of the remote OpenCode server. */
    password: string
  }
}

export type AdapterDeps = {
  /** The local project folder (the plugin input's `directory`). */
  directory: string
  options: ResolvedOptions
  /** Made on first use, so loading the plugin never needs a key. */
  groundbox: () => GroundboxClient
  /** The local OpenCode version, so the sandbox runs the same one. */
  resolveRuntimeVersion: () => Promise<string>
  logger: Logger
  fetch?: typeof fetch
  env?: NodeJS.ProcessEnv
  /** Milliseconds between readiness polls. Tests set it low. */
  pollMs?: number
}

export const METADATA_KEY = "opencode_workspace_id"
const PREVIEW_TOKEN_HEADER = "X-Groundbox-Preview-Token"
const PREVIEW_RENEW_MS = 60 * 60_000
const STATUS_CACHE_MS = 10_000
const execFileP = promisify(execFile)

export function readExtra(info: WorkspaceInfo): PersistedExtra["groundbox"] | undefined {
  const extra = info.extra as Partial<PersistedExtra> | null
  const g = extra?.groundbox
  if (!g || g.v !== 1 || typeof g.password !== "string" || typeof g.projectPath !== "string") return undefined
  return g
}

export function workspaceName(info: Pick<WorkspaceInfo, "id" | "branch">): string {
  const slug = (info.branch ?? "")
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "")
  return `groundbox-${slug || info.id.slice(-8).toLowerCase()}`
}

export function targetHeaders(preview: Preview, password: string): Record<string, string> {
  const token = preview.share_url ? new URL(preview.share_url).searchParams.get("gb_token") : null
  const headers: Record<string, string> = {
    authorization: `Basic ${Buffer.from(`opencode:${password}`).toString("base64")}`,
  }
  if (token) headers[PREVIEW_TOKEN_HEADER] = token
  return headers
}

export function createGroundboxWorkspaceAdapter(deps: AdapterDeps): WorkspaceAdapter {
  const fetchFn = deps.fetch ?? fetch
  const env = deps.env ?? process.env
  const pollMs = deps.pollMs ?? 750
  const cache = new Map<string, { sandboxId: string; checkedAt: number; preview?: Preview }>()

  const client = () => {
    const key = env.GROUNDBOX_API_KEY
    if (!key || key.trim() === "") {
      throw new Error(
        "GROUNDBOX_API_KEY is not set. Export it before starting OpenCode. Create a key at https://app.groundbox.dev/keys",
      )
    }
    return withRateLimitRetry(deps.groundbox())
  }

  async function findLive(gb: GroundboxClient, workspaceID: string): Promise<Sandbox[]> {
    const { data } = await gb.list_sandboxes({ live: true, metadata: { [METADATA_KEY]: workspaceID } })
    return data as Sandbox[]
  }

  async function exposePreview(gb: GroundboxClient, sandboxId: string, port: number): Promise<Preview> {
    let preview = await gb.expose_port(sandboxId, port)
    if (expiresSoon(preview)) {
      // A private preview's share token lasts 24 hours; a new preview gets a new one.
      await gb.delete_preview(sandboxId, port)
      preview = await gb.expose_port(sandboxId, port)
    }
    return preview
  }

  async function waitReady(gb: GroundboxClient, sandboxId: string, preview: Preview, password: string) {
    const headers = targetHeaders(preview, password)
    const base = preview.url
    const deadline = Date.now() + deps.options.readyTimeoutMs
    let last = "no answer yet"
    // No probe of the /global/event stream: a stream opened through a preview keeps the
    // sandbox from idling even after the client goes (seen on Groundbox, November 2026).
    while (Date.now() < deadline) {
      try {
        await probe(fetchFn, new URL("/global/health", base), { headers })
        await probe(fetchFn, new URL("/sync/history", base), {
          method: "POST",
          headers: { ...headers, "content-type": "application/json" },
          body: "{}",
        })
        return
      } catch (error) {
        last = errorMessage(error)
      }
      await sleep(pollMs)
    }
    const tail = await gb
      .run_command(sandboxId, serverLogTailScript())
      .then((r) => r.stdout.trim())
      .catch(() => "")
    throw new Error(
      `The OpenCode server in sandbox ${sandboxId} did not answer at ${base} (${last})` +
        (tail ? `\nLast lines of ~/${STATE_DIR}/server.log:\n${tail}` : ""),
    )
  }

  async function runChecked(gb: GroundboxClient, sandboxId: string, script: string, timeout_s = 600): Promise<string> {
    const r = await gb.run_command(sandboxId, script, { timeout_s })
    if (r.exit_code !== 0) {
      const detail = [r.stderr.trim(), r.stdout.trim()].filter(Boolean).join("\n").slice(-4000)
      throw new Error(`Command in sandbox ${sandboxId} failed (exit ${r.exit_code})${detail ? `:\n${detail}` : ""}`)
    }
    return r.stdout
  }

  async function provision(gb: GroundboxClient, info: WorkspaceInfo, extra: PersistedExtra["groundbox"], coreEnv: Record<string, string | undefined>) {
    const o = deps.options
    let sandbox = await gb.create_sandbox(
      {
        cpus: o.cpus,
        ...(o.memoryGb ? { memory_gb: o.memoryGb } : {}),
        ...(o.diskGb ? { disk_gb: o.diskGb } : {}),
        ...(o.template ? { template: o.template } : {}),
        ...(o.image ? { image: o.image } : {}),
        timeout_minutes: o.timeoutMinutes,
        on_timeout: o.onTimeout,
        ...(o.idleTimeoutSeconds !== null ? { idle_timeout_seconds: o.idleTimeoutSeconds, wake_on_request: true } : {}),
        ...(o.allowHosts ? { allow_hosts: o.allowHosts } : {}),
        metadata: {
          ...o.metadata,
          [METADATA_KEY]: info.id,
          opencode_project_id: info.projectID,
          created_by: "groundbox-opencode",
        },
      },
      { idempotencyKey: randomUUID() },
    )
    await deps.logger.info("sandbox created", { workspaceID: info.id, sandboxID: sandbox.id, status: sandbox.status })

    try {
      if (sandbox.status !== "running") sandbox = await gb.wait_for_sandbox(sandbox.id)
      if (sandbox.status !== "running") {
        throw new Error(`Sandbox ${sandbox.id} did not start (${sandbox.status}${sandbox.error ? `: ${sandbox.error}` : ""})`)
      }

      const home = (await runChecked(gb, sandbox.id, prepareDirScript(extra.projectPath))).trim()

      const plan = await planSnapshot(deps.directory, o.uploadIgnore, o.maxFileBytes)
      if (plan.skipped.length > 0) {
        await deps.logger.warn("files not uploaded", { skipped: plan.skipped.slice(0, 20), count: plan.skipped.length })
      }
      for (const batch of batchFiles(plan.files)) {
        await gb.upload_files(sandbox.id, extra.projectPath, await readBatch(deps.directory, batch), { timeout_s: 600 })
      }
      await deps.logger.info("project uploaded", { sandboxID: sandbox.id, files: plan.files.length, source: plan.source })

      const branch = info.branch ?? "main"
      const setupOut = await runChecked(
        gb,
        sandbox.id,
        setupScript({
          projectPath: extra.projectPath,
          branch,
          projectID: info.projectID,
          prepareCommands: o.prepareCommands,
          opencodeVersion: extra.runtimeVersion,
        }),
        1800,
      )
      const bin = setupOut.trim().split("\n").pop()?.trim()
      if (!bin) throw new Error(`OpenCode was not found in sandbox ${sandbox.id} after install`)

      const forwarded = Object.fromEntries(o.forwardEnv.map((name) => [name, env[name]]))
      const serverEnv = {
        ...forwarded,
        ...o.env,
        ...coreEnv,
        OPENCODE_SERVER_USERNAME: "opencode",
        OPENCODE_SERVER_PASSWORD: extra.password,
      }
      await gb.write_file(sandbox.id, `${home}/${STATE_DIR}/env`, renderEnvFile(serverEnv))
      await gb.write_file(
        sandbox.id,
        `${home}/${STATE_DIR}/serve.sh`,
        serveScript({ home, projectPath: extra.projectPath, bin, port: extra.serverPort }),
      )
      await runChecked(gb, sandbox.id, startServerScript())

      const preview = await exposePreview(gb, sandbox.id, extra.serverPort)
      await waitReady(gb, sandbox.id, preview, extra.password)
      cache.set(info.id, { sandboxId: sandbox.id, checkedAt: Date.now(), preview })
      await deps.logger.info("workspace ready", { workspaceID: info.id, sandboxID: sandbox.id, url: preview.url })
    } catch (error) {
      await deps.logger.error("workspace create failed; deleting its sandbox", {
        workspaceID: info.id,
        sandboxID: sandbox.id,
        error: errorMessage(error),
      })
      await gb.delete_sandbox(sandbox.id).catch(() => {})
      throw error
    }
  }

  /** Brings the workspace's sandbox to running, restarting the server after a stop. */
  async function ensureRunning(gb: GroundboxClient, info: WorkspaceInfo, extra: PersistedExtra["groundbox"]) {
    const cached = cache.get(info.id)
    if (cached && Date.now() - cached.checkedAt < STATUS_CACHE_MS) return cached

    let sandbox: Sandbox | undefined
    if (cached) sandbox = await gb.get_sandbox(cached.sandboxId).catch(() => undefined)
    if (!sandbox || sandbox.status === "stopped" || sandbox.status === "failed") sandbox = (await findLive(gb, info.id))[0]
    if (!sandbox) {
      cache.delete(info.id)
      throw new Error(
        `The Groundbox sandbox of workspace ${info.name} is gone (deleted, or past its timeout). Remove the workspace and create a new one.`,
      )
    }

    switch (sandbox.status) {
      case "paused":
      case "pausing":
        // An idle pause wakes on the next preview request; anything else needs a resume.
        if (!sandbox.wakes_on_request) sandbox = await gb.resume_sandbox(sandbox.id)
        break
      case "shut_down":
      case "shutting_down": {
        await deps.logger.info("starting stopped sandbox", { workspaceID: info.id, sandboxID: sandbox.id })
        if (sandbox.status === "shutting_down") sandbox = await gb.wait_for_sandbox(sandbox.id)
        sandbox = await gb.start_sandbox(sandbox.id)
        if (sandbox.status !== "running") sandbox = await gb.wait_for_sandbox(sandbox.id)
        await runChecked(gb, sandbox.id, startServerScript())
        const preview = await exposePreview(gb, sandbox.id, extra.serverPort)
        await waitReady(gb, sandbox.id, preview, extra.password)
        break
      }
      case "queued":
      case "starting":
      case "resuming":
        sandbox = await gb.wait_for_sandbox(sandbox.id)
        break
      default:
        break
    }

    const entry = { sandboxId: sandbox.id, checkedAt: Date.now(), preview: cached?.sandboxId === sandbox.id ? cached.preview : undefined }
    cache.set(info.id, entry)
    return entry
  }

  return {
    name: "Groundbox Sandbox",
    description: "Run this workspace's OpenCode in a Groundbox sandbox (a Firecracker microVM)",

    async configure(info) {
      const projectPath = resolveProjectPath(deps.options, deps.directory)
      const branch = info.branch ?? (await readLocalBranch(deps.directory))
      const runtimeVersion = deps.options.opencodeVersion ?? (await deps.resolveRuntimeVersion())
      const configured = { ...info, branch: branch ?? info.branch }
      const extra: PersistedExtra = {
        groundbox: {
          v: 1,
          runtimeVersion,
          projectPath,
          serverPort: deps.options.serverPort,
          password: randomBytes(24).toString("base64url"),
        },
      }
      return { ...configured, name: workspaceName(configured), directory: projectPath, extra }
    },

    async create(info, coreEnv) {
      const gb = client()
      const extra = readExtra(info)
      if (!extra) throw new Error(`Workspace ${info.id} was not configured by @groundbox/opencode`)

      for (const old of await findLive(gb, info.id)) {
        // A sandbox left from an earlier attempt: start clean.
        await deps.logger.warn("deleting earlier sandbox of this workspace", { workspaceID: info.id, sandboxID: old.id })
        await gb.delete_sandbox(old.id).catch(() => {})
      }
      cache.delete(info.id)
      await provision(gb, info, extra, coreEnv)
    },

    async remove(info) {
      const gb = client()
      const ids = new Set<string>()
      const cached = cache.get(info.id)
      if (cached) ids.add(cached.sandboxId)
      for (const s of await findLive(gb, info.id)) ids.add(s.id)
      for (const id of ids) await gb.delete_sandbox(id).catch(() => {})
      cache.delete(info.id)
      await deps.logger.info("workspace removed", { workspaceID: info.id, sandboxes: [...ids] })
    },

    async target(info): Promise<WorkspaceTarget> {
      const gb = client()
      const extra = readExtra(info)
      if (!extra) throw new Error(`Workspace ${info.id} was not configured by @groundbox/opencode`)
      const entry = await ensureRunning(gb, info, extra)
      if (!entry.preview || expiresSoon(entry.preview)) {
        entry.preview = await exposePreview(gb, entry.sandboxId, extra.serverPort)
      }
      return { type: "remote", url: entry.preview.url, headers: targetHeaders(entry.preview, extra.password) }
    },
  }
}

/**
 * Retries a call the API refused with 429 (rate limit), waiting as long as it asks, up to 5
 * times. A key shared by several agents can hit the limit in bursts.
 */
export function withRateLimitRetry<T extends object>(target: T, maxWaitMs = 30_000): T {
  return new Proxy(target, {
    get(obj, prop, receiver) {
      const value = Reflect.get(obj, prop, receiver)
      if (typeof value !== "function") return value
      return async (...args: unknown[]) => {
        for (let attempt = 0; ; attempt++) {
          try {
            return await value.apply(obj, args)
          } catch (error) {
            const e = error as { status?: number; message?: string }
            if (e?.status !== 429 || attempt >= 5) throw error
            const seconds = Number(/in (\d+) seconds?/.exec(e.message ?? "")?.[1] ?? 2 ** attempt)
            await sleep(Math.min(maxWaitMs, Math.max(500, seconds * 1000)))
          }
        }
      }
    },
  })
}

function expiresSoon(preview: Preview): boolean {
  if (preview.public || !preview.expires_at) return false
  return Date.parse(preview.expires_at) - Date.now() < PREVIEW_RENEW_MS
}

async function probe(fetchFn: typeof fetch, url: URL, init: RequestInit) {
  const res = await fetchFn(url, { ...init, signal: AbortSignal.timeout(5000) })
  try {
    if (!res.ok) throw new Error(`${url.pathname} answered HTTP ${res.status}`)
  } finally {
    await res.body?.cancel().catch(() => {})
  }
}

async function readLocalBranch(directory: string): Promise<string | null> {
  try {
    const { stdout } = await execFileP("git", ["-C", directory, "rev-parse", "--abbrev-ref", "HEAD"])
    const branch = stdout.trim()
    return branch && branch !== "HEAD" ? branch : null
  } catch {
    return null
  }
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}
