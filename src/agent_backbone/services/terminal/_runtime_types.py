"""Shared runtime enum for terminal integrations."""

from __future__ import annotations

from enum import StrEnum


class TerminalRuntime(StrEnum):
    """Known interactive CLI runtimes."""

    CLAUDE = "claude"
    GEMINI = "gemini"
    CODEX = "codex"
    CURSOR = "cursor"
    OPENCODE = "opencode"
    SHELL = "shell"
    UNKNOWN = "unknown"
