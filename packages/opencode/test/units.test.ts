import { execFileSync, spawnSync } from "node:child_process"
import { mkdir, mkdtemp, symlink, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"

import { describe, expect, it } from "vitest"

import { withRateLimitRetry } from "../src/adapter.js"
import plugin from "../src/index.js"
import { resolveOptions, resolveProjectPath } from "../src/options.js"
import { renderEnvFile, setupScript, shq } from "../src/remote.js"
import { batchFiles, planSnapshot } from "../src/upload.js"

describe("options", () => {
  it("has defaults", () => {
    const o = resolveOptions(undefined)
    expect(o).toMatchObject({ cpus: 2, timeoutMinutes: 120, onTimeout: "pause", idleTimeoutSeconds: 600, serverPort: 4096 })
  })
  it("rejects bad values", () => {
    expect(() => resolveOptions({ cpus: 99 })).toThrow(/cpus/)
    expect(() => resolveOptions({ template: "a", image: "b" })).toThrow(/not both/)
    expect(() => resolveOptions({ onTimeout: "explode" })).toThrow(/onTimeout/)
    expect(() => resolveOptions({ projectPath: "relative" })).toThrow(/absolute/)
    expect(() => resolveOptions({ forwardEnv: ["BAD-NAME"] })).toThrow(/valid/)
    expect(() => resolveOptions({ idleTimeoutSeconds: 5 })).toThrow(/idleTimeoutSeconds/)
  })
  it("turns idle pause off with null", () => {
    expect(resolveOptions({ idleTimeoutSeconds: null }).idleTimeoutSeconds).toBeNull()
  })
  it("mirrors POSIX host paths and falls back on Windows", () => {
    const o = resolveOptions({})
    expect(resolveProjectPath(o, "/Users/me/app", "darwin")).toBe("/Users/me/app")
    expect(resolveProjectPath(o, "C:\\work\\app", "win32")).toBe("/home/runner/project")
  })
})

describe("remote scripts", () => {
  it("quotes for sh", () => {
    const out = spawnSync("sh", ["-c", `printf '%s' ${shq("it's $HOME `x`")}`]).stdout.toString()
    expect(out).toBe("it's $HOME `x`")
  })
  it("writes an env file sh reads back unchanged", async () => {
    const dir = await mkdtemp(join(tmpdir(), "gb-oc-env-"))
    const file = join(dir, "env")
    const tricky = `{"k":"a'b $X \`y\` \\n"}`
    await writeFile(file, renderEnvFile({ OPENCODE_AUTH_CONTENT: tricky, "BAD-NAME": "x", EMPTY: undefined }))
    const out = execFileSync("sh", ["-c", `set -a; . "${file}"; set +a; printf '%s' "$OPENCODE_AUTH_CONTENT"`]).toString()
    expect(out).toBe(tricky)
  })
  it("installs the pinned OpenCode version", () => {
    const s = setupScript({ projectPath: "/p", branch: "main", projectID: "x", prepareCommands: [], opencodeVersion: "1.19.6" })
    expect(s).toContain("'opencode-ai@1.19.6'")
    expect(spawnSync("sh", ["-n", "-c", s]).status).toBe(0)
  })
})

describe("snapshot", () => {
  it("lets git decide in a repository", async () => {
    const dir = await mkdtemp(join(tmpdir(), "gb-oc-git-"))
    execFileSync("git", ["init", "-q", dir])
    await writeFile(join(dir, ".gitignore"), "secret.env\nout/\n")
    await writeFile(join(dir, "keep.ts"), "x")
    await writeFile(join(dir, "secret.env"), "TOKEN=1")
    await mkdir(join(dir, "out"))
    await writeFile(join(dir, "out", "big.js"), "x")
    await symlink("keep.ts", join(dir, "link.ts"))
    const plan = await planSnapshot(dir, [], 1024)
    expect(plan.source).toBe("git")
    expect(plan.files.map((f) => f.path).sort()).toEqual([".gitignore", "keep.ts", "link.ts"])
    expect(plan.files.find((f) => f.path === "link.ts")!.symlink).toBe("keep.ts")
  })
  it("walks and skips ignored names elsewhere, and skips big files", async () => {
    const dir = await mkdtemp(join(tmpdir(), "gb-oc-walk-"))
    await mkdir(join(dir, "node_modules"))
    await writeFile(join(dir, "node_modules", "x.js"), "x")
    await mkdir(join(dir, "src"))
    await writeFile(join(dir, "src", "a.ts"), "x")
    await writeFile(join(dir, "huge.bin"), Buffer.alloc(2000))
    const plan = await planSnapshot(dir, ["node_modules"], 1000)
    expect(plan.source).toBe("walk")
    expect(plan.files.map((f) => f.path)).toEqual(["src/a.ts"])
    expect(plan.skipped.map((s) => s.path)).toEqual(["huge.bin"])
  })
  it("batches by count and bytes", () => {
    const files = Array.from({ length: 5 }, (_, i) => ({ path: `f${i}`, size: 10, mode: 0o644 }))
    expect(batchFiles(files, 2).map((b) => b.length)).toEqual([2, 2, 1])
    expect(batchFiles(files, 100, 25).map((b) => b.length)).toEqual([2, 2, 1])
  })
})

describe("plugin module", () => {
  it("default-exports an OpenCode 1 PluginModule that registers the groundbox type", async () => {
    expect(plugin.id).toBe("groundbox")
    const registered: string[] = []
    const hooks = await plugin.server(
      {
        client: { app: { log: async () => ({}) } },
        directory: "/tmp/x",
        experimental_workspace: { register: (type: string) => registered.push(type) },
        serverUrl: new URL("http://127.0.0.1:1"),
      } as never,
      {},
    )
    expect(hooks).toEqual({})
    expect(registered).toEqual(["groundbox"])
  })
})

describe("rate limit retry", () => {
  it("retries 429s and passes other errors through", async () => {
    let calls = 0
    const api = {
      async flaky() {
        calls++
        if (calls < 3) throw Object.assign(new Error("Too many requests from this API key. Try again in 0 seconds"), { status: 429 })
        return "ok"
      },
      async broken() {
        throw Object.assign(new Error("nope"), { status: 404 })
      },
    }
    const wrapped = withRateLimitRetry(api, 10)
    expect(await wrapped.flaky()).toBe("ok")
    expect(calls).toBe(3)
    await expect(wrapped.broken()).rejects.toThrow("nope")
  })
})
