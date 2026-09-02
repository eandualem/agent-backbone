"""agent-backbone — a local control plane for terminal AI agents."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("agent-backbone")  # pyproject.toml is the one source
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = "0.0.0+unknown"
