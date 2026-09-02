"""Agents — the registry (``store``), their state (``_inference``) and their
sessions (``launch``). ``AgentSpec`` itself lives in ``config``."""

from agent_backbone.services.agents._file_reader import (
    clear_starting_marker,
    read_plan,
    read_state_file,
    write_starting_marker,
    write_state_file,
)
from agent_backbone.services.agents._inference import (
    agent_state,
    get_agent_state,
    infer_state_from_pane,
)
from agent_backbone.services.agents.acknowledgement import (
    find_outgoing_comment,
    has_commented_on_issue,
    rotate_action_log,
)
from agent_backbone.services.agents.launch import (
    StartResult,
    approve_agent,
    approve_plan,
    start_agent,
    stop_agent,
    wait_until_ready,
)
from agent_backbone.services.agents.models import AgentState, StateSnapshot
from agent_backbone.services.agents.store import AgentStore

__all__ = [
    "AgentState",
    "AgentStore",
    "StartResult",
    "StateSnapshot",
    "agent_state",
    "approve_agent",
    "approve_plan",
    "clear_starting_marker",
    "find_outgoing_comment",
    "get_agent_state",
    "has_commented_on_issue",
    "infer_state_from_pane",
    "read_plan",
    "read_state_file",
    "rotate_action_log",
    "start_agent",
    "stop_agent",
    "wait_until_ready",
    "write_starting_marker",
    "write_state_file",
]
