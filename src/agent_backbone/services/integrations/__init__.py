"""Integrations — the human-facing channels (Telegram today) behind one contract.

See ``base.py`` for the contract and ``docs/integrations.md`` for how to add
one. Vendor packages live underneath (``integrations.telegram``).
"""

from agent_backbone.services.integrations._notify import notify_humans
from agent_backbone.services.integrations._registry import Integrations, build_integrations
from agent_backbone.services.integrations.base import Integration

__all__ = ["Integration", "Integrations", "build_integrations", "notify_humans"]
