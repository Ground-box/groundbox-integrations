/**
 * An in-memory stand-in for the Groundbox client: enough of its behaviour for the provider's
 * unit tests, with every call recorded.
 */
import { GroundboxError } from '@groundbox/sdk';
import type { CommandStream, Groundbox, Sandbox } from '@groundbox/sdk';

type Call = { method: string; args: unknown[] };

export interface FakeCommandIO {
  stdout(text: string): void;
  stderr(text: string): void;
  /** Resolves with the next stdin write, or null once stdin is closed. */
  readStdin(): Promise<string | null>;
  killed: Promise<void>;
}

export type FakeCommand = (io: FakeCommandIO, request: { cmd: string; cwd?: string; env?: Record<string, string> }) => Promise<number>;

interface RunningCommand {
  id: string;
  cmd: string;
  state: 'running' | 'exited';
  exit_code: number | null;
  stdin: boolean;
  timed_out: boolean;
  output: Array<['stdout' | 'stderr', string]>;
  listeners: Set<(event: ['stdout' | 'stderr', string] | ['exit', number]) => void>;
  stdinQueue: Array<string | null>;
  stdinWaiters: Array<(value: string | null) => void>;
  kill: () => void;
}

function apiError(code: string, status: number, message = code): GroundboxError {
  return new GroundboxError(code as never, message, status, { method: 'GET', path: '/' });
}

export function fakeSandbox(overrides: Partial<Sandbox> = {}): Sandbox {
  return {
    id: 'sbx_fake1',
    status: 'running',
    cpus: 2,
    memory_gb: 4,
    disk_gb: 20,
    booted: null,
    timeout_minutes: 60,
    metadata: {},
    allow_hosts: null,
    flex: false,
    shared_cpu: false,
    forked_from: null,
    cost_micros: 0,
    created_at: '2026-11-19T00:00:00.000Z',
    started_at: '2026-11-19T00:00:00.000Z',
    expires_at: '2026-11-19T01:00:00.000Z',
    paused_at: null,
    stopped_at: null,
    stop_reason: null,
    ...overrides,
  } as Sandbox;
}

export class FakeGroundbox {
  calls: Call[] = [];
  sandboxes = new Map<string, Sandbox>();
  files = new Map<string, { content: Uint8Array; mtime: string }>();
  dirs = new Set<string>(['/', '/home', '/home/runner', '/tmp']);
  commands = new Map<string, RunningCommand>();
  snapshots: Array<{ id: string; label: string | null; status: 'ready'; created_at: string }> = [];
  private nextId = 1;

  /** What `run_command` answers. */
  onRun: (cmd: string | readonly string[], options: Record<string, unknown>) => { exit_code: number; stdout: string; stderr: string; timed_out?: boolean } =
    () => ({ exit_code: 0, stdout: '', stderr: '' });
  /** What a started command does. */
  onStart: FakeCommand = async () => 0;
  /** Fail the next calls of a method with this error, once each. */
  failNext = new Map<string, unknown>();

  private record(method: string, ...args: unknown[]): void {
    this.calls.push({ method, args });
    const failure = this.failNext.get(method);
    if (failure) {
      this.failNext.delete(method);
      throw failure;
    }
  }

  callsOf(method: string): unknown[][] {
    return this.calls.filter((call) => call.method === method).map((call) => call.args);
  }

  asClient(): Groundbox {
    return this as unknown as Groundbox;
  }

  // Sandboxes ---------------------------------------------------------------

  async create_sandbox(spec: Record<string, unknown>, options?: unknown): Promise<Sandbox> {
    this.record('create_sandbox', spec, options);
    const id = `sbx_fake${this.nextId++}`;
    const sandbox = fakeSandbox({
      id,
      metadata: (spec.metadata as Record<string, string>) ?? {},
      cpus: (spec.cpus as number) ?? 2,
      ...(spec.snapshot_id ? { snapshot_id: spec.snapshot_id as string } : {}),
      ...(spec.desktop ? { desktop: { width: 1280, height: 800, browser_profile_dir: '/x', recording: false, status: 'ready', error: null, control: 'agent', ready_ms: 1 } } : {}),
    });
    this.sandboxes.set(id, sandbox);
    return { ...sandbox };
  }

  async wait_for_sandbox(id: string): Promise<Sandbox> {
    this.record('wait_for_sandbox', id);
    const sandbox = this.sandboxes.get(id);
    if (!sandbox) throw apiError('not_found', 404);
    if (['queued', 'starting', 'resuming'].includes(sandbox.status)) sandbox.status = 'running';
    return { ...sandbox };
  }

  async get_sandbox(id: string): Promise<Sandbox> {
    this.record('get_sandbox', id);
    const sandbox = this.sandboxes.get(id);
    if (!sandbox) throw apiError('not_found', 404);
    if (sandbox.status === 'pausing') sandbox.status = 'paused';
    return { ...sandbox };
  }

  async list_sandboxes(query: { live?: boolean; metadata?: Record<string, string> } = {}): Promise<{ data: Sandbox[]; next_cursor: null }> {
    this.record('list_sandboxes', query);
    const data = [...this.sandboxes.values()]
      .filter((s) => !query.live || !['stopped', 'failed'].includes(s.status))
      .filter((s) => Object.entries(query.metadata ?? {}).every(([k, v]) => s.metadata[k] === v))
      .reverse()
      .map((s) => ({ ...s }));
    return { data, next_cursor: null };
  }

  private transition(id: string, from: string[], to: Sandbox['status']): Sandbox {
    const sandbox = this.sandboxes.get(id);
    if (!sandbox) throw apiError('not_found', 404);
    if (!from.includes(sandbox.status)) throw apiError('conflict', 409, `sandbox is ${sandbox.status}`);
    sandbox.status = to;
    return { ...sandbox };
  }

  async pause_sandbox(id: string): Promise<Sandbox> {
    this.record('pause_sandbox', id);
    return this.transition(id, ['running'], 'paused');
  }

  async resume_sandbox(id: string): Promise<Sandbox> {
    this.record('resume_sandbox', id);
    return this.transition(id, ['paused'], 'running');
  }

  async stop_sandbox(id: string): Promise<Sandbox> {
    this.record('stop_sandbox', id);
    return this.transition(id, ['running'], 'shut_down');
  }

  async start_sandbox(id: string): Promise<Sandbox> {
    this.record('start_sandbox', id);
    return this.transition(id, ['shut_down'], 'running');
  }

  async delete_sandbox(id: string): Promise<Sandbox> {
    this.record('delete_sandbox', id);
    const sandbox = this.sandboxes.get(id);
    if (!sandbox) throw apiError('not_found', 404);
    sandbox.status = 'stopped';
    sandbox.stop_reason = 'deleted';
    return { ...sandbox };
  }

  private requireRunning(id: string): void {
    const sandbox = this.sandboxes.get(id);
    if (!sandbox) throw apiError('not_found', 404);
    if (sandbox.status !== 'running') throw apiError('conflict', 409, 'The sandbox is not running');
  }

  // Commands ----------------------------------------------------------------

  async run_command(id: string, cmd: string | readonly string[], options: Record<string, unknown> = {}) {
    this.record('run_command', id, cmd, options);
    this.requireRunning(id);
    const result = this.onRun(cmd, options);
    return { duration_ms: 1, timed_out: false, ...result };
  }

  async start_command(id: string, cmd: string, options: { cwd?: string; env?: Record<string, string>; stdin?: boolean; timeout_s?: number } = {}) {
    this.record('start_command', id, cmd, options);
    this.requireRunning(id);
    const execId = `ex_${this.nextId++}`;
    let killedResolve!: () => void;
    const killed = new Promise<void>((resolve) => (killedResolve = resolve));
    const command: RunningCommand = {
      id: execId,
      cmd,
      state: 'running',
      exit_code: null,
      stdin: options.stdin ?? false,
      timed_out: false,
      output: [],
      listeners: new Set(),
      stdinQueue: [],
      stdinWaiters: [],
      kill: () => killedResolve(),
    };
    this.commands.set(execId, command);
    const emit = (event: ['stdout' | 'stderr', string] | ['exit', number]) => {
      if (event[0] !== 'exit') command.output.push(event);
      for (const listener of command.listeners) listener(event);
    };
    const io: FakeCommandIO = {
      stdout: (text) => emit(['stdout', text]),
      stderr: (text) => emit(['stderr', text]),
      readStdin: () => {
        const queued = command.stdinQueue.shift();
        if (queued !== undefined) return Promise.resolve(queued);
        return new Promise((resolve) => command.stdinWaiters.push(resolve));
      },
      killed,
    };
    const finish = (code: number) => {
      if (command.state !== 'running') return;
      command.state = 'exited';
      command.exit_code = code;
      emit(['exit', code]);
    };
    setTimeout(() => {
      Promise.race([this.onStart(io, { cmd, ...options }), killed.then(() => 137)]).then(finish, () => finish(1));
    }, 0);
    return this.commandView(command);
  }

  private commandView(command: RunningCommand) {
    return {
      id: command.id,
      cmd: command.cmd,
      args: null,
      cwd: null,
      state: command.state,
      background: true,
      stdin: command.stdin,
      tty: false,
      started_at: '2026-11-19T00:00:00.000Z',
      ended_at: command.state === 'exited' ? '2026-11-19T00:00:01.000Z' : null,
      exit_code: command.exit_code,
      duration_ms: command.state === 'exited' ? 1 : null,
      timed_out: command.timed_out,
      output_seq: 0,
      output_kept: true,
    };
  }

  attach_command(
    id: string,
    execId: string,
    options: { seq?: number } = {},
    handlers: { onStdout?: (text: string) => void; onStderr?: (text: string) => void } = {},
  ): CommandStream {
    this.record('attach_command', id, execId, options);
    const command = this.commands.get(execId);
    if (!command) throw apiError('not_found', 404);
    let settle!: { resolve: (value: { exit_code: number; duration_ms: number; timed_out: boolean }) => void; reject: (error: unknown) => void };
    const done = new Promise<{ exit_code: number; duration_ms: number; timed_out: boolean }>((resolve, reject) => (settle = { resolve, reject }));
    const deliver = (event: ['stdout' | 'stderr', string] | ['exit', number]) => {
      if (event[0] === 'stdout') handlers.onStdout?.(event[1]);
      else if (event[0] === 'stderr') handlers.onStderr?.(event[1]);
      else settle.resolve({ exit_code: event[1] as number, duration_ms: 1, timed_out: command.timed_out });
    };
    // Replay what was printed before attaching, then follow.
    queueMicrotask(() => {
      for (const event of command.output) deliver(event);
      if (command.state === 'exited') deliver(['exit', command.exit_code ?? 0]);
      else command.listeners.add(deliver);
    });
    const pushStdin = (value: string | null) => {
      const waiter = command.stdinWaiters.shift();
      if (waiter) waiter(value);
      else command.stdinQueue.push(value);
    };
    return {
      write: (data) => pushStdin(typeof data === 'string' ? data : Buffer.from(data).toString()),
      resize: () => undefined,
      end: () => pushStdin(null),
      kill: () => command.kill(),
      detach: () => {
        command.listeners.delete(deliver);
        settle.reject(apiError('network', 0, 'Detached'));
      },
      exec_id: execId,
      done,
    };
  }

  async get_command(id: string, execId: string) {
    this.record('get_command', id, execId);
    const command = this.commands.get(execId);
    if (!command) throw apiError('not_found', 404);
    return this.commandView(command);
  }

  async kill_command(id: string, execId: string) {
    this.record('kill_command', id, execId);
    const command = this.commands.get(execId);
    if (!command) throw apiError('not_found', 404);
    command.kill();
    await new Promise((resolve) => setTimeout(resolve, 0));
    return this.commandView(command);
  }

  // Files -------------------------------------------------------------------

  private now(): string {
    return new Date(Date.UTC(2026, 10, 19, 0, 0, this.nextId++)).toISOString();
  }

  private addParents(path: string): void {
    let dir = path.slice(0, path.lastIndexOf('/')) || '/';
    while (!this.dirs.has(dir)) {
      this.dirs.add(dir);
      dir = dir.slice(0, dir.lastIndexOf('/')) || '/';
    }
  }

  async write_file(id: string, path: string, content: string | Uint8Array) {
    this.record('write_file', id, path, content);
    this.requireRunning(id);
    if (this.dirs.has(path)) throw apiError('bad_request', 400, `${path} is a folder`);
    this.addParents(path);
    const bytes = typeof content === 'string' ? Buffer.from(content) : content;
    this.files.set(path, { content: bytes, mtime: this.now() });
    return { path, size_bytes: bytes.length };
  }

  async read_file(id: string, path: string): Promise<Uint8Array> {
    this.record('read_file', id, path);
    this.requireRunning(id);
    if (this.dirs.has(path)) throw apiError('bad_request', 400, `${path} is a folder`);
    const file = this.files.get(path);
    if (!file) throw apiError('not_found', 404);
    return file.content;
  }

  async upload_files(id: string, dir: string, files: Array<{ path: string; content?: string | Uint8Array; mode?: number }>, options?: unknown) {
    this.record('upload_files', id, dir, files, options);
    this.requireRunning(id);
    for (const file of files) {
      const path = `${dir === '/' ? '' : dir}/${file.path}`;
      this.addParents(path);
      this.files.set(path, { content: typeof file.content === 'string' ? Buffer.from(file.content) : (file.content ?? new Uint8Array()), mtime: this.now() });
    }
    return { written: files.length, removed: 0, bytes: 1 };
  }

  private entry(path: string) {
    const name = path.slice(path.lastIndexOf('/') + 1);
    if (this.dirs.has(path)) return { name, path, type: 'dir' as const, size_bytes: 4096, modified_at: '2026-11-19T00:00:00.000Z' };
    const file = this.files.get(path);
    if (file) return { name, path, type: 'file' as const, size_bytes: file.content.length, modified_at: file.mtime };
    return undefined;
  }

  async list_files(id: string, dir: string) {
    this.record('list_files', id, dir);
    this.requireRunning(id);
    if (this.files.has(dir)) throw apiError('bad_request', 400, `Not a folder: ${dir}`);
    if (!this.dirs.has(dir)) throw apiError('not_found', 404);
    const prefix = dir === '/' ? '/' : `${dir}/`;
    const children = [...this.dirs, ...this.files.keys()].filter((p) => p !== dir && p.startsWith(prefix) && !p.slice(prefix.length).includes('/'));
    return { dir, entries: children.map((p) => this.entry(p)!) };
  }

  async make_dir(id: string, path: string) {
    this.record('make_dir', id, path);
    this.requireRunning(id);
    if (this.files.has(path)) throw apiError('bad_request', 400, `Cannot make ${path}: File exists`);
    const created = !this.dirs.has(path);
    this.addParents(`${path}/x`);
    return { path, created };
  }

  async stat_file(id: string, path: string) {
    this.record('stat_file', id, path);
    this.requireRunning(id);
    const entry = this.entry(path);
    if (!entry) throw apiError('not_found', 404);
    return entry;
  }

  async file_exists(id: string, path: string) {
    this.record('file_exists', id, path);
    this.requireRunning(id);
    return this.entry(path) !== undefined;
  }

  async delete_file(id: string, path: string, options: { recursive?: boolean } = {}) {
    this.record('delete_file', id, path, options);
    this.requireRunning(id);
    if (this.dirs.has(path)) {
      const inside = [...this.dirs, ...this.files.keys()].filter((p) => p.startsWith(`${path}/`));
      if (inside.length > 0 && !options.recursive) throw apiError('conflict', 409, 'Folder is not empty');
      for (const p of inside) {
        this.dirs.delete(p);
        this.files.delete(p);
      }
      this.dirs.delete(path);
      return { path, deleted: true };
    }
    return { path, deleted: this.files.delete(path) };
  }

  // Previews, snapshots, computer --------------------------------------------

  async expose_port(id: string, port: number, request: { public?: boolean } = {}) {
    this.record('expose_port', id, port, request);
    this.requireRunning(id);
    return { url: `https://slug-${port}.groundbox.page/`, public: request.public ?? false, share_url: 'https://share' };
  }

  async keep_snapshot(request: { sandbox_id: string; label?: string }) {
    this.record('keep_snapshot', request);
    const snapshot = { id: `snap_${this.nextId++}`, label: request.label ?? null, status: 'ready' as const, created_at: this.now() };
    this.snapshots.push(snapshot);
    return { ...snapshot, error: null };
  }

  async list_snapshots(query?: unknown) {
    this.record('list_snapshots', query);
    return { data: [...this.snapshots].reverse(), next_cursor: null };
  }

  async delete_snapshot(id: string) {
    this.record('delete_snapshot', id);
    this.snapshots = this.snapshots.filter((s) => s.id !== id);
    return { id };
  }

  async computer(id: string, action: Record<string, unknown>) {
    this.record('computer', id, action);
    this.requireRunning(id);
    return { action: action.action, duration_ms: 1, ...(action.action === 'cursor_position' && { cursor: { x: 10, y: 20 } }) };
  }

  async computer_screenshot(id: string, options?: unknown) {
    this.record('computer_screenshot', id, options);
    this.requireRunning(id);
    return { media_type: 'image/png', data: Buffer.from('PNGDATA').toString('base64'), width: 1280, height: 800, screen: { width: 1280, height: 800 } };
  }

  async create_desktop_link(id: string, request?: unknown) {
    this.record('create_desktop_link', id, request);
    return { url: 'https://live.groundbox.dev/view/abc', token: 't' };
  }
}

export { apiError };
