"""OpenAI Codex CLI. Markers verified live against codex-cli 0.152."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
import tomllib
from pathlib import Path

from agent_backbone.fs import atomic_write_text
from agent_backbone.services.runtimes._pane import sanitize_pane_content
from agent_backbone.services.runtimes._usage import count
from agent_backbone.services.runtimes.base import Runtime, RuntimeDiagnostic, read_brief
from agent_backbone.usage import UsageEvent, timestamp

log = logging.getLogger(__name__)
_usage_lineage: dict[str, tuple[float, dict[str, list[tuple[str, Path]]]]] = {}


def pre_trust_codex_directory(directory: Path | str, *, codex_config: Path | None = None) -> bool:
    """Mark a directory as trusted in Codex's ``~/.codex/config.toml``.

    Writes the same record Codex's own trust dialog writes
    (``[projects."<dir>"] trust_level = "trusted"``). A directory that already
    has any ``projects`` entry is left untouched — the user decided. The write
    is best-effort: on any error the dialog simply appears as before. The
    read-modify-write is not locked against Codex itself (which has no writer
    protocol to join); the window is a few milliseconds at agent start.
    """
    path = str(Path(directory).expanduser().resolve())
    config_file = codex_config or (Path.home() / ".codex" / "config.toml")
    try:
        raw = config_file.read_text() if config_file.is_file() else ""
        data = tomllib.loads(raw)
        projects = data.get("projects")
        existing = projects.get(path) if isinstance(projects, dict) else None
        if existing is not None:
            # Valid TOML with an unexpected shape is the user's; leave it alone.
            return isinstance(existing, dict) and existing.get("trust_level") == "trusted"
        # json.dumps yields a TOML basic string: quotes and backslashes in the
        # directory name cannot open another table or change the key.
        entry = f'\n[projects.{json.dumps(path)}]\ntrust_level = "trusted"\n'
        updated = raw.rstrip("\n") + "\n" + entry if raw else entry.lstrip("\n")
        tomllib.loads(updated)  # never leave codex an unparseable config
        atomic_write_text(config_file, updated)
        log.info("Pre-trusted %s for Codex", path)
        return True
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        log.warning("Could not pre-trust %s for Codex (the trust dialog will appear)", path)
        return False


def _toml_string(value: str) -> str:
    # A JSON string is a valid TOML basic string for the escapes json emits.
    return json.dumps(value)


def _toml_entries(entries: list[dict]) -> str:
    """The TOML inline form of one event's hook entries."""
    parts = []
    for entry in entries:
        hooks = ", ".join(
            f"{{type = {_toml_string(h['type'])}, command = {_toml_string(h['command'])}, "
            f"timeout = {int(h['timeout'])}}}"
            for h in entry["hooks"]
        )
        fields = []
        if "matcher" in entry:
            fields.append(f"matcher = {_toml_string(entry['matcher'])}")
        fields.append(f"hooks = [{hooks}]")
        parts.append("{" + ", ".join(fields) + "}")
    return "[" + ", ".join(parts) + "]"


# Codex's workspace-write sandbox has no network, so `backbone tell` from a
# member cannot reach the backbone API on 127.0.0.1 — every message to a peer
# fails, Codex escalates, and a person has to answer a dialog per message.
# This override lets the sandbox reach the network; verified live against
# codex-cli 0.153 (API probe: 000 without it, 401 with it).
_LOCAL_API_ACCESS = ("-c", "sandbox_workspace_write.network_access=true")

_MODEL_FOR_ACCOUNT = re.compile(
    r"The\s*'(?P<model>[A-Za-z0-9_./:@+\-]{1,160})'\s*model\s*is\s*not\s*supported\s*"
    r"when\s*using\s*Codex\s*with\s*a\s*ChatGPT\s*account\.",
    re.IGNORECASE,
)
_MODEL_CHANGED = re.compile(
    r"[•●] Model changed to (?P<model>[A-Za-z0-9_./:@+\-]{1,160})"
    r"(?: (?P<effort>low|medium|high|xhigh|max|ultra))?\s*"
)


class Codex(Runtime):
    supports_exact_resume = True
    id = "codex"
    skill_dirs = (".agents/skills",)
    fallback_prompts = ("›",)
    display_name = "Codex"
    binary = "codex"
    brief_mode = "initial_prompt"
    models = ("gpt-5.6-sol", "gpt-6-astra")  # as shown by codex's own status line (live capture)
    # Codex has no effort flag; the level is a config override. Levels as
    # gpt-6-astra reports them in codex 0.153 (`~/.codex/models_cache.json`).
    efforts = ("low", "medium", "high", "xhigh", "max", "ultra")
    # "never: Never ask for user approval. Execution failures are immediately
    # returned to the model." The workspace-write sandbox is pinned alongside
    # (a `sandbox_mode = "danger-full-access"` in the user's config.toml
    # would otherwise silently take the wall away): the agent's directory,
    # temp, the network opened below, and the `--add-dir`s from
    # `writable_dir_args` (validated shared/private Git commit paths among
    # them; hooks and config stay protected); a write anywhere else
    # fails with "Operation not permitted" and the model is told so.
    # codex-cli 0.153: both are global options, valid before the TUI and
    # before `resume`.
    unattended_args = ("-a", "never", "-s", "workspace-write")
    sandboxed = True
    mouse_scroll = True
    # Codex 0.153.4: on-request approvals go to its automatic reviewer,
    # with workspace-write enforced by this switch (verified with --help).
    auto_review_args = ("--approve-for-me",)

    hook_events = (
        ("SessionStart", None),
        ("SessionEnd", None),
        ("UserPromptSubmit", None),
        ("PermissionRequest", None),
        ("PreToolUse", None),
        ("PostToolUse", None),
        ("Stop", None),
        ("Interrupt", None),
    )
    hook_timeout = 10  # seconds

    prompt_prefixes = ("›",)
    runtime_markers = ("openai codex", "gpt-5.", "context left")
    placeholder_fragments = (
        "ask codex to do anything",
        "implement {feature}",
        "explain this codebase",
    )
    status_fragments = (
        "gpt-5.",
        "context left",
        "for shortcuts",
        "messages to be submitted after next tool call",
    )
    queue_markers = (
        "tab to queue message",
        "messages to be submitted after next tool call",
        "press esc to interrupt and send immediately",
    )
    busy_markers = ("esc to interrupt",)
    provider_error_patterns = (
        r"^(?:error:\s*)?selected model is at capacity\b",
        r"^(?:error:\s*)?you(?:'ve| have) hit your usage limit\b",
        r"^(?:error:\s*)?rate limit (?:reached|exceeded)\b",
        r"^(?:error:\s*)?(?:too many requests|insufficient_quota)\b",
    )
    provider_error_prefixes = ("■",)
    prompt_markers = (
        "approve this command",
        "allow command",
        "would you like to run the following command",
        "yes, and don't ask again",
        "do you trust the contents of this directory",
        "press enter to continue",
        "press enter to confirm",
    )
    # "› 1. Yes, proceed (y)" is preselected; "Press enter to confirm" (0.152);
    # "3. No, and tell Codex what to do differently (esc)".
    approve_keys = ("Enter",)
    deny_keys = ("Escape",)
    # "Approaching rate limits — Switch to gpt-5.6-luna?" with Switch
    # preselected (live capture, 0.153): Enter changes the model, Escape
    # ("go back") keeps it.
    choice_markers = ("keep current model", "switch to gpt-")

    def diagnostics(self, pane_content: str) -> tuple[RuntimeDiagnostic, ...]:
        """Observe typed error banners, including one still visible after a model change.

        A banner in scrollback is evidence it was visible, not proof the error
        still blocks the runtime. The state classifier remains independent.
        Ordinary prose, quoted JSON and unknown provider text are not recorded.
        """
        observations = {item.observation_key: item for item in super().diagnostics(pane_content)}
        lines = sanitize_pane_content(pane_content).splitlines()[-80:]
        in_code_block = False
        for index, raw in enumerate(lines):
            line = raw.strip()
            if line.startswith(("```", "~~~")):
                in_code_block = not in_code_block
                continue
            if in_code_block:
                continue
            if changed := _MODEL_CHANGED.fullmatch(line):
                signal = RuntimeDiagnostic(
                    code="model_changed",
                    severity="info",
                    model=changed["model"],
                    observed_effort=changed["effort"],
                )
                observations[signal.observation_key] = signal
            if not line.startswith("■ {"):
                continue
            # A narrow pane may wrap a JSON error over several display lines.
            # Bound both the lines and bytes inspected; json.loads must still
            # prove there is exactly one object rather than a prose fragment.
            payload = raw.lstrip()[1:].lstrip()
            for continuation in range(8):
                if len(payload) > 4096:
                    break
                try:
                    error = json.loads(payload)
                except ValueError:
                    if index + continuation + 1 >= len(lines):
                        break
                    payload += lines[index + continuation + 1]
                    continue
                if not isinstance(error, dict):
                    break
                body = error.get("error")
                status = error.get("status")
                if (
                    error.get("type") != "error"
                    or type(status) is not int
                    or not 400 <= status <= 599
                    or not isinstance(body, dict)
                    or not isinstance(body.get("type"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", body["type"])
                ):
                    break
                signal = RuntimeDiagnostic(
                    code="request_error", error_type=body["type"], http_status=status
                )
                if (
                    status == 400
                    and body["type"] == "invalid_request_error"
                    and isinstance(body.get("message"), str)
                    and (unsupported := _MODEL_FOR_ACCOUNT.fullmatch(body["message"]))
                ):
                    signal = RuntimeDiagnostic(
                        code="model_account_incompatible",
                        reason="unsupported_model_for_account",
                        error_type=body["type"],
                        model=unsupported["model"],
                        http_status=status,
                    )
                observations[signal.observation_key] = signal
                break
        return tuple(observations.values())

    def pre_trust(self, directory: Path | str) -> None:
        pre_trust_codex_directory(directory)

    def writable_dir_args(self, dirs: tuple[str, ...]) -> list[str]:
        """``--add-dir <path>`` per writable root, including Git bookkeeping
        files and their locks. What a project's
        tooling keeps outside the checkout — uv's cache under `~/.cache`, the
        one wall a member hit live — is opened here, nothing else."""
        args: list[str] = []
        for directory in dirs:
            args.extend(["--add-dir", str(Path(directory).expanduser())])
        return args

    def effort_args(self, effort: str | None) -> list[str]:
        """``-c model_reasoning_effort=<level>``, Codex's config override.

        A global option, so it is valid both before the TUI and before the
        ``resume`` subcommand. Verified live against codex-cli 0.153.
        """
        return ["-c", f"model_reasoning_effort={effort}"] if effort else []

    def hook_settings_path(self, project_dir: Path | None) -> Path:
        # Codex reads `hooks.json` from its home and from a trusted project's
        # `.codex/`; entries there still need a one-time `/hooks` trust.
        if project_dir is not None:
            return Path(project_dir).expanduser() / ".codex" / "hooks.json"
        return Path.home() / ".codex" / "hooks.json"

    def hook_launch_args(
        self, data_dir: Path | str | None, state_dir: Path | str | None
    ) -> list[str]:
        """``-c hooks.<Event>=[…]`` per event, plus ``--dangerously-bypass-hook-trust``.

        Codex takes configuration overrides on the command line (dotted keys,
        TOML values), so the hooks live only in this launch: nothing in
        ``~/.codex`` or the repository is touched. Codex asks a person to trust
        any hook it has not seen; these are the backbone's own scripts, wired
        by the backbone, so the trust prompt is bypassed for this session.
        Verified live against codex-cli 0.152.
        """
        if data_dir is None or state_dir is None:
            return []
        try:
            _, settings = self.hook_settings(data_dir, state_dir)
        except OSError as exc:
            log.warning("Could not write the hook files: %s", exc)
            return []
        args: list[str] = []
        for event, entries in settings["hooks"].items():
            args.extend(["-c", f"hooks.{event}={_toml_entries(entries)}"])
        args.append("--dangerously-bypass-hook-trust")
        return args

    def launch_args(self, *, model, resume, brief_file, pre_trust, data_dir, state_dir):
        hook = self.hook_launch_args(data_dir, state_dir)
        # `codex resume` accepts explicit model overrides as well as a session ID.
        # Both the TUI and `resume` take `-c` and the hook-trust flag.
        if resume:
            target = resume if isinstance(resume, str) else "--last"
            return [
                "resume",
                target,
                *_LOCAL_API_ACCESS,
                "--no-alt-screen",
                *hook,
                *(["--model", model] if model else []),
            ]
        # Inline output gives tmux scrollback to display; session mouse
        # handling keeps the wheel from becoming Up/Down in the composer.
        args: list[str] = [*_LOCAL_API_ACCESS, "--no-alt-screen", *hook]
        if model:
            args.extend(["--model", model])
        if brief_file is not None and (brief := read_brief(brief_file)):
            args.append(brief)  # positional initial prompt, after every flag
        return args

    usage_supported = True

    def usage_paths(self, session_id: str, env: dict[str, str]) -> list[Path]:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,160}", session_id):
            return []
        home = Path(
            env.get("CODEX_HOME") or os.environ.get("CODEX_HOME") or Path.home() / ".codex"
        ).expanduser()
        return sorted(
            [
                *home.glob(f"sessions/**/rollout-*{session_id}.jsonl"),
                *home.glob(f"archived_sessions/rollout-*{session_id}.jsonl"),
            ]
        )

    def usage_children(
        self, path: Path, session_id: str, env: dict[str, str]
    ) -> list[tuple[str, Path]]:
        home = Path(
            env.get("CODEX_HOME") or os.environ.get("CODEX_HOME") or Path.home() / ".codex"
        ).expanduser()
        cached = _usage_lineage.get(str(home))
        if cached and time.monotonic() - cached[0] < 30:
            return cached[1].get(session_id, [])
        children: dict[str, list[tuple[str, Path]]] = {}
        for candidate in [
            *home.glob("sessions/**/*.jsonl"),
            *home.glob("archived_sessions/*.jsonl"),
        ]:
            try:
                with candidate.open("rb") as stream:
                    record = json.loads(stream.readline(256 * 1024))
                meta = record.get("payload") or {}
                source = meta.get("source") or {}
                if not isinstance(source, dict):
                    continue
                subagent = source.get("subagent") or {}
                spawn = subagent.get("thread_spawn") or subagent.get("spawn") or {}
                parent = spawn.get("parent_thread_id")
                child = meta.get("id")
                if isinstance(parent, str) and isinstance(child, str) and child != parent:
                    children.setdefault(parent, []).append((child, candidate))
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        _usage_lineage[str(home)] = (time.monotonic(), children)
        return children.get(session_id, [])

    def parse_usage(self, record: dict, state: dict):
        p = record.get("payload") or {}
        kind = record.get("type")
        if kind == "session_meta":
            state["has_start"] = True
            if "born" not in state and p.get("timestamp"):
                state["born"] = timestamp(p["timestamp"])
                state["provider"] = p.get("model_provider") or "unknown"
        if kind == "turn_context":
            state["model"] = p.get("model") or "unknown"
            state["turn_id"] = p.get("turn_id")
            state["service_tier"] = p.get("service_tier") or "unknown"
        if (
            state.get("born")
            and record.get("timestamp")
            and timestamp(record["timestamp"]) < state["born"]
        ):
            return None
        if kind != "event_msg" or p.get("type") != "token_count":
            return None
        if isinstance(p.get("rate_limits"), dict):
            limits = p["rate_limits"]
            # Store only quota metadata, never account credentials or raw payloads.
            state["limits"] = {
                "observed_at": timestamp(record["timestamp"]),
                "bucket": str(limits.get("limit_id") or "unknown")[:200],
                "windows": [
                    {
                        "name": key,
                        **{
                            k: value[k]
                            for k in ("used_percent", "window_minutes", "resets_at")
                            if isinstance(value.get(k), (int, float))
                            and not isinstance(value[k], bool)
                            and math.isfinite(value[k])
                            and value[k] >= 0
                        },
                    }
                    for key in ("primary", "secondary")
                    if isinstance(value := limits.get(key), dict)
                ],
            }
        info = p.get("info") or {}
        total = info.get("total_token_usage")
        if not isinstance(total, dict):
            return None
        fields = (
            "input_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        )
        current = {k: count(total, k) for k in fields}
        previous = state.get("total")
        if current == previous:
            return None
        delta = {k: current[k] - (previous or {}).get(k, 0) for k in fields}
        state["total"] = current
        last = info.get("last_token_usage") or {}
        partial = bool(state.get("partial"))
        if previous is not None and delta != {k: count(last, k) for k in fields}:
            # Counters may bridge unrecorded requests. Preserve the token delta,
            # but one last-request context/model cannot price that whole gap.
            state["partial"] = partial = True
        if any(n < 0 for n in delta.values()):
            # A reset cannot prove what happened in between. Retain the observed
            # last request only, with explicitly incomplete accounting.
            delta = {k: count(last, k) for k in fields}
            state["partial"] = partial = True
        if previous is None and current != {k: count(last, k) for k in fields}:
            delta = {k: count(last, k) for k in fields}
            state["partial"] = partial = True  # exclude unattributed inherited totals
        cached = delta["cached_input_tokens"]
        written = delta["cache_write_input_tokens"]
        uncached = delta["input_tokens"] - cached - written
        if uncached < 0:
            raise ValueError("overlapping input counters")
        at = timestamp(record["timestamp"])
        return UsageEvent(
            key=at + ":" + ":".join(str(current[k]) for k in fields),
            at=at,
            model=state.get("model", "unknown"),
            provider=state.get("provider", "unknown"),
            turn_id=state.get("turn_id"),
            input_tokens=uncached,
            cache_read_tokens=cached,
            cache_write_tokens=written,
            output_tokens=delta["output_tokens"],
            reasoning_tokens=delta["reasoning_output_tokens"],
            context_tokens=count(last, "input_tokens") if last else None,
            service_tier=state.get("service_tier", "unknown"),
            coverage="partial" if partial else "measured",
        )


RUNTIME = Codex()
