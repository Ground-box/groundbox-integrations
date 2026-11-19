import { posix } from 'node:path';

import { GroundboxError } from '@groundbox/sdk';

/** Quote one argument for POSIX `sh`. */
export function shellQuote(value: string): string {
  if (value !== '' && /^[A-Za-z0-9_\-./=:@%+,]+$/.test(value)) return value;
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

/** Drop undefined values from an env record. */
export function definedEnv(env: Record<string, string | undefined> | NodeJS.ProcessEnv | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  if (!env) return out;
  for (const [key, value] of Object.entries(env)) {
    if (value !== undefined) out[key] = value;
  }
  return out;
}

/** Milliseconds to the whole seconds the Groundbox API takes, at least 1. */
export function toSeconds(ms: number): number {
  return Math.max(1, Math.ceil(ms / 1000));
}

/** Absolute path inside the sandbox; a relative one is resolved against `base`. */
export function toAbsolute(path: string, base = '/'): string {
  return posix.resolve(base, path);
}

/** The API's error code when `error` is a GroundboxError. */
export function errorCode(error: unknown): string | undefined {
  if (error instanceof GroundboxError) return error.code;
  if (error && typeof error === 'object' && 'code' in error && typeof (error as { code: unknown }).code === 'string') {
    return (error as { code: string }).code;
  }
  return undefined;
}

export function errorStatus(error: unknown): number | undefined {
  if (error && typeof error === 'object' && 'status' in error && typeof (error as { status: unknown }).status === 'number') {
    return (error as { status: number }).status;
  }
  return undefined;
}

export function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Keep the newest `maxBytes` UTF-8 bytes of `text`, as the process handles do.
 * Infinity keeps everything; 0 keeps nothing.
 */
export function retainNewest(text: string, maxBytes: number): { text: string; dropped: number } {
  if (!Number.isFinite(maxBytes)) return { text, dropped: 0 };
  const bytes = Buffer.from(text, 'utf8');
  if (bytes.length <= maxBytes) return { text, dropped: 0 };
  const dropped = bytes.length - maxBytes;
  let start = dropped;
  // Do not start in the middle of a multi-byte character.
  while (start < bytes.length && (bytes[start]! & 0xc0) === 0x80) start++;
  return { text: bytes.subarray(start).toString('utf8'), dropped: start };
}

/** Bytes of a FileContent. */
export function toBytes(content: string | Buffer | Uint8Array): Uint8Array {
  if (typeof content === 'string') return Buffer.from(content, 'utf8');
  return content;
}
