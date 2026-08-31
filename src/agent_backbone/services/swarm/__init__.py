"""Swarm service — coordinator + members sharing one worktree, working one issue."""

from agent_backbone.services.swarm._roster import MemberSpec, parse_member_spec, parse_roster
from agent_backbone.services.swarm._templates import render_brief
from agent_backbone.services.swarm.interface import (
    SwarmError,
    SwarmResult,
    create_swarm,
    parse_issue_ref,
    swarm_overview,
    teardown_for_issue,
    teardown_swarm,
)

__all__ = [
    "MemberSpec",
    "SwarmError",
    "SwarmResult",
    "create_swarm",
    "parse_issue_ref",
    "parse_member_spec",
    "parse_roster",
    "render_brief",
    "swarm_overview",
    "teardown_for_issue",
    "teardown_swarm",
]
