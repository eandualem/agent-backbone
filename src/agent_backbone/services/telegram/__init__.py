"""Telegram service — bot interface for agent management."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {
    "TelegramService": ("agent_backbone.services.telegram.interface", "TelegramService"),
    "TelegramServiceError": (
        "agent_backbone.services.telegram.exceptions",
        "TelegramServiceError",
    ),
    "_delivery_reply": ("agent_backbone.services.telegram._routing", "_delivery_reply"),
    "get_delivery_queue": ("agent_backbone.services.telegram._flows", "get_delivery_queue"),
    "get_system_status": ("agent_backbone.services.telegram._flows", "get_system_status"),
    "send_notification": ("agent_backbone.services.telegram.interface", "send_notification"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    """Lazily load Telegram exports to keep package imports lightweight."""
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
