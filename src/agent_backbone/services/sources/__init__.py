"""Sources — inbound event streams agents subscribe to (Gmail today).

See ``base.py`` for the contract and ``docs/sources.md`` for how to add
one. Vendor modules live underneath (``sources.gmail``).
"""

from agent_backbone.services.sources._registry import Sources, build_sources
from agent_backbone.services.sources.base import Source, SourceEvent

__all__ = ["Source", "SourceEvent", "Sources", "build_sources"]
