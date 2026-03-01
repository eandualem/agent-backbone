"""Dispatch service — event routing, lifecycle, and dependency tracking."""

from agent_backbone.services.dispatch._dependencies import (
    check_parent_resolved,
    on_dependency_resolved,
    sync_dependencies,
)
from agent_backbone.services.dispatch._lifecycle import (
    _ONBOARDING_TITLE_PREFIX,
    _check_onboarding_chain,
    deliver_next,
    find_next_issue,
    on_issue_closed,
)
from agent_backbone.services.dispatch._router import (
    DispatchResult,
    issue_dispatcher,
    resolve_session,
)
from agent_backbone.services.dispatch.interface import DispatchService
from agent_backbone.services.dispatch.models import DispatchResult as DispatchResultModel

__all__ = [
    "DispatchResult",
    "DispatchResultModel",
    "DispatchService",
    "_ONBOARDING_TITLE_PREFIX",
    "_check_onboarding_chain",
    "check_parent_resolved",
    "deliver_next",
    "find_next_issue",
    "issue_dispatcher",
    "on_dependency_resolved",
    "on_issue_closed",
    "resolve_session",
    "sync_dependencies",
]
