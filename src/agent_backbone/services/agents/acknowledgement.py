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


def _read_receipts(action_log: str | Path | None):
    """Stream retained publication evidence, independent of the recent-command tail."""
    if action_log is None:
        return
    log_path = Path(action_log).expanduser()
    for path in (log_path.parent / "comment-receipts").glob("*.json"):
        try:
            entry = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(entry, dict):
            yield entry
    try:
        with log_path.open(errors="replace") as file:
            for line in file:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    yield entry
    except OSError:
        return


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
    include_intent: bool = True,
    comment_id: int | None = None,
) -> str | None:
    """Session that recently commented on an issue according to the hook action log.

    Log format: ``{"ts": 1234567890.0, "session": "reviewer", "action": "comment",
    "issue": 42, "repo": "owner/name"}``.
    """
    now = time.time()
    entries = (
        _read_receipts(action_log) if comment_id is not None else _read_tail(action_log, max_lines)
    )
    latest: tuple[float, str | None] | None = None
    for entry in entries:
        if entry.get("action") != "comment" or entry.get("issue") != issue_number:
            continue
        if not include_intent and entry.get("phase") != "succeeded":
            continue
        if comment_id is not None and (
            type(comment_id) is not int
            or comment_id <= 0
            or type(entry.get("comment_id")) is not int
            or entry.get("comment_id") != comment_id
            or entry.get("phase") != "succeeded"
        ):
            continue
        if not _repo_matches(entry, repo):
            continue
        if comment_id is not None:
            timestamp = entry.get("ts", 0)
            if isinstance(timestamp, (int, float)) and (latest is None or timestamp > latest[0]):
                latest = timestamp, entry.get("session")
        elif now - float(entry.get("ts", 0)) <= recency_seconds:
            return entry.get("session")
    return latest[1] if latest else None


def find_outgoing_pull_request(
    head_repo: str,
    head_ref: str,
    action_log: str | Path | None = None,
    max_lines: int = 200,
    recency_seconds: float = 900.0,
    *,
    base_repo: str = "",
    include_intent: bool = True,
    at: float | None = None,
) -> str | None:
    """Session that ran ``gh pr create`` from this head repository and branch
    within ``recency_seconds`` of ``at`` (default now).

    Log format: ``{"ts": …, "session": "app", "action": "pull_request",
    "repo": "owner/name", "head_repo": "forker/name", "branch": "feat/x"}``.
    The head repository *and* the branch must match: two forks may use the
    same branch name, and the event names the head repository exactly.

    ``head_repo`` is what the event named, ``base_repo`` the repository the
    pull request was opened against. When the event names a head, only a
    head is compared to it (an entry that predates ``head_repo`` falls back
    to its own ``repo``); when it names none — a fork deleted before the
    event arrived — the two base repositories are compared instead. An
    explicit head is therefore never satisfied by a base repository.
    """
    if not head_ref or not (head_repo or base_repo):
        return None
    now = time.time() if at is None else at
    for entry in _read_tail(action_log, max_lines):
        if entry.get("action") != "pull_request":
            continue
        if not include_intent and entry.get("phase") != "succeeded":
            continue
        entry_head = (entry.get("head_repo") or "").casefold()
        entry_base = (entry.get("repo") or "").casefold()
        if head_repo:
            if (entry_head or entry_base) != head_repo.casefold():
                continue
        elif entry_base != base_repo.casefold():
            continue
        if (entry.get("branch") or "") != head_ref:
            continue
        if abs(now - float(entry.get("ts", 0))) <= recency_seconds:
            return entry.get("session")
    return None


def has_commented_on_issue(
    issue_number: int,
    session: str,
    action_log: str | Path | None = None,
    max_lines: int = 200,
    *,
    repo: str = "",
    since: float | None = None,
    reset_comment_id: int | None = None,
) -> bool:
    """Whether a retained receipt was published after the latest acknowledgment reset."""
    for entry in _read_receipts(action_log):
        if (
            entry.get("action") == "comment"
            and entry.get("phase") == "succeeded"
            and type(entry.get("comment_id")) is int
            and entry["comment_id"] > 0
            and entry.get("issue") == issue_number
            and entry.get("session") == session
            and _repo_matches(entry, repo)
            and (
                since is None
                or (
                    entry.get("edited") is True
                    and isinstance(entry.get("ts"), (int, float))
                    and entry["ts"] > since
                )
                or (
                    entry["comment_id"] >= reset_comment_id
                    if reset_comment_id
                    else isinstance(entry.get("ts"), (int, float)) and entry["ts"] > since
                )
            )
        ):
            return True
    return False


def rotate_action_log(
    action_log: str | Path, keep_lines: int = 2000, *, receipt_retention_days: int = 30
) -> int:
    """Keep the newest ``keep_lines`` entries of the action log. Returns lines dropped.

    Successful comment receipts are immutable separate files, so rotation cannot
    discard an unprocessed or concurrently appended publication. Those files use
    the configured delivery retention period.
    """
    log_path = Path(action_log).expanduser()
    cutoff = time.time() - receipt_retention_days * 86400
    for path in (log_path.parent / "comment-receipts").glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            continue
    try:
        lines = log_path.read_text(errors="replace").splitlines()
    except OSError:
        return 0
    if len(lines) <= keep_lines:
        return 0
    kept = lines[-keep_lines:]
    tmp = log_path.with_name(f".{log_path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as file:
        os.fchmod(file.fileno(), 0o600)
        file.write("\n".join(kept) + "\n")
    os.replace(tmp, log_path)
    return len(lines) - len(kept)
