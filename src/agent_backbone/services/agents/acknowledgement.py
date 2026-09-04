"""Acknowledgement evidence from the hook action log (``actions.jsonl``)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

_TAIL_BLOCK = 64 * 1024
"""Bytes read from the end of the action log per lookup: a few hundred
entries, far more than ``max_lines``. The log itself is rotated by the
prune job; the reader never loads the whole file."""


def _read_tail(action_log: str | Path | None, max_lines: int) -> list[dict]:
    if action_log is None:
        return []
    log_path = Path(action_log).expanduser()
    try:
        with log_path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _TAIL_BLOCK))
            chunk = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = chunk.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    entries: list[dict] = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _repo_matches(entry: dict, repo: str) -> bool:
    """Scoped lookups require the entry to name the same repository.

    An entry without repo metadata (e.g. ``gh issue comment`` run without
    ``--repo``) matches only unscoped lookups — otherwise a comment on A#42
    could be attributed to B#42.
    """
    entry_repo = entry.get("repo") or ""
    return not repo or (bool(entry_repo) and entry_repo.casefold() == repo.casefold())


def find_outgoing_comment(
    issue_number: int,
    action_log: str | Path | None = None,
    max_lines: int = 50,
    recency_seconds: float = 30.0,
    *,
    repo: str = "",
) -> str | None:
    """Session that recently commented on an issue according to the hook action log.

    Log format: ``{"ts": 1234567890.0, "session": "reviewer", "action": "comment",
    "issue": 42, "repo": "owner/name"}``.
    """
    now = time.time()
    for entry in _read_tail(action_log, max_lines):
        if entry.get("action") != "comment" or entry.get("issue") != issue_number:
            continue
        if not _repo_matches(entry, repo):
            continue
        if now - float(entry.get("ts", 0)) <= recency_seconds:
            return entry.get("session")
    return None


def find_outgoing_pull_request(
    head_repo: str,
    head_ref: str,
    action_log: str | Path | None = None,
    max_lines: int = 200,
    recency_seconds: float = 900.0,
) -> str | None:
    """Session that recently ran ``gh pr create`` from this head repository and branch.

    Log format: ``{"ts": …, "session": "app", "action": "pull_request",
    "repo": "owner/name", "head_repo": "forker/name", "branch": "feat/x"}``.
    The head repository *and* the branch must match: two forks may use the
    same branch name, and the event names the head repository exactly.
    An older entry without ``head_repo`` is matched on ``repo`` instead.
    """
    if not head_repo or not head_ref:
        return None
    now = time.time()
    for entry in _read_tail(action_log, max_lines):
        if entry.get("action") != "pull_request":
            continue
        # The event's head repository, matched against what the hook recorded:
        # its head repository, or the base one when the event named no head
        # (a deleted fork) or the entry predates head_repo.
        candidates = {
            (entry.get("head_repo") or "").casefold(),
            (entry.get("repo") or "").casefold(),
        } - {""}
        if head_repo.casefold() not in candidates:
            continue
        if (entry.get("branch") or "") != head_ref:
            continue
        if now - float(entry.get("ts", 0)) <= recency_seconds:
            return entry.get("session")
    return None


def has_commented_on_issue(
    issue_number: int,
    session: str,
    action_log: str | Path | None = None,
    max_lines: int = 200,
    *,
    repo: str = "",
) -> bool:
    """Whether a session has ever commented on this issue (per action log)."""
    for entry in _read_tail(action_log, max_lines):
        if (
            entry.get("action") == "comment"
            and entry.get("issue") == issue_number
            and entry.get("session") == session
            and _repo_matches(entry, repo)
        ):
            return True
    return False


def rotate_action_log(action_log: str | Path, keep_lines: int = 2000) -> int:
    """Keep the newest ``keep_lines`` entries of the action log. Returns lines dropped.

    The hook appends forever; nothing else prunes the file. Runs in the
    prune job. The rewrite is atomic; a line the hook appends during it is
    lost, which costs at most one acknowledgement that GitHub confirms on
    the next poll anyway.
    """
    log_path = Path(action_log).expanduser()
    try:
        lines = log_path.read_text(errors="replace").splitlines()
    except OSError:
        return 0
    if len(lines) <= keep_lines:
        return 0
    kept = lines[-keep_lines:]
    tmp = log_path.with_name(f".{log_path.name}.{os.getpid()}.tmp")
    tmp.write_text("\n".join(kept) + "\n")
    os.replace(tmp, log_path)
    return len(lines) - len(kept)
