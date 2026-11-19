"""Groundbox sandboxes for Pydantic AI agents.

`GroundboxSandbox` is the capability to add to an agent; compose it with tools that use the
workspace, such as `Coder`, `Shell` or `FileSystem` from pydantic-ai-harness.
`GroundboxSandboxBackend` is the Groundbox implementation of Pydantic AI's `WorkspaceBackend`
protocol, for applications that create or attach to a sandbox themselves and pass it to a run as
`workspace=`.
"""

from ._backend import PROVIDER, GroundboxSandboxBackend, delete_sandbox
from ._capability import GroundboxSandbox

__all__ = ['PROVIDER', 'GroundboxSandbox', 'GroundboxSandboxBackend', 'delete_sandbox']
__version__ = '0.1.0'
