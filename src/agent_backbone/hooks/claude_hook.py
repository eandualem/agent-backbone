#!/usr/bin/env python3
"""Claude Code hook: push agent state to the agent-backbone state directory.

Installed by ``backbone hooks install claude``. Claude Code invokes this
script for the configured hook events with a JSON payload on stdin. It
writes ``<state_dir>/<agent>.json`` (read by the backbone's readiness check)
and appends GitHub comment actions to ``<state_dir>/actions.jsonl``.

Standard library only — it must run under any ``python3``.

Usage (as configured by the installer):
    claude_hook.py --state-dir /path/to/state [--agent NAME]

The agent name is resolved from ``--agent``, then ``$BACKBONE_AGENT``, then
the surrounding tmux session name. Without one the hook exits silently.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

STATE_IDLE = "idle"
STATE_BUSY = "busy"
STATE_PLAN_WAITING = "plan_waiting"
STATE_PERMISSION_WAITING = "permission_waiting"
STATE_STARTING = "starting"
STATE_UNKNOWN = "unknown"

_GH_COMMENT_RE = re.compile(r"\bgh\s+issue\s+comment\s+(?:\S+\s+)*?(\d+)\b")
_ISSUE_NUMBER_RE = re.compile(r"(?:^|[\s#])(\d{1,7})\b")


def resolve_agent(explicit: str | None) -> str | None:
    """Agent name from CLI flag, environment, or the enclosing tmux session."""
    if explicit:
        return explicit
    env_name = os.environ.get("BACKBONE_AGENT", "").strip()
    if env_name:
        return env_name
    if os.environ.get("TMUX"):
        try:
            out = subprocess.run(
                ["tmux", "display-message", "-p", "#S"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            name = out.stdout.strip()
            if out.returncode == 0 and name:
                return name
        except (OSError, subprocess.SubprocessError):
            return None
    return None


def _issue_from_text(text: str) -> int | None:
    match = _ISSUE_NUMBER_RE.search(text or "")
    return int(match.group(1)) if match else None


def _plan_title(plan: str) -> str:
    for line in plan.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:120]
    return "Untitled plan"


def derive(payload: dict, current: dict | None) -> tuple[dict | None, dict | None]:
    """Map a hook payload to (new_state_record, action_record).

    ``current`` is the previously written state (used to keep ``issue`` and
    ``started_at`` stable across events). Either return value may be None.
    """
    event = payload.get("hook_event_name", "")
    now = time.time()
    current = current or {}
    issue = current.get("issue")
    started_at = current.get("started_at") or now

    def state(new_state: str, **extra) -> dict:
        record = {"state": new_state, "issue": issue, "ts": now, "started_at": started_at}
        record.update(extra)
        return record

    if event == "SessionStart":
        # Claude Code fires this once it is at the prompt, so the agent is
        # ready for input — reporting "starting" here would block deliveries
        # until the first prompt.
        return state(STATE_IDLE, started_at=now), None
    if event == "SessionEnd":
        return state(STATE_UNKNOWN), None
    if event == "UserPromptSubmit":
        prompt = payload.get("prompt", "") or ""
        found = _issue_from_text(prompt) if "issue" in prompt.lower() else None
        if found is not None:
            issue = found
        return state(STATE_BUSY), None
    if event == "Stop":
        return state(STATE_IDLE), None
    if event == "Notification":
        message = (payload.get("message", "") or "").lower()
        if "permission" in message:
            return state(STATE_PERMISSION_WAITING), None
        if "waiting for your input" in message:
            return state(STATE_IDLE), None
        return None, None
    if event == "PreToolUse":
        if payload.get("tool_name") == "ExitPlanMode":
            plan = (payload.get("tool_input") or {}).get("plan", "") or ""
            return state(STATE_PLAN_WAITING, plan_title=_plan_title(plan), plan_text=plan), None
        return None, None
    if event == "PostToolUse":
        tool = payload.get("tool_name", "")
        tool_input = payload.get("tool_input") or {}
        if tool == "ExitPlanMode":
            return state(STATE_BUSY), None
        action = _comment_action(tool, tool_input, now)
        return None, action
    return None, None


def _comment_action(tool: str, tool_input: dict, now: float) -> dict | None:
    """Detect a GitHub issue comment posted through a tool call."""
    number: int | None = None
    if tool == "Bash":
        match = _GH_COMMENT_RE.search(tool_input.get("command", "") or "")
        if match:
            number = int(match.group(1))
    elif tool.startswith("mcp__github__") and "comment" in tool:
        raw = tool_input.get("issue_number") or tool_input.get("issueNumber")
        try:
            number = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            number = None
    if number is None:
        return None
    return {"ts": now, "action": "comment", "issue": number}


def write_state(state_dir: Path, agent: str, record: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    plan_text = record.pop("plan_text", None)
    if plan_text is not None:
        plans_dir = state_dir / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        plan_path = plans_dir / f"{agent}.md"
        plan_path.write_text(plan_text)
        record["plan_file"] = str(plan_path)
    target = state_dir / f"{agent}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record))
    os.replace(tmp, target)


def append_action(state_dir: Path, agent: str, action: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "actions.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({**action, "session": agent}) + "\n")


def read_current(state_dir: Path, agent: str) -> dict | None:
    try:
        return json.loads((state_dir / f"{agent}.json").read_text())
    except (OSError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--agent", default=None)
    parser.add_argument("--tag", default=None, help="marker used by the installer; ignored")
    args = parser.parse_args(argv)

    agent = resolve_agent(args.agent)
    if not agent:
        return 0

    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0

    state_dir = Path(args.state_dir).expanduser()
    try:
        record, action = derive(payload, read_current(state_dir, agent))
        if record is not None:
            write_state(state_dir, agent, record)
        if action is not None:
            append_action(state_dir, agent, action)
    except OSError:
        # Never break the agent because the backbone's disk is unhappy.
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
