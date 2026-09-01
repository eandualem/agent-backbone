#!/usr/bin/env python3
"""Claude Code hook: push agent state to the agent-backbone state directory.

Installed by ``backbone hooks install claude``. Claude Code invokes this
script for the configured hook events with a JSON payload on stdin. It
writes ``<state_dir>/<agent>.json`` (read by the backbone's readiness check)
and appends GitHub comment actions to ``<state_dir>/actions.jsonl``.

States written (runtime-agnostic vocabulary shared by every hook):

    idle                       at the prompt, nothing running
    busy                       working on a prompt
    waiting_for_human (reason) plan | permission | question

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
STATE_WAITING = "waiting_for_human"
STATE_UNKNOWN = "unknown"

REASON_PLAN = "plan"
REASON_PERMISSION = "permission"
REASON_QUESTION = "question"

_GH_COMMENT_RE = re.compile(r"\bgh\s+issue\s+comment\s+(?:\S+\s+)*?(\d+)\b")
_GH_REPO_RE = re.compile(r"(?:--repo|-R)[\s=]+([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)")
_ISSUE_NUMBER_RE = re.compile(r"(?:^|[\s#])(\d{1,7})\b")
_ISSUE_REF_RE = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d{1,7})\b")


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


def _issue_from_text(text: str) -> tuple[int | None, str | None]:
    """``(number, repo)`` mentioned in a prompt, e.g. ``owner/name#42`` or ``issue #42``."""
    ref = _ISSUE_REF_RE.search(text or "")
    if ref:
        return int(ref.group(2)), ref.group(1)
    match = _ISSUE_NUMBER_RE.search(text or "")
    return (int(match.group(1)) if match else None), None


def _plan_title(plan: str) -> str:
    for line in plan.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:120]
    return "Untitled plan"


def derive(payload: dict, current: dict | None) -> tuple[dict | None, dict | None]:
    """Map a hook payload to (new_state_record, action_record).

    ``current`` is the previously written state (used to keep ``issue``,
    ``repo`` and ``started_at`` stable across events).
    """
    event = payload.get("hook_event_name", "")
    now = time.time()
    current = current or {}
    issue = current.get("issue")
    repo = current.get("repo")
    started_at = current.get("started_at") or now

    def state(new_state: str, reason: str | None = None, **extra) -> dict:
        record = {
            "state": new_state,
            "reason": reason,
            "issue": issue,
            "repo": repo,
            "ts": now,
            "started_at": started_at,
        }
        record.update(extra)
        return record

    if event == "SessionStart":
        # Fired once Claude Code is at its prompt: ready for input.
        return state(STATE_IDLE, started_at=now), None
    if event == "SessionEnd":
        return state(STATE_UNKNOWN), None
    if event == "UserPromptSubmit":
        prompt = payload.get("prompt", "") or ""
        if "issue" in prompt.lower() or "#" in prompt:
            found, found_repo = _issue_from_text(prompt)
            if found is not None:
                issue = found
                if found_repo:
                    repo = found_repo
        return state(STATE_BUSY), None
    if event == "Stop":
        return state(STATE_IDLE), None
    if event == "Notification":
        message = (payload.get("message", "") or "").lower()
        if "permission" in message:
            return state(STATE_WAITING, REASON_PERMISSION), None
        if "waiting for your input" in message:
            return state(STATE_IDLE), None
        return None, None
    if event == "PreToolUse":
        tool = payload.get("tool_name", "")
        if tool == "ExitPlanMode":
            plan = (payload.get("tool_input") or {}).get("plan", "") or ""
            return state(
                STATE_WAITING, REASON_PLAN, plan_title=_plan_title(plan), plan_text=plan
            ), None
        if tool == "AskUserQuestion":
            return state(STATE_WAITING, REASON_QUESTION), None
        return None, None
    if event == "PostToolUse":
        tool = payload.get("tool_name", "")
        tool_input = payload.get("tool_input") or {}
        if tool in ("ExitPlanMode", "AskUserQuestion"):
            return state(STATE_BUSY), None
        return None, _comment_action(tool, tool_input, now)
    return None, None


def _comment_action(tool: str, tool_input: dict, now: float) -> dict | None:
    """Detect a GitHub issue comment posted through a tool call."""
    number: int | None = None
    repo: str | None = None
    if tool == "Bash":
        command = tool_input.get("command", "") or ""
        match = _GH_COMMENT_RE.search(command)
        if match:
            number = int(match.group(1))
            repo_match = _GH_REPO_RE.search(command)
            repo = repo_match.group(1) if repo_match else None
    elif tool.startswith("mcp__") and tool.endswith("__add_issue_comment"):
        raw = tool_input.get("issue_number") or tool_input.get("issueNumber")
        try:
            number = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            number = None
        owner = tool_input.get("owner")
        name = tool_input.get("repo")
        if owner and name:
            repo = f"{owner}/{name}"
    if number is None:
        return None
    action = {"ts": now, "action": "comment", "issue": number}
    if repo:
        action["repo"] = repo
    return action


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
    parser.add_argument("--state-dir", default=None)
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

    # The backbone exports BACKBONE_STATE_DIR into every session it starts;
    # that wins over the path baked into the installed hook command.
    raw_state_dir = os.environ.get("BACKBONE_STATE_DIR", "").strip() or args.state_dir
    if not raw_state_dir:
        return 0
    state_dir = Path(raw_state_dir).expanduser()
    try:
        record, action = derive(payload, read_current(state_dir, agent))
        if record is not None:
            write_state(state_dir, agent, record)
        if action is not None:
            append_action(state_dir, agent, action)
    except OSError:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
