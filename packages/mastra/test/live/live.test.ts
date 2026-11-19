/**
 * Live tests against the Groundbox API. They run only when GROUNDBOX_API_KEY is set, create
 * real sandboxes (billed) and delete every one of them.
 *
 *   GROUNDBOX_API_KEY=gbx_... npm run test:live
 */
import { Groundbox } from '@groundbox/sdk';
import { WORKSPACE_TOOLS, Workspace, createWorkspaceTools } from '@mastra/core/workspace';
import { afterAll, describe, expect, it } from 'vitest';

import { GroundboxSandbox } from '../../src/index.js';

const live = !!process.env.GROUNDBOX_API_KEY;
const run = `mastra-live-${Date.now().toString(36)}`;
const created: GroundboxSandbox[] = [];

function track(sandbox: GroundboxSandbox): GroundboxSandbox {
  created.push(sandbox);
  return sandbox;
}

afterAll(async () => {
  if (!live) return;
  const client = new Groundbox();
  for (const sandbox of created) await sandbox._destroy().catch(() => undefined);
  // Anything this run left behind, found by its metadata.
  const left = await client.list_sandboxes({ live: true, metadata: { 'mastra-live-run': run } });
  for (const sandbox of left.data) await client.delete_sandbox(sandbox.id).catch(() => undefined);
}, 120_000);

describe.skipIf(!live)('GroundboxSandbox against Groundbox', () => {
  const sandbox = track(
    new GroundboxSandbox({
      id: `${run}-main`,
      timeoutMinutes: 20,
      metadata: { 'mastra-live-run': run },
      env: { FROM_OPTIONS: 'yes' },
      publicPreviews: true,
    }),
  );
  const workspace = new Workspace({ sandbox, filesystem: sandbox.filesystem });

  it('starts through Workspace.init and runs commands', async () => {
    await workspace.init();
    expect(sandbox.status).toBe('running');
    expect(sandbox.sandboxId).toMatch(/^sbx_/);
    const who = await sandbox.executeCommand('sh', ['-c', 'whoami; pwd; echo $FROM_OPTIONS; sudo -n true && echo sudo-ok']);
    expect(who.exitCode).toBe(0);
    expect(who.stdout.split('\n')).toEqual(['runner', '/home/runner', 'yes', 'sudo-ok', '']);
    sandbox.setEnv((env) => ({ ...env, OVERLAY: 'o v' }));
    const quoted = await sandbox.executeCommand('printf', ['%s|', 'a b', "it's", '$HOME', '$OVERLAY']);
    expect(quoted.stdout).toBe("a b|it's|$HOME|$OVERLAY|");
    const overlay = await sandbox.executeCommand('echo "$OVERLAY"');
    expect(overlay.stdout).toBe('o v\n');
    const failed = await sandbox.executeCommand('echo bad >&2; exit 4');
    expect(failed).toMatchObject({ success: false, exitCode: 4, stderr: 'bad\n' });
  }, 120_000);

  it('times a command out', async () => {
    const result = await sandbox.executeCommand('sleep 20', [], { timeout: 2000 });
    expect(result.timedOut).toBe(true);
    expect(result.success).toBe(false);
    expect(result.executionTimeMs).toBeLessThan(10_000);
  }, 60_000);

  it('streams output to callbacks', async () => {
    const chunks: string[] = [];
    const result = await sandbox.executeCommand('for i in 1 2 3; do echo line$i; sleep 0.3; done; echo err >&2; exit 2', [], {
      onStdout: (data) => chunks.push(data),
    });
    expect(chunks.join('')).toBe('line1\nline2\nline3\n');
    expect(chunks.length).toBeGreaterThan(1);
    expect(result).toMatchObject({ exitCode: 2, stdout: 'line1\nline2\nline3\n', stderr: 'err\n' });
  }, 60_000);

  it('runs background processes with stdin, list and kill', async () => {
    const cat = await sandbox.processes.spawn('cat');
    await cat.sendStdin('hello\n');
    await cat.closeStdin();
    expect(await cat.wait()).toMatchObject({ exitCode: 0, stdout: 'hello\n' });

    const sleeper = await sandbox.processes.spawn('sleep 300', { stdinMode: 'ignore' });
    expect(sleeper.pid).toMatch(/^ex_/);
    const listed = await sandbox.processes.list();
    expect(listed.find((p) => p.pid === sleeper.pid)?.running).toBe(true);
    expect(await sandbox.processes.kill(sleeper.pid)).toBe(true);
    expect(sleeper.exitCode).toBe(137);
  }, 60_000);

  it("serves Mastra's own workspace tools (what an agent calls)", async () => {
    const tools = await createWorkspaceTools(workspace);
    const exec = tools[WORKSPACE_TOOLS.SANDBOX.EXECUTE_COMMAND];
    const write = tools[WORKSPACE_TOOLS.FILESYSTEM.WRITE_FILE];
    const read = tools[WORKSPACE_TOOLS.FILESYSTEM.READ_FILE];
    const list = tools[WORKSPACE_TOOLS.FILESYSTEM.LIST_FILES];
    const output = tools[WORKSPACE_TOOLS.SANDBOX.GET_PROCESS_OUTPUT];
    const kill = tools[WORKSPACE_TOOLS.SANDBOX.KILL_PROCESS];
    expect(exec && write && read && list && output && kill).toBeTruthy();
    const context = { workspace };

    const wrote = await write.execute({ path: '/home/runner/tooltest/hello.py', content: 'print(6 * 7)\n' }, context);
    expect(String(wrote)).toMatch(/hello\.py/);
    const ran = await exec.execute({ command: 'python3 /home/runner/tooltest/hello.py' }, context);
    expect(String(ran)).toContain('42');
    const readBack = await read.execute({ path: '/home/runner/tooltest/hello.py' }, context);
    expect(JSON.stringify(readBack)).toContain('print(6 * 7)');
    const listing = await list.execute({ path: '/home/runner/tooltest' }, context);
    expect(JSON.stringify(listing)).toContain('hello.py');

    const started = String(await exec.execute({ command: 'echo started; sleep 300', background: true }, context));
    const pid = /PID: (\S+)\)/.exec(started)?.[1];
    expect(pid).toMatch(/^ex_/);
    await new Promise((r) => setTimeout(r, 1500));
    expect(JSON.stringify(await output.execute({ pid }, context))).toContain('started');
    expect(JSON.stringify(await kill.execute({ pid }, context))).toMatch(/kill|stop|terminat/i);
  }, 120_000);

  it('works the filesystem', async () => {
    const fs = sandbox.filesystem;
    await fs.mkdir('/home/runner/fs/sub');
    await fs.writeFile('/home/runner/fs/a.txt', 'alpha');
    await fs.writeFile('fs/sub/b.ts', 'export const b = 1;\n');
    await fs.appendFile('/home/runner/fs/a.txt', '-beta');
    expect(await fs.readFile('/home/runner/fs/a.txt', { encoding: 'utf-8' })).toBe('alpha-beta');
    await fs.copyFile('/home/runner/fs/a.txt', '/home/runner/fs/copy.txt');
    await fs.moveFile('/home/runner/fs/copy.txt', '/home/runner/fs/moved.txt');
    await expect(fs.copyFile('/home/runner/fs/a.txt', '/home/runner/fs/moved.txt', { overwrite: false })).rejects.toThrow(/exists/i);
    await sandbox.executeCommand('ln -s /home/runner/fs/sub /home/runner/fs/link; ln -s /nowhere /home/runner/fs/broken');
    const entries = await fs.readdir('/home/runner/fs');
    const byName = Object.fromEntries(entries.map((e) => [e.name, e]));
    expect(byName['a.txt']).toEqual({ name: 'a.txt', type: 'file', size: 10 });
    expect(byName['sub']).toEqual({ name: 'sub', type: 'directory' });
    expect(byName['link']).toMatchObject({ type: 'directory', isSymlink: true, symlinkTarget: '/home/runner/fs/sub' });
    expect(byName['broken']).toBeUndefined();
    expect(byName['moved.txt']).toBeDefined();
    const deep = await fs.readdir('/home/runner/fs', { recursive: true, extension: 'ts' });
    expect(deep.map((e) => e.name)).toContain('sub/b.ts');
    const stat = await fs.stat('/home/runner/fs/link');
    expect(stat.type).toBe('directory');
    await expect(fs.rmdir('/home/runner/fs')).rejects.toThrow(/not empty/i);
    await fs.rmdir('/home/runner/fs', { recursive: true });
    expect(await fs.exists('/home/runner/fs')).toBe(false);
    await expect(fs.readFile('/home/runner/fs/a.txt')).rejects.toThrow(/not found/i);
  }, 120_000);

  it('writes many files at once, keeping modes', async () => {
    await sandbox.writeFiles([
      { path: '/home/runner/bulk/run.sh', content: '#!/bin/sh\necho ran\n', mode: 0o755 },
      { path: 'bulk/data.bin', content: Buffer.from([0, 1, 2, 255]) },
    ]);
    const check = await sandbox.executeCommand('/home/runner/bulk/run.sh && stat -c %a /home/runner/bulk/run.sh && od -An -tu1 /home/runner/bulk/data.bin');
    expect(check.stdout.replace(/\s+/g, ' ').trim()).toBe('ran 755 0 1 2 255');
  }, 60_000);

  it('gives a working preview URL for a port', async () => {
    await sandbox.processes.spawn('mkdir -p /tmp/site && echo groundbox-preview-ok > /tmp/site/index.html && cd /tmp/site && python3 -m http.server 8000 --bind 0.0.0.0', {
      stdinMode: 'ignore',
    });
    await sandbox.executeCommand('for i in $(seq 50); do curl -sf localhost:8000 >/dev/null && exit 0; sleep 0.2; done; exit 1');
    const url = await sandbox.networking.getPortUrl(8000);
    expect(url).toMatch(/^https:\/\/.+-8000\.groundbox\.page\/?$/);
    // publicPreviews: anyone can open it (private previews ask for a Groundbox sign-in).
    let body = '';
    for (let i = 0; i < 20 && !body.includes('groundbox-preview-ok'); i++) {
      const response = await fetch(url!).catch(() => undefined);
      body = response?.ok ? await response.text() : '';
      if (!body) await new Promise((r) => setTimeout(r, 500));
    }
    expect(body).toContain('groundbox-preview-ok');
  }, 60_000);

  it('pauses on stop and reconnects from another instance with the same id', async () => {
    await sandbox.executeCommand('echo kept > /home/runner/marker; (sleep 600 &) ');
    const physical = sandbox.sandboxId;
    await sandbox._stop();
    const groundbox = new Groundbox();
    expect((await groundbox.get_sandbox(physical!)).status).toBe('paused');

    const again = track(new GroundboxSandbox({ id: `${run}-main`, metadata: { 'mastra-live-run': run } }));
    expect(await again.start()).toEqual({ outcome: 'connected' });
    expect(again.sandboxId).toBe(physical);
    const after = await again.executeCommand('cat /home/runner/marker; pgrep -f "sleep 600" >/dev/null && echo still-running');
    expect(after.stdout).toBe('kept\nstill-running\n');
  }, 180_000);

  it('deletes the sandbox on destroy', async () => {
    const physical = sandbox.sandboxId ?? created.at(-1)!.sandboxId;
    for (const s of created) await s._destroy();
    const after = await new Groundbox().get_sandbox(physical!);
    expect(after.status).toBe('stopped');
    expect(after.stop_reason).toBe('deleted');
  }, 60_000);
});

describe.skipIf(!live)('GroundboxSandbox from a Docker image', () => {
  it('runs as root in the image home', async () => {
    const sandbox = track(new GroundboxSandbox({ id: `${run}-image`, image: 'python:3.12-slim', timeoutMinutes: 15, metadata: { 'mastra-live-run': run } }));
    await sandbox.start();
    expect(sandbox.workingDirectory).toBe('/root');
    const result = await sandbox.executeCommand('whoami && python -V');
    expect(result.stdout).toMatch(/^root\nPython 3\.12/);
    await sandbox.filesystem.writeFile('/srv/app/main.py', 'print("hi")\n');
    expect((await sandbox.executeCommand('python /srv/app/main.py')).stdout).toBe('hi\n');
    await sandbox._destroy();
  }, 900_000);
});

describe.skipIf(!live)('GroundboxSandbox with a desktop', () => {
  it('drives the desktop through the computer capability', async () => {
    const sandbox = track(new GroundboxSandbox({ id: `${run}-desktop`, desktop: true, timeoutMinutes: 15, metadata: { 'mastra-live-run': run } }));
    const computer = sandbox.computer!;
    const shot = await computer.screenshot();
    expect(Buffer.from(shot.data.subarray(0, 8)).toString('hex')).toBe('89504e470d0a1a0a');
    const size = await computer.getScreenSize();
    expect(size.width).toBeGreaterThan(0);
    await computer.moveMouse(100, 120);
    expect(await computer.getCursorPosition()).toEqual({ x: 100, y: 120 });
    await computer.press(['ctrl', 'l']);
    await computer.type('about:blank');
    await computer.press('Enter');
    const view = await computer.streamUrl!();
    expect(view).toMatch(/^https:\/\//);
    await sandbox._destroy();
  }, 600_000);
});

describe.skipIf(!live)('GroundboxSandbox checkpoints', () => {
  it('keeps a checkpoint and boots a clone from it', async () => {
    const checkpointName = `${run}-cp`;
    const first = track(new GroundboxSandbox({ id: `${run}-cp1`, checkpointName, timeoutMinutes: 15, metadata: { 'mastra-live-run': run } }));
    await first.executeCommand('echo from-checkpoint > /home/runner/state.txt');
    await first.snapshot();
    await first._destroy();
    const second = track(first.clone({ id: `${run}-cp2`, checkpointName }));
    await second.start();
    expect((await second.executeCommand('cat /home/runner/state.txt')).stdout).toBe('from-checkpoint\n');
    await second._destroy();
    // Clean up the kept snapshot.
    const client = new Groundbox();
    const snapshots = await client.list_snapshots({ limit: 100 });
    for (const s of snapshots.data.filter((s) => s.label === `mastra:${checkpointName}`)) await client.delete_snapshot(s.id).catch(() => undefined);
  }, 600_000);
});

describe.skipIf(!live)('GroundboxSandbox when its sandbox goes away', () => {
  it('starts a new one and runs the command there', async () => {
    const sandbox = track(new GroundboxSandbox({ id: `${run}-revive`, timeoutMinutes: 15, metadata: { 'mastra-live-run': run } }));
    await sandbox.start();
    const first = sandbox.sandboxId!;
    await new Groundbox().delete_sandbox(first);
    const result = await sandbox.executeCommand('echo revived');
    expect(result.stdout).toBe('revived\n');
    expect(sandbox.sandboxId).toMatch(/^sbx_/);
    expect(sandbox.sandboxId).not.toBe(first);
    await sandbox._destroy();
  }, 120_000);
});
