/**
 * Live tests against the real Groundbox API. They run only when
 * GROUNDBOX_API_KEY is set, and delete every sandbox they start.
 */

import { mkdir, mkdtemp, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { PassThrough } from "node:stream";
import { createSandbox, run } from "@ai-hero/sandcastle";
import { Groundbox } from "@groundbox/sdk";
import { afterAll, describe, expect, it } from "vitest";
import { groundbox } from "../src/index.js";
import { internals } from "./internals.js";
import { hostGit, makeHostRepo, scriptedAgent } from "./scriptedAgent.js";

const live = process.env.GROUNDBOX_API_KEY ? describe : describe.skip;

/** Whether the sandbox is gone or on its way out. */
const isGone = async (id: string): Promise<boolean> => {
  try {
    const sb = await new Groundbox().get_sandbox(id);
    return ["shutting_down", "shut_down", "stopped", "stopping"].includes(sb.status);
  } catch {
    return true;
  }
};

live("groundbox() against Groundbox production", () => {
  const cleanup: string[] = [];
  afterAll(async () => {
    for (const dir of cleanup) await rm(dir, { recursive: true, force: true });
  });

  it("runs the whole handle contract in a real sandbox", async () => {
    const scratch = await mkdtemp(path.join(tmpdir(), "gbx-live-"));
    cleanup.push(scratch);
    const handle = await internals(groundbox({
      cpus: 2,
      timeout_minutes: 15,
      metadata: { purpose: "sandcastle-live-test" },
      env: { PROVIDER_VAR: "from-provider" },
    })).create({ env: { PROVIDER_VAR: "from-provider", RUN_VAR: "from-run" } });
    try {
      expect(handle.worktreePath).toBe("/home/runner/workspace");
      expect(handle.sandboxId).toMatch(/\S/);

      const lines: string[] = [];
      const streamed = await handle.exec("echo one; sleep 1; echo two; pwd; echo $RUN_VAR", {
        onLine: (l: string) => lines.push(l),
      });
      expect(streamed.exitCode).toBe(0);
      expect(lines).toEqual(["one", "two", "/home/runner/workspace", "from-run"]);

      const failed = await handle.exec("echo bad >&2; exit 5");
      expect(failed).toMatchObject({ exitCode: 5, stderr: "bad\n" });

      const big = "y".repeat(200_000);
      expect((await handle.exec("wc -c", { stdin: big })).stdout.trim()).toBe("200000");

      const root = await handle.exec("id -u; whoami", { sudo: true });
      expect(root.stdout).toBe("0\nroot\n");
      expect((await handle.exec("id -un")).stdout).toBe("runner\n");

      // copyIn: one file, then a directory with a symlink and an empty folder
      const file = path.join(scratch, "tool.sh");
      await writeFile(file, "#!/bin/sh\necho tool ran\n", { mode: 0o755 });
      await handle.copyIn(file, "/tmp/in/tool.sh");
      expect((await handle.exec("/tmp/in/tool.sh")).stdout).toBe("tool ran\n");

      const dir = path.join(scratch, "dir");
      await mkdir(path.join(dir, "nested", "deep"), { recursive: true });
      await mkdir(path.join(dir, "empty"), { recursive: true });
      await writeFile(path.join(dir, "nested", "deep", "a.txt"), "A");
      await symlink("nested/deep/a.txt", path.join(dir, "link"));
      await handle.copyIn(dir, `${handle.worktreePath}/dir`);
      const listing = await handle.exec(
        "cat dir/link; echo; test -d dir/empty && echo empty-ok; readlink dir/link; stat -c %U dir/nested/deep/a.txt",
      );
      expect(listing.stdout).toBe("A\nempty-ok\nnested/deep/a.txt\nrunner\n");

      // a large file, as a git bundle of a real repo would be
      const large = path.join(scratch, "large.bin");
      await writeFile(large, Buffer.alloc(30 * 1024 * 1024, 7));
      await handle.copyIn(large, "/tmp/large.bin");
      expect((await handle.exec("stat -c %s /tmp/large.bin")).stdout.trim()).toBe(String(30 * 1024 * 1024));

      await handle.exec("printf 'out of the box' > result.txt");
      const out = path.join(scratch, "back", "result.txt");
      await handle.copyFileOut(`${handle.worktreePath}/result.txt`, out);
      expect(await readFile(out, "utf8")).toBe("out of the box");

      const stdin = new PassThrough();
      const stdout = new PassThrough();
      const stderr = new PassThrough();
      let printed = "";
      stdout.on("data", (c: Buffer) => (printed += c.toString()));
      const done = handle.interactiveExec!(["sh", "-c", "read x; echo got $x; pwd"], { stdin, stdout, stderr });
      stdin.end("hello\n");
      expect(await done).toEqual({ exitCode: 0 });
      expect(printed).toBe("got hello\n/home/runner/workspace\n");
    } finally {
      await handle.close();
    }
    expect(await isGone(handle.sandboxId!)).toBe(true);
  }, 300_000);

  it("runs Sandcastle's run() end to end and brings the agent's commit back", async () => {
    const repo = makeHostRepo();
    cleanup.push(repo);
    const result = await run({
      agent: scriptedAgent(),
      sandbox: groundbox({ timeout_minutes: 15, metadata: { purpose: "sandcastle-live-test" } }),
      cwd: repo,
      prompt: "written by an agent in a Groundbox sandbox",
      branchStrategy: { type: "branch", branch: "agent/live" },
      hooks: {
        sandbox: {
          onSandboxReady: [
            { command: "git --version" },
            { command: "apt-get --version >/dev/null && whoami", sudo: true },
          ],
        },
      },
      logging: { type: "file", path: path.join(repo, ".git", "sandcastle-run.log") },
    });
    expect(result.commits).toHaveLength(1);
    expect(hostGit(repo, "show", "agent/live:agent-note.txt")).toBe(
      "written by an agent in a Groundbox sandbox",
    );
    const log = await readFile(path.join(repo, ".git", "sandcastle-run.log"), "utf8");
    expect(log).toContain("running on Linux as runner");
  }, 300_000);

  it("keeps one sandbox for several runs with createSandbox()", async () => {
    const repo = makeHostRepo();
    cleanup.push(repo);
    let id: string | undefined;
    {
      await using sandbox = await createSandbox({
        branch: "agent/reuse",
        cwd: repo,
        sandbox: groundbox({ timeout_minutes: 15, metadata: { purpose: "sandcastle-live-test" } }),
      });
      id = sandbox.sandboxId;
      await sandbox.exec("echo warm > /tmp/marker");
      const first = await sandbox.run({ agent: scriptedAgent(), prompt: "first" });
      expect(first.commits).toHaveLength(1);
      expect((await sandbox.exec("cat /tmp/marker")).stdout.trim()).toBe("warm");
    }
    expect(id).toBeTruthy();
    expect(await isGone(id!)).toBe(true);
    expect(hostGit(repo, "show", "agent/reuse:agent-note.txt")).toBe("first");
  }, 300_000);

  it("runs from a Docker image, as root", async () => {
    const repo = makeHostRepo();
    cleanup.push(repo);
    const provider = groundbox({
      image: "node:22-bookworm",
      timeout_minutes: 20,
      metadata: { purpose: "sandcastle-live-test" },
    });
    expect(provider.env).toMatchObject({ IS_SANDBOX: "1" });
    const result = await run({
      agent: scriptedAgent(),
      sandbox: provider,
      cwd: repo,
      prompt: "from an image",
      branchStrategy: { type: "branch", branch: "agent/image" },
      hooks: { sandbox: { onSandboxReady: [{ command: "test $(id -u) = 0", sudo: true }] } },
      logging: { type: "file", path: path.join(repo, ".git", "sandcastle-image.log") },
    });
    expect(result.commits).toHaveLength(1);
    expect(hostGit(repo, "show", "agent/image:agent-note.txt")).toBe("from an image");
    const log = await readFile(path.join(repo, ".git", "sandcastle-image.log"), "utf8");
    expect(log).toContain("running on Linux as root");
  }, 900_000);

  it("left no sandbox running", async () => {
    const { data } = await new Groundbox().list_sandboxes({
      live: true,
      metadata: { purpose: "sandcastle-live-test" },
    });
    const running = data.filter((sb) => !["shutting_down", "shut_down"].includes(sb.status));
    expect(running.map((sb) => `${sb.id} ${sb.status}`)).toEqual([]);
  });
});
