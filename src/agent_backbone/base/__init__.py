"""Foundation layer — the lifecycle protocol and its manager."""

from agent_backbone.base.lifecycle import LifecycleManager
from agent_backbone.base.logging import DiagnosticFormatter, log_policy, set_log_mode
from agent_backbone.base.protocols import LifecycleAware

__all__ = [
    "DiagnosticFormatter",
    "LifecycleAware",
    "LifecycleManager",
    "log_policy",
    "set_log_mode",
]
