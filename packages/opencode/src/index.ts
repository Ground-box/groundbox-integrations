/**
 * @groundbox/opencode: registers a "groundbox" type with OpenCode's experimental workspace API.
 * Each workspace is a Groundbox sandbox running a remote OpenCode server on a copy of the
 * project; the local OpenCode reaches it through a private Groundbox preview.
 *
 * OpenCode 1 reads `server` from the default export (its PluginModule).
 */

import { Groundbox } from "@groundbox/sdk"
import type { Plugin, PluginInput, PluginModule } from "@opencode-ai/plugin"

import { createGroundboxWorkspaceAdapter, errorMessage, type Logger } from "./adapter.js"
import { resolveOptions } from "./options.js"

export type { GroundboxWorkspaceOptions } from "./options.js"

export const PLUGIN_VERSION = "0.1.0"

const server: Plugin = async (input, rawOptions) => {
  const options = resolveOptions(rawOptions)
  const logger = createLogger(input.client)
  let groundbox: Groundbox | undefined

  const adapter = createGroundboxWorkspaceAdapter({
    directory: input.directory,
    options,
    logger,
    groundbox: () => (groundbox ??= new Groundbox({ userAgent: `groundbox-opencode/${PLUGIN_VERSION}` })),
    resolveRuntimeVersion: () => resolveRuntimeVersion(input, logger),
  })
  input.experimental_workspace.register("groundbox", adapter)
  await logger.info("registered groundbox workspace type", { directory: input.directory })
  return {}
}

const plugin: PluginModule = { id: "groundbox", server }
export default plugin

function createLogger(client: PluginInput["client"]): Logger {
  const write = (level: "info" | "warn" | "error") => async (message: string, extra?: Record<string, unknown>) => {
    await client.app.log({ body: { service: "groundbox-workspace", level, message, extra } }).catch(() => {})
  }
  return { info: write("info"), warn: write("warn"), error: write("error") }
}

let runtimeVersion: Promise<string> | undefined

/** The local OpenCode's version, from its own /global/health. Falls back to "latest". */
function resolveRuntimeVersion(input: PluginInput, logger: Logger): Promise<string> {
  runtimeVersion ??= (async () => {
    const raw = (input.client as unknown as { _client?: { get(o: { url: string; signal?: AbortSignal }): Promise<unknown> } })
      ._client
    try {
      if (raw) {
        const result = (await raw.get({ url: "/global/health", signal: AbortSignal.timeout(2000) })) as {
          data?: { version?: unknown }
        }
        if (typeof result?.data?.version === "string") return result.data.version
      }
    } catch (error) {
      await logger.warn("health probe through the client failed", { error: errorMessage(error) })
    }
    try {
      const res = await fetch(new URL("/global/health", input.serverUrl), { signal: AbortSignal.timeout(2000) })
      const body = (await res.json()) as { version?: unknown }
      if (typeof body.version === "string") return body.version
    } catch (error) {
      await logger.warn("health probe on serverUrl failed", { error: errorMessage(error) })
    }
    await logger.warn("local OpenCode version unknown; the sandbox installs the latest. Set opencodeVersion to pin it.")
    return "latest"
  })()
  return runtimeVersion
}
