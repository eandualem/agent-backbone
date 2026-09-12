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
    bind_task,
    get_agent_state,
    infer_state_from_pane,
    note_submission,
)
from agent_backbone.services.agents._locks import lifecycle_lock
from agent_backbone.services.agents.acknowledgement import (
    find_outgoing_comment,
    find_outgoing_pull_request,
    has_commented_on_issue,
    rotate_action_log,
)
from agent_backbone.services.agents.audit import record_answer
from agent_backbone.services.agents.instructions import instruction_preview
from agent_backbone.services.agents.launch import (
    StartResult,
    approve_agent,
    deny_agent,
    plan_control,
    start_agent,
    stop_agent,
    wait_until_ready,
)
from agent_backbone.services.agents.models import AgentState, StateSnapshot, prompt_id
from agent_backbone.services.agents.skills_preview import skills_preview
from agent_backbone.services.agents.store import AgentStore

__all__ = [
    "AgentConfigView",
    "AgentState",
    "AgentStore",
    "EnrichedAgent",
    "StartResult",
    "StateSnapshot",
    "agent_state",
    "approve_agent",
    "bind_task",
    "build_enriched_agent",
    "build_session_snapshot",
    "clear_starting_marker",
    "collect_usage",
    "deny_agent",
    "find_outgoing_comment",
    "find_outgoing_pull_request",
    "get_agent_state",
    "has_commented_on_issue",
    "infer_state_from_pane",
    "instruction_preview",
    "lifecycle_lock",
    "listable_sessions",
    "note_submission",
    "plan_control",
    "prompt_id",
    "read_plan",
    "read_state_file",
    "record_answer",
    "rotate_action_log",
    "skills_preview",
    "start_agent",
    "stop_agent",
    "usage_view",
    "wait_until_ready",
    "write_starting_marker",
    "write_state_file",
]

from agent_backbone.services.agents.queries import (
    AgentConfigView,
    EnrichedAgent,
    build_enriched_agent,
    build_session_snapshot,
    listable_sessions,
)
from agent_backbone.services.agents.usage import collect_usage, usage_view
