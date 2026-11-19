import { execFile } from "node:child_process"
import { lstat, readFile, readdir, readlink } from "node:fs/promises"
import { join, relative, sep } from "node:path"
import { promisify } from "node:util"

const run = promisify(execFile)

export type LocalFile = {
  /** Path relative to the project folder, with `/` separators. */
  path: string
  size: number
  mode: number
  symlink?: string
}

export type SnapshotPlan = {
  files: LocalFile[]
  skipped: { path: string; reason: string }[]
  source: "git" | "walk"
}

/**
 * The files to copy into the sandbox. In a git repository, git decides: tracked files plus
 * untracked ones that are not ignored, so `.gitignore`d output and secrets in ignored files
 * stay on this machine. Elsewhere, a walk that skips `ignore`.
 */
export async function planSnapshot(root: string, ignore: string[], maxFileBytes: number): Promise<SnapshotPlan> {
  const listed = await gitListFiles(root)
  const paths = listed ?? (await walk(root, ignore))
  const files: LocalFile[] = []
  const skipped: SnapshotPlan["skipped"] = []
  for (const path of paths) {
    const abs = join(root, ...path.split("/"))
    let st
    try {
      st = await lstat(abs)
    } catch {
      continue // tracked but deleted in the working tree
    }
    if (st.isSymbolicLink()) {
      files.push({ path, size: 0, mode: 0o777, symlink: await readlink(abs) })
    } else if (st.isFile()) {
      if (st.size > maxFileBytes) {
        skipped.push({ path, reason: `larger than ${maxFileBytes} bytes` })
        continue
      }
      files.push({ path, size: st.size, mode: st.mode & 0o777 })
    }
  }
  return { files, skipped, source: listed ? "git" : "walk" }
}

async function gitListFiles(root: string): Promise<string[] | undefined> {
  try {
    const { stdout } = await run("git", ["-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"], {
      maxBuffer: 256 * 1024 * 1024,
    })
    return [...new Set(stdout.split("\0").filter(Boolean))]
  } catch {
    return undefined
  }
}

async function walk(root: string, ignore: string[]): Promise<string[]> {
  const out: string[] = []
  const skip = (rel: string, name: string) => ignore.includes(name) || ignore.includes(rel)
  async function visit(dir: string) {
    for (const entry of await readdir(dir, { withFileTypes: true })) {
      const abs = join(dir, entry.name)
      const rel = relative(root, abs).split(sep).join("/")
      if (skip(rel, entry.name)) continue
      if (entry.isDirectory()) await visit(abs)
      else if (entry.isFile() || entry.isSymbolicLink()) out.push(rel)
    }
  }
  await visit(root)
  return out
}

/** Groups files into upload batches of at most `maxFiles` files and about `maxBytes` bytes. */
export function batchFiles(files: LocalFile[], maxFiles = 500, maxBytes = 48 * 1024 * 1024): LocalFile[][] {
  const batches: LocalFile[][] = []
  let current: LocalFile[] = []
  let bytes = 0
  for (const file of files) {
    if (current.length > 0 && (current.length >= maxFiles || bytes + file.size > maxBytes)) {
      batches.push(current)
      current = []
      bytes = 0
    }
    current.push(file)
    bytes += file.size
  }
  if (current.length > 0) batches.push(current)
  return batches
}

export async function readBatch(root: string, batch: LocalFile[]) {
  return Promise.all(
    batch.map(async (f) =>
      f.symlink !== undefined
        ? { path: f.path, symlink: f.symlink }
        : { path: f.path, content: new Uint8Array(await readFile(join(root, ...f.path.split("/")))), mode: f.mode },
    ),
  )
}
