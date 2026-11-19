/**
 * Moving files between the host and a Groundbox sandbox: `copyIn` (a file or
 * a whole directory) and `copyFileOut`.
 */

import { lstat, mkdir, readFile, readdir, readlink, stat, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { shellEscape, type GroundboxClient, type UploadFile } from "./client.js";

/** Runs a shell command in the sandbox and fails on a non-zero exit. */
export type RunChecked = (command: string) => Promise<void>;

/** A directory's files and symlinks, plus the empty folders tar would not make. */
export const walk = async (
  root: string,
): Promise<{ files: UploadFile[]; emptyDirs: string[] }> => {
  const files: UploadFile[] = [];
  const emptyDirs: string[] = [];
  const visit = async (rel: string): Promise<void> => {
    const entries = await readdir(rel === "" ? root : join(root, rel), {
      withFileTypes: true,
    });
    if (entries.length === 0 && rel !== "") {
      emptyDirs.push(rel);
    }
    for (const entry of entries) {
      const path = rel === "" ? entry.name : `${rel}/${entry.name}`;
      const full = join(root, path);
      if (entry.isDirectory()) {
        await visit(path);
      } else if (entry.isSymbolicLink()) {
        files.push({ path, symlink: await readlink(full) });
      } else if (entry.isFile()) {
        const info = await lstat(full);
        files.push({ path, content: await readFile(full), mode: info.mode & 0o777 });
      }
      // Sockets, FIFOs and devices are skipped.
    }
  };
  await visit("");
  return { files, emptyDirs };
};

/** The handle's `copyIn`: a file or a directory, keeping the host's modes. */
export const makeCopyIn =
  (client: GroundboxClient, sandboxId: string, run: RunChecked) =>
  async (hostPath: string, sandboxPath: string): Promise<void> => {
    const info = await stat(hostPath);
    if (info.isDirectory()) {
      const { files, emptyDirs } = await walk(hostPath);
      await run(`mkdir -p ${shellEscape(sandboxPath)}`);
      if (files.length > 0) {
        await client.upload_files(sandboxId, sandboxPath, files);
      }
      if (emptyDirs.length > 0) {
        const dirs = emptyDirs.map((d) => shellEscape(`${sandboxPath}/${d}`));
        await run(`mkdir -p ${dirs.join(" ")}`);
      }
      return;
    }
    await client.write_file(sandboxId, sandboxPath, await readFile(hostPath));
    const mode = (info.mode & 0o777).toString(8);
    await run(`chmod ${mode} ${shellEscape(sandboxPath)}`);
  };

/** The handle's `copyFileOut`: one file, parent folders made on the host. */
export const makeCopyFileOut =
  (client: GroundboxClient, sandboxId: string) =>
  async (sandboxPath: string, hostPath: string): Promise<void> => {
    const content = await client.read_file(sandboxId, sandboxPath);
    await mkdir(dirname(hostPath), { recursive: true });
    await writeFile(hostPath, content);
  };
