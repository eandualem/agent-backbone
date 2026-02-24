"""Shared StreamBroker singleton.

Extracted from api/routes/stream.py so both the SSE endpoint and
Socket.IO server can share the same broker instance.
"""

from __future__ import annotations

from src.config import BackboneConfig
from src.stream_broker import StreamBroker

_broker: StreamBroker | None = None


def get_or_create_broker(config: BackboneConfig) -> StreamBroker:
    """Get the shared StreamBroker, creating it on first call."""
    global _broker
    if _broker is None:
        _broker = StreamBroker(config)
    return _broker


def get_broker_instance() -> StreamBroker | None:
    """Return the current broker instance (or None if not yet created)."""
    return _broker


def reset_broker() -> None:
    """Reset the broker singleton. Used in tests."""
    global _broker
    _broker = None
