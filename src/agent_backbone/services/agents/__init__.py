"""Agents service — state tracking and monitoring."""

from agent_backbone.services.agents._delivery_check import (
    find_outgoing_comment,
    has_commented_on_issue,
    should_deliver,
)
from agent_backbone.services.agents._file_reader import read_state_file
from agent_backbone.services.agents._inference import get_agent_state, infer_state_from_pane
from agent_backbone.services.agents.interface import StateService
from agent_backbone.services.agents.models import AgentState, StateSnapshot

__all__ = [
    "AgentState",
    "StateService",
    "StateSnapshot",
    "find_outgoing_comment",
    "get_agent_state",
    "has_commented_on_issue",
    "infer_state_from_pane",
    "read_state_file",
    "should_deliver",
]
