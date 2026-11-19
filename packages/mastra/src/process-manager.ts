/**
 * Groundbox process manager.
 *
 * Every spawn is a Groundbox background command (`POST /sandboxes/{id}/commands`), so it runs on
 * in the sandbox with nobody connected and has an id (`ex_...`) at once. Its output is followed
 * over the exec stream (`attach_command`), which reconnects by itself and reads on from where it
 * was, so a dropped connection loses no output. Kill, stdin and close-stdin go through the same
 * command id. A command started in another process can be picked up again with `get(id)`.
 */
import type { CommandStream, Groundbox, SandboxCommand } from '@groundbox/sdk';
import { ProcessHandle, SandboxProcessManager } from '@mastra/core/workspace';
import type { CommandResult, ProcessInfo, SpawnProcessOptions } from '@mastra/core/workspace';

import type { GroundboxSandbox } from './sandbox.js';
import { definedEnv, errorCode, sleep, toSeconds } from './utils.js';

/** Exit code Groundbox reports for a command it killed (SIGKILL), also on a timeout. */
const KILLED_EXIT_CODE = 137;
/**
 * A spawn with no timeout (a dev server, a watcher) gets the longest the API allows, 30 days, so
 * it runs for as long as the sandbox does instead of stopping at the API's default.
 */
const NO_TIMEOUT_S = 2_592_000;
/** How often a command is read when its output stream failed and only its state is left. */
const POLL_MS = 1000;

export interface GroundboxProcessManagerOptions {
  /** Default timeout in milliseconds for commands that don't specify one. Unset: no timeout (the sandbox's lifetime). */
  defaultTimeout?: number;
}

type Ended = { exit_code: number; duration_ms: number; timed_out: boolean };

/** A Groundbox command, as Mastra's ProcessHandle. */
export class GroundboxProcessHandle extends ProcessHandle {
  readonly pid: string;
  private _exitCode: number | undefined;
  private _timedOut = false;
  private _killed = false;
  private _detached = false;
  private _waitPromise: Promise<CommandResult> | undefined;
  private readonly _ended: Promise<Ended>;

  constructor(
    pid: string,
    private readonly client: Groundbox,
    private readonly sandboxId: string,
    private readonly stream: CommandStream | undefined,
    private readonly startTime: number,
    private readonly stdinOpen: boolean,
    options?: SpawnProcessOptions,
    ended?: Ended,
  ) {
    super(options);
    this.pid = pid;
    if (ended) {
      this._ended = Promise.resolve(ended);
      this.settle(ended);
    } else if (stream) {
      this._ended = stream.done.then(
        (result) => {
          this.settle(result);
          return result;
        },
        (error: unknown) => {
          if (this._detached) throw error;
          return this.pollUntilEnded();
        },
      );
    } else {
      this._ended = this.pollUntilEnded();
    }
    // Never leave an unhandled rejection behind when nobody calls wait().
    this._ended.catch(() => undefined);
  }

  get exitCode(): number | undefined {
    return this._exitCode;
  }

  private settle(result: Ended): void {
    if (this._exitCode !== undefined) return;
    this._exitCode = result.exit_code;
    this._timedOut = result.timed_out;
  }

  /** The output stream failed for good: read the command's state until it ends. */
  private async pollUntilEnded(): Promise<Ended> {
    for (;;) {
      let command: SandboxCommand;
      try {
        command = await this.client.get_command(this.sandboxId, this.pid);
      } catch (error) {
        const code = errorCode(error);
        if (code === 'not_found' || code === 'conflict') {
          const ended = { exit_code: this._killed ? KILLED_EXIT_CODE : 1, duration_ms: Date.now() - this.startTime, timed_out: false };
          this.settle(ended);
          return ended;
        }
        throw error;
      }
      if (command.state !== 'running') {
        const ended = {
          exit_code: command.exit_code ?? (command.state === 'lost' ? KILLED_EXIT_CODE : 1),
          duration_ms: command.duration_ms ?? Date.now() - this.startTime,
          timed_out: command.timed_out,
        };
        this.settle(ended);
        return ended;
      }
      await sleep(POLL_MS);
    }
  }

  async wait(): Promise<CommandResult> {
    if (!this._waitPromise) this._waitPromise = this.doWait();
    return this._waitPromise;
  }

  private async doWait(): Promise<CommandResult> {
    const ended = await this._ended;
    // Output that arrived with the exit message has been emitted by now.
    const exitCode = this._exitCode ?? ended.exit_code;
    const timedOut = this._timedOut;
    const result: CommandResult = {
      success: exitCode === 0 && !timedOut,
      exitCode,
      stdout: this.stdout,
      stderr: this.stderr,
      executionTimeMs: Date.now() - this.startTime,
      ...(timedOut && { timedOut: true }),
      ...((this._killed || timedOut) && { killed: true }),
      ...(this.stdoutTruncated && { stdoutTruncated: true, stdoutDroppedBytes: this.stdoutDroppedBytes }),
      ...(this.stderrTruncated && { stderrTruncated: true, stderrDroppedBytes: this.stderrDroppedBytes }),
    };
    return result;
  }

  async kill(): Promise<boolean> {
    if (this._exitCode !== undefined) return false;
    this._killed = true;
    try {
      const command = await this.client.kill_command(this.sandboxId, this.pid);
      if (command.state !== 'running' && command.exit_code !== null) {
        this.settle({ exit_code: command.exit_code, duration_ms: command.duration_ms ?? 0, timed_out: command.timed_out });
      }
    } catch (error) {
      const code = errorCode(error);
      if (code === 'not_found' || code === 'conflict') return false;
      throw error;
    }
    return true;
  }

  async sendStdin(data: string): Promise<void> {
    if (this._exitCode !== undefined) throw new Error(`Process ${this.pid} has already exited with code ${this._exitCode}`);
    if (!this.stdinOpen || !this.stream) throw new Error(`Process ${this.pid} was started without stdin (stdinMode: 'ignore')`);
    this.stream.write(data);
  }

  async closeStdin(): Promise<void> {
    if (this._exitCode !== undefined) return;
    if (!this.stdinOpen || !this.stream) throw new Error(`Process ${this.pid} was started without stdin (stdinMode: 'ignore')`);
    this.stream.end();
  }

  /** Stop following the output; the command runs on in the sandbox. */
  detach(): void {
    if (this._exitCode !== undefined || this._detached) return;
    this._detached = true;
    this.stream?.detach();
  }
}

/**
 * Mastra process manager over Groundbox background commands.
 */
export class GroundboxProcessManager extends SandboxProcessManager<GroundboxSandbox> {
  private readonly defaultTimeout: number | undefined;

  constructor(options: GroundboxProcessManagerOptions = {}) {
    super();
    this.defaultTimeout = options.defaultTimeout;
  }

  async spawn(command: string, options: SpawnProcessOptions = {}): Promise<ProcessHandle> {
    const timeout = options.timeout ?? this.defaultTimeout;
    const cwd = options.cwd ?? this.sandbox.workingDirectory;
    const env = definedEnv(options.env);
    const stdinOpen = options.stdinMode !== 'ignore';
    return this.sandbox.retryOnDead(async () => {
      const client = this.sandbox.groundbox;
      const sandboxId = this.sandbox.requireSandboxId();
      const startTime = Date.now();
      const started = await client.start_command(sandboxId, command, {
        ...(cwd !== undefined && { cwd }),
        ...(Object.keys(env).length > 0 && { env }),
        timeout_s: timeout !== undefined ? Math.min(toSeconds(timeout), NO_TIMEOUT_S) : NO_TIMEOUT_S,
        stdin: stdinOpen,
      });
      let handle: GroundboxProcessHandle | undefined;
      const early: Array<['stdout' | 'stderr', string]> = [];
      const stream = client.attach_command(sandboxId, started.id, { seq: 0 }, {
        onStdout: (text) => (handle ? handle.emitStdout(text) : early.push(['stdout', text])),
        onStderr: (text) => (handle ? handle.emitStderr(text) : early.push(['stderr', text])),
      });
      handle = new GroundboxProcessHandle(started.id, client, sandboxId, stream, startTime, stdinOpen, options);
      for (const [kind, text] of early.splice(0)) {
        if (kind === 'stdout') handle.emitStdout(text);
        else handle.emitStderr(text);
      }
      this._tracked.set(handle.pid, handle);
      return handle;
    });
  }

  async list(): Promise<ProcessInfo[]> {
    const result: ProcessInfo[] = [];
    for (const [pid, handle] of this._tracked) {
      result.push({
        pid,
        ...(handle.command !== undefined && { command: handle.command }),
        running: handle.exitCode === undefined,
        ...(handle.exitCode !== undefined && { exitCode: handle.exitCode }),
      });
    }
    return result;
  }

  /**
   * A tracked process, or a Groundbox command this manager did not start (by its `ex_...` id),
   * attached from the start of its kept output.
   */
  async get(pid: string, options?: { release?: boolean }): Promise<ProcessHandle | undefined> {
    const tracked = this._tracked.get(pid);
    if (tracked) return tracked;
    if (!pid.startsWith('ex_')) return undefined;
    const client = this.sandbox.groundbox;
    const sandboxId = this.sandbox.sandboxId;
    if (!sandboxId) return undefined;
    let command: SandboxCommand;
    try {
      command = await client.get_command(sandboxId, pid);
    } catch (error) {
      if (errorCode(error) === 'not_found') return undefined;
      throw error;
    }
    const startTime = command.started_at ? Date.parse(command.started_at) : Date.now();
    let handle: GroundboxProcessHandle | undefined;
    const early: Array<['stdout' | 'stderr', string]> = [];
    const stream = command.output_kept || command.state === 'running'
      ? client.attach_command(sandboxId, pid, { seq: 0 }, {
          onStdout: (text) => (handle ? handle.emitStdout(text) : early.push(['stdout', text])),
          onStderr: (text) => (handle ? handle.emitStderr(text) : early.push(['stderr', text])),
        })
      : undefined;
    handle = new GroundboxProcessHandle(pid, client, sandboxId, stream, startTime, command.stdin);
    for (const [kind, text] of early.splice(0)) {
      if (kind === 'stdout') handle.emitStdout(text);
      else handle.emitStderr(text);
    }
    const commandLine = command.cmd ?? command.args?.join(' ');
    if (commandLine) handle.command = commandLine;
    // The base class releases an exited process after this read unless `release: false`.
    void options;
    this._tracked.set(pid, handle);
    return handle;
  }

  /** Stop following every tracked process's output and forget them (the sandbox is going away). */
  detachAll(): void {
    for (const pid of [...this._tracked.keys()]) this.release(pid);
  }

  /** Release a process and stop following its output. */
  release(pid: string): void {
    const handle = this._tracked.get(pid);
    if (handle instanceof GroundboxProcessHandle) handle.detach();
    super.release(pid);
  }
}
