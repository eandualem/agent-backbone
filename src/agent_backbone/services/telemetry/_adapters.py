"""Runtime-specific native telemetry adapter registry and stable facade."""

from __future__ import annotations

from agent_backbone.services.telemetry._claude import ClaudeTelemetryAdapter
from agent_backbone.services.telemetry._codex import CodexTelemetryAdapter
from agent_backbone.services.telemetry._common import (
    cursor_project_slug as _cursor_project_slug,
)
from agent_backbone.services.telemetry._common import event_id as _event_id
from agent_backbone.services.telemetry._common import (
    extract_text_parts as _extract_text_parts,
)
from agent_backbone.services.telemetry._common import mtime as _mtime
from agent_backbone.services.telemetry._common import (
    normalize_claude_content as _normalize_claude_content,
)
from agent_backbone.services.telemetry._common import (
    normalize_codex_content as _normalize_codex_content,
)
from agent_backbone.services.telemetry._common import parse_timestamp as _parse_timestamp
from agent_backbone.services.telemetry._common import read_jsonl_since as _read_jsonl_since
from agent_backbone.services.telemetry._common import read_text_since as _read_text_since
from agent_backbone.services.telemetry._common import simplify_value as _simplify_value
from agent_backbone.services.telemetry._cursor import CursorTelemetryAdapter
from agent_backbone.services.telemetry._gemini import GeminiTelemetryAdapter
from agent_backbone.services.telemetry._opencode import OpenCodeTelemetryAdapter
from agent_backbone.services.telemetry.interface import TelemetryAdapter
from agent_backbone.services.terminal import TerminalRuntime

__all__ = [
    "ClaudeTelemetryAdapter",
    "CodexTelemetryAdapter",
    "CursorTelemetryAdapter",
    "GeminiTelemetryAdapter",
    "OpenCodeTelemetryAdapter",
    "TelemetryAdapter",
    "_ADAPTERS",
    "_cursor_project_slug",
    "_event_id",
    "_extract_text_parts",
    "_mtime",
    "_normalize_claude_content",
    "_normalize_codex_content",
    "_parse_timestamp",
    "_read_jsonl_since",
    "_read_text_since",
    "_simplify_value",
    "get_telemetry_adapter",
]

_ADAPTERS: dict[TerminalRuntime, TelemetryAdapter] = {
    TerminalRuntime.CLAUDE: ClaudeTelemetryAdapter(),
    TerminalRuntime.CODEX: CodexTelemetryAdapter(),
    TerminalRuntime.GEMINI: GeminiTelemetryAdapter(),
    TerminalRuntime.OPENCODE: OpenCodeTelemetryAdapter(),
    TerminalRuntime.CURSOR: CursorTelemetryAdapter(),
}


def get_telemetry_adapter(runtime: TerminalRuntime | str) -> TelemetryAdapter | None:
    """Return the adapter for a supported runtime, if any."""
    try:
        normalized = runtime if isinstance(runtime, TerminalRuntime) else TerminalRuntime(runtime)
    except ValueError:
        return None
    return _ADAPTERS.get(normalized)
