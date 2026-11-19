/**
 * The handle's `exec`: each command runs over a Groundbox exec stream, so
 * stdout reaches `onLine` line by line while the command runs. `stdin` is
 * written to the command and then closed.
 */

import type { ExecResult } from "@ai-hero/sandcastle";
import { shellEscape, type GroundboxClient } from "./client.js";

/** Default characters of output kept per stream when `onLine` is given. */
export const MAX_TAIL_CHARS = 64 * 1024;

/** The options Sandcastle passes to `exec`. */
export interface ExecOptions {
  onLine?: (line: string) => void;
  cwd?: string;
  sudo?: boolean;
  stdin?: string;
}

/** A rolling tail of text, at most `max` characters long. */
export class Tail {
  private text = "";
  constructor(private readonly max: number) {}
  push(chunk: string): void {
    this.text += chunk;
    if (this.text.length > this.max) {
      this.text = this.text.slice(this.text.length - this.max);
    }
  }
  toString(): string {
    return this.text;
  }
}

/** Split streamed stdout into lines for `onLine`. */
export class LineSplitter {
  private partial = "";
  constructor(private readonly onLine: (line: string) => void) {}
  push(chunk: string): void {
    const lines = (this.partial + chunk).split("\n");
    this.partial = lines.pop() ?? "";
    for (const line of lines) {
      this.onLine(line.endsWith("\r") ? line.slice(0, -1) : line);
    }
  }
  flush(): void {
    if (this.partial !== "") {
      const last = this.partial;
      this.partial = "";
      this.onLine(last);
    }
  }
}

/** What `exec` needs from the provider. */
export interface ExecContext {
  readonly client: GroundboxClient;
  readonly sandboxId: string;
  readonly worktreePath: string;
  /** True when commands already run as root (a sandbox from a Docker image). */
  readonly isRoot: boolean;
  /** Seconds a command may run; the sandbox's own lifetime by default. */
  readonly timeoutSeconds: number;
  readonly maxTail: number;
}

/** Wrap a command so it runs as root through the sandbox's `sudo`. */
export const asRoot = (command: string): string =>
  `sudo -E -- sh -c ${shellEscape(command)}`;

/** The handle's `exec`, bound to one sandbox and its worktree. */
export const makeExec =
  (ctx: ExecContext) =>
  async (command: string, opts?: ExecOptions): Promise<ExecResult> => {
    const onLine = opts?.onLine;
    // Without onLine the whole output is returned, like the SDK's own run_command.
    const limit = onLine === undefined ? Number.POSITIVE_INFINITY : ctx.maxTail;
    const stdout = new Tail(limit);
    const stderr = new Tail(limit);
    const lines = onLine === undefined ? undefined : new LineSplitter(onLine);
    const withStdin = opts?.stdin !== undefined;

    const stream = ctx.client.stream_command(
      ctx.sandboxId,
      opts?.sudo === true && !ctx.isRoot ? asRoot(command) : command,
      {
        cwd: opts?.cwd ?? ctx.worktreePath,
        timeout_s: ctx.timeoutSeconds,
        // Resumable: the command keeps running through a dropped connection.
        resumable: true,
        ...(withStdin ? { stdin: true } : {}),
      },
      {
        onStdout: (text) => {
          stdout.push(text);
          lines?.push(text);
        },
        onStderr: (text) => {
          stderr.push(text);
        },
      },
    );
    const input = opts?.stdin;
    if (input !== undefined) {
      if (input !== "") {
        stream.write(input);
      }
      stream.end();
    }
    const done = await stream.done;
    lines?.flush();
    // A command the API stopped at its timeout reports non-zero.
    const exitCode =
      done.timed_out && done.exit_code === 0 ? 124 : done.exit_code;
    return {
      stdout: stdout.toString(),
      stderr: stderr.toString(),
      exitCode,
    };
  };
