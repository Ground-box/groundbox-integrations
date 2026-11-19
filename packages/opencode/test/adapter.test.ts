import { mkdtemp, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"

import type { WorkspaceInfo } from "@opencode-ai/plugin"
import { beforeEach, describe, expect, it } from "vitest"

import { METADATA_KEY, createGroundboxWorkspaceAdapter, readExtra } from "../src/adapter.js"
import { resolveOptions } from "../src/options.js"
import { createFakeGroundbox, okFetch, silentLogger } from "./fake.js"

const ENV = { GROUNDBOX_API_KEY: "gbx_test" }

async function project() {
  const dir = await mkdtemp(join(tmpdir(), "gb-oc-adapter-"))
  await writeFile(join(dir, "a.txt"), "hello")
  return dir
}

function baseInfo(): WorkspaceInfo {
  return { id: "wrk_0123456789ABCDEF", type: "groundbox", name: "x", branch: null, directory: null, extra: null, projectID: "proj_1" }
}

describe("groundbox workspace adapter", () => {
  let dir: string
  beforeEach(async () => {
    dir = await project()
  })

  function setup(rawOptions: Record<string, unknown> = {}, fakeOpts = {}) {
    const fake = createFakeGroundbox(fakeOpts)
    const http = okFetch()
    const adapter = createGroundboxWorkspaceAdapter({
      directory: dir,
      options: resolveOptions(rawOptions),
      groundbox: () => fake.client,
      resolveRuntimeVersion: async () => "1.19.6",
      logger: silentLogger,
      fetch: http.fetch,
      env: { ...ENV, MY_KEY: "secret value" },
      pollMs: 1,
    })
    return { fake, http, adapter }
  }

  it("configure mirrors the host path, names the workspace and keeps runtime settings in extra", async () => {
    const { adapter } = setup()
    const info = await adapter.configure({ ...baseInfo(), branch: "Feature/Login" })
    expect(info.directory).toBe(dir)
    expect(info.name).toBe("groundbox-feature-login")
    const extra = readExtra(info)!
    expect(extra.runtimeVersion).toBe("1.19.6")
    expect(extra.serverPort).toBe(4096)
    expect(extra.password.length).toBeGreaterThanOrEqual(32)
  })

  it("configure falls back to the id for the name and honours projectPath and opencodeVersion", async () => {
    const { adapter } = setup({ projectPath: "/work/app", opencodeVersion: "1.18.0" })
    const info = await adapter.configure(baseInfo())
    expect(info.name).toBe("groundbox-89abcdef")
    expect(info.directory).toBe("/work/app")
    expect(readExtra(info)!.runtimeVersion).toBe("1.18.0")
  })

  it("create makes a sandbox, uploads the project, starts the server and waits for it", async () => {
    const { adapter, fake, http } = setup({ forwardEnv: ["MY_KEY"], prepareCommands: ["npm ci"], cpus: 4 })
    const info = await adapter.configure(baseInfo())
    await adapter.create(info, { OPENCODE_AUTH_CONTENT: '{"a":1}', OPENCODE_WORKSPACE_ID: info.id })

    const create = fake.calls.find((c) => c.method === "create_sandbox")!
    const spec = create.args[0] as Record<string, unknown>
    expect(spec.cpus).toBe(4)
    expect(spec.idle_timeout_seconds).toBe(600)
    expect(spec.wake_on_request).toBe(true)
    expect(spec.on_timeout).toBe("pause")
    expect((spec.metadata as Record<string, string>)[METADATA_KEY]).toBe(info.id)
    expect(JSON.stringify(spec.metadata)).not.toContain(readExtra(info)!.password)

    expect(fake.uploads).toHaveLength(1)
    expect(fake.uploads[0]!.dir).toBe(dir)
    expect(fake.uploads[0]!.paths).toEqual(["a.txt"])

    const scripts = fake.calls.filter((c) => c.method === "run_command").map((c) => String(c.args[1]))
    expect(scripts.some((s) => s.includes("opencode-ai@1.19.6") && s.includes("(npm ci)"))).toBe(true)
    expect(scripts.some((s) => s.includes("nohup"))).toBe(true)

    const envFile = fake.files.get("sbx_fake1:/home/runner/.groundbox-opencode/env")!
    expect(envFile).toContain(`OPENCODE_AUTH_CONTENT='{"a":1}'`)
    expect(envFile).toContain("MY_KEY='secret value'")
    expect(envFile).toContain(`OPENCODE_SERVER_PASSWORD='${readExtra(info)!.password}'`)
    expect(fake.files.get("sbx_fake1:/home/runner/.groundbox-opencode/serve.sh")).toContain(
      "exec '/usr/local/bin/opencode' serve --hostname 0.0.0.0 --port 4096",
    )

    const paths = http.seen.map((s) => new URL(s.url).pathname)
    expect(paths).toEqual(["/global/health", "/sync/history"])
    expect(http.seen[0]!.headers["x-groundbox-preview-token"]).toBe("tok1")
    expect(http.seen[0]!.headers.authorization).toMatch(/^Basic /)
  })

  it("create deletes the sandbox when setup fails", async () => {
    const { adapter, fake } = setup({}, { failCommand: /oc_bin/ })
    const info = await adapter.configure(baseInfo())
    await expect(adapter.create(info, {})).rejects.toThrow(/exit 1/)
    expect(fake.methods()).toContain("delete_sandbox")
    expect(fake.sandboxes.get("sbx_fake1")!.status).toBe("stopped")
  })

  it("create replaces a sandbox left from an earlier attempt", async () => {
    const { adapter, fake } = setup()
    const info = await adapter.configure(baseInfo())
    await adapter.create(info, {})
    await adapter.create(info, {})
    expect(fake.sandboxes.get("sbx_fake1")!.status).toBe("stopped")
    expect(fake.sandboxes.get("sbx_fake2")!.status).toBe("running")
  })

  it("target returns the preview URL with the preview token and basic auth", async () => {
    const { adapter } = setup()
    const info = await adapter.configure(baseInfo())
    await adapter.create(info, {})
    const target = await adapter.target(info)
    expect(target.type).toBe("remote")
    if (target.type !== "remote") return
    expect(String(target.url)).toBe("https://slug1-4096.groundbox.page/")
    const headers = new Headers(target.headers)
    expect(headers.get("x-groundbox-preview-token")).toBe("tok1")
    const decoded = Buffer.from(headers.get("authorization")!.slice(6), "base64").toString()
    expect(decoded).toBe(`opencode:${readExtra(info)!.password}`)
  })

  it("target finds the sandbox by metadata after a restart, and resumes a manually paused one", async () => {
    const first = setup()
    const info = await first.adapter.configure(baseInfo())
    await first.adapter.create(info, {})
    first.fake.sandboxes.get("sbx_fake1")!.status = "paused"
    first.fake.sandboxes.get("sbx_fake1")!.wakes_on_request = false

    // A fresh adapter (OpenCode restarted) over the same fake account.
    const again = createGroundboxWorkspaceAdapter({
      directory: dir,
      options: resolveOptions({}),
      groundbox: () => first.fake.client,
      resolveRuntimeVersion: async () => "1.19.6",
      logger: silentLogger,
      fetch: okFetch().fetch,
      env: ENV,
      pollMs: 1,
    })
    const target = await again.target(info)
    expect(target.type).toBe("remote")
    expect(first.fake.methods()).toContain("resume_sandbox")
  })

  it("target leaves an idle-paused sandbox to wake on the request", async () => {
    const { adapter, fake } = setup()
    const info = await adapter.configure(baseInfo())
    await adapter.create(info, {})
    const s = fake.sandboxes.get("sbx_fake1")!
    s.status = "paused"
    s.wakes_on_request = true
    await new Promise((r) => setTimeout(r, 0))
    const fresh = createGroundboxWorkspaceAdapter({
      directory: dir,
      options: resolveOptions({}),
      groundbox: () => fake.client,
      resolveRuntimeVersion: async () => "1.19.6",
      logger: silentLogger,
      fetch: okFetch().fetch,
      env: ENV,
    })
    await fresh.target(info)
    expect(fake.methods()).not.toContain("resume_sandbox")
  })

  it("target starts a stopped sandbox and restarts the server", async () => {
    const { adapter, fake } = setup()
    const info = await adapter.configure(baseInfo())
    await adapter.create(info, {})
    fake.sandboxes.get("sbx_fake1")!.status = "shut_down"
    const fresh = createGroundboxWorkspaceAdapter({
      directory: dir,
      options: resolveOptions({}),
      groundbox: () => fake.client,
      resolveRuntimeVersion: async () => "1.19.6",
      logger: silentLogger,
      fetch: okFetch().fetch,
      env: ENV,
      pollMs: 1,
    })
    const before = fake.calls.length
    await fresh.target(info)
    const after = fake.calls.slice(before)
    expect(after.map((c) => c.method)).toContain("start_sandbox")
    expect(after.some((c) => c.method === "run_command" && String(c.args[1]).includes("nohup"))).toBe(true)
  })

  it("target explains a sandbox that is gone", async () => {
    const { adapter, fake } = setup()
    const info = await adapter.configure(baseInfo())
    await adapter.create(info, {})
    fake.sandboxes.get("sbx_fake1")!.status = "stopped"
    const fresh = createGroundboxWorkspaceAdapter({
      directory: dir,
      options: resolveOptions({}),
      groundbox: () => fake.client,
      resolveRuntimeVersion: async () => "1.19.6",
      logger: silentLogger,
      env: ENV,
    })
    await expect(fresh.target(info)).rejects.toThrow(/is gone/)
  })

  it("renews a preview whose share token is about to expire", async () => {
    const { adapter, fake } = setup({}, { previewExpiresInMs: 10 * 60_000 })
    const info = await adapter.configure(baseInfo())
    await adapter.create(info, {})
    expect(fake.methods()).toContain("delete_preview")
  })

  it("remove deletes the workspace's sandboxes", async () => {
    const { adapter, fake } = setup()
    const info = await adapter.configure(baseInfo())
    await adapter.create(info, {})
    await adapter.remove(info)
    expect(fake.sandboxes.get("sbx_fake1")!.status).toBe("stopped")
  })

  it("asks for GROUNDBOX_API_KEY", async () => {
    const fake = createFakeGroundbox()
    const adapter = createGroundboxWorkspaceAdapter({
      directory: dir,
      options: resolveOptions({}),
      groundbox: () => fake.client,
      resolveRuntimeVersion: async () => "1.19.6",
      logger: silentLogger,
      env: {},
    })
    const info = await adapter.configure(baseInfo())
    await expect(adapter.create(info, {})).rejects.toThrow(/GROUNDBOX_API_KEY/)
    expect(fake.calls).toHaveLength(0)
  })
})
