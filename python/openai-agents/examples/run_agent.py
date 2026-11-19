"""Run a SandboxAgent on Groundbox.

    pip install openai-agents-groundbox
    export GROUNDBOX_API_KEY=gbx_...  OPENAI_API_KEY=sk-...
    python examples/run_agent.py "Summarize this workspace in two sentences."

Without OPENAI_API_KEY, pass --scripted to run the same sandbox with a scripted model
(no model call): it runs one shell command in the Groundbox sandbox and prints its output.
"""

from __future__ import annotations

import argparse
import asyncio

from agents import Runner
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Shell
from agents.sandbox.entries import File

from openai_agents_groundbox import GroundboxSandboxClient, GroundboxSandboxClientOptions

MANIFEST = Manifest(
    entries={
        "README.md": File(content=b"# Demo workspace\n\nA tiny project to inspect.\n"),
        "todo.md": File(content=b"1. Read the files.\n2. Ship on Friday.\n"),
    }
)


async def main(question: str, model: str | None, scripted: bool) -> None:
    agent_model: object = model
    if scripted:
        from agents.testing import ScriptedModel, assistant_message, function_call

        agent_model = ScriptedModel(
            [
                [function_call("exec_command", {"cmd": "uname -a && ls -la && cat todo.md"}, call_id="c1")],
                [assistant_message("Done: see the command output above.")],
            ]
        )
    agent = SandboxAgent(
        name="Groundbox workspace assistant",
        model=agent_model,  # type: ignore[arg-type]
        instructions="Inspect the workspace with the shell before answering. Be concise.",
        default_manifest=MANIFEST,
        capabilities=[Shell()],
    )
    run_config = RunConfig(
        tracing_disabled=scripted,
        sandbox=SandboxRunConfig(
            client=GroundboxSandboxClient(),
            options=GroundboxSandboxClientOptions(cpus=2, timeout_minutes=15),
        ),
    )
    result = await Runner.run(agent, question, run_config=run_config)
    for item in result.new_items:
        if item.type == "tool_call_output_item":
            print(item.output)
    print(result.final_output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("question", nargs="?", default="Summarize this workspace in two sentences.")
    parser.add_argument("--model", default=None, help="Model name; the SDK default when omitted.")
    parser.add_argument("--scripted", action="store_true", help="Use a scripted model (no OpenAI key needed).")
    args = parser.parse_args()
    asyncio.run(main(args.question, args.model, args.scripted))
