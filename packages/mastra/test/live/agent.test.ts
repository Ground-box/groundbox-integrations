/**
 * A real Mastra Agent loop against a real Groundbox sandbox, with a scripted model in place of
 * an LLM: the model asks for workspace tools in turn, Mastra runs them in the sandbox, and the
 * model's last answer repeats what the tools returned. This checks the wiring an LLM-driven run
 * uses (tool registration, schemas, execution, results back into the prompt) without an LLM key.
 *
 *   GROUNDBOX_API_KEY=gbx_... npm run test:live
 */
import { Agent } from '@mastra/core/agent';
import { WORKSPACE_TOOLS, Workspace } from '@mastra/core/workspace';
import { afterAll, describe, expect, it } from 'vitest';

import { GroundboxSandbox } from '../../src/index.js';

const live = !!process.env.GROUNDBOX_API_KEY;

type ToolCall = { toolName: string; input: Record<string, unknown> };

/** A LanguageModelV2 that plays back tool calls, then answers with every tool result it saw. */
function scriptedModel(calls: ToolCall[]) {
  const seenTools: string[] = [];
  type Options = { prompt: Array<{ role: string; content: unknown }>; tools?: Array<{ name: string }> };
  /** The next step: a tool call while there are calls left, then the tool results as text. */
  const step = (options: Options) => {
    seenTools.splice(0, seenTools.length, ...(options.tools ?? []).map((t) => t.name));
    const results = options.prompt
      .filter((m) => m.role === 'tool')
      .flatMap((m) => m.content as Array<{ type: string; output: { type: string; value: unknown } }>)
      .filter((part) => part.type === 'tool-result')
      .map((part) => (typeof part.output.value === 'string' ? part.output.value : JSON.stringify(part.output.value)));
    const next = calls[results.length];
    const usage = { inputTokens: 1, outputTokens: 1, totalTokens: 2 };
    return next
      ? { toolCall: { type: 'tool-call' as const, toolCallId: `call_${results.length}`, toolName: next.toolName, input: JSON.stringify(next.input) }, usage }
      : { text: results.join('\n---\n'), usage };
  };
  const model = {
    specificationVersion: 'v2' as const,
    provider: 'scripted',
    modelId: 'scripted-tool-caller',
    supportedUrls: {},
    seenTools,
    async doGenerate(options: Options) {
      const s = step(options);
      return {
        content: s.toolCall ? [s.toolCall] : [{ type: 'text' as const, text: s.text! }],
        finishReason: s.toolCall ? ('tool-calls' as const) : ('stop' as const),
        usage: s.usage,
        warnings: [],
      };
    },
    async doStream(options: Options) {
      const s = step(options);
      const parts: unknown[] = [{ type: 'stream-start', warnings: [] }];
      if (s.toolCall) parts.push(s.toolCall, { type: 'finish', finishReason: 'tool-calls', usage: s.usage });
      else parts.push({ type: 'text-start', id: 't' }, { type: 'text-delta', id: 't', delta: s.text }, { type: 'text-end', id: 't' }, { type: 'finish', finishReason: 'stop', usage: s.usage });
      return {
        stream: new ReadableStream({
          start(controller) {
            for (const part of parts) controller.enqueue(part);
            controller.close();
          },
        }),
      };
    },
  };
  return model;
}

const sandboxes: GroundboxSandbox[] = [];
afterAll(async () => {
  for (const sandbox of sandboxes) await sandbox._destroy().catch(() => undefined);
}, 60_000);

describe.skipIf(!live)('a Mastra Agent with a Groundbox workspace', () => {
  it('runs workspace tool calls in the sandbox', async () => {
    const sandbox = new GroundboxSandbox({ timeoutMinutes: 15, metadata: { 'mastra-live-run': 'agent' } });
    sandboxes.push(sandbox);
    const workspace = new Workspace({ sandbox, filesystem: sandbox.filesystem });
    const model = scriptedModel([
      { toolName: WORKSPACE_TOOLS.FILESYSTEM.WRITE_FILE, input: { path: '/home/runner/agent/fib.py', content: 'a, b = 0, 1\nfor _ in range(30):\n    a, b = b, a + b\nprint("fib30", a)\n' } },
      { toolName: WORKSPACE_TOOLS.SANDBOX.EXECUTE_COMMAND, input: { command: 'cd /home/runner/agent && python3 fib.py && uname -s' } },
      { toolName: WORKSPACE_TOOLS.FILESYSTEM.LIST_FILES, input: { path: '/home/runner/agent' } },
    ]);
    const agent = new Agent({
      id: 'groundbox-agent',
      name: 'groundbox-agent',
      instructions: 'Use the workspace tools.',
      model: model as never,
      workspace,
    });
    const result = await agent.generate('Compute the 30th Fibonacci number in Python.', { maxSteps: 6 });
    expect(model.seenTools).toEqual(expect.arrayContaining([
      WORKSPACE_TOOLS.SANDBOX.EXECUTE_COMMAND,
      WORKSPACE_TOOLS.FILESYSTEM.WRITE_FILE,
      WORKSPACE_TOOLS.FILESYSTEM.READ_FILE,
      WORKSPACE_TOOLS.FILESYSTEM.LIST_FILES,
    ]));
    expect(result.text).toContain('fib30 832040');
    expect(result.text).toContain('Linux');
    expect(result.text).toContain('fib.py');
    expect(sandbox.sandboxId).toMatch(/^sbx_/);
  }, 180_000);
});
