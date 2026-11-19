/**
 * The part of the `@groundbox/sdk` client this provider uses. Tests pass a
 * fake with the same shape through the `client` option.
 */

import type { Groundbox } from "@groundbox/sdk";

export type GroundboxClient = Pick<
  Groundbox,
  | "create_sandbox"
  | "wait_for_sandbox"
  | "delete_sandbox"
  | "stream_command"
  | "write_file"
  | "read_file"
  | "upload_files"
>;

/** A file for `upload_files`: a path relative to the folder, and bytes or a symlink target. */
export type UploadFile = Parameters<Groundbox["upload_files"]>[2][number];

/** A running command, as `stream_command` returns it. */
export type CommandStream = ReturnType<Groundbox["stream_command"]>;

/** Single-quote a string for a POSIX shell. */
export const shellEscape = (s: string): string =>
  `'${s.replaceAll("'", String.raw`'\''`)}'`;

/** Whether an error is the API's "nothing there" answer. */
export const isNotFound = (error: unknown): boolean =>
  typeof error === "object" &&
  error !== null &&
  (("code" in error && error.code === "not_found") ||
    ("status" in error && error.status === 404));
