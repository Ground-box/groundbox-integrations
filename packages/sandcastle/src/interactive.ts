/**
 * The handle's `interactiveExec`, for Sandcastle's `interactive()`: the agent
 * runs on a terminal in the sandbox when the host's stdin is a TTY, with
 * keystrokes, output and window size passed through.
 */

import type { InteractiveExecOptions } from "@ai-hero/sandcastle";
import type { GroundboxClient } from "./client.js";

type TtyIn = NodeJS.ReadableStream & {
  isTTY?: boolean;
  isRaw?: boolean;
  setRawMode?: (mode: boolean) => unknown;
};
type TtyOut = NodeJS.WritableStream & {
  isTTY?: boolean;
  rows?: number;
  columns?: number;
};

const positive = (n: number | undefined): number | undefined =>
  n !== undefined && n > 0 ? n : undefined;

export interface InteractiveContext {
  readonly client: GroundboxClient;
  readonly sandboxId: string;
  readonly worktreePath: string;
  readonly timeoutSeconds: number;
}

export const makeInteractiveExec =
  (ctx: InteractiveContext) =>
  async (
    args: string[],
    opts: InteractiveExecOptions,
  ): Promise<{ exitCode: number }> => {
    if (args.length === 0) {
      throw new Error("interactiveExec needs a command to run");
    }
    const stdin = opts.stdin as TtyIn;
    const stdout = opts.stdout as TtyOut;
    const tty = stdin.isTTY === true;
    // A terminal can report 0x0 (under `script`, for one): fall back to 24x80.
    const size = {
      rows: positive(stdout.rows) ?? 24,
      cols: positive(stdout.columns) ?? 80,
    };

    const stream = ctx.client.stream_command(
      ctx.sandboxId,
      args,
      {
        cwd: opts.cwd ?? ctx.worktreePath,
        timeout_s: ctx.timeoutSeconds,
        stdin: true,
        resumable: true,
        ...(tty ? { tty: size } : {}),
      },
      {
        onStdout: (text) => {
          opts.stdout.write(text);
        },
        onStderr: (text) => {
          opts.stderr.write(text);
        },
      },
    );

    const onData = (chunk: Buffer | string): void => {
      stream.write(typeof chunk === "string" ? chunk : new Uint8Array(chunk));
    };
    const onEnd = (): void => {
      stream.end();
    };
    const onResize = (): void => {
      stream.resize(positive(stdout.rows) ?? size.rows, positive(stdout.columns) ?? size.cols);
    };
    const wasRaw = stdin.isRaw === true;
    if (tty) {
      stdin.setRawMode?.(true);
      stdout.on?.("resize", onResize);
    }
    stdin.on("data", onData);
    stdin.on("end", onEnd);
    stdin.resume();

    try {
      const done = await stream.done;
      return { exitCode: done.exit_code };
    } finally {
      stdin.off("data", onData);
      stdin.off("end", onEnd);
      if (tty) {
        stdin.setRawMode?.(wasRaw);
        stdout.off?.("resize", onResize);
      }
      stdin.pause();
    }
  };
