/**
 * Groundbox sandbox provider descriptor for the Mastra editor.
 *
 * @example
 * ```typescript
 * import { MastraEditor } from '@mastra/editor';
 * import { groundboxSandboxProvider } from '@groundbox/mastra';
 *
 * const editor = new MastraEditor({ sandboxes: [groundboxSandboxProvider] });
 * ```
 */
import type { SandboxProvider } from '@mastra/core/editor';

import { GroundboxSandbox } from './sandbox.js';

/** The serializable subset of GroundboxSandboxOptions the editor stores. */
export interface GroundboxProviderConfig {
  cpus?: number;
  memoryGb?: number;
  diskGb?: number;
  image?: string;
  template?: string;
  timeout?: number;
  timeoutMinutes?: number;
  idleTimeoutSeconds?: number;
  env?: Record<string, string>;
  metadata?: Record<string, string>;
  allowHosts?: string[];
  denyHosts?: string[];
  desktop?: boolean;
  publicPreviews?: boolean;
  apiKey?: string;
  baseUrl?: string;
}

export const groundboxSandboxProvider: SandboxProvider<GroundboxProviderConfig> = {
  id: 'groundbox',
  name: 'Groundbox Sandbox',
  description: 'Firecracker microVM Linux sandbox from Groundbox',
  configSchema: {
    type: 'object',
    properties: {
      cpus: { type: 'number', description: 'vCPUs, 1 to 16', default: 2 },
      memoryGb: { type: 'number', description: 'Memory in GB, up to 4 per vCPU' },
      diskGb: { type: 'number', description: 'Disk in GB', default: 20 },
      image: { type: 'string', description: 'Docker image to start from (commands then run as root)' },
      template: { type: 'string', description: 'Groundbox template id or name' },
      timeout: { type: 'number', description: 'Command timeout in milliseconds', default: 300_000 },
      timeoutMinutes: { type: 'number', description: 'Sandbox lifetime in minutes', default: 60 },
      idleTimeoutSeconds: { type: 'number', description: 'Pause after this many idle seconds (at least 30)' },
      env: { type: 'object', description: 'Environment variables', additionalProperties: { type: 'string' } },
      metadata: { type: 'object', description: 'Metadata pairs', additionalProperties: { type: 'string' } },
      allowHosts: { type: 'array', items: { type: 'string' }, description: 'Hosts the sandbox may reach' },
      denyHosts: { type: 'array', items: { type: 'string' }, description: 'Hosts the sandbox may not reach' },
      desktop: { type: 'boolean', description: 'Start a Linux desktop with Chrome for computer use', default: false },
      publicPreviews: { type: 'boolean', description: 'Make port previews public', default: false },
      apiKey: { type: 'string', description: 'Groundbox API key (defaults to GROUNDBOX_API_KEY)' },
      baseUrl: { type: 'string', description: 'Groundbox API URL' },
    },
  },
  createSandbox: (config) => new GroundboxSandbox(config),
};
