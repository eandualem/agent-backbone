"""Configuration for the agent backbone.

Entity-to-session mapping, routing constants, and runtime config.
Ported from webhook-receiver.py with additions for Prefect integration.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Entity → tmux session name mapping
ENTITY_SESSIONS: dict[str, str] = {
    "feynman": "feynman",
    "ike": "ike",
    "leo": "leo",
    "ada": "ada",
}

# Skip these recipients (no tmux routing)
SKIP_ENTITIES: set[str] = {"elias"}

# Fallback routing: if a target can't be resolved to a session, route here
FALLBACK_ROUTING: dict[str, str] = {
    "coding-agent": "ike",
}

# Repo name pattern: "[type] repo-name: description"
REPO_NAME_PATTERN: re.Pattern[str] = re.compile(r"^\[[^\]]+\]\s+([\w-]+):")

# Known coding agent repos (for session resolution)
CODING_REPOS: set[str] = {
    "platform-api",
    "platform-web",
    "arclio-assistant",
    "arclio-janus",
    "mcp-hub",
    "AI-chatbot",
    "arclio-mcp-tooling",
    "prefect-workflow-migration",
    "rag-eval",
    "rules-clients",
}

# All named entities (for agent_monitor iteration)
ALL_ENTITIES: list[str] = list(ENTITY_SESSIONS.keys())


@dataclass(frozen=True)
class BackboneConfig:
    """Runtime configuration loaded from environment."""

    github_token: str = field(default_factory=lambda: os.environ.get("GITHUB_TOKEN", ""))
    github_owner: str = field(default_factory=lambda: os.environ.get("GITHUB_OWNER", "eandualem"))
    github_repo: str = field(default_factory=lambda: os.environ.get("GITHUB_REPO", "orchestration"))
    gateway_port: int = field(
        default_factory=lambda: int(os.environ.get("GATEWAY_PORT", "9877"))
    )
    webhook_secret: str = field(default_factory=lambda: _load_webhook_secret())
    max_delivery_ids: int = 100
    notify_dedup_seconds: int = 5


def _load_webhook_secret() -> str:
    """Load webhook secret from env or fallback to file."""
    secret = os.environ.get("WEBHOOK_SECRET", "")
    if secret:
        return secret
    secret_path = Path.home() / ".claude" / "services" / ".webhook-secret"
    try:
        return secret_path.read_text().strip()
    except FileNotFoundError:
        return ""
