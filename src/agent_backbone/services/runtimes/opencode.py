"""OpenCode. Markers verified live against opencode 1.18.

The "Ask anything..." placeholder disappears after the first message, but
the bottom bar ("ctrl+p commands") is always visible, so idle is that bar
without the working spinner's "esc interrupt".
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path

from agent_backbone.hooks import install as hooks
from agent_backbone.services.runtimes._opencode_launch import merge_config
from agent_backbone.services.runtimes._usage import UsageBatch, count
from agent_backbone.services.runtimes.base import Runtime, read_brief
from agent_backbone.usage import UsageEvent, timestamp

log = logging.getLogger(__name__)


class OpenCode(Runtime):
    supports_exact_resume = True
    id = "opencode"
    skill_dirs = (".agents/skills",)
    display_name = "OpenCode"
    aliases = ("open-code", "open_code")
    binary = "opencode"
    brief_mode = "initial_prompt"
    model_tags = True

    runtime_markers = ("opencode", "ask anything...", "tab agents")
    placeholder_fragments = ("ask anything...", "ctrl+p commands")
    status_fragments = ("tab agents", "ctrl+p commands")
    busy_markers = ("esc interrupt",)
    provider_error_patterns = (
        r"^(?:error:\s*)?you exceeded your current quota\b",
        r"^(?:error:\s*)?(?:rate limit (?:reached|exceeded)|resource_exhausted)\b",
        r"^(?:error:\s*)?selected model is at capacity\b",
    )
    # "△ Permission required … Allow once  Allow always  Reject … enter confirm"
    # with "Allow once" preselected (live capture, 1.18).
    prompt_markers = ("permission required", "allow once", "allow always")
    approve_keys = ("Enter",)
    # "--auto  auto-approve permissions that are not explicitly denied"
    # (opencode 1.18 TUI); a `permission` deny in the user's config still
    # holds. OpenCode has no OS sandbox: this is trust on the machine.
    unattended_args = ("--auto",)

    def hook_settings(self, data_dir, state_dir, *, python=None):
        raise RuntimeError("OpenCode state comes from a plugin, not a command hook")

    def hook_launch_env(
        self,
        data_dir: Path | str | None,
        state_dir: Path | str | None,
        *,
        env: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """``OPENCODE_CONFIG_CONTENT`` → ``{"plugin": ["file://…/opencode_hook.js"]}``.

        OpenCode merges that inline configuration over the user's own, so
        the plugin loads for this session only; nothing in
        ``~/.config/opencode`` or the repository is touched. The plugin reads
        ``BACKBONE_AGENT`` and ``BACKBONE_STATE_DIR`` from the session.
        Verified live against OpenCode 1.18 (TUI).
        """
        if data_dir is None or state_dir is None:
            return {}
        # Inline config has higher precedence than the user's files. Preserve
        # its provider options, permission denies and existing plugins. If we
        # cannot compose it (e.g. JSONC), leave it intact for OpenCode to read
        # and use terminal state detection instead of dropping configuration.
        # When no explicit value is supplied, only the launched process knows
        # the effective tmux-server environment. Its wrapper composes it there.
        if env is None or "OPENCODE_CONFIG_CONTENT" not in env:
            return {}
        try:
            plugin = hooks.install_hook_files(Path(data_dir)) / self.hook_script
        except OSError as exc:
            log.warning("Could not write the hook files: %s", exc)
            return {}
        content = merge_config(env["OPENCODE_CONFIG_CONTENT"], plugin.as_uri())
        if content is None:
            log.warning(
                "Skipping OpenCode hook injection: preserving unsupported inline configuration"
            )
            return {}
        return {"OPENCODE_CONFIG_CONTENT": content}

    def build_command(self, **kwargs):
        command = super().build_command(**kwargs)
        data_dir, state_dir = kwargs.get("data_dir"), kwargs.get("state_dir")
        if command is None or data_dir is None or state_dir is None:
            return command
        try:
            plugin = hooks.install_hook_files(Path(data_dir)) / self.hook_script
        except OSError as exc:
            log.warning("Could not install OpenCode launch hooks: %s", exc)
            return command
        return [
            hooks.default_python(),
            str(Path(__file__).with_name("_opencode_launch.py")),
            str(plugin),
            *command,
        ]

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
            or Path.home() / ".local/share"
        )
        path = root / "opencode/opencode.db"
        if not path.is_file():
            return []
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            found = conn.execute("SELECT 1 FROM session WHERE id=?", (session_id,)).fetchone()
        return [path] if found else []

    def usage_children(
        self, path: Path, session_id: str, env: dict[str, str]
    ) -> list[tuple[str, Path]]:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            return [
                (row[0], path)
                for row in conn.execute("SELECT id FROM session WHERE parent_id=?", (session_id,))
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
                    "SELECT id,time_updated,data FROM message WHERE session_id=? "
                    "AND time_updated>=? AND (time_updated>? OR (time_updated=? AND id>?)) "
                    "ORDER BY time_updated,id LIMIT 10001",
                    (state["_session_id"], floor, after_time, after_time, after_id),
                ).fetchall()
            batch.caught_up = len(rows) <= 10000
            for key, updated, encoded in rows[:10000]:
                batch.offset = max(batch.offset, updated)
                state["scan_floor"] = floor
                state["scan_after"] = (updated, key)
                data = json.loads(encoded)
                if not isinstance(data, dict):
                    raise ValueError("invalid message record")
                if data.get("role") != "assistant" or not data.get("tokens"):
                    continue
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
                    model=data.get("modelID") or "unknown",
                    provider=data.get("providerID") or "unknown",
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


RUNTIME = OpenCode()
