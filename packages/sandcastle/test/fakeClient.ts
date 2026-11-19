/**
 * A fake Groundbox client for unit tests. "Sandboxes" are folders on this
 * machine: commands run in a real local `sh` whose HOME is a temp folder, and
 * the file calls read and write real paths. Output streams back as it comes.
 */

import { spawn } from "node:child_process";
import { chmod, mkdir, mkdtemp, readFile, realpath, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import type { GroundboxClient } from "../src/client.js";

type Client = GroundboxClient;
type CreateSpec = Parameters<Client["create_sandbox"]>[0];
type StreamOptions = Parameters<Client["stream_command"]>[2];

export interface FakeCommand {
  readonly cmd: string | readonly string[];
  readonly options: StreamOptions;
}

export interface FakeWorld {
  home: string;
  /** Status `create_sandbox` answers with. */
  createStatus: string;
  /** Status `wait_for_sandbox` answers with. */
  waitStatus: string;
  /** Make `delete_sandbox` throw this. */
  deleteError?: unknown;
  readonly created: CreateSpec[];
  readonly deleted: string[];
  readonly waited: string[];
  readonly commands: FakeCommand[];
  readonly uploads: { dir: string; paths: string[] }[];
}

const SUDO = "sudo -E -- sh -c ";

/** Undo the provider's `sh -c '...'` quoting, to run the inner command locally. */
const unquote = (s: string): string =>
  s.slice(1, -1).replaceAll(String.raw`'\''`, "'");

export const makeFake = async (): Promise<{ client: Client; world: FakeWorld; cleanup: () => Promise<void> }> => {
  const home = await realpath(await mkdtemp(join(tmpdir(), "gbx-fake-home-")));
  const world: FakeWorld = {
    home,
    createStatus: "running",
    waitStatus: "running",
    created: [],
    deleted: [],
    waited: [],
    commands: [],
    uploads: [],
  };
  let next = 1;
  const sandbox = (id: string, status: string, error: string | null = null) =>
    ({ id, status, error }) as unknown as Awaited<ReturnType<Client["create_sandbox"]>>;

  const client: Client = {
    create_sandbox: async (spec) => {
      world.created.push(spec);
      return sandbox(`sb_${next++}`, world.createStatus);
    },
    wait_for_sandbox: async (id) => {
      world.waited.push(id);
      return sandbox(id, world.waitStatus, world.waitStatus === "failed" ? "image could not be used" : null);
    },
    delete_sandbox: async (id) => {
      world.deleted.push(id);
      if (world.deleteError !== undefined) {
        throw world.deleteError;
      }
      return sandbox(id, "shut_down");
    },
    stream_command: (_id, cmd, options, handlers) => {
      world.commands.push({ cmd, options });
      let program: string;
      let args: string[];
      if (typeof cmd === "string") {
        const inner = cmd.startsWith(SUDO) ? unquote(cmd.slice(SUDO.length)) : cmd;
        program = "sh";
        args = ["-c", inner];
      } else {
        program = cmd[0] ?? "true";
        args = cmd.slice(1);
      }
      const created = world.created.at(-1);
      const child = spawn(program, args, {
        cwd: options?.cwd,
        env: { ...process.env, ...(created?.env ?? {}), ...(options?.env ?? {}), HOME: home },
        stdio: ["pipe", "pipe", "pipe"],
      });
      child.stdout.setEncoding("utf8");
      child.stderr.setEncoding("utf8");
      child.stdout.on("data", (t: string) => handlers?.onStdout?.(t));
      child.stderr.on("data", (t: string) => handlers?.onStderr?.(t));
      if (options?.stdin !== true) {
        child.stdin.end();
      }
      const started = Date.now();
      const done = new Promise<{ exit_code: number; duration_ms: number; timed_out: boolean }>((resolve, reject) => {
        child.on("error", reject);
        child.on("close", (code) => {
          resolve({ exit_code: code ?? 1, duration_ms: Date.now() - started, timed_out: false });
        });
      });
      return {
        write: (data: string | Uint8Array) => {
          child.stdin.write(data);
        },
        resize: () => undefined,
        end: () => {
          child.stdin.end();
        },
        kill: () => {
          child.kill("SIGKILL");
        },
        detach: () => undefined,
        exec_id: undefined,
        done,
      };
    },
    write_file: async (_id, path, content) => {
      await mkdir(dirname(path), { recursive: true });
      await writeFile(path, content);
      return { path, bytes: content.length } as unknown as Awaited<ReturnType<Client["write_file"]>>;
    },
    read_file: async (_id, path) => new Uint8Array(await readFile(path)),
    upload_files: async (_id, dir, files) => {
      world.uploads.push({ dir, paths: files.map((f) => f.path) });
      for (const file of files) {
        const target = join(dir, file.path);
        await mkdir(dirname(target), { recursive: true });
        if (file.symlink !== undefined) {
          await symlink(file.symlink, target);
        } else {
          await writeFile(target, file.content ?? "");
          await chmod(target, file.mode ?? 0o644);
        }
      }
      return { written: files.length, removed: 0, bytes: 0 };
    },
  };
  return {
    client,
    world,
    cleanup: () => rm(home, { recursive: true, force: true }),
  };
};
