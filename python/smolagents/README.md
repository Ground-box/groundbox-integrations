# smolagents-groundbox

Run [smolagents](https://github.com/huggingface/smolagents)' `CodeAgent` code in a
[Groundbox](https://groundbox.dev) sandbox: a Linux microVM (Firecracker) per agent, started in
about a second.

`GroundboxExecutor` is a remote Python executor, built like smolagents' own E2B, Modal, Docker and
Blaxel executors. Pass it to `CodeAgent(executor=...)`.

Needs smolagents 1.25.0 or newer. `executor=` arrived in 1.23.0, but 1.25.0 moved remote
executors from pickle to safe JSON serialization (`allow_pickle`), and this package uses that.

## Install

```
pip install smolagents-groundbox
```

Set `GROUNDBOX_API_KEY` (create one at https://app.groundbox.dev/keys), or pass `api_key=`.

## Use

```python
from smolagents import CodeAgent, InferenceClientModel
from smolagents_groundbox import GroundboxExecutor

with CodeAgent(
    tools=[],
    model=InferenceClientModel(),
    executor=GroundboxExecutor(additional_imports=["numpy"]),
) as agent:
    agent.run("Compute the 20th Fibonacci number with numpy.")
# Leaving the block deletes the sandbox.
```

Without the `with` block, call `agent.cleanup()` (or `executor.cleanup()`) when you are done. A
sandbox the executor created also ends on its own after `timeout_minutes` (60 unless you set it).

## Options

```python
GroundboxExecutor(
    additional_imports=["pandas"],     # pip installed in the sandbox before the first step
    create_kwargs={                    # passed to Groundbox.create_sandbox
        "cpus": 2,
        "memory_gb": 4,
        "image": "python:3.12-slim",   # any public Docker image, or a template=...
        "allow_hosts": ["pypi.org", "files.pythonhosted.org"],
        "timeout_minutes": 30,
    },
    timeout_seconds=300,               # longest one code action may run (default 300)
    sandbox_id=None,                   # reuse a running sandbox; it is not deleted on cleanup
    allow_pickle=False,                # same meaning as in smolagents' built-in executors
)
```

`logger` is optional: `CodeAgent` does not hand its logger to an executor you build yourself, so
by default the executor logs to a new `AgentLogger`. Pass `logger=agent.logger` to share one.

## How it works

The executor starts the sandbox, uploads a small kernel (standard library only), and starts it as
a background command. Each code action is sent to that one Python process, so variables, imports,
functions and tools stay from step to step, the way the built-in executors keep them in a Jupyter
kernel. Tools and `final_answer` go through smolagents' own `send_tools`, `send_variables` and
final-answer handling.

- Output: the value of the last expression, as its `repr` (a PIL image comes back as an image),
  like the text result of the Jupyter-based executors.
- Logs: everything printed to stdout and stderr during the step, including subprocesses.
- Errors: raised as `AgentError` with the exception name, message and traceback.
- Timeouts: past `timeout_seconds`, the step is interrupted with `ExecutionTimeout`; the namespace
  is kept.
- A step that kills the Python process (`os._exit`, running out of memory) restarts the kernel and
  says so; earlier variables are gone.

The default image is Ubuntu with Python 3.12, running as the user `runner` (with sudo); packages
are installed with `pip --user`. With `image=`, commands run as root.

When the agent starts, smolagents pip installs each tool's requirements in the sandbox. Its own
`final_answer` tool asks for `numpy` and `pillow`, so the first run in a fresh sandbox spends a few
seconds installing them (the same happens with the built-in executors). To skip that, start from a
[template](https://groundbox.dev/docs/templates) that has them: `create_kwargs={"template": "..."}`.

## Limits

- No Jupyter: IPython syntax such as `!pip install` or `%magic` lines is not supported in agent
  code (the built-in executors run on Jupyter and accept it). Use `subprocess` or
  `additional_imports` instead.
- Rich outputs other than PIL images (HTML, charts) come back as their `repr`.
- One code action at a time per executor; each step makes one API call (about 0.2 s overhead).
- Not built into smolagents: use `executor=GroundboxExecutor()`, not `executor_type="groundbox"`.

## Tests

```
pip install -e '.[test]'
pytest                                  # unit tests, with a local stand-in for the sandbox
GROUNDBOX_API_KEY=gbx_... pytest tests/test_live.py   # against Groundbox itself
```

## License

MIT. See [LICENSE](LICENSE).
