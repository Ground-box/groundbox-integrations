"""Groundbox sandboxes for the OpenAI Agents SDK.

    from agents.run import RunConfig
    from agents.sandbox import SandboxRunConfig
    from openai_agents_groundbox import GroundboxSandboxClient, GroundboxSandboxClientOptions

    run_config = RunConfig(
        sandbox=SandboxRunConfig(
            client=GroundboxSandboxClient(),  # reads GROUNDBOX_API_KEY
            options=GroundboxSandboxClientOptions(cpus=2),
        )
    )
"""

from .sandbox import (
    DEFAULT_GROUNDBOX_WORKSPACE_ROOT,
    GroundboxSandboxClient,
    GroundboxSandboxClientOptions,
    GroundboxSandboxSession,
    GroundboxSandboxSessionState,
    GroundboxSandboxTimeouts,
)

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_GROUNDBOX_WORKSPACE_ROOT",
    "GroundboxSandboxClient",
    "GroundboxSandboxClientOptions",
    "GroundboxSandboxSession",
    "GroundboxSandboxSessionState",
    "GroundboxSandboxTimeouts",
    "__version__",
]
