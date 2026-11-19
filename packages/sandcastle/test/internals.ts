/**
 * Sandcastle marks a provider's `tag` and `create` as internal, so its public
 * types leave them out. Tests call `create` directly, as Sandcastle does.
 */

import type { IsolatedCreateOptions, IsolatedSandboxHandle, IsolatedSandboxProvider } from "@ai-hero/sandcastle";

export interface ProviderInternals {
  readonly tag: "isolated";
  readonly name: string;
  readonly env: Record<string, string>;
  readonly create: (options: IsolatedCreateOptions) => Promise<IsolatedSandboxHandle>;
}

export const internals = (provider: IsolatedSandboxProvider): ProviderInternals =>
  provider as unknown as ProviderInternals;
