"""Coder from pydantic-ai-harness, working in a Groundbox sandbox.

    pip install pydantic-ai-groundbox "pydantic-ai-harness[coder,anthropic]"
    export GROUNDBOX_API_KEY=gbx_... ANTHROPIC_API_KEY=...
    python examples/coder.py

Set MODEL to use another provider, e.g. MODEL=openai:gpt-5 (with that provider's extra and key).
"""

import asyncio
import os

from pydantic_ai import Agent
from pydantic_ai_harness.coder import Coder

from pydantic_ai_groundbox import GroundboxSandbox


async def main() -> None:
    sandbox = GroundboxSandbox(cpus=2, timeout_minutes=30, working_dir='/home/runner/project')
    agent = Agent(os.environ.get('MODEL', 'anthropic:claude-sonnet-4-5'), capabilities=[sandbox, Coder()])

    result = await agent.run('Write fizzbuzz.py that prints FizzBuzz from 1 to 15, run it, and show me the output.')
    print(result.output)
    assert result.workspace is not None and result.workspace.ref is not None
    ref = result.workspace.ref
    print('sandbox:', ref.id)

    try:
        # The message history carries the sandbox's ref, so this run works in the same sandbox.
        followup = await agent.run(
            'Add a test for fizzbuzz.py with pytest and run it.', message_history=result.all_messages()
        )
        print(followup.output)
    finally:
        await sandbox.destroy(ref)


if __name__ == '__main__':
    asyncio.run(main())
