"""State service — agent state tracking with push+pull reconciliation."""

from agent_backbone.services.state._delivery_check import (
    find_outgoing_comment,
    has_commented_on_issue,
    should_deliver,
)
from agent_backbone.services.state._file_reader import read_state_file
from agent_backbone.services.state._inference import get_agent_state, infer_state_from_pane
from agent_backbone.services.state.exceptions import AgentStateError
from agent_backbone.services.state.interface import StateService
from agent_backbone.services.state.models import AgentState, StateSnapshot

__all__ = [
    "AgentState",
    "AgentStateError",
    "StateService",
    "StateSnapshot",
    "find_outgoing_comment",
    "get_agent_state",
    "has_commented_on_issue",
    "infer_state_from_pane",
    "read_state_file",
    "should_deliver",
]
