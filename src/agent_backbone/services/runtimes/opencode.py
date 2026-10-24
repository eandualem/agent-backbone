"""OpenCode current native integration (2.1.4+).

The "Ask anything..." placeholder disappears after the first message, but
the bottom bar ("ctrl+p commands") is always visible, so idle is that bar
without the working spinner's "esc interrupt".
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

from agent_backbone.fs import atomic_write_text
from agent_backbone.hooks import install as hooks
from agent_backbone.services.runtimes._opencode_v2_launch import failure_path
from agent_backbone.services.runtimes._pane import sanitize_pane_content
from agent_backbone.services.runtimes._usage import UsageBatch, count
from agent_backbone.services.runtimes.base import (
    Runtime,
    TranscriptEntry,
    _error_foreground,
    agent_home,
    has_text,
    read_brief,
    transcript_clock,
)
from agent_backbone.usage import UsageEvent, timestamp

# OpenCode's boolean flags, as it reads them (1.18: case-sensitive).
_TRUE = ("true", "yes", "on", "1", "y")


def _setting(env: dict[str, str], key: str) -> str:
    """An environment value for the agent: its own ``env``, then the backbone's."""
    return env.get(key) or os.environ.get(key) or ""


class OpenCode(Runtime):
    supports_exact_resume = True
    id = "opencode"
    skill_dirs = (".agents/skills",)
    # 1.18.32; the first skill found under a name wins.
    skill_read_dirs = (".claude/skills", ".agents/skills", ".opencode/skills", ".opencode/skill")
    display_name = "OpenCode"
    refusal_check = "OpenCode's permission policy"
    aliases = ("open-code", "open_code")
    binary = "opencode"
    brief_mode = "initial_prompt"
    # An interrupted turn, or a rejected permission, ends in session.status
    # idle and session.idle (live, 1.18.32): its plugin reports it.
    declared_capabilities = frozenset(
        {"observed-model", "project-instructions", "request-diagnostics", "interrupted-turn"}
    )
    model_tags = True
    efforts = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

    def split_model(self, spec):
        if not spec or "::" not in spec:
            return spec or None, None
        model, effort = spec.rsplit("::", 1)
        if not effort.strip():
            raise ValueError("OpenCode model::effort requires an effort after '::'")
        return model or None, effort.strip().lower()

    runtime_markers = ("opencode", "ask anything...", "tab agents")
    placeholder_fragments = ("ask anything...", "ctrl+p commands")
    status_fragments = ("tab agents", "ctrl+p commands")
    busy_markers = ("esc interrupt",)
    provider_error_patterns = (
        r"^(?:error:\s*)?you exceeded your current quota\b",
        r"^(?:error:\s*)?(?:rate limit (?:reached|exceeded)|resource_exhausted)\b",
        r"^(?:error:\s*)?selected model is at capacity\b",
        r"^(?:error:\s*)?you[’']ve reached your (?:\d+-hour )?usage limit\b",
    )
    # "△ Permission required … Allow once  Allow always  Reject … enter confirm"
    # with "Allow once" preselected (live capture, 1.18).
    prompt_markers = ("permission required", "allow once", "allow always")
    approve_keys = ("Enter",)
    # Escape rejects the tool call wherever the cursor is and ends the turn
    # ("The user rejected permission to use this specific tool call", live, 1.18).
    deny_keys = ("Escape",)
    # plan_exit's question: "1. Yes" (switch to the build agent and start
    # implementing), "2. No" (stay with the plan agent); a digit answers at
    # once, and No ends the turn in the plan agent (live, 1.18.32).
    plan_approve_keys = ("1",)
    plan_reject_keys = ("2",)
    # "--auto  auto-approve permissions that are not explicitly denied"
    # (opencode 1.18 TUI); a `permission` deny in the user's config still
    # holds. OpenCode has no OS sandbox: this is trust on the machine.
    unattended_args = ("--auto",)
    hook_context = True  # the plugin adds a user message to the running turn

    def provider_failure(self, pane_content: str) -> str | None:
        # 1.18.32 (live): a failure is a block of "┃" lines with a red bar and
        # grey text, above the "▣  Build · <model>" line under each message,
        # the input box (the "┃" lines down to its "╹▀" edge) and the footer
        # below that box. A reply or a quoted error has no red bar.
        lines = pane_content.splitlines()
        clean = [sanitize_pane_content(raw).strip() for raw in lines]
        edge = max((i for i, text in enumerate(clean) if text.startswith("╹")), default=None)
        if edge is None:
            return super().provider_failure(pane_content)
        end = edge
        while end and clean[end - 1].startswith("┃"):
            end -= 1
        # v2.1.4 draws a red, unboxed error immediately before the turn footer.
        latest = end
        while latest and not clean[latest - 1]:
            latest -= 1
        if latest and re.fullmatch(
            r".+ · .+ · (?:\d+ms|\d+\.\d+s|\d+m \d+s|\d+h \d+m|\d+d \d+h)",
            clean[latest - 1],
        ):
            latest -= 1
            while latest and not clean[latest - 1]:
                latest -= 1
            # capture-pane preserves SGR state across wrapped rows.
            red_prefixes = []
            color = ""
            for raw in lines[:latest]:
                prefix = re.match(r"(?:\s|\x1b\[[0-9;]*m)*[^\s\x1b]+", raw)
                red_prefixes.append(bool(prefix and _error_foreground(color + prefix[0])))
                styles = "".join(re.findall(r"\x1b\[[0-9;]*m", raw))
                color = "\x1b[31m" if _error_foreground(color + styles + "x") else ""
            start = latest
            while start and clean[start - 1] and red_prefixes[start - 1]:
                start -= 1
            text = " ".join(clean[start:latest])
            if any(
                re.match(pattern, text, re.IGNORECASE) for pattern in self.provider_error_patterns
            ):
                return text[:500]
        block: list[str] = []
        for raw, text in reversed(list(zip(lines[:end], clean[:end], strict=True))):
            if text.startswith("┃"):
                block.insert(0, raw)
            elif block or (text and not text.startswith("▣")):
                break
        if not block or not all(
            _error_foreground(raw.split("┃", 1)[0] + "┃", glyphs="┃") for raw in block
        ):
            return None
        texts = (sanitize_pane_content(raw).strip().lstrip("┃").strip() for raw in block)
        text = " ".join(line for line in texts if line)
        if any(re.match(pattern, text, re.IGNORECASE) for pattern in self.provider_error_patterns):
            return text[:500]
        return None

    def hook_settings(self, data_dir, state_dir, *, python=None):
        raise RuntimeError("OpenCode state comes from a plugin, not a command hook")

    def check_effort(self, effort):
        super().check_effort(effort)
        if effort and self._version() < (2, 1, 4):
            raise RuntimeError("OpenCode effort selection requires @opencode/cli 2.1.4 or newer")

    def _version(self) -> tuple[int, ...]:
        version = self.installed_version() or ""
        match = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
        return tuple(map(int, match.groups())) if match else ()

    def build_command(self, **kwargs):
        if not self.available():
            raise RuntimeError(f"Runtime '{self.id}' binary not found: {self.binary}")
        version = self._version()
        if not version:
            raise RuntimeError("Could not determine the OpenCode version; launch refused")
        if version < (2, 1, 4):
            raise RuntimeError(
                "OpenCode integration requires current @opencode/cli (2.1.4 or newer)"
            )
        model, effort = self.split_model(kwargs.get("model"))
        if effort and not model:
            raise RuntimeError("OpenCode effort selection requires a provider/model")
        Runtime.check_effort(self, effort)
        resume = kwargs.get("resume", False)
        selection = {"model": model, "effort": effort, "resume": resume}
        kwargs = {**kwargs, "model": None, "resume": False}
        if resume:
            kwargs["brief_file"] = None
        command = super().build_command(**kwargs)
        if command is None:
            return command
        command = [
            hooks.default_python(),
            str(Path(__file__).with_name("_opencode_v2_launch.py")),
            json.dumps(selection),
            command[0],
            "--standalone",
            *command[1:],
        ]
        data_dir, state_dir = kwargs.get("data_dir"), kwargs.get("state_dir")
        if data_dir is None or state_dir is None:
            return command
        try:
            plugin = hooks.install_hook_files(Path(data_dir)) / "opencode-v2"
            plugin.mkdir(exist_ok=True)
            atomic_write_text(
                plugin / "index.js", hooks.hook_source("opencode_v2_hook.js").read_text()
            )
        except OSError as exc:
            raise RuntimeError("Could not install OpenCode launch hooks") from exc
        return [
            hooks.default_python(),
            str(Path(__file__).with_name("_opencode_launch.py")),
            str(plugin),
            *command,
        ]

    def startup_error(self, state_dir, agent, launch_id):
        try:
            return failure_path(Path(state_dir), agent, launch_id).read_text()[:1000] or None
        except OSError:
            return None

    def user_skill_dirs(self, env):
        home = agent_home(env)
        config = Path(_setting(env, "XDG_CONFIG_HOME") or home / ".config").expanduser()
        # 1.18.32 reads the first two recursively; its own directory measured 1.18.29.
        return [
            home / ".agents" / "skills",
            home / ".claude" / "skills",
            config / "opencode" / "skills",
        ]

    def reload_skills(self, state_dir, session):
        # The native plugin reloads skills between turns, keeping the conversation.
        marker = Path(state_dir) / f"{session}.skills-reload"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        return "OpenCode reloads its skills at its next pause between turns"

    def user_instructions(self, env, project=None):
        home = agent_home(env)
        config = Path(_setting(env, "XDG_CONFIG_HOME") or home / ".config").expanduser()
        candidates = [config / "opencode" / "AGENTS.md"]
        # Claude Code's user memory stands in unless disabled (1.18: either flag,
        # true as its boolean config reads it, case-sensitive).
        flags = ("OPENCODE_DISABLE_CLAUDE_CODE", "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT")
        if not any(_setting(env, f) in _TRUE for f in flags):
            candidates.append(home / ".claude" / "CLAUDE.md")
        # The first file that exists is the one read, even an empty one.
        first = next((path for path in candidates if path.exists()), None)
        return [first] if first is not None and has_text(first) else []

    def plan_approval_needs(self, env):
        return (
            "plan approval is unavailable on OpenCode: "
            "switch between Plan and Build in the native session"
        )

    def launch_args(self, *, model, resume, brief_file, pre_trust, data_dir, state_dir):
        args: list[str] = []
        if model:
            args.extend(["--model", model])
        if resume:
            args.extend(["--session", resume] if isinstance(resume, str) else ["--continue"])
        if brief_file is not None and (brief := read_brief(brief_file)):
            args.extend(["--prompt", brief])
        return args

    usage_supported = True

    def usage_paths(self, session_id: str, env: dict[str, str]) -> list[Path]:
        root = Path(
            env.get("XDG_DATA_HOME")
            or os.environ.get("XDG_DATA_HOME")
            or agent_home(env) / ".local/share"
        )
        path = root / "opencode/opencode.db"
        if not path.is_file():
            return []
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            found = conn.execute("SELECT 1 FROM session_v2 WHERE id=?", (session_id,)).fetchone()
        return [path] if found else []

    def usage_children(
        self, path: Path, session_id: str, env: dict[str, str]
    ) -> list[tuple[str, Path]]:
        if not path.is_file():
            return []  # Parent and child usage share the same database.
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            return [
                (row[0], path)
                for row in conn.execute(
                    "SELECT id FROM session_v2 WHERE parent_id=?", (session_id,)
                )
            ]

    def read_usage(self, path: Path, offset: int, state: dict):
        state = dict(state)
        batch = UsageBatch(offset, state)
        try:
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
                # Replay the boundary timestamp to catch same-millisecond revisions.
                floor = state.get("scan_floor", max(0, offset - 1000))
                after_time, after_id = state.get("scan_after", (floor, ""))
                rows = conn.execute(
                    "SELECT id,time_updated,data,type FROM session_message WHERE session_id=? "
                    "AND time_updated>=? AND (time_updated>? OR (time_updated=? AND id>?)) "
                    "ORDER BY time_updated,id LIMIT 10001",
                    (state["_session_id"], floor, after_time, after_time, after_id),
                ).fetchall()
            batch.caught_up = len(rows) <= 10000
            for key, updated, encoded, kind in rows[:10000]:
                batch.offset = max(batch.offset, updated)
                state["scan_floor"] = floor
                state["scan_after"] = (updated, key)
                data = json.loads(encoded)
                if not isinstance(data, dict):
                    raise ValueError("invalid message record")
                if kind not in ("assistant", "compaction") or not data.get("tokens"):
                    continue
                model = data.get("model") or {}
                if not isinstance(model, dict):
                    raise ValueError("invalid model record")
                tokens = data["tokens"]
                if not isinstance(tokens, dict):
                    raise ValueError("invalid token usage")
                cache = tokens.get("cache") or {}
                if not isinstance(cache, dict):
                    raise ValueError("invalid token cache usage")
                # This runtime reports reasoning separately from text output.
                event = UsageEvent(
                    key=key,
                    at=timestamp(data["time"]["created"] / 1000),
                    revision_at=timestamp(updated / 1000),
                    model=model.get("id") or "unknown",
                    provider=model.get("providerID") or "unknown",
                    reported_cost_usd=data.get("cost"),
                    input_tokens=count(tokens, "input"),
                    output_tokens=count(tokens, "output") + count(tokens, "reasoning"),
                    reasoning_tokens=count(tokens, "reasoning"),
                    cache_read_tokens=count(cache, "read"),
                    cache_write_tokens=count(cache, "write"),
                    cache_duration_known=not count(cache, "write"),
                    context_tokens=count(tokens, "input")
                    + count(cache, "read")
                    + count(cache, "write"),
                )
                batch.events.append(event)
            if batch.caught_up:
                state.pop("scan_floor", None)
                state.pop("scan_after", None)
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
            batch.error = type(exc).__name__
            batch.caught_up = False
            if isinstance(exc, (ValueError, KeyError, TypeError)):
                state["partial"] = True
        return batch

    transcript_supported = True

    def transcript_page(
        self,
        path: Path,
        session_id: str,
        *,
        limit: int,
        since: int | None = None,
        before: int | None = None,
        end: int | None = None,
    ) -> tuple[list[TranscriptEntry], bool, bool]:
        """Completed assistant text in the current native conversation, by position."""
        messages = (
            "WITH positioned AS (SELECT m.time_created * 1000 + ROW_NUMBER() "
            "OVER (PARTITION BY m.time_created ORDER BY m.seq, p.key) - 1 AS position, "
            "json_object('text', json_extract(p.value, '$.text'), 'time', "
            "json_object('end', json_extract(m.data, '$.time.completed'))) AS data "
            "FROM session_message m, json_each(m.data, '$.content') p "
            "WHERE m.session_id=? AND m.type='assistant' "
            "AND json_extract(m.data, '$.time.completed') IS NOT NULL "
            "AND json_extract(p.value, '$.type')='text'), "
            "messages AS (SELECT position, data FROM positioned "
            "WHERE trim(json_extract(data, '$.text'), ' ' || char(9, 10, 13)) != '') "
        )
        try:
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:

                def exists(condition: str, value: int) -> bool:
                    query = f"{messages}SELECT EXISTS(SELECT 1 FROM messages WHERE {condition})"
                    return bool(conn.execute(query, (session_id, value)).fetchone()[0])

                if since is not None:
                    bound, params = "", [session_id, since]
                    if end is not None:
                        bound, params = "AND position < ? ", [*params, end]
                    rows = conn.execute(
                        f"{messages}SELECT position, data FROM messages WHERE position >= ? "
                        f"{bound}ORDER BY position LIMIT ?",
                        (*params, limit + 1),
                    ).fetchall()
                    more_before, more_after = exists("position < ?", since), len(rows) > limit
                    rows = rows[:limit]
                else:
                    bound, params = "", [session_id]
                    if before is not None:
                        bound, params = "WHERE position < ? ", [*params, before]
                    rows = conn.execute(
                        f"{messages}SELECT position, data FROM messages "
                        f"{bound}ORDER BY position DESC LIMIT ?",
                        (*params, limit + 1),
                    ).fetchall()
                    more_before = len(rows) > limit
                    more_after = before is not None and exists("position >= ?", before)
                    rows = rows[:limit][::-1]
        except sqlite3.Error as exc:
            raise ValueError(f"OpenCode database: {exc}") from exc
        entries = []
        for position, encoded in rows:
            part = json.loads(encoded)
            ended = part["time"]["end"]
            clock = (
                transcript_clock(timestamp(ended / 1000)) if isinstance(ended, (int, float)) else ""
            )
            entries.append(
                TranscriptEntry(clock, "assistant", part["text"], position, position + 1)
            )
        return entries, more_before, more_after


RUNTIME = OpenCode()
