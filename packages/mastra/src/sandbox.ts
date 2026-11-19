/**
 * Groundbox sandbox provider for Mastra workspaces.
 *
 * A Groundbox sandbox is a Firecracker microVM with its own cores: Linux, a 20 GB disk by
 * default, user `runner` with passwordless sudo on Groundbox's own image (root on a Docker image
 * passed as `image`). This class plugs it into Mastra's `WorkspaceSandbox` interface.
 *
 * @see https://groundbox.dev/docs/sandboxes
 */
import { randomUUID } from 'node:crypto';

import { Groundbox } from '@groundbox/sdk';
import type { CreateSandboxRequest, Sandbox, Snapshot } from '@groundbox/sdk';
import type { RequestContext } from '@mastra/core/di';
import {
  MastraSandbox,
  SandboxAbortError,
  SandboxNotReadyError,
  validateSandboxFileMode,
} from '@mastra/core/workspace';
import type {
  CommandResult,
  ExecuteCommandOptions,
  MastraSandboxOptions,
  ProviderStatus,
  SandboxCloneOptions,
  SandboxComputer,
  SandboxFileInput,
  SandboxInfo,
  SandboxNetworking,
  WriteFilesOptions,
} from '@mastra/core/workspace';

import { createGroundboxComputer } from './computer.js';
import { GroundboxSandboxFilesystem } from './filesystem.js';
import { GroundboxProcessManager } from './process-manager.js';
import { definedEnv, errorCode, retainNewest, shellQuote, sleep, toAbsolute, toSeconds } from './utils.js';

const LOG_PREFIX = '[@groundbox/mastra]';

/** Metadata key that ties a Groundbox sandbox to this sandbox's logical id. */
export const MASTRA_ID_METADATA_KEY = 'mastra-sandbox-id';
/** Kept snapshots written by `snapshot()` are labelled `mastra:<checkpoint name>`. */
const CHECKPOINT_LABEL_PREFIX = 'mastra:';

const DEFAULT_COMMAND_TIMEOUT_MS = 300_000;
/** `POST /exec` waits for at most an hour; longer commands go over the stream. */
const MAX_RUN_COMMAND_MS = 3_600_000;
const DEFAULT_MAX_RETAINED_BYTES = 1_048_576;
/** Statuses a sandbox passes through on its way somewhere else. */
const TRANSITIONAL: ReadonlySet<string> = new Set(['queued', 'starting', 'resuming', 'pausing', 'shutting_down']);
/** Statuses of a sandbox that has ended for good. */
const ENDED: ReadonlySet<string> = new Set(['stopping', 'stopped', 'failed']);
const SETTLE_TIMEOUT_MS = 300_000;
const SETTLE_POLL_MS = 1_000;

type InstructionsOption =
  | string
  | ((opts: { defaultInstructions: string; requestContext?: RequestContext }) => string);

/** A Groundbox volume to attach at creation. */
export interface GroundboxVolumeMount {
  /** Volume id (`vol_...`) or name. */
  volume: string;
  /** Absolute path in the sandbox. */
  path: string;
  /** Read-only, so other sandboxes can attach it too. */
  readOnly?: boolean;
}

/** A Linux desktop with Chrome, for computer use. */
export interface GroundboxDesktopOptions {
  width?: number;
  height?: number;
  /** Page Chrome opens at start. */
  startUrl?: string;
  /** Record the session. */
  recording?: boolean;
}

export interface GroundboxSandboxOptions extends Omit<MastraSandboxOptions, 'processes'> {
  /**
   * Logical id of this sandbox. A Groundbox sandbox started for it carries the id in its
   * metadata (`mastra-sandbox-id`), so a later `start()` with the same id, in this process or
   * another, reconnects to it (resuming it when paused) instead of creating a new one.
   */
  id?: string;
  /** Reattach to this Groundbox sandbox (`sbx_...`) first, when it is still there. */
  sandboxId?: string;
  /** API key. Defaults to the GROUNDBOX_API_KEY environment variable. */
  apiKey?: string;
  /** API base URL, ending in /v1. Defaults to GROUNDBOX_API_URL, then https://api.groundbox.dev/v1. */
  baseUrl?: string;
  /** A Groundbox client to use instead of making one (tests, a custom fetch or WebSocket). */
  client?: Groundbox;
  /**
   * Default timeout for a command, in milliseconds.
   * @default 300_000 (5 minutes)
   */
  timeout?: number;
  /** vCPUs, 1 to 16. @default 2 */
  cpus?: number;
  /** Memory in GB, up to 4 per vCPU. @default 2 per vCPU */
  memoryGb?: number;
  /** Disk in GB (a minimum: never smaller than the image's disk). @default 20 */
  diskGb?: number;
  /** Start from a public Docker image (`python:3.12-slim`) or one in your Groundbox registry. Commands then run as root. */
  image?: string;
  /** Start from a Groundbox template (id or name), built ahead with `create_template`. */
  template?: string;
  /** Start from a kept Groundbox snapshot (`snap_...`): its memory, disk and processes. */
  snapshotId?: string;
  /**
   * Environment for every command: set on the Groundbox sandbox at creation, and kept as
   * Mastra's runtime overlay (change it later with `setEnv`).
   */
  env?: Record<string, string>;
  /** Metadata pairs on the Groundbox sandbox (list and filter sandboxes by them). */
  metadata?: Record<string, string>;
  /** Names of team secrets to expose as environment variables of the same names. */
  secrets?: string[];
  /** Environment variable name to team secret name. */
  envFromSecrets?: Record<string, string>;
  /** Hosts the sandbox may reach; everything else is blocked. Names, `*.wildcards`, IPv4 addresses and networks. */
  allowHosts?: string[];
  /** Hosts the sandbox may not reach. */
  denyHosts?: string[];
  /** Volumes to attach at creation. A sandbox with volumes cannot be paused, so `stop()` shuts it down instead. */
  volumes?: GroundboxVolumeMount[];
  /**
   * How long the Groundbox sandbox may live, in minutes, from its last start or wake (up to
   * 43,200). What happens then is `onTimeout`.
   * @default 60
   */
  timeoutMinutes?: number;
  /**
   * What the sandbox's timeout does: delete it, shut it down keeping its disk, or pause it.
   * `stop` and `pause` need the paid plan.
   * @default 'delete'
   */
  onTimeout?: 'delete' | 'stop' | 'pause';
  /** Pause the sandbox after this many seconds with nothing using it (at least 30); the next request wakes it. */
  idleTimeoutSeconds?: number;
  /** Half price for work that can wait: it starts when a machine has spare room. */
  flex?: boolean;
  /** Start a Linux desktop with Chrome and expose Mastra's computer-use tools. */
  desktop?: boolean | GroundboxDesktopOptions;
  /**
   * Make port previews (`networking.getPortUrl`) public. Private previews open only for
   * members of your Groundbox team who are signed in.
   * @default false
   */
  publicPreviews?: boolean;
  /** Kept-snapshot name `snapshot()` writes to and the next start boots from. Defaults to the sandbox's id. */
  checkpointName?: string;
  /** Kept-snapshot name to boot from when `checkpointName` has none yet. */
  seedCheckpointName?: string;
  /** Replace or extend the instructions the agent gets about this sandbox. */
  instructions?: InstructionsOption;
}

/**
 * Groundbox sandbox provider for Mastra workspaces.
 *
 * @example
 * ```typescript
 * import { Workspace } from '@mastra/core/workspace';
 * import { GroundboxSandbox } from '@groundbox/mastra';
 *
 * const workspace = new Workspace({ sandbox: new GroundboxSandbox({ cpus: 2 }) });
 * ```
 */
export class GroundboxSandbox extends MastraSandbox<Sandbox> {
  readonly id: string;
  readonly name = 'GroundboxSandbox';
  readonly provider = 'groundbox';
  status: ProviderStatus = 'pending';
  declare readonly processes: GroundboxProcessManager;
  declare readonly computer?: SandboxComputer;
  readonly supportsCheckpoints = true;

  /**
   * Public HTTPS URLs for ports in the sandbox, through Groundbox previews
   * (`https://<slug>-<port>.groundbox.page`). The app must listen on 0.0.0.0. Private unless
   * `publicPreviews` is set. Previews are on the paid plan.
   */
  readonly networking: SandboxNetworking = {
    getPortUrl: async (port: number) => {
      try {
        const id = this._sandboxId ?? (await this.lookup())?.id;
        if (!id) return null;
        const preview = await this.client.expose_port(id, port, this.publicPreviews ? { public: true } : {});
        return preview.url;
      } catch (error) {
        this.logger.debug(`${LOG_PREFIX} No preview for port ${port}`, { error });
        return null;
      }
    },
  };

  private readonly client: Groundbox;
  private readonly commandTimeout: number;
  private readonly options: GroundboxSandboxOptions;
  private readonly publicPreviews: boolean;
  private readonly checkpointName: string;
  private _sandbox: Sandbox | null = null;
  private _sandboxId: string | undefined;
  private _preferredSandboxId: string | undefined;
  private _createdAt: Date | null = null;
  private _isRetrying = false;
  private _filesystem: GroundboxSandboxFilesystem | undefined;
  /** The `workingDirectory` option as given, apart from the detected home folder. */
  private readonly explicitWorkingDirectory: string | undefined;

  constructor(options: GroundboxSandboxOptions = {}) {
    const commandTimeout = options.timeout ?? DEFAULT_COMMAND_TIMEOUT_MS;
    super({
      ...options,
      name: 'GroundboxSandbox',
      processes: new GroundboxProcessManager(),
    });
    this.options = { ...options };
    this.id = options.id ?? `groundbox-sandbox-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    this.client =
      options.client ??
      new Groundbox({
        ...(options.apiKey !== undefined && { apiKey: options.apiKey }),
        ...(options.baseUrl !== undefined && { baseUrl: options.baseUrl }),
        userAgent: 'groundbox-mastra',
      });
    this.commandTimeout = commandTimeout;
    this.defaultCommandTimeout = commandTimeout;
    this.publicPreviews = options.publicPreviews ?? false;
    this.checkpointName = options.checkpointName ?? this.id;
    this._preferredSandboxId = options.sandboxId;
    this.explicitWorkingDirectory = options.workingDirectory;
    if (options.desktop) this.computer = createGroundboxComputer(this);
  }

  /** The Groundbox client this sandbox uses, for any API call this class does not wrap. */
  get groundbox(): Groundbox {
    return this.client;
  }

  /** The Groundbox sandbox id (`sbx_...`), once started. Persist it to reattach with `sandboxId`. */
  get sandboxId(): string | undefined {
    return this._sandboxId;
  }

  /** The Groundbox sandbox as last read. */
  get instance(): Sandbox {
    if (!this._sandbox) throw new SandboxNotReadyError(this.id);
    return this._sandbox;
  }

  /** The sandbox's disk through Groundbox's files API. Pass it as the workspace `filesystem` to give the agent file tools on it. */
  get filesystem(): GroundboxSandboxFilesystem {
    if (!this._filesystem) this._filesystem = new GroundboxSandboxFilesystem(this);
    return this._filesystem;
  }

  /** @internal The Groundbox sandbox id; throws when the sandbox has not been started. */
  requireSandboxId(): string {
    if (!this._sandboxId || !this._sandbox) throw new SandboxNotReadyError(this.id);
    return this._sandboxId;
  }

  /**
   * A sibling sandbox with this one's settings and per-instance overrides. No I/O: it creates
   * (or reconnects to the sandbox for its `id`, or reattaches to `sandboxId`) on its own start.
   * `idleTimeoutMinutes` becomes Groundbox's idle pause.
   */
  clone(options: SandboxCloneOptions = {}): GroundboxSandbox {
    const { id: _id, sandboxId: _sandboxId, checkpointName: _cp, seedCheckpointName: _seed, ...base } = this.options;
    return new GroundboxSandbox({
      ...base,
      client: this.client,
      ...(options.id !== undefined && { id: options.id }),
      ...(options.sandboxId !== undefined && { sandboxId: options.sandboxId }),
      ...(options.env !== undefined && { env: options.env }),
      ...(options.workingDirectory !== undefined && { workingDirectory: options.workingDirectory }),
      ...(options.idleTimeoutMinutes !== undefined && { idleTimeoutSeconds: Math.max(30, Math.round(options.idleTimeoutMinutes * 60)) }),
      ...(options.checkpointName !== undefined && { checkpointName: options.checkpointName }),
      ...(options.seedCheckpointName !== undefined && { seedCheckpointName: options.seedCheckpointName }),
    });
  }

  // ---------------------------------------------------------------------------
  // Start: find / connect / create (the base class orchestrates them)
  // ---------------------------------------------------------------------------

  /**
   * The Groundbox sandbox for this logical id: the one named by `sandboxId` (or the last one
   * this instance had) when it is still there, else the newest live sandbox whose metadata
   * carries this id. Errors other than not-found propagate: making a duplicate on an auth or
   * network error would be worse than failing.
   */
  protected async find(): Promise<Sandbox | undefined> {
    if (this._sandbox) return this._sandbox;
    if (this._preferredSandboxId) {
      try {
        const sandbox = await this.client.get_sandbox(this._preferredSandboxId);
        if (!ENDED.has(sandbox.status)) return sandbox;
      } catch (error) {
        if (errorCode(error) !== 'not_found') throw error;
      }
      this._preferredSandboxId = undefined;
    }
    return this.lookup();
  }

  /** The newest live sandbox for this logical id, without starting or resuming it. */
  private async lookup(): Promise<Sandbox | undefined> {
    const list = await this.client.list_sandboxes({ live: true, metadata: { [MASTRA_ID_METADATA_KEY]: this.id }, limit: 10 });
    return list.data.find((sandbox) => !ENDED.has(sandbox.status));
  }

  /** Bring an existing sandbox to `running`: wait out a transition, resume a paused one, start a shut-down one. */
  protected async connect(existing: Sandbox): Promise<void> {
    if (existing === this._sandbox && existing.status === 'running') return;
    let sandbox = await this.settle(existing);
    if (sandbox.status === 'paused') {
      this.logger.debug(`${LOG_PREFIX} Resuming ${sandbox.id} for ${this.id}`);
      sandbox = await this.client.resume_sandbox(sandbox.id);
    } else if (sandbox.status === 'shut_down') {
      this.logger.debug(`${LOG_PREFIX} Starting ${sandbox.id} for ${this.id}`);
      sandbox = await this.client.start_sandbox(sandbox.id);
    }
    if (sandbox.status !== 'running') sandbox = await this.client.wait_for_sandbox(sandbox.id);
    if (sandbox.status !== 'running') {
      throw new Error(`${LOG_PREFIX} Groundbox sandbox ${sandbox.id} is ${sandbox.status}${sandbox.error ? `: ${sandbox.error}` : ''}`);
    }
    await this.adopt(sandbox);
    this.logger.debug(`${LOG_PREFIX} Reconnected to ${sandbox.id} for ${this.id}`);
  }

  protected async create(): Promise<void> {
    const spec = await this.createSpec();
    this.logger.debug(`${LOG_PREFIX} Creating a sandbox for ${this.id}`);
    let sandbox = await this.client.create_sandbox(spec, { idempotencyKey: randomUUID() });
    if (sandbox.status !== 'running') sandbox = await this.client.wait_for_sandbox(sandbox.id);
    if (sandbox.status !== 'running') {
      await this.client.delete_sandbox(sandbox.id).catch(() => undefined);
      throw new Error(`${LOG_PREFIX} Groundbox sandbox ${sandbox.id} did not start (${sandbox.status})${sandbox.error ? `: ${sandbox.error}` : ''}`);
    }
    await this.adopt(sandbox);
    this.logger.debug(`${LOG_PREFIX} Created ${sandbox.id} for ${this.id}`);
  }

  /** The create request: this sandbox's settings, booted from a checkpoint when one is kept. */
  private async createSpec(): Promise<CreateSandboxRequest> {
    const o = this.options;
    const env = definedEnv(o.env);
    const common: CreateSandboxRequest = {
      metadata: { ...o.metadata, [MASTRA_ID_METADATA_KEY]: this.id },
      ...(Object.keys(env).length > 0 && { env }),
      ...(o.secrets && { secrets: o.secrets }),
      ...(o.envFromSecrets && { env_from_secrets: o.envFromSecrets }),
      ...(o.allowHosts && { allow_hosts: o.allowHosts }),
      ...(o.denyHosts && { deny_hosts: o.denyHosts }),
      ...(o.timeoutMinutes !== undefined && { timeout_minutes: o.timeoutMinutes }),
      ...(o.onTimeout && { on_timeout: o.onTimeout }),
      ...(o.idleTimeoutSeconds !== undefined && { idle_timeout_seconds: o.idleTimeoutSeconds }),
      ...(o.flex && { flex: true }),
    };
    const checkpoint =
      o.snapshotId === undefined
        ? ((await this.findCheckpoint(this.checkpointName)) ??
          (o.seedCheckpointName ? await this.findCheckpoint(o.seedCheckpointName) : undefined))
        : undefined;
    const snapshotId = o.snapshotId ?? checkpoint?.id;
    if (snapshotId) {
      // A sandbox from a snapshot has the snapshot's size and disk.
      return { ...common, snapshot_id: snapshotId };
    }
    const desktop =
      o.desktop === undefined || o.desktop === false
        ? undefined
        : o.desktop === true
          ? true
          : {
              ...(o.desktop.width !== undefined && { width: o.desktop.width }),
              ...(o.desktop.height !== undefined && { height: o.desktop.height }),
              ...(o.desktop.startUrl !== undefined && { start_url: o.desktop.startUrl }),
              ...(o.desktop.recording !== undefined && { recording: o.desktop.recording }),
            };
    return {
      ...common,
      ...(o.cpus !== undefined && { cpus: o.cpus }),
      ...(o.memoryGb !== undefined && { memory_gb: o.memoryGb }),
      ...(o.diskGb !== undefined && { disk_gb: o.diskGb }),
      ...(o.image !== undefined && { image: o.image }),
      ...(o.template !== undefined && { template: o.template }),
      ...(o.volumes && { volumes: o.volumes.map((v) => ({ volume: v.volume, path: v.path, read_only: v.readOnly ?? false })) }),
      ...(desktop !== undefined && { desktop }),
    };
  }

  /** Wait while the sandbox is between states (queued, pausing, shutting down, ...). */
  private async settle(sandbox: Sandbox): Promise<Sandbox> {
    const deadline = Date.now() + SETTLE_TIMEOUT_MS;
    let current = sandbox;
    while (TRANSITIONAL.has(current.status) && Date.now() < deadline) {
      if (current.status === 'queued' || current.status === 'starting' || current.status === 'resuming') {
        current = await this.client.wait_for_sandbox(current.id);
        continue;
      }
      await sleep(SETTLE_POLL_MS);
      current = await this.client.get_sandbox(current.id);
    }
    return current;
  }

  private async adopt(sandbox: Sandbox): Promise<void> {
    this._sandbox = sandbox;
    this._sandboxId = sandbox.id;
    this._preferredSandboxId = sandbox.id;
    this._createdAt = new Date(sandbox.created_at);
    await this.detectWorkingDirectory();
  }

  /**
   * Groundbox's own image starts commands in /home/runner. On a Docker image, a template or a
   * snapshot the home folder can differ, so it is read from the sandbox once.
   */
  private async detectWorkingDirectory(): Promise<void> {
    if (this.explicitWorkingDirectory !== undefined || !this._sandboxId) return;
    const o = this.options;
    if (!o.image && !o.template && !o.snapshotId && !this._sandbox?.snapshot_id && !this._sandbox?.template) {
      this.setWorkingDirectory('/home/runner');
      return;
    }
    try {
      const result = await this.client.run_command(this._sandboxId, 'pwd', { timeout_s: 30 });
      const dir = result.stdout.trim();
      if (result.exit_code === 0 && dir.startsWith('/')) this.setWorkingDirectory(dir);
    } catch (error) {
      this.logger.debug(`${LOG_PREFIX} Could not read the working directory`, { error });
    }
  }

  /** Read the Groundbox sandbox again and keep it. */
  async refresh(): Promise<Sandbox | undefined> {
    if (!this._sandboxId) return undefined;
    const sandbox = await this.client.get_sandbox(this._sandboxId);
    if (this._sandbox) this._sandbox = sandbox;
    return sandbox;
  }

  // ---------------------------------------------------------------------------
  // Stop / destroy
  // ---------------------------------------------------------------------------

  /**
   * Pause the Groundbox sandbox: memory, processes and disk are kept and only storage is
   * billed. The next start resumes it. A sandbox with volumes cannot be paused, so it is shut
   * down instead (disk kept, a cold boot on the next start).
   */
  async stop(): Promise<void> {
    const sandbox = this._sandbox ?? (await this.lookup().catch(() => undefined)) ?? null;
    // Paused processes keep running after a resume; stop following them now so their handles
    // do not report an exit. `processes.get(id)` attaches to one again after the next start.
    this.processes.detachAll();
    if (sandbox) {
      const current = await this.settle(await this.client.get_sandbox(sandbox.id).catch(() => sandbox));
      if (current.status === 'running') {
        if (this.options.volumes?.length) await this.client.stop_sandbox(current.id);
        else await this.client.pause_sandbox(current.id);
        this.logger.debug(`${LOG_PREFIX} ${this.options.volumes?.length ? 'Shut down' : 'Paused'} ${current.id} for ${this.id}`);
      }
      this._preferredSandboxId = current.id;
    }
    this._sandbox = null;
  }

  /** Delete the Groundbox sandbox for good. Billing stops. */
  async destroy(): Promise<void> {
    const id = this._sandboxId ?? this._preferredSandboxId ?? (await this.lookup().catch(() => undefined))?.id;
    this.processes.detachAll();
    if (id) {
      try {
        await this.client.delete_sandbox(id);
        this.logger.debug(`${LOG_PREFIX} Deleted ${id} for ${this.id}`);
      } catch (error) {
        if (errorCode(error) !== 'not_found') throw error;
      }
    }
    this._sandbox = null;
    this._sandboxId = undefined;
    this._preferredSandboxId = undefined;
  }

  async isReady(): Promise<boolean> {
    return this.status === 'running' && this._sandbox !== null;
  }

  async getInfo(): Promise<SandboxInfo> {
    const s = this._sandbox;
    return {
      id: this.id,
      name: this.name,
      provider: this.provider,
      status: this.status,
      createdAt: this._createdAt ?? new Date(),
      ...(s?.expires_at && { timeoutAt: new Date(s.expires_at) }),
      mounts: [],
      ...(s && { resources: { cpuCores: s.cpus, memoryMB: s.memory_gb * 1024, diskMB: s.disk_gb * 1024 } }),
      metadata: {
        ...(this._sandboxId && { sandboxId: this._sandboxId }),
        ...(s && { groundboxStatus: s.status }),
        ...(this.options.image && { image: this.options.image }),
        ...(this.options.template && { template: this.options.template }),
        ...(s?.snapshot_id && { snapshotId: s.snapshot_id }),
        ...(s?.desktop && { desktop: { width: s.desktop.width, height: s.desktop.height } }),
      },
    };
  }

  getInstructions(opts?: { requestContext?: RequestContext }): string {
    const defaultInstructions = this.defaultInstructions();
    const custom = this.options.instructions;
    if (custom === undefined) return defaultInstructions;
    if (typeof custom === 'string') return custom;
    return custom({ defaultInstructions, ...(opts?.requestContext && { requestContext: opts.requestContext }) });
  }

  private defaultInstructions(): string {
    const o = this.options;
    const parts: string[] = [];
    if (o.image) {
      parts.push(`Groundbox sandbox: an isolated Linux microVM started from the Docker image ${o.image}. Commands run as root.`);
    } else {
      parts.push(
        'Groundbox sandbox: an isolated Linux (Ubuntu) microVM. Commands run as user `runner`, who has passwordless sudo, so `sudo apt-get install` works.',
      );
    }
    if (this.workingDirectory) parts.push(`Default working directory: ${this.workingDirectory}.`);
    parts.push(`Command timeout: ${Math.ceil(this.commandTimeout / 1000)}s; use background processes for servers and long jobs.`);
    if (o.allowHosts) parts.push(`Network: only these hosts are reachable: ${o.allowHosts.join(', ')}.`);
    else if (o.denyHosts) parts.push(`Network: these hosts are blocked: ${o.denyHosts.join(', ')}.`);
    if (o.desktop) parts.push('A Linux desktop with Chrome is running for computer use.');
    if (o.volumes?.length) parts.push(`Volumes: ${o.volumes.map((v) => `${v.path}${v.readOnly ? ' (read-only)' : ''}`).join(', ')}.`);
    return parts.join(' ');
  }

  // ---------------------------------------------------------------------------
  // Commands and files
  // ---------------------------------------------------------------------------

  /**
   * Run a command and wait for it. Without streaming callbacks or an abort signal this is one
   * `POST /exec` call; otherwise it is a background command followed over the exec stream.
   */
  async executeCommand(command: string, args?: string[], options: ExecuteCommandOptions = {}): Promise<CommandResult> {
    const fullCommand = args?.length ? `${command} ${args.map(shellQuote).join(' ')}` : command;
    const timeout = options.timeout ?? this.defaultCommandTimeout ?? this.commandTimeout;
    const maxRetainedBytes = options.maxRetainedBytes ?? DEFAULT_MAX_RETAINED_BYTES;
    const streamed = options.onStdout || options.onStderr || options.abortSignal || timeout > MAX_RUN_COMMAND_MS;
    if (streamed) {
      const handle = await this.processes.spawn(fullCommand, {
        ...options,
        timeout,
        maxRetainedBytes,
        stdinMode: 'ignore',
        ...(args?.length && { originalInvocation: { command, args: [...args] } }),
      });
      try {
        const result = await handle.wait();
        if (handle.exitCode === undefined) await handle.kill();
        return { ...result, command: fullCommand, ...(args && { args }) };
      } finally {
        this.processes.release(handle.pid);
      }
    }

    await this.ensureRunning();
    const env = definedEnv({ ...this.getEnv(), ...options.env });
    const cwd = options.cwd ?? this.workingDirectory;
    const start = Date.now();
    const result = await this.retryOnDead(async () =>
      this.client.run_command(this.requireSandboxId(), fullCommand, {
        ...(cwd !== undefined && { cwd }),
        ...(Object.keys(env).length > 0 && { env }),
        timeout_s: toSeconds(timeout),
      }),
    );
    const stdout = retainNewest(result.stdout, maxRetainedBytes);
    const stderr = retainNewest(result.stderr, maxRetainedBytes);
    return {
      success: result.exit_code === 0 && !result.timed_out,
      exitCode: result.exit_code,
      stdout: stdout.text,
      stderr: stderr.text,
      executionTimeMs: Date.now() - start,
      ...(result.timed_out && { timedOut: true, killed: true }),
      ...(stdout.dropped > 0 && { stdoutTruncated: true, stdoutDroppedBytes: stdout.dropped }),
      ...(stderr.dropped > 0 && { stderrTruncated: true, stderrDroppedBytes: stderr.dropped }),
      command: fullCommand,
      ...(args && { args }),
    };
  }

  /**
   * Write many files at once: they go up as one tar.gz and are unpacked in the sandbox. Modes are
   * kept. A relative path is resolved against the working directory. Files are written as the
   * sandbox user (`runner` on Groundbox's image), so a path it cannot write to fails.
   */
  async writeFiles(files: SandboxFileInput[], options?: WriteFilesOptions): Promise<void> {
    for (const file of files) if (file.mode !== undefined) validateSandboxFileMode(file.mode);
    if (options?.abortSignal?.aborted) throw new SandboxAbortError('writeFiles', options.abortSignal.reason);
    if (files.length === 0) return;
    await this.ensureRunning();
    const base = this.workingDirectory ?? '/';
    const entries = files.map((file) => {
      const abs = toAbsolute(file.path, base);
      if (abs === '/') throw new Error(`${LOG_PREFIX} Cannot write a file at /`);
      return {
        path: abs.slice(1),
        content: typeof file.content === 'string' ? file.content : new Uint8Array(file.content),
        ...(file.mode !== undefined && { mode: file.mode }),
      };
    });
    try {
      await this.retryOnDead(() =>
        this.client.upload_files(this.requireSandboxId(), '/', entries, options?.abortSignal ? { signal: options.abortSignal } : {}),
      );
    } catch (error) {
      if (options?.abortSignal?.aborted) throw new SandboxAbortError('writeFiles', options.abortSignal.reason);
      throw error;
    }
  }

  // ---------------------------------------------------------------------------
  // Checkpoints (Groundbox kept snapshots)
  // ---------------------------------------------------------------------------

  /**
   * Keep the sandbox as it is now (memory, disk and processes) as a Groundbox snapshot named
   * `checkpointName`, replacing the previous one of that name. The next sandbox started for this
   * checkpoint boots from it. Kept snapshots are on the paid plan.
   */
  async snapshot(): Promise<void> {
    if (!this._sandbox || !this._sandboxId) return;
    const label = CHECKPOINT_LABEL_PREFIX + this.checkpointName;
    const kept = await this.client.keep_snapshot({ sandbox_id: this._sandboxId, label });
    if (kept.status !== 'ready') throw new Error(`${LOG_PREFIX} Snapshot ${kept.id} is ${kept.status}${kept.error ? `: ${kept.error}` : ''}`);
    for (const old of await this.listCheckpoints(label)) {
      if (old.id !== kept.id) await this.client.delete_snapshot(old.id).catch(() => undefined);
    }
  }

  private async listCheckpoints(label: string): Promise<Snapshot[]> {
    const list = await this.client.list_snapshots({ limit: 100 });
    return list.data.filter((snapshot) => snapshot.label === label);
  }

  private async findCheckpoint(name: string): Promise<Snapshot | undefined> {
    try {
      const snapshots = await this.listCheckpoints(CHECKPOINT_LABEL_PREFIX + name);
      return snapshots
        .filter((snapshot) => snapshot.status === 'ready')
        .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))[0];
    } catch (error) {
      // Kept snapshots are a paid-plan feature; a trial key just boots fresh.
      this.logger.debug(`${LOG_PREFIX} Could not list snapshots`, { error });
      return undefined;
    }
  }

  // ---------------------------------------------------------------------------
  // A sandbox that went away under us
  // ---------------------------------------------------------------------------

  /** Whether `error` came from the sandbox not running, as opposed to the call itself. */
  private async isSandboxGone(error: unknown): Promise<boolean> {
    const code = errorCode(error);
    if (code !== 'conflict' && code !== 'not_found') return false;
    if (!this._sandboxId) return false;
    try {
      const sandbox = await this.client.get_sandbox(this._sandboxId);
      if (sandbox.status === 'running') return false;
      if (ENDED.has(sandbox.status)) this._preferredSandboxId = undefined;
      return true;
    } catch (lookupError) {
      if (errorCode(lookupError) === 'not_found') {
        this._preferredSandboxId = undefined;
        return true;
      }
      return false;
    }
  }

  /**
   * Run `fn`; when it fails because the sandbox stopped, was paused or timed out, start it again
   * (resuming it, or making a new one when it ended) and run `fn` once more.
   */
  async retryOnDead<T>(fn: () => Promise<T>): Promise<T> {
    try {
      return await fn();
    } catch (error) {
      if (this._isRetrying || !(await this.isSandboxGone(error))) throw error;
      this.logger.debug(`${LOG_PREFIX} Sandbox ${this._sandboxId} is not running; starting it again`);
      this._isRetrying = true;
      try {
        this._sandbox = null;
        this._sandboxId = undefined;
        this.status = 'stopped';
        await this.ensureRunning();
        return await fn();
      } finally {
        this._isRetrying = false;
      }
    }
  }
}
