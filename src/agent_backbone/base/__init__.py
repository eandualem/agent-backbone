"""Foundation layer — the lifecycle protocol and its manager."""

from agent_backbone.base.lifecycle import LifecycleManager
from agent_backbone.base.protocols import LifecycleAware

__all__ = ["LifecycleAware", "LifecycleManager"]
