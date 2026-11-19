/**
 * The disk of a Groundbox sandbox as a Mastra filesystem.
 *
 * Reads, writes, listings, stat, mkdir and deletes go through Groundbox's files API, which works
 * on any image (Groundbox adds its own tools under /opt/groundbox). Copy, move and append run as
 * one `sh` command in the sandbox, with the paths passed as arguments (never interpolated), so
 * file bytes do not round-trip through the host.
 *
 * Paths are absolute inside the sandbox. A relative path is resolved against the sandbox's
 * working directory (the home folder unless `workingDirectory` is set). Nothing is confined to a
 * root: the agent can reach every path the sandbox user can.
 */
import type { FileEntry as GroundboxFileEntry } from '@groundbox/sdk';
import {
  DirectoryNotEmptyError,
  DirectoryNotFoundError,
  FileExistsError,
  FileNotFoundError,
  IsDirectoryError,
  MastraFilesystem,
  NotDirectoryError,
  StaleFileError,
} from '@mastra/core/workspace';
import type {
  CopyOptions,
  FileContent,
  FileEntry,
  FileStat,
  ListOptions,
  ProviderStatus,
  ReadOptions,
  RemoveOptions,
  WriteOptions,
} from '@mastra/core/workspace';
import { posix } from 'node:path';

import type { GroundboxSandbox } from './sandbox.js';
import { errorCode, errorMessage, toAbsolute, toBytes } from './utils.js';

/** Exit codes of the in-sandbox file scripts. */
const EXIT = { notFound: 20, isDirectory: 21, exists: 22, notDirectory: 23, destIsDirectory: 24 } as const;

const DEFAULT_MAX_DEPTH = 100;
const LIST_CONCURRENCY = 8;

/** Copy `$1` to `$2`. `$3`: 1 to copy a directory's contents. `$4`: 0 to refuse an existing `$2`. */
const COPY_SCRIPT = `src=$1 dest=$2 recursive=$3 overwrite=$4
[ -e "$src" ] || exit ${EXIT.notFound}
if [ -d "$src" ]; then
  [ "$recursive" = 1 ] || exit ${EXIT.isDirectory}
  if [ "$overwrite" = 0 ] && { [ -e "$dest" ] || [ -L "$dest" ]; }; then exit ${EXIT.exists}; fi
  mkdir -p "$dest" && cp -R -P "$src"/. "$dest"/
  exit $?
fi
if [ -e "$dest" ] || [ -L "$dest" ]; then
  [ "$overwrite" = 0 ] && exit ${EXIT.exists}
  [ -d "$dest" ] && exit ${EXIT.destIsDirectory}
fi
mkdir -p "$(dirname "$dest")" && cp "$src" "$dest"`;

/** Move `$1` to `$2`. `$3`: 0 to refuse an existing `$2`. */
const MOVE_SCRIPT = `src=$1 dest=$2 overwrite=$3
{ [ -e "$src" ] || [ -L "$src" ]; } || exit ${EXIT.notFound}
if [ -e "$dest" ] || [ -L "$dest" ]; then
  [ "$overwrite" = 0 ] && exit ${EXIT.exists}
  if [ -d "$dest" ] && [ ! -L "$dest" ]; then exit ${EXIT.destIsDirectory}; fi
fi
mkdir -p "$(dirname "$dest")" && mv -f "$src" "$dest"`;

/** Append stdin to `$1`, making it and its parent when missing. */
const APPEND_SCRIPT = `[ -d "$1" ] && exit ${EXIT.isDirectory}
mkdir -p "$(dirname "$1")" && cat >> "$1"`;

/** For each argument: its target's type (d, f or x when broken) and the link's target. */
const RESOLVE_LINKS_SCRIPT = `for p do
  if [ -d "$p" ]; then t=d; elif [ -e "$p" ]; then t=f; else t=x; fi
  printf '%s\\t%s\\n' "$t" "$(readlink "$p")"
done`;

/** Print the path a symlink finally points at. */
const REALPATH_SCRIPT = `readlink -f -- "$1"`;

export class GroundboxSandboxFilesystem extends MastraFilesystem {
  readonly id: string;
  readonly name = 'GroundboxSandboxFilesystem';
  readonly provider = 'groundbox';
  status: ProviderStatus = 'pending';

  constructor(private readonly sandbox: GroundboxSandbox) {
    super({ name: 'GroundboxSandboxFilesystem' });
    this.id = `${sandbox.id}-fs`;
  }

  get basePath(): string | undefined {
    return this.sandbox.workingDirectory;
  }

  getInstructions(): string {
    return `The disk of a Groundbox sandbox. Paths are absolute inside the sandbox; a relative path is resolved against ${this.sandbox.workingDirectory ?? 'the home folder'}.`;
  }

  async getCommandCwd(): Promise<string | undefined> {
    return this.sandbox.workingDirectory;
  }

  resolveAbsolutePath(path: string): string {
    return this.abs(path);
  }

  private abs(path: string): string {
    return toAbsolute(path, this.sandbox.workingDirectory ?? '/');
  }

  /** Run `fn` with the sandbox started, retrying once when it died under us. */
  private run<T>(fn: (id: string) => Promise<T>): Promise<T> {
    return this.sandbox.retryOnDead(async () => {
      await this.sandbox.ensureRunning();
      return fn(this.sandbox.requireSandboxId());
    });
  }

  private async script(script: string, args: string[], stdin?: Uint8Array): Promise<{ exitCode: number; stdout: string; stderr: string }> {
    const result = await this.run((id) =>
      this.sandbox.groundbox.run_command(id, ['sh', '-c', script, 'sh', ...args], stdin ? { stdin } : {}),
    );
    return { exitCode: result.exit_code, stdout: result.stdout, stderr: result.stderr };
  }

  /** The entry itself (a symlink is described, not followed), or undefined when nothing is there. */
  private async lstat(abs: string): Promise<GroundboxFileEntry | undefined> {
    try {
      return await this.run((id) => this.sandbox.groundbox.stat_file(id, abs));
    } catch (error) {
      if (errorCode(error) === 'not_found') return undefined;
      throw error;
    }
  }

  async readFile(path: string, options?: ReadOptions): Promise<string | Buffer> {
    const abs = this.abs(path);
    let bytes: Uint8Array;
    try {
      bytes = await this.run((id) => this.sandbox.groundbox.read_file(id, abs));
    } catch (error) {
      throw this.fileError(error, path);
    }
    const buffer = Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    return options?.encoding ? buffer.toString(options.encoding) : buffer;
  }

  async writeFile(path: string, content: FileContent, options?: WriteOptions): Promise<void> {
    const abs = this.abs(path);
    if (options?.overwrite === false || options?.expectedMtime) {
      const current = await this.lstat(abs);
      if (current && options?.overwrite === false) throw new FileExistsError(path);
      if (options?.expectedMtime && current) {
        const actual = new Date(current.modified_at);
        if (actual.getTime() !== options.expectedMtime.getTime()) throw new StaleFileError(path, options.expectedMtime, actual);
      }
    }
    if (options?.recursive === false) await this.assertParentDirectory(abs, path);
    try {
      await this.run((id) => this.sandbox.groundbox.write_file(id, abs, toBytes(content)));
    } catch (error) {
      throw this.fileError(error, path);
    }
  }

  async appendFile(path: string, content: FileContent): Promise<void> {
    const abs = this.abs(path);
    const result = await this.script(APPEND_SCRIPT, [abs], toBytes(content));
    this.check(result, 'appendFile', path, { isDirectory: () => new IsDirectoryError(path) });
  }

  async deleteFile(path: string, options?: RemoveOptions): Promise<void> {
    const abs = this.abs(path);
    const entry = await this.lstat(abs);
    if (!entry) {
      if (options?.force) return;
      throw new FileNotFoundError(path);
    }
    if (entry.type === 'dir') throw new IsDirectoryError(path);
    await this.run((id) => this.sandbox.groundbox.delete_file(id, abs));
  }

  async copyFile(src: string, dest: string, options?: CopyOptions): Promise<void> {
    const result = await this.script(COPY_SCRIPT, [
      this.abs(src),
      this.abs(dest),
      options?.recursive ? '1' : '0',
      options?.overwrite === false ? '0' : '1',
    ]);
    this.check(result, 'copyFile', `${src} -> ${dest}`, {
      notFound: () => new FileNotFoundError(src),
      isDirectory: () => new IsDirectoryError(src),
      exists: () => new FileExistsError(dest),
      destIsDirectory: () => new IsDirectoryError(dest),
    });
  }

  async moveFile(src: string, dest: string, options?: CopyOptions): Promise<void> {
    const result = await this.script(MOVE_SCRIPT, [this.abs(src), this.abs(dest), options?.overwrite === false ? '0' : '1']);
    this.check(result, 'moveFile', `${src} -> ${dest}`, {
      notFound: () => new FileNotFoundError(src),
      exists: () => new FileExistsError(dest),
      destIsDirectory: () => new IsDirectoryError(dest),
    });
  }

  async mkdir(path: string, options?: { recursive?: boolean }): Promise<void> {
    const abs = this.abs(path);
    if (options?.recursive === false) {
      await this.assertParentDirectory(abs, path);
      if (await this.lstat(abs)) throw new FileExistsError(path);
    }
    try {
      await this.run((id) => this.sandbox.groundbox.make_dir(id, abs));
    } catch (error) {
      if (errorCode(error) === 'bad_request') {
        const entry = await this.lstat(abs);
        if (entry && entry.type !== 'dir') throw new FileExistsError(path);
        throw new NotDirectoryError(posix.dirname(path));
      }
      throw error;
    }
  }

  async rmdir(path: string, options?: RemoveOptions): Promise<void> {
    const abs = this.abs(path);
    const entry = await this.lstat(abs);
    if (!entry) {
      if (options?.force) return;
      throw new DirectoryNotFoundError(path);
    }
    if (entry.type !== 'dir') throw new NotDirectoryError(path);
    try {
      await this.run((id) => this.sandbox.groundbox.delete_file(id, abs, { recursive: options?.recursive ?? false }));
    } catch (error) {
      if (errorCode(error) === 'conflict') throw new DirectoryNotEmptyError(path);
      throw error;
    }
  }

  async readdir(path: string, options?: ListOptions): Promise<FileEntry[]> {
    const root = this.abs(path);
    const maxDepth = options?.recursive ? (options.maxDepth ?? DEFAULT_MAX_DEPTH) : 1;
    const result: FileEntry[] = [];
    let level: string[] = [root];
    for (let depth = 1; depth <= maxDepth && level.length > 0; depth++) {
      const next: string[] = [];
      for (let i = 0; i < level.length; i += LIST_CONCURRENCY) {
        const batch = level.slice(i, i + LIST_CONCURRENCY);
        const listed = await Promise.all(batch.map((dir) => this.listOne(dir, dir === root ? path : posix.relative(root, dir))));
        for (const entries of listed) {
          const links = entries.filter((entry) => entry.type === 'symlink');
          const resolved = links.length > 0 ? await this.resolveLinks(links.map((entry) => entry.path)) : [];
          const linkInfo = new Map(links.map((entry, index) => [entry.path, resolved[index]]));
          for (const entry of entries) {
            if (entry.type === 'other') continue;
            const name = posix.relative(root, entry.path);
            if (entry.type === 'symlink') {
              const info = linkInfo.get(entry.path);
              if (!info || info.type === 'x') continue;
              const type = info.type === 'd' ? 'directory' : 'file';
              if (type === 'file' && !matchesExtension(name, options?.extension)) continue;
              result.push({ name, type, isSymlink: true, ...(info.target && { symlinkTarget: info.target }) });
              continue;
            }
            if (entry.type === 'dir') {
              result.push({ name, type: 'directory' });
              next.push(entry.path);
              continue;
            }
            if (!matchesExtension(name, options?.extension)) continue;
            result.push({ name, type: 'file', size: entry.size_bytes });
          }
        }
      }
      level = next;
    }
    return result;
  }

  private async listOne(abs: string, shown: string): Promise<GroundboxFileEntry[]> {
    try {
      const listing = await this.run((id) => this.sandbox.groundbox.list_files(id, abs));
      return listing.entries;
    } catch (error) {
      const code = errorCode(error);
      if (code === 'not_found') throw new DirectoryNotFoundError(shown);
      if (code === 'bad_request') throw new NotDirectoryError(shown);
      throw error;
    }
  }

  private async resolveLinks(paths: string[]): Promise<Array<{ type: 'd' | 'f' | 'x'; target: string }>> {
    const result = await this.script(RESOLVE_LINKS_SCRIPT, paths);
    const lines = result.stdout.split('\n');
    return paths.map((_, index) => {
      const [type, ...rest] = (lines[index] ?? 'x\t').split('\t');
      return { type: type === 'd' || type === 'f' ? type : 'x', target: rest.join('\t') };
    });
  }

  async exists(path: string): Promise<boolean> {
    const abs = this.abs(path);
    return this.run((id) => this.sandbox.groundbox.file_exists(id, abs));
  }

  async stat(path: string): Promise<FileStat> {
    const abs = this.abs(path);
    let entry = await this.lstat(abs);
    if (entry?.type === 'symlink') {
      const target = (await this.script(REALPATH_SCRIPT, [abs])).stdout.trim();
      entry = target ? await this.lstat(target) : undefined;
    }
    if (!entry || entry.type === 'symlink') throw new FileNotFoundError(path);
    const modifiedAt = new Date(entry.modified_at);
    const isDir = entry.type === 'dir';
    return {
      name: posix.basename(abs),
      path: abs,
      type: isDir ? 'directory' : 'file',
      size: isDir ? 0 : entry.size_bytes,
      createdAt: modifiedAt,
      modifiedAt,
    };
  }

  private async assertParentDirectory(abs: string, path: string): Promise<void> {
    const parent = await this.lstat(posix.dirname(abs));
    if (!parent) throw new DirectoryNotFoundError(posix.dirname(path));
    if (parent.type !== 'dir') throw new NotDirectoryError(posix.dirname(path));
  }

  /** A files API error about one path, as the Mastra error it means. */
  private fileError(error: unknown, path: string): unknown {
    const code = errorCode(error);
    if (code === 'not_found') return new FileNotFoundError(path);
    if (code === 'bad_request') {
      const message = errorMessage(error);
      if (/is a folder|is a directory/i.test(message)) return new IsDirectoryError(path);
      if (/not a (folder|directory)/i.test(message)) return new NotDirectoryError(posix.dirname(path));
    }
    return error;
  }

  private check(
    result: { exitCode: number; stderr: string },
    operation: string,
    subject: string,
    errors: Partial<Record<keyof typeof EXIT, () => Error>>,
  ): void {
    if (result.exitCode === 0) return;
    for (const [outcome, code] of Object.entries(EXIT) as Array<[keyof typeof EXIT, number]>) {
      const make = errors[outcome];
      if (code === result.exitCode && make) throw make();
    }
    throw new Error(`${operation} ${subject} failed (exit ${result.exitCode}): ${result.stderr.trim()}`);
  }
}

function matchesExtension(name: string, extension: string | string[] | undefined): boolean {
  if (!extension) return true;
  const ext = posix.extname(name);
  return (Array.isArray(extension) ? extension : [extension]).some((e) => e === ext || e === ext.slice(1) || `.${e}` === ext);
}
