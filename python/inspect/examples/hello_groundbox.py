"""Two tiny Inspect evals on Groundbox sandboxes that need no LLM key.

    export GROUNDBOX_API_KEY=gbx_...
    inspect eval examples/hello_groundbox.py --model mockllm/model

`hello` drives the sandbox straight from a solver and a scorer. `agent_bash`
runs Inspect's own agent loop and bash() tool, with the mock model scripted to
call the tool, so the agent's tool calls run in the Groundbox sandbox.
"""

from __future__ import annotations

from inspect_ai import Task, task
from inspect_ai.agent import react
from inspect_ai.dataset import Sample
from inspect_ai.model import ModelOutput, get_model
from inspect_ai.scorer import CORRECT, INCORRECT, Score, Target, accuracy, scorer
from inspect_ai.solver import Generate, TaskState, solver
from inspect_ai.tool import bash
from inspect_ai.util import sandbox


@solver
def compute_in_sandbox():
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        box = sandbox()
        await box.write_file("work/input.txt", state.input_text)
        result = await box.exec(["sh", "-c", "wc -w < work/input.txt > work/count.txt && uname -sm"])
        state.metadata["uname"] = result.stdout.strip()
        state.output.completion = (await box.read_file("work/count.txt")).strip()
        return state

    return solve


@scorer(metrics=[accuracy()])
def matches_target():
    async def score(state: TaskState, target: Target) -> Score:
        answer = state.output.completion
        return Score(
            value=CORRECT if answer == target.text else INCORRECT,
            answer=answer,
            explanation=f"ran on {state.metadata.get('uname')}",
        )

    return score


@scorer(metrics=[accuracy()])
def answer_file_says():
    """Correct when the file the agent's bash call wrote in the sandbox holds the target."""

    async def score(state: TaskState, target: Target) -> Score:
        written = (await sandbox().read_file("answer.txt")).strip()
        return Score(value=CORRECT if written == target.text else INCORRECT, answer=written)

    return score


@task
def hello() -> Task:
    """Count words in a Groundbox sandbox, one sandbox per sample."""
    return Task(
        dataset=[
            Sample(input="the quick brown fox", target="4"),
            Sample(input="jumps over the lazy dog today", target="6"),
        ],
        solver=compute_in_sandbox(),
        scorer=matches_target(),
        sandbox="groundbox",
    )


@task
def agent_bash() -> Task:
    """A react agent with bash(); the mock model calls the tool, and the scorer reads the file it wrote."""
    model = get_model(
        "mockllm/model",
        custom_outputs=[
            ModelOutput.for_tool_call(
                "mockllm/model", "bash", {"command": "echo $((6 * 7)) > answer.txt && cat answer.txt"}
            ),
            ModelOutput.for_tool_call("mockllm/model", "submit", {"answer": "42"}),
        ],
    )
    return Task(
        dataset=[Sample(input="What is 6 times 7? Use bash.", target="42")],
        solver=react(tools=[bash(timeout=60)]),
        scorer=answer_file_says(),
        sandbox="groundbox",
        model=model,
    )
