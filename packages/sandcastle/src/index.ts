/**
 * Groundbox sandbox provider for Sandcastle (`@ai-hero/sandcastle`).
 *
 * `groundbox()` is an isolated provider: each sandbox is a Groundbox
 * Firecracker microVM. Sandcastle copies the repo in as a git bundle, the
 * agent works in `$HOME/workspace`, and its commits come back out.
 */

import {
  createIsolatedSandboxProvider,
  type ExecResult,
  type IsolatedSandboxHandle,
  type IsolatedSandboxProvider,
} from "@ai-hero/sandcastle";
import { Groundbox, type CreateSandboxRequest } from "@groundbox/sdk";
import { isNotFound, type GroundboxClient } from "./client.js";
import { MAX_TAIL_CHARS, makeExec } from "./exec.js";
import { makeCopyFileOut, makeCopyIn } from "./files.js";
import { makeInteractiveExec } from "./interactive.js";

export type { GroundboxClient } from "./client.js";

/**
 * Options for the Groundbox sandbox provider.
 *
 * Every `create_sandbox` option of `@groundbox/sdk` passes through (`cpus`,
 * `memory_gb`, `disk_gb`, `template`, `image`, `snapshot_id`, `allow_hosts`,
 * `secrets`, `metadata`, `timeout_minutes`, ...), except `env`: use the `env`
 * option below, which Sandcastle merges with its own.
 */
export interface GroundboxOptions extends Omit<CreateSandboxRequest, "env"> {
  /** A `gbx_...` key. Defaults to the `GROUNDBOX_API_KEY` environment variable. */
  readonly apiKey?: string;
  /** The API's base URL, ending in /v1. Defaults to `GROUNDBOX_API_URL`, then https://api.groundbox.dev/v1. */
  readonly baseUrl?: string;
  /** Environment variables injected by this provider. Merged at launch with Sandcastle's env resolver and the agent's env. */
  readonly env?: Record<string, string>;
  /**
   * Maximum characters of streamed `exec` output kept per stream for the
   * returned `ExecResult` when `onLine` is supplied (default: 64KiB). `onLine`
   * still gets every line.
   */
  readonly maxOutputTailChars?: number;
  /**
   * Seconds one command may run before Groundbox ends it. Defaults to the
   * sandbox's whole lifetime (`timeout_minutes`).
   */
  readonly commandTimeoutSeconds?: number;
  /** A client to use instead of `new Groundbox(...)`, for tests. */
  readonly client?: GroundboxClient;
}

/** One hour: the trial's limit, and room for setup, a run and syncing back. */
const DEFAULT_TIMEOUT_MINUTES = 60;

/** States a new sandbox passes through on its way to `running`. */
const COMING_UP = new Set(["queued", "starting", "resuming"]);

/**
 * Create a Groundbox sandbox provider for `run()`, `createSandbox()` and
 * `interactive()`.
 *
 * Each sandbox is new: `create()` starts a Groundbox sandbox and `close()`
 * deletes it. It is also deleted by Groundbox when `timeout_minutes`
 * (default 60) runs out, so a crashed host never leaves one running.
 *
 * The default image is Ubuntu 24.04 with git, Node and npm, running as the
 * user `runner` with passwordless `sudo`. It has no agent CLI: install one in
 * a `sandbox.onSandboxReady` hook, or start from a template that has it.
 */
export const groundbox = (options?: GroundboxOptions): IsolatedSandboxProvider => {
  const {
    apiKey,
    baseUrl,
    env,
    maxOutputTailChars = MAX_TAIL_CHARS,
    commandTimeoutSeconds,
    client: givenClient,
    ...spec
  } = options ?? {};
  const timeoutMinutes = spec.timeout_minutes ?? DEFAULT_TIMEOUT_MINUTES;
  const timeoutSeconds = commandTimeoutSeconds ?? timeoutMinutes * 60;

  return createIsolatedSandboxProvider({
    name: "groundbox",
    // From a Docker image commands run as root, where Claude Code refuses
    // --dangerously-skip-permissions unless it knows it is in a sandbox.
    env: spec.image === undefined ? env : { IS_SANDBOX: "1", ...env },
    create: async (createOptions): Promise<IsolatedSandboxHandle> => {
      const client: GroundboxClient =
        givenClient ??
        new Groundbox({
          // An empty key counts as unset, so the SDK reads GROUNDBOX_API_KEY
          ...(apiKey === undefined || apiKey === "" ? {} : { apiKey }),
          ...(baseUrl === undefined ? {} : { baseUrl }),
        });

      let sandbox = await client.create_sandbox({
        ...spec,
        timeout_minutes: timeoutMinutes,
        on_timeout: spec.on_timeout ?? "delete",
        metadata: { created_by: "sandcastle", ...spec.metadata },
        env: createOptions.env,
      });
      const sandboxId = sandbox.id;

      const bootstrap = makeExec({
        client,
        sandboxId,
        worktreePath: "/",
        isRoot: true,
        timeoutSeconds: 300,
        maxTail: maxOutputTailChars,
      });

      let worktreePath: string;
      let isRoot: boolean;
      try {
        if (COMING_UP.has(sandbox.status)) {
          sandbox = await client.wait_for_sandbox(sandboxId);
        }
        if (sandbox.status !== "running") {
          const why = sandbox.error ? `: ${String(sandbox.error)}` : "";
          throw new Error(`sandbox ${sandboxId} is ${sandbox.status}${why}`);
        }
        const prepared = await bootstrap(
          'mkdir -p "$HOME/workspace" && cd "$HOME/workspace" && pwd && id -u',
        );
        if (prepared.exitCode !== 0) {
          throw new Error(
            `could not create $HOME/workspace: ${prepared.stderr.trim() || `exit ${prepared.exitCode}`}`,
          );
        }
        const [path, uid] = prepared.stdout.trim().split("\n");
        if (path === undefined || path === "") {
          throw new Error("could not read the path of $HOME/workspace");
        }
        worktreePath = path.trim();
        isRoot = uid?.trim() === "0";
      } catch (error) {
        // No handle exists yet, so nothing else could delete it.
        await client.delete_sandbox(sandboxId).catch(() => undefined);
        const message = error instanceof Error ? error.message : String(error);
        throw new Error(`groundbox(): ${message}`, { cause: error });
      }

      const exec = makeExec({
        client,
        sandboxId,
        worktreePath,
        isRoot,
        timeoutSeconds,
        maxTail: maxOutputTailChars,
      });
      const runChecked = async (command: string): Promise<void> => {
        const result: ExecResult = await exec(command, { cwd: "/" });
        if (result.exitCode !== 0) {
          throw new Error(
            `${command} exited with ${result.exitCode}: ${result.stderr.trim()}`,
          );
        }
      };

      return {
        worktreePath,
        sandboxId,
        exec,
        interactiveExec: makeInteractiveExec({
          client,
          sandboxId,
          worktreePath,
          timeoutSeconds,
        }),
        copyIn: makeCopyIn(client, sandboxId, runChecked),
        copyFileOut: makeCopyFileOut(client, sandboxId),
        close: async () => {
          try {
            await client.delete_sandbox(sandboxId);
          } catch (error) {
            if (!isNotFound(error)) {
              throw error;
            }
          }
        },
      };
    },
  });
};
