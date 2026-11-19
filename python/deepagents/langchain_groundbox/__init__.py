"""Groundbox sandbox integration for Deep Agents.

`GroundboxSandbox` is the backend; `langchain_groundbox.provider.GroundboxProvider`
is the Deep Agents Code (`dcode --sandbox groundbox`) provider, loaded through the
`deepagents_code.sandbox_providers` entry point. It is not imported here, so this
package works with plain `deepagents` too.
"""

from langchain_groundbox._version import __version__
from langchain_groundbox.sandbox import GroundboxSandbox

__all__ = ["GroundboxSandbox", "__version__"]
