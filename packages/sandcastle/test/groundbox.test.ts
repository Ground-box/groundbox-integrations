import { lstat, mkdir, mkdtemp, readFile, readlink, rm, stat, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { PassThrough } from "node:stream";
import { run } from "@ai-hero/sandcastle";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { groundbox } from "../src/index.js";
import { internals } from "./internals.js";
import { makeFake, type FakeWorld } from "./fakeClient.js";
import { hostGit, makeHostRepo, scriptedAgent } from "./scriptedAgent.js";

type Fake = Awaited<ReturnType<typeof makeFake>>;

let fake: Fake;
let world: FakeWorld;
let scratch: string;

beforeEach(async () => {
  fake = await makeFake();
  world = fake.world;
  scratch = await mkdtemp(path.join(tmpdir(), "gbx-sandcastle-test-"));
});

afterEach(async () => {
  await fake.cleanup();
  await rm(scratch, { recursive: true, force: true });
});

const start = (options: Parameters<typeof groundbox>[0] = {}) =>
  internals(groundbox({ client: fake.client, ...options })).create({ env: { FROM_SANDCASTLE: "yes" } });

describe("groundbox() provider", () => {
  it("is an isolated provider named groundbox", () => {
    const provider = internals(groundbox({ client: fake.client, env: { A: "1" } }));
    expect(provider.tag).toBe("isolated");
    expect(provider.name).toBe("groundbox");
    expect(provider.env).toEqual({ A: "1" });
  });

  it("sets IS_SANDBOX when the sandbox starts from a Docker image (root)", () => {
    const provider = groundbox({ client: fake.client, image: "node:22", env: { A: "1" } });
    expect(provider.env).toEqual({ IS_SANDBOX: "1", A: "1" });
  });

  it("creates the sandbox with Sandcastle's env, spec pass-through and safe defaults", async () => {
    const handle = await start({ cpus: 4, metadata: { project: "x" }, allow_hosts: ["github.com"] });
    expect(world.created).toHaveLength(1);
    expect(world.created[0]).toMatchObject({
      cpus: 4,
      allow_hosts: ["github.com"],
      env: { FROM_SANDCASTLE: "yes" },
      timeout_minutes: 60,
      on_timeout: "delete",
      metadata: { created_by: "sandcastle", project: "x" },
    });
    expect(handle.sandboxId).toBe("sb_1");
    expect(handle.worktreePath).toBe(path.join(await realHome(), "workspace"));
    expect((await stat(handle.worktreePath)).isDirectory()).toBe(true);
  });

  it("keeps a caller's timeout and on_timeout", async () => {
    await start({ timeout_minutes: 15, on_timeout: "stop" });
    expect(world.created[0]).toMatchObject({ timeout_minutes: 15, on_timeout: "stop" });
    const handle = await start({ timeout_minutes: 15 });
    await handle.exec("true");
    expect(world.commands.at(-1)?.options?.timeout_s).toBe(900);
  });

  it("waits for a queued sandbox", async () => {
    world.createStatus = "queued";
    const handle = await start({ image: "python:3.12-slim" });
    expect(world.waited).toEqual(["sb_1"]);
    expect(handle.sandboxId).toBe("sb_1");
  });

  it("deletes the sandbox and names the reason when it fails to start", async () => {
    world.createStatus = "queued";
    world.waitStatus = "failed";
    await expect(start({ image: "nope:latest" })).rejects.toThrow(
      "groundbox(): sandbox sb_1 is failed: image could not be used",
    );
    expect(world.deleted).toEqual(["sb_1"]);
  });
});

describe("exec", () => {
  it("runs in the worktree and streams stdout line by line", async () => {
    const handle = await start();
    const lines: string[] = [];
    const result = await handle.exec('printf "a\\nb\\n"; sleep 0.2; printf "c"; pwd', {
      onLine: (line: string) => lines.push(line),
    });
    expect(lines).toEqual(["a", "b", `c${handle.worktreePath}`]);
    expect(result.exitCode).toBe(0);
    expect(result.stdout).toBe(`a\nb\nc${handle.worktreePath}\n`);
    expect(world.commands.at(-1)?.options).toMatchObject({ cwd: handle.worktreePath, resumable: true });
  });

  it("delivers lines while the command still runs", async () => {
    const handle = await start();
    let firstLineAt = 0;
    const startedAt = Date.now();
    await handle.exec("echo first; sleep 0.5; echo second", {
      onLine: (line: string) => {
        if (line === "first") firstLineAt = Date.now() - startedAt;
      },
    });
    const total = Date.now() - startedAt;
    expect(firstLineAt).toBeGreaterThan(0);
    expect(firstLineAt).toBeLessThan(total - 300);
  });

  it("returns a non-zero exit code and stderr instead of throwing", async () => {
    const handle = await start();
    const result = await handle.exec("echo out; echo oops >&2; exit 7");
    expect(result).toEqual({ stdout: "out\n", stderr: "oops\n", exitCode: 7 });
  });

  it("pipes stdin to the command and closes it", async () => {
    const handle = await start();
    const big = "x".repeat(300_000);
    const result = await handle.exec("wc -c", { stdin: big });
    expect(result.stdout.trim()).toBe("300000");
    expect(world.commands.at(-1)?.options).toMatchObject({ stdin: true, resumable: true });
    const empty = await handle.exec("wc -c", { stdin: "" });
    expect(empty.stdout.trim()).toBe("0");
  });

  it("honours cwd", async () => {
    const handle = await start();
    const result = await handle.exec("pwd", { cwd: scratch });
    expect(result.stdout.trim()).toBe(await realpathOf(scratch));
  });

  it("sees Sandcastle's env", async () => {
    const handle = await start();
    expect((await handle.exec("echo $FROM_SANDCASTLE")).stdout).toBe("yes\n");
  });

  it("wraps sudo commands in the sandbox's sudo", async () => {
    const handle = await start();
    await handle.exec("echo 'hi there'", { sudo: true });
    expect(world.commands.at(-1)?.cmd).toBe(String.raw`sudo -E -- sh -c 'echo '\''hi there'\'''`);
  });

  it("keeps only a bounded tail when onLine is given", async () => {
    const handle = await internals(groundbox({ client: fake.client, maxOutputTailChars: 10 })).create({ env: {} });
    const lines: string[] = [];
    const result = await handle.exec("seq 1 100", { onLine: (l: string) => lines.push(l) });
    expect(lines).toHaveLength(100);
    expect(result.stdout.length).toBeLessThanOrEqual(10);
    expect(result.stdout.endsWith("99\n100\n")).toBe(true);
  });
});

describe("files", () => {
  it("copies a file in, keeping its mode", async () => {
    const handle = await start();
    const host = path.join(scratch, "run.sh");
    await writeFile(host, "#!/bin/sh\necho hi\n", { mode: 0o755 });
    const target = path.join(handle.worktreePath, "bin", "run.sh");
    await handle.copyIn(host, target);
    expect(await readFile(target, "utf8")).toBe("#!/bin/sh\necho hi\n");
    expect((await stat(target)).mode & 0o777).toBe(0o755);
  });

  it("copies a directory in with nested files, symlinks and empty folders", async () => {
    const handle = await start();
    const src = path.join(scratch, "src");
    await mkdir(path.join(src, "a", "b"), { recursive: true });
    await mkdir(path.join(src, "empty", "deeper"), { recursive: true });
    await writeFile(path.join(src, "top.txt"), "top");
    await writeFile(path.join(src, "a", "b", "deep.sh"), "deep", { mode: 0o700 });
    await symlink("top.txt", path.join(src, "link"));
    const dest = path.join(handle.worktreePath, "copy");
    await handle.copyIn(src, dest);
    expect(await readFile(path.join(dest, "top.txt"), "utf8")).toBe("top");
    expect(await readFile(path.join(dest, "a", "b", "deep.sh"), "utf8")).toBe("deep");
    expect((await stat(path.join(dest, "a", "b", "deep.sh"))).mode & 0o777).toBe(0o700);
    expect((await lstat(path.join(dest, "link"))).isSymbolicLink()).toBe(true);
    expect(await readlink(path.join(dest, "link"))).toBe("top.txt");
    expect((await stat(path.join(dest, "empty", "deeper"))).isDirectory()).toBe(true);
    expect(world.uploads).toHaveLength(1);
  });

  it("copies a file out, making parent folders on the host", async () => {
    const handle = await start();
    await handle.exec("printf 'from sandbox' > out.txt");
    const host = path.join(scratch, "x", "y", "out.txt");
    await handle.copyFileOut(path.join(handle.worktreePath, "out.txt"), host);
    expect(await readFile(host, "utf8")).toBe("from sandbox");
  });
});

describe("interactiveExec", () => {
  it("pipes stdin, stdout and stderr and returns the exit code", async () => {
    const handle = await start();
    const stdin = new PassThrough();
    const stdout = new PassThrough();
    const stderr = new PassThrough();
    let out = "";
    let err = "";
    stdout.on("data", (c: Buffer) => (out += c.toString()));
    stderr.on("data", (c: Buffer) => (err += c.toString()));
    const done = handle.interactiveExec!(["sh", "-c", "read name; echo hello $name; echo bye >&2; exit 3"], {
      stdin,
      stdout,
      stderr,
    });
    stdin.write("world\n");
    stdin.end();
    expect(await done).toEqual({ exitCode: 3 });
    expect(out).toBe("hello world\n");
    expect(err).toBe("bye\n");
    expect(world.commands.at(-1)?.options).toMatchObject({ stdin: true, cwd: handle.worktreePath });
    expect(world.commands.at(-1)?.options).not.toHaveProperty("tty");
  });
});

describe("interactiveExec on a terminal", () => {
  it("asks for a terminal, sets raw mode and restores it, and never sends a 0x0 size", async () => {
    const handle = await start();
    const rawCalls: boolean[] = [];
    const stdin = Object.assign(new PassThrough(), {
      isTTY: true,
      isRaw: false,
      setRawMode: (mode: boolean) => rawCalls.push(mode),
    });
    const stdout = Object.assign(new PassThrough(), { isTTY: true, rows: 0, columns: 0 });
    const done = handle.interactiveExec!(["true"], { stdin, stdout, stderr: new PassThrough() });
    stdin.end();
    expect(await done).toEqual({ exitCode: 0 });
    expect(world.commands.at(-1)?.options).toMatchObject({ tty: { rows: 24, cols: 80 } });
    expect(rawCalls).toEqual([true, false]);
  });
});

describe("close", () => {
  it("deletes the sandbox", async () => {
    const handle = await start();
    await handle.close();
    expect(world.deleted).toEqual(["sb_1"]);
  });

  it("ignores a sandbox that is already gone", async () => {
    const handle = await start();
    world.deleteError = Object.assign(new Error("gone"), { code: "not_found", status: 404 });
    await expect(handle.close()).resolves.toBeUndefined();
  });

  it("surfaces other delete errors", async () => {
    const handle = await start();
    world.deleteError = Object.assign(new Error("boom"), { code: "internal", status: 500 });
    await expect(handle.close()).rejects.toThrow("boom");
  });
});

describe("Sandcastle run() end to end on the fake", () => {
  it("syncs the repo in, runs the agent and brings its commit back", async () => {
    const repo = makeHostRepo();
    try {
      const result = await run({
        agent: scriptedAgent(),
        sandbox: groundbox({ client: fake.client }),
        cwd: repo,
        prompt: "hello from the prompt",
        branchStrategy: { type: "branch", branch: "agent/test" },
        logging: { type: "file", path: path.join(scratch, "run.log") },
      });
      expect(result.commits).toHaveLength(1);
      expect(hostGit(repo, "show", "agent/test:agent-note.txt")).toBe("hello from the prompt");
      expect(world.deleted).toEqual(["sb_1"]);
    } finally {
      await rm(repo, { recursive: true, force: true });
    }
  });
});

const realpathOf = async (p: string): Promise<string> => (await import("node:fs/promises")).realpath(p);
const realHome = async (): Promise<string> => realpathOf(world.home);
