"""Agents service — state tracking and monitoring."""

from agent_backbone.services.agents._file_reader import (
    clear_starting_marker,
    read_plan,
    read_state_file,
    write_starting_marker,
    write_state_file,
)
from agent_backbone.services.agents._inference import get_agent_state, infer_state_from_pane
from agent_backbone.services.agents.acknowledgement import (
    find_outgoing_comment,
    has_commented_on_issue,
    rotate_action_log,
)
from agent_backbone.services.agents.interface import StateService
from agent_backbone.services.agents.models import AgentState, StateSnapshot

__all__ = [
    "AgentState",
    "StateService",
    "StateSnapshot",
    "clear_starting_marker",
    "find_outgoing_comment",
    "get_agent_state",
    "has_commented_on_issue",
    "infer_state_from_pane",
    "read_plan",
    "read_state_file",
    "rotate_action_log",
    "write_starting_marker",
    "write_state_file",
]
