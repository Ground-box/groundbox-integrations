import type { GroundboxClient, Logger } from "../src/adapter.js"

type Status = "running" | "paused" | "shut_down" | "stopped" | "queued"

export type FakeSandbox = {
  id: string
  status: Status
  metadata: Record<string, string>
  wakes_on_request?: boolean
  spec: Record<string, unknown>
}

/** An in-memory Groundbox: just enough of the SDK for the adapter, and a log of what was called. */
export function createFakeGroundbox(opts: { failCommand?: RegExp; previewExpiresInMs?: number } = {}) {
  const sandboxes = new Map<string, FakeSandbox>()
  const calls: { method: string; args: unknown[] }[] = []
  const files = new Map<string, string>()
  const uploads: { id: string; dir: string; paths: string[] }[] = []
  let n = 0
  let previews = 0

  const record = (method: string, ...args: unknown[]) => calls.push({ method, args })
  const get = (id: string) => {
    const s = sandboxes.get(id)
    if (!s) throw Object.assign(new Error("not found"), { status: 404 })
    return s
  }
  const view = (s: FakeSandbox) => ({ ...s, error: null }) as never

  const client: GroundboxClient = {
    async create_sandbox(spec, options) {
      record("create_sandbox", spec, options)
      const id = `sbx_fake${++n}`
      const s: FakeSandbox = { id, status: "running", metadata: { ...(spec?.metadata ?? {}) }, spec: spec as never }
      sandboxes.set(id, s)
      return view(s)
    },
    async wait_for_sandbox(id) {
      record("wait_for_sandbox", id)
      const s = get(id)
      if (s.status === "queued") s.status = "running"
      return view(s)
    },
    async get_sandbox(id) {
      record("get_sandbox", id)
      return view(get(id))
    },
    async list_sandboxes(query) {
      record("list_sandboxes", query)
      const meta = (query?.metadata ?? {}) as Record<string, string>
      const data = [...sandboxes.values()].filter(
        (s) => s.status !== "stopped" && Object.entries(meta).every(([k, v]) => s.metadata[k] === v),
      )
      return { data: data.map(view) } as never
    },
    async delete_sandbox(id) {
      record("delete_sandbox", id)
      const s = get(id)
      s.status = "stopped"
      return view(s)
    },
    async resume_sandbox(id) {
      record("resume_sandbox", id)
      const s = get(id)
      s.status = "running"
      return view(s)
    },
    async start_sandbox(id) {
      record("start_sandbox", id)
      const s = get(id)
      s.status = "running"
      return view(s)
    },
    async run_command(id, cmd) {
      record("run_command", id, cmd)
      const text = String(cmd)
      if (opts.failCommand?.test(text)) return { exit_code: 1, stdout: "", stderr: "boom", duration_ms: 1, timed_out: false } as never
      let stdout = ""
      if (text.includes('printf \'%s\' "$HOME"')) stdout = "/home/runner"
      else if (text.includes("oc_bin")) stdout = "installing...\n/usr/local/bin/opencode\n"
      return { exit_code: 0, stdout, stderr: "", duration_ms: 1, timed_out: false } as never
    },
    async write_file(id, path, content) {
      record("write_file", id, path)
      files.set(`${id}:${path}`, String(content))
      return {} as never
    },
    async upload_files(id, dir, list) {
      record("upload_files", id, dir)
      uploads.push({ id, dir, paths: list.map((f) => f.path) })
      return { written: list.length, removed: 0, bytes: 1 }
    },
    async expose_port(id, port) {
      record("expose_port", id, port)
      get(id)
      previews++
      const slug = `slug${previews}`
      return {
        id: "00000000-0000-0000-0000-000000000000",
        sandbox_id: id,
        port,
        public: false,
        url: `https://${slug}-${port}.groundbox.page/`,
        share_url: `https://${slug}-${port}.groundbox.page/?gb_token=tok${previews}`,
        expires_at: new Date(Date.now() + (opts.previewExpiresInMs ?? 24 * 3600_000)).toISOString(),
        disabled: false,
        bytes_in: 0,
        bytes_out: 0,
        requests: 0,
        last_request_at: null,
        created_at: new Date().toISOString(),
      }
    },
    async delete_preview(id, port) {
      record("delete_preview", id, port)
      return { ok: true } as never
    },
  }

  return { client, sandboxes, calls, files, uploads, methods: () => calls.map((c) => c.method) }
}

export const silentLogger: Logger = { info: async () => {}, warn: async () => {}, error: async () => {} }

/** A fetch that answers 200 to the readiness probes and records what it saw. */
export function okFetch(seen: { url: string; headers: Record<string, string> }[] = []) {
  const fn = (async (url: URL | string, init?: RequestInit) => {
    seen.push({ url: String(url), headers: Object.fromEntries(new Headers(init?.headers).entries()) })
    return new Response("{}", { status: 200 })
  }) as typeof fetch
  return { fetch: fn, seen }
}
