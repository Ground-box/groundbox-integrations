/**
 * Sandcastle reads the host's git identity and brings back no commits without
 * one (a bare CI machine or a fresh sandbox has none). Give the tests one,
 * in a throwaway global config, only when the host has none.
 */

import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

const configured = (key: string): boolean => {
  try {
    return execFileSync("git", ["config", "--get", key], { encoding: "utf8" }).trim() !== "";
  } catch {
    return false;
  }
};

if (!configured("user.name") || !configured("user.email")) {
  const file = path.join(mkdtempSync(path.join(tmpdir(), "gbx-gitconfig-")), "gitconfig");
  writeFileSync(file, "[user]\n\tname = Sandcastle Test\n\temail = test@example.com\n");
  process.env.GIT_CONFIG_GLOBAL = file;
}
