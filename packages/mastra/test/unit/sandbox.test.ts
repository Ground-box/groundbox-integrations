import {
  DirectoryNotEmptyError,
  DirectoryNotFoundError,
  FileExistsError,
  FileNotFoundError,
  IsDirectoryError,
  NotDirectoryError,
  Workspace,
} from '@mastra/core/workspace';
import { describe, expect, it } from 'vitest';

import { GroundboxSandbox, MASTRA_ID_METADATA_KEY, groundboxSandboxProvider, toKeyCombo } from '../../src/index.js';
import { FakeGroundbox, apiError, fakeSandbox } from './fake-groundbox.js';

function setup(options: ConstructorParameters<typeof GroundboxSandbox>[0] = {}) {
  const fake = new FakeGroundbox();
  const sandbox = new GroundboxSandbox({ id: 'logical-1', client: fake.asClient(), ...options });
  return { fake, sandbox };
}

describe('lifecycle', () => {
  it('creates a sandbox tagged with its logical id on first start', async () => {
    const { fake, sandbox } = setup({ cpus: 4, env: { A: '1' }, allowHosts: ['github.com'], timeoutMinutes: 30, idleTimeoutSeconds: 120 });
    const result = await sandbox.start();
    expect(result).toEqual({ outcome: 'created' });
    expect(sandbox.status).toBe('running');
    expect(sandbox.sandboxId).toBe('sbx_fake1');
    expect(sandbox.workingDirectory).toBe('/home/runner');
    const [spec, opts] = fake.callsOf('create_sandbox')[0]!;
    expect(spec).toMatchObject({
      cpus: 4,
      env: { A: '1' },
      allow_hosts: ['github.com'],
      timeout_minutes: 30,
      idle_timeout_seconds: 120,
      metadata: { [MASTRA_ID_METADATA_KEY]: 'logical-1' },
    });
    expect((opts as { idempotencyKey: string }).idempotencyKey).toMatch(/^[0-9a-f-]{36}$/);
  });

  it('reconnects to a live sandbox for the same logical id, resuming it when paused', async () => {
    const fake = new FakeGroundbox();
    fake.sandboxes.set('sbx_old', fakeSandbox({ id: 'sbx_old', status: 'paused', metadata: { [MASTRA_ID_METADATA_KEY]: 'logical-1' } }));
    const sandbox = new GroundboxSandbox({ id: 'logical-1', client: fake.asClient() });
    expect(await sandbox.start()).toEqual({ outcome: 'connected' });
    expect(sandbox.sandboxId).toBe('sbx_old');
    expect(fake.callsOf('resume_sandbox')).toEqual([['sbx_old']]);
    expect(fake.callsOf('create_sandbox')).toHaveLength(0);
  });

  it('starts a shut-down sandbox and reattaches by sandboxId', async () => {
    const fake = new FakeGroundbox();
    fake.sandboxes.set('sbx_kept', fakeSandbox({ id: 'sbx_kept', status: 'shut_down' }));
    const sandbox = new GroundboxSandbox({ id: 'other', sandboxId: 'sbx_kept', client: fake.asClient() });
    expect(await sandbox.start()).toEqual({ outcome: 'connected' });
    expect(fake.callsOf('start_sandbox')).toEqual([['sbx_kept']]);
  });

  it('creates a new sandbox when the remembered one has ended', async () => {
    const fake = new FakeGroundbox();
    fake.sandboxes.set('sbx_gone', fakeSandbox({ id: 'sbx_gone', status: 'stopped' }));
    const sandbox = new GroundboxSandbox({ sandboxId: 'sbx_gone', client: fake.asClient() });
    expect(await sandbox.start()).toEqual({ outcome: 'created' });
    expect(sandbox.sandboxId).not.toBe('sbx_gone');
  });

  it('pauses on stop, resumes on the next start, deletes on destroy', async () => {
    const { fake, sandbox } = setup();
    await sandbox.start();
    await sandbox._stop();
    expect(sandbox.status).toBe('stopped');
    expect(fake.sandboxes.get('sbx_fake1')!.status).toBe('paused');
    await sandbox.executeCommand('true');
    expect(fake.callsOf('resume_sandbox')).toEqual([['sbx_fake1']]);
    await sandbox._destroy();
    expect(fake.callsOf('delete_sandbox')).toEqual([['sbx_fake1']]);
    expect(sandbox.status).toBe('destroyed');
  });

  it('shuts down instead of pausing a sandbox with volumes', async () => {
    const { fake, sandbox } = setup({ volumes: [{ volume: 'data', path: '/data' }] });
    await sandbox.start();
    expect(fake.callsOf('create_sandbox')[0]![0]).toMatchObject({ volumes: [{ volume: 'data', path: '/data', read_only: false }] });
    await sandbox._stop();
    expect(fake.callsOf('stop_sandbox')).toEqual([['sbx_fake1']]);
  });

  it('starts again by itself when the sandbox ended under it', async () => {
    const { fake, sandbox } = setup();
    await sandbox.start();
    fake.sandboxes.get('sbx_fake1')!.status = 'stopped';
    fake.onRun = () => ({ exit_code: 0, stdout: 'ok\n', stderr: '' });
    const result = await sandbox.executeCommand('echo ok');
    expect(result.stdout).toBe('ok\n');
    expect(sandbox.sandboxId).toBe('sbx_fake2');
    expect(fake.callsOf('create_sandbox')).toHaveLength(2);
  });

  it('does not treat a missing file as a dead sandbox', async () => {
    const { fake, sandbox } = setup();
    await sandbox.start();
    await expect(sandbox.filesystem.readFile('/nope')).rejects.toBeInstanceOf(FileNotFoundError);
    expect(fake.callsOf('create_sandbox')).toHaveLength(1);
  });

  it('boots from a kept checkpoint and replaces it on snapshot()', async () => {
    const { fake, sandbox } = setup({ checkpointName: 'proj-a' });
    fake.snapshots.push({ id: 'snap_old', label: 'mastra:proj-a', status: 'ready', created_at: '2026-11-18T00:00:00.000Z' });
    await sandbox.start();
    expect(fake.callsOf('create_sandbox')[0]![0]).toMatchObject({ snapshot_id: 'snap_old' });
    expect(fake.callsOf('create_sandbox')[0]![0]).not.toHaveProperty('cpus');
    expect(sandbox.supportsCheckpoints).toBe(true);
    await sandbox.snapshot();
    expect(fake.callsOf('keep_snapshot')[0]![0]).toEqual({ sandbox_id: 'sbx_fake1', label: 'mastra:proj-a' });
    expect(fake.snapshots.map((s) => s.id)).not.toContain('snap_old');
  });

  it('clones settings with per-instance overrides and no I/O', async () => {
    const { fake, sandbox } = setup({ cpus: 8, image: 'python:3.12-slim' });
    const sibling = sandbox.clone({ id: 'logical-2', env: { B: '2' }, idleTimeoutMinutes: 5 });
    expect(fake.calls).toHaveLength(0);
    expect(sibling.id).toBe('logical-2');
    await sibling.start();
    expect(fake.callsOf('create_sandbox')[0]![0]).toMatchObject({
      cpus: 8,
      image: 'python:3.12-slim',
      env: { B: '2' },
      idle_timeout_seconds: 300,
      metadata: { [MASTRA_ID_METADATA_KEY]: 'logical-2' },
    });
  });

  it('reports info and instructions', async () => {
    const { sandbox } = setup({ allowHosts: ['github.com'] });
    await sandbox.start();
    const info = await sandbox.getInfo();
    expect(info).toMatchObject({ provider: 'groundbox', status: 'running', resources: { cpuCores: 2, memoryMB: 4096, diskMB: 20480 } });
    expect(info.metadata).toMatchObject({ sandboxId: 'sbx_fake1' });
    const instructions = sandbox.getInstructions();
    expect(instructions).toContain('runner');
    expect(instructions).toContain('github.com');
    const custom = new GroundboxSandbox({ client: new FakeGroundbox().asClient(), instructions: ({ defaultInstructions }) => `${defaultInstructions} Extra.` });
    expect(custom.getInstructions()).toMatch(/Extra\.$/);
  });
});

describe('commands', () => {
  it('runs a command in one call with quoted args, the env overlay, cwd and timeout', async () => {
    const { fake, sandbox } = setup({ env: { BASE: 'x' }, timeout: 90_500 });
    fake.onRun = () => ({ exit_code: 3, stdout: 'out', stderr: 'err' });
    sandbox.setEnv((env) => ({ ...env, TOKEN: 't' }));
    const result = await sandbox.executeCommand('echo', ['a b', "it's"], { cwd: '/tmp', env: { EXTRA: 'y' } });
    const [, cmd, opts] = fake.callsOf('run_command').at(-1)!;
    expect(cmd).toBe(`echo 'a b' 'it'\\''s'`);
    expect(opts).toEqual({ cwd: '/tmp', env: { BASE: 'x', TOKEN: 't', EXTRA: 'y' }, timeout_s: 91 });
    expect(result).toMatchObject({ success: false, exitCode: 3, stdout: 'out', stderr: 'err', command: `echo 'a b' 'it'\\''s'` });
  });

  it('maps a timeout and keeps only the newest output', async () => {
    const { fake, sandbox } = setup();
    fake.onRun = () => ({ exit_code: 137, stdout: 'abcdefghij', stderr: '', timed_out: true });
    const result = await sandbox.executeCommand('sleep 9', [], { timeout: 1000, maxRetainedBytes: 4 });
    expect(result).toMatchObject({ success: false, timedOut: true, killed: true, stdout: 'ghij', stdoutTruncated: true, stdoutDroppedBytes: 6 });
  });

  it('streams output through a background command when callbacks are given', async () => {
    const { fake, sandbox } = setup();
    fake.onStart = async (io) => {
      io.stdout('one\n');
      io.stderr('warn\n');
      io.stdout('two\n');
      return 5;
    };
    const chunks: string[] = [];
    const result = await sandbox.executeCommand('make', ['all'], { onStdout: (d) => chunks.push(d) });
    expect(chunks).toEqual(['one\n', 'two\n']);
    expect(result).toMatchObject({ exitCode: 5, success: false, stdout: 'one\ntwo\n', stderr: 'warn\n', command: 'make all' });
    expect(fake.callsOf('start_command')[0]![2]).toMatchObject({ stdin: false, timeout_s: 300, cwd: '/home/runner' });
  });

  it('spawns a process that takes stdin, and kills one', async () => {
    const { fake, sandbox } = setup();
    fake.onStart = async (io, request) => {
      if (request.cmd === 'cat') {
        for (let line = await io.readStdin(); line !== null; line = await io.readStdin()) io.stdout(`got:${line}`);
        return 0;
      }
      await io.killed;
      return 137;
    };
    await sandbox.start();
    const cat = await sandbox.processes.spawn('cat');
    await cat.sendStdin('hello');
    await cat.closeStdin();
    const done = await cat.wait();
    expect(done).toMatchObject({ exitCode: 0, stdout: 'got:hello' });

    const server = await sandbox.processes.spawn('sleep 100', { stdinMode: 'ignore' });
    expect((await sandbox.processes.list()).find((p) => p.pid === server.pid)?.running).toBe(true);
    await expect(server.sendStdin('x')).rejects.toThrow(/without stdin/);
    expect(await sandbox.processes.kill(server.pid)).toBe(true);
    expect(fake.callsOf('kill_command').at(-1)).toEqual(['sbx_fake1', server.pid]);
  });

  it('picks up a command started elsewhere by its id', async () => {
    const { fake, sandbox } = setup();
    fake.onStart = async (io) => {
      io.stdout('from before\n');
      return 0;
    };
    await sandbox.start();
    const started = await fake.start_command('sbx_fake1', 'echo from before');
    await new Promise((r) => setTimeout(r, 5));
    const handle = await sandbox.processes.get(started.id);
    expect(handle).toBeDefined();
    expect(await handle!.wait()).toMatchObject({ exitCode: 0, stdout: 'from before\n' });
  });
});

describe('files', () => {
  it('writes many files in one upload, keeping modes and resolving relative paths', async () => {
    const { fake, sandbox } = setup();
    await sandbox.start();
    await sandbox.writeFiles([
      { path: '/app/index.mjs', content: 'console.log(1)' },
      { path: 'bin/run.sh', content: Buffer.from('#!/bin/sh\n'), mode: 0o755 },
    ]);
    const [, dir, files] = fake.callsOf('upload_files')[0]!;
    expect(dir).toBe('/');
    expect(files).toEqual([
      { path: 'app/index.mjs', content: 'console.log(1)' },
      { path: 'home/runner/bin/run.sh', content: new Uint8Array(Buffer.from('#!/bin/sh\n')), mode: 0o755 },
    ]);
    await expect(sandbox.writeFiles([{ path: '/x', content: '', mode: 0 }])).rejects.toThrow();
  });

  it('maps the files API onto the Mastra filesystem', async () => {
    const { sandbox } = setup();
    const fs = sandbox.filesystem;
    await fs.writeFile('/home/runner/a/b.txt', 'hello');
    expect(await fs.readFile('a/b.txt', { encoding: 'utf-8' })).toBe('hello');
    expect(await fs.exists('/home/runner/a/b.txt')).toBe(true);
    await expect(fs.writeFile('/home/runner/a/b.txt', 'x', { overwrite: false })).rejects.toBeInstanceOf(FileExistsError);
    await expect(fs.writeFile('/nowhere/x.txt', 'x', { recursive: false })).rejects.toBeInstanceOf(DirectoryNotFoundError);
    await expect(fs.readFile('/home/runner/a')).rejects.toBeInstanceOf(IsDirectoryError);
    await expect(fs.deleteFile('/home/runner/a')).rejects.toBeInstanceOf(IsDirectoryError);
    await expect(fs.rmdir('/home/runner/a')).rejects.toBeInstanceOf(DirectoryNotEmptyError);
    await expect(fs.rmdir('/home/runner/a/b.txt')).rejects.toBeInstanceOf(NotDirectoryError);
    await expect(fs.readdir('/missing')).rejects.toBeInstanceOf(DirectoryNotFoundError);

    await fs.mkdir('/home/runner/a/c');
    await fs.writeFile('/home/runner/a/c/d.ts', 'export {}');
    const byName = (a: { name: string }, b: { name: string }) => a.name.localeCompare(b.name);
    expect((await fs.readdir('/home/runner/a')).sort(byName)).toEqual([
      { name: 'b.txt', type: 'file', size: 5 },
      { name: 'c', type: 'directory' },
    ]);
    expect((await fs.readdir('/home/runner/a', { recursive: true, extension: '.ts' })).sort(byName)).toEqual([
      { name: 'c', type: 'directory' },
      { name: 'c/d.ts', type: 'file', size: 9 },
    ]);
    const stat = await fs.stat('/home/runner/a/b.txt');
    expect(stat).toMatchObject({ name: 'b.txt', path: '/home/runner/a/b.txt', type: 'file', size: 5 });

    await fs.deleteFile('/home/runner/a/b.txt');
    await fs.deleteFile('/home/runner/a/b.txt', { force: true });
    await expect(fs.deleteFile('/home/runner/a/b.txt')).rejects.toBeInstanceOf(FileNotFoundError);
    await fs.rmdir('/home/runner/a', { recursive: true });
    expect(await fs.exists('/home/runner/a')).toBe(false);
  });

  it('runs copy, move and append as one sh command with the paths as arguments', async () => {
    const { fake, sandbox } = setup();
    const seen: unknown[] = [];
    fake.onRun = (cmd, options) => {
      seen.push([cmd, options]);
      return { exit_code: 0, stdout: '', stderr: '' };
    };
    await sandbox.filesystem.copyFile('/a', '/b; rm -rf x', { overwrite: false });
    await sandbox.filesystem.moveFile('/a', '/c');
    await sandbox.filesystem.appendFile('/log.txt', 'line\n');
    const [copy, move, append] = seen as Array<[string[], Record<string, unknown>]>;
    expect(copy![0].slice(0, 2)).toEqual(['sh', '-c']);
    expect(copy![0].slice(3)).toEqual(['sh', '/a', '/b; rm -rf x', '0', '0']);
    expect(move![0].slice(3)).toEqual(['sh', '/a', '/c', '1']);
    expect(append![0].slice(3)).toEqual(['sh', '/log.txt']);
    expect(Buffer.from(append![1].stdin as Uint8Array).toString()).toBe('line\n');

    fake.onRun = () => ({ exit_code: 22, stdout: '', stderr: '' });
    await expect(sandbox.filesystem.copyFile('/a', '/b', { overwrite: false })).rejects.toBeInstanceOf(FileExistsError);
    fake.onRun = () => ({ exit_code: 20, stdout: '', stderr: '' });
    await expect(sandbox.filesystem.moveFile('/a', '/b')).rejects.toBeInstanceOf(FileNotFoundError);
  });
});

describe('networking and computer', () => {
  it('gives a preview URL for a port', async () => {
    const { fake, sandbox } = setup({ publicPreviews: true });
    await sandbox.start();
    expect(await sandbox.networking.getPortUrl(3000)).toBe('https://slug-3000.groundbox.page/');
    expect(fake.callsOf('expose_port')[0]).toEqual(['sbx_fake1', 3000, { public: true }]);
    fake.failNext.set('expose_port', apiError('forbidden', 403));
    expect(await sandbox.networking.getPortUrl(3000)).toBeNull();
  });

  it('exposes computer use only with a desktop', async () => {
    expect(setup().sandbox.computer).toBeUndefined();
    const { fake, sandbox } = setup({ desktop: { width: 1280, height: 800 } });
    expect(fake.calls).toHaveLength(0);
    const computer = sandbox.computer!;
    const shot = await computer.screenshot();
    expect(Buffer.from(shot.data).toString()).toBe('PNGDATA');
    expect(fake.callsOf('create_sandbox')[0]![0]).toMatchObject({ desktop: { width: 1280, height: 800 } });
    await computer.press(['ctrl', 's']);
    await computer.press('Enter');
    await computer.drag({ x: 1, y: 2 }, { x: 3, y: 4 });
    await computer.scroll('down', 3);
    const actions = fake.callsOf('computer').map(([, action]) => action);
    expect(actions).toEqual([
      { action: 'key', text: 'ctrl+s' },
      { action: 'key', text: 'Return' },
      { action: 'left_click_drag', start_coordinate: [1, 2], coordinate: [3, 4] },
      { action: 'scroll', scroll_direction: 'down', scroll_amount: 3 },
    ]);
    expect(await computer.getCursorPosition()).toEqual({ x: 10, y: 20 });
    expect(await computer.getScreenSize()).toEqual({ width: 1280, height: 800 });
    expect(await computer.streamUrl!()).toBe('https://live.groundbox.dev/view/abc');
  });

  it('maps key names', () => {
    expect(toKeyCombo(['Control', 'Shift', 'T'])).toBe('ctrl+shift+T');
    expect(toKeyCombo('ArrowUp')).toBe('Up');
    expect(toKeyCombo('f5')).toBe('F5');
  });
});

describe('Mastra integration', () => {
  it('works as a Workspace sandbox', async () => {
    const fake = new FakeGroundbox();
    fake.onRun = (cmd) => ({ exit_code: 0, stdout: `ran ${String(cmd)}`, stderr: '' });
    const sandbox = new GroundboxSandbox({ client: fake.asClient() });
    const workspace = new Workspace({ sandbox });
    await workspace.init();
    expect(sandbox.status).toBe('running');
    const result = await workspace.sandbox!.executeCommand!('uname', ['-a']);
    expect(result.stdout).toBe('ran uname -a');
    await workspace.destroy();
    expect(fake.callsOf('delete_sandbox')).toHaveLength(1);
  });

  it('registers with the editor', () => {
    expect(groundboxSandboxProvider.id).toBe('groundbox');
    const sandbox = groundboxSandboxProvider.createSandbox({ cpus: 2, apiKey: 'gbx_test' });
    expect(sandbox).toBeInstanceOf(GroundboxSandbox);
  });
});
