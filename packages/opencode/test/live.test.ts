/**
 * Live test against Groundbox production. Runs only with GROUNDBOX_API_KEY set (a paid-plan key:
 * previews are not in the free trial). Creates one sandbox and deletes it.
 *
 *   GROUNDBOX_API_KEY=gbx_... npm run test:live
 */
import { execFileSync } from "node:child_process"
import { mkdtemp, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"

import { Groundbox } from "@groundbox/sdk"
import type { WorkspaceInfo } from "@opencode-ai/plugin"
import { describe, expect, it } from "vitest"

import { METADATA_KEY, createGroundboxWorkspaceAdapter, withRateLimitRetry, type Logger } from "../src/adapter.js"
import { resolveOptions } from "../src/options.js"

const KEY = process.env.GROUNDBOX_API_KEY
const VERSION = process.env.OPENCODE_VERSION ?? "1.19.6"

const log: Logger = {
  info: async (m, e) => console.log("[info]", m, e ?? ""),
  warn: async (m, e) => console.log("[warn]", m, e ?? ""),
  error: async (m, e) => console.log("[error]", m, e ?? ""),
}

describe.skipIf(!KEY)("live: a groundbox workspace", () => {
  it(
    "creates a sandbox running OpenCode on the project, reaches it, and removes it",
    async () => {
      const dir = await mkdtemp(join(tmpdir(), "gb-oc-live-"))
      execFileSync("git", ["init", "-q", "-b", "main", dir])
      await writeFile(join(dir, ".gitignore"), "secret.env\n")
      await writeFile(join(dir, "hello.txt"), "hello from the host\n")
      await writeFile(join(dir, "secret.env"), "NOT_UPLOADED=1\n")

      const groundbox = withRateLimitRetry(new Groundbox({ userAgent: "groundbox-opencode-live-test" }))
      const adapter = createGroundboxWorkspaceAdapter({
        directory: dir,
        options: resolveOptions({ opencodeVersion: VERSION, timeoutMinutes: 30, metadata: { purpose: "groundbox-opencode-live-test" } }),
        groundbox: () => groundbox,
        resolveRuntimeVersion: async () => VERSION,
        logger: log,
      })

      const info = await adapter.configure({
        id: `wrk_live${Date.now().toString(36)}`,
        type: "groundbox",
        name: "",
        branch: null,
        directory: null,
        extra: null,
        projectID: "live-test-project",
      } satisfies WorkspaceInfo)
      expect(info.directory).toBe(dir)

      try {
        const t0 = Date.now()
        await adapter.create(info, { OPENCODE_WORKSPACE_ID: info.id, OPENCODE_EXPERIMENTAL_WORKSPACES: "true" })
        console.log(`create took ${((Date.now() - t0) / 1000).toFixed(1)}s`)

        const target = await adapter.target(info)
        if (target.type !== "remote") throw new Error("expected a remote target")
        const health = await fetch(new URL("/global/health", target.url), { headers: target.headers })
        expect(health.status).toBe(200)
        expect(await health.json()).toMatchObject({ healthy: true, version: VERSION })

        // Without the headers the preview refuses the request.
        expect((await fetch(new URL("/global/health", target.url))).status).toBe(401)

        // The remote server serves the uploaded project at the mirrored path.
        const file = await fetch(new URL(`/file/content?path=hello.txt&directory=${encodeURIComponent(dir)}`, target.url), {
          headers: target.headers,
        })
        expect(file.status).toBe(200)
        expect(JSON.stringify(await file.json())).toContain("hello from the host")

        const [sandbox] = (await groundbox.list_sandboxes({ live: true, metadata: { [METADATA_KEY]: info.id } })).data
        expect(sandbox).toBeDefined()
        const check = await groundbox.run_command(
          sandbox!.id,
          `cd '${dir}' && ls -A && git log --oneline && cat .git/opencode && test ! -e secret.env && echo no-secret`,
        )
        console.log(check.stdout)
        expect(check.stdout).toContain("hello.txt")
        expect(check.stdout).toContain("Workspace snapshot")
        expect(check.stdout).toContain("live-test-project")
        expect(check.stdout).toContain("no-secret")
      } finally {
        await adapter.remove(info)
      }
      const left = await groundbox.list_sandboxes({ live: true, metadata: { [METADATA_KEY]: info.id } })
      expect(left.data).toHaveLength(0)
    },
    10 * 60_000,
  )

  // A few minutes long: GROUNDBOX_LIVE_SLOW=1 to run it.
  it.skipIf(!process.env.GROUNDBOX_LIVE_SLOW)(
    "wakes an idle-paused sandbox, and starts a stopped one with its server",
    async () => {
      const dir = await mkdtemp(join(tmpdir(), "gb-oc-live-idle-"))
      await writeFile(join(dir, "hello.txt"), "hi\n")
      const groundbox = withRateLimitRetry(new Groundbox({ userAgent: "groundbox-opencode-live-test" }))
      const make = () =>
        createGroundboxWorkspaceAdapter({
          directory: dir,
          options: resolveOptions({ opencodeVersion: VERSION, timeoutMinutes: 30, idleTimeoutSeconds: 30 }),
          groundbox: () => groundbox,
          resolveRuntimeVersion: async () => VERSION,
          logger: log,
        })
      const adapter = make()
      const info = await adapter.configure({
        id: `wrk_idle${Date.now().toString(36)}`,
        type: "groundbox",
        name: "",
        branch: null,
        directory: null,
        extra: null,
        projectID: "live-idle",
      })
      const health = async (a: ReturnType<typeof make>) => {
        const t = await a.target(info)
        if (t.type !== "remote") throw new Error("expected remote")
        const r = await fetch(new URL("/global/health", t.url), { headers: t.headers, signal: AbortSignal.timeout(120_000) })
        return r.status
      }
      try {
        await adapter.create(info, {})
        const [sb] = (await groundbox.list_sandboxes({ live: true, metadata: { [METADATA_KEY]: info.id } })).data
        const id = sb!.id

        let status = ""
        for (let i = 0; i < 60 && status !== "paused"; i++) {
          await new Promise((r) => setTimeout(r, 5000))
          status = (await groundbox.get_sandbox(id)).status
        }
        console.log("after idling:", status)
        expect(status).toBe("paused")
        const t0 = Date.now()
        expect(await health(make())).toBe(200)
        console.log(`woke and answered in ${((Date.now() - t0) / 1000).toFixed(1)}s`)

        await groundbox.stop_sandbox(id)
        for (let i = 0; i < 60 && status !== "shut_down"; i++) {
          await new Promise((r) => setTimeout(r, 2000))
          status = (await groundbox.get_sandbox(id)).status
        }
        console.log("after stop:", status)
        const t1 = Date.now()
        expect(await health(make())).toBe(200)
        console.log(`started, server restarted and answered in ${((Date.now() - t1) / 1000).toFixed(1)}s`)
      } finally {
        await adapter.remove(info)
      }
    },
    15 * 60_000,
  )
})
