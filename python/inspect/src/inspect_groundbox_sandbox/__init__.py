"""Groundbox sandboxes for Inspect AI: `sandbox="groundbox"`."""

from inspect_groundbox_sandbox._config import GroundboxSandboxEnvironmentConfig
from inspect_groundbox_sandbox._sandbox import GroundboxSandboxEnvironment

__version__ = "0.1.0"

__all__ = ["GroundboxSandboxEnvironment", "GroundboxSandboxEnvironmentConfig", "__version__"]
