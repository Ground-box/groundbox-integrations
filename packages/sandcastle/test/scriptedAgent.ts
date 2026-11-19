/**
 * A real Claude Code agent provider whose command is swapped for a short
 * shell script, so Sandcastle's whole run (sync in, hooks, agent, commits
 * back out) works without an LLM key. The script reads the prompt from stdin,
 * writes it to a file, commits it, and prints Claude Code's stream-json with
 * the completion signal.
 */

import { execFileSync } from "node:child_process";
import { mkdtempSync, realpathSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { claudeCode, type AgentProvider } from "@ai-hero/sandcastle";

const SCRIPT = [
  "set -e",
  "cat > agent-note.txt",
  'echo "{\\"type\\":\\"system\\",\\"subtype\\":\\"init\\",\\"session_id\\":\\"scripted-1\\"}"',
  'echo "{\\"type\\":\\"assistant\\",\\"message\\":{\\"content\\":[{\\"type\\":\\"text\\",\\"text\\":\\"running on $(uname -s) as $(id -un)\\"}]}}"',
  "git add agent-note.txt",
  'git -c user.name="Scripted Agent" -c user.email=agent@example.com commit -q -m "Add agent note"',
  'echo "{\\"type\\":\\"result\\",\\"subtype\\":\\"success\\",\\"result\\":\\"<promise>COMPLETE</promise>\\"}"',
].join("\n");

export const scriptedAgent = (): AgentProvider => {
  const base = claudeCode("claude-opus-4-8", { captureSessions: false });
  return {
    ...base,
    buildPrintCommand: (options) => ({
      command: `sh -c '${SCRIPT.replaceAll("'", String.raw`'\''`)}'`,
      stdin: options.prompt,
    }),
  };
};

/** A fresh git repo on the host with one commit on `main`. */
export const makeHostRepo = (): string => {
  const repo = realpathSync(mkdtempSync(path.join(tmpdir(), "gbx-sandcastle-repo-")));
  const git = (...args: string[]): void => {
    execFileSync("git", args, { cwd: repo, stdio: "ignore" });
  };
  git("init", "-b", "main");
  writeFileSync(path.join(repo, "README.md"), "# repo\n");
  git("add", ".");
  git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init");
  return repo;
};

export const hostGit = (repo: string, ...args: string[]): string =>
  execFileSync("git", args, { cwd: repo, encoding: "utf8" }).trim();
