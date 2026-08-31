"""Configuration for agent-backbone.

One TOML file (``backbone.toml``) describes everything structural: the agents,
the task tracker, Telegram, tuning knobs. Secrets come from environment
variables (or a ``.env`` file next to the config).

Config discovery order:

1. ``BACKBONE_CONFIG`` environment variable (explicit path)
2. ``backbone.toml`` in the current directory or any parent
3. ``~/.config/agent-backbone/backbone.toml``
4. Built-in defaults (no agents, no tracker, SQLite in the data dir)
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from agent_backbone.services.database.config import DatabaseConfig

CONFIG_FILENAME = "backbone.toml"
DEFAULT_DATA_DIR = "~/.local/share/agent-backbone"
DEFAULT_PORT = 7120


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentSpec:
    """A configured terminal agent.

    The agent's name doubles as its tmux session name and as the value of the
    ``for:<name>`` label that routes tracker issues to it.
    """

    name: str
    dir: str
    runtime: str = "claude"
    model: str | None = None
    repo: str = ""
    """Optional ``owner/name`` repository this agent owns. Issues opened in that
    repository without ``for:`` labels are routed to this agent."""
    tags: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    description: str = ""

    @property
    def path(self) -> Path:
        return Path(self.dir).expanduser()


@dataclass(frozen=True)
class AgentsConfig:
    """The set of configured agents, keyed by name."""

    specs: dict[str, AgentSpec] = field(default_factory=dict)

    def get(self, name: str) -> AgentSpec | None:
        return self.specs.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self.specs

    def __iter__(self):
        return iter(self.specs.values())

    def __len__(self) -> int:
        return len(self.specs)

    @property
    def names(self) -> list[str]:
        return list(self.specs)

    def dir_for(self, name: str) -> str:
        spec = self.specs.get(name)
        return str(spec.path) if spec else ""

    def for_repo(self, repo_full_name: str) -> list[AgentSpec]:
        """Agents that own the given ``owner/name`` repository."""
        key = repo_full_name.casefold()
        return [spec for spec in self.specs.values() if spec.repo.casefold() == key]

    def with_tag(self, tag: str) -> list[AgentSpec]:
        return [spec for spec in self.specs.values() if tag in spec.tags]


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackboneSection:
    """Process-level settings."""

    data_dir: str = DEFAULT_DATA_DIR
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    session_name: str = "backbone"
    """tmux session used by ``backbone up --detach``."""
    cors_origins: tuple[str, ...] = ()
    max_delivery_ids: int = 100

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir).expanduser()


@dataclass(frozen=True)
class GitHubConfig:
    """GitHub task tracker settings (non-secret)."""

    repo: str = ""
    """Default ``owner/name`` repository used for coordination issues."""
    mode: str = "webhook"
    """``webhook`` (push, needs a public URL) or ``poll`` (pull, no URL)."""
    poll_interval_seconds: int = 30

    @property
    def enabled(self) -> bool:
        return bool(self.repo)

    @property
    def owner(self) -> str:
        return self.repo.split("/", 1)[0] if "/" in self.repo else ""

    @property
    def name(self) -> str:
        return self.repo.split("/", 1)[1] if "/" in self.repo else ""


@dataclass(frozen=True)
class RoutingConfig:
    """Routing knobs."""

    ignore_targets: frozenset[str] = frozenset()
    """``for:`` label values that should never be routed (e.g. humans)."""
    notification_dedup_seconds: int = 10


@dataclass(frozen=True)
class AgentStateConfig:
    """Where agents push their state (hook-written files) and how fresh it must be."""

    state_dir: str = ""
    """Defaults to ``<data_dir>/state`` when empty."""
    stale_threshold_seconds: int = 300

    def state_path(self, data_dir: Path) -> Path:
        if self.state_dir:
            return Path(self.state_dir).expanduser()
        return data_dir / "state"


@dataclass(frozen=True)
class MonitorConfig:
    """Background job intervals."""

    interval_seconds: int = 60
    retry_interval_seconds: int = 300


@dataclass(frozen=True)
class DeliveryConfig:
    """Delivery persistence and timing."""

    retention_days: int = 30
    grace_period_seconds: int = 5
    queue_retry_seconds: int = 30


@dataclass(frozen=True)
class TelegramConfig:
    """Telegram bot settings (token comes from ``TELEGRAM_TOKEN``)."""

    allowed_chat_ids: tuple[int, ...] = ()
    topic_routes: dict[int, str] = field(default_factory=dict)
    group_chat_id: int | None = None
    notification_chat_id: int | None = None
    topic_discovery_file: str = ""

    def topic_discovery_path(self, data_dir: Path) -> Path:
        if self.topic_discovery_file:
            return Path(self.topic_discovery_file).expanduser()
        return data_dir / "telegram-topics.json"


@dataclass(frozen=True)
class PriorityScoringConfig:
    """Priority scoring weights for issue queue ordering."""

    blocking_weight: float = 1000.0
    type_weights: dict[str, float] = field(
        default_factory=lambda: {
            "spec-gap": 100.0,
            "bug": 90.0,
            "task": 50.0,
            "question": 20.0,
            "optimization": 10.0,
        }
    )
    dependents_multiplier: float = 1.5
    age_tiebreaker_weight: float = 0.01


@dataclass(frozen=True)
class EscalationConfig:
    """Stall / offline / plan-waiting escalation."""

    target: str = ""
    """Agent that receives escalations. Empty disables agent escalation."""
    stall_threshold_seconds: int = 5400
    dedup_seconds: int = 1800


@dataclass(frozen=True)
class SecurityConfig:
    """Security toggles."""

    allow_remote_plan_control: bool = False
    """Allow approving/rejecting plans (keystroke injection) via API/Telegram."""
    allow_unauthenticated: bool = False
    """Serve the API without an API key. Only for isolated dev setups."""


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackboneConfig:
    """Fully-resolved configuration."""

    # Secrets (environment only)
    api_key: str = ""
    webhook_secret: str = ""
    github_token: str = ""
    github_app_id: int | None = None
    github_app_private_key_path: str = ""
    telegram_token: str = ""

    # Sections
    backbone: BackboneSection = field(default_factory=BackboneSection)
    agents: AgentsConfig = field(default_factory=AgentsConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    agent_state: AgentStateConfig = field(default_factory=AgentStateConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    priority_scoring: PriorityScoringConfig = field(default_factory=PriorityScoringConfig)
    escalation: EscalationConfig = field(default_factory=EscalationConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    source_path: Path | None = None

    # --- Derived paths -----------------------------------------------------

    @property
    def data_dir(self) -> Path:
        return self.backbone.data_path

    @property
    def state_dir(self) -> Path:
        return self.agent_state.state_path(self.data_dir)

    @property
    def action_log_path(self) -> Path:
        return self.state_dir / "actions.jsonl"

    @property
    def telegram_topic_discovery_path(self) -> Path:
        return self.telegram.topic_discovery_path(self.data_dir)

    @property
    def database_url(self) -> str:
        return self.database.resolved_url(self.data_dir)

    # --- Derived flags -----------------------------------------------------

    @property
    def github_app_ready(self) -> bool:
        return bool(self.github_app_id and self.github_app_private_key_path)

    @property
    def github_ready(self) -> bool:
        return self.github.enabled and (bool(self.github_token) or self.github_app_ready)

    @property
    def telegram_ready(self) -> bool:
        return bool(self.telegram_token)

    @property
    def webhook_secrets(self) -> tuple[str, ...]:
        return (self.webhook_secret,) if self.webhook_secret else ()

    # --- Loading -----------------------------------------------------------

    @classmethod
    def load(cls, path: Path | str | None = None) -> BackboneConfig:
        """Load config from TOML (discovered or explicit) plus environment."""
        toml_path = Path(path).expanduser() if path else find_config_file()
        raw: dict = {}
        if toml_path and toml_path.exists():
            load_dotenv(toml_path.parent / ".env")
            with open(toml_path, "rb") as fh:
                raw = tomllib.load(fh)
        load_dotenv()
        return cls.from_dict(raw, source_path=toml_path)

    @classmethod
    def from_dict(cls, raw: dict, *, source_path: Path | None = None) -> BackboneConfig:
        """Build a config from an already-parsed TOML dictionary."""
        env = os.environ

        bb = raw.get("backbone", {})
        gh = raw.get("github", {})
        rt = raw.get("routing", {})
        ag = raw.get("agent_state", {})
        mo = raw.get("monitor", {})
        dl = raw.get("delivery", {})
        db = raw.get("database", {})
        tg = raw.get("telegram", {})
        ps = raw.get("priority_scoring", {})
        es = raw.get("escalation", {})
        sec = raw.get("security", {})

        agents = _parse_agents(raw.get("agents", {}))

        defaults = PriorityScoringConfig()
        return cls(
            api_key=env.get("BACKBONE_API_KEY", ""),
            webhook_secret=env.get("GITHUB_WEBHOOK_SECRET", env.get("WEBHOOK_SECRET", "")),
            github_token=env.get("GITHUB_TOKEN", ""),
            github_app_id=_optional_int(env.get("GITHUB_APP_ID", "")),
            github_app_private_key_path=env.get("GITHUB_APP_PRIVATE_KEY_PATH", ""),
            telegram_token=env.get("TELEGRAM_TOKEN", ""),
            backbone=BackboneSection(
                data_dir=env.get("BACKBONE_DATA_DIR") or bb.get("data_dir", DEFAULT_DATA_DIR),
                host=bb.get("host", "127.0.0.1"),
                port=int(env.get("BACKBONE_PORT") or bb.get("port", DEFAULT_PORT)),
                session_name=bb.get("session_name", "backbone"),
                cors_origins=tuple(bb.get("cors_origins", [])),
                max_delivery_ids=bb.get("max_delivery_ids", 100),
            ),
            agents=agents,
            github=GitHubConfig(
                repo=gh.get("repo", ""),
                mode=gh.get("mode", "webhook"),
                poll_interval_seconds=gh.get("poll_interval_seconds", 30),
            ),
            routing=RoutingConfig(
                ignore_targets=frozenset(rt.get("ignore_targets", [])),
                notification_dedup_seconds=rt.get("notification_dedup_seconds", 10),
            ),
            agent_state=AgentStateConfig(
                state_dir=ag.get("state_dir", ""),
                stale_threshold_seconds=ag.get("stale_threshold_seconds", 300),
            ),
            monitor=MonitorConfig(
                interval_seconds=mo.get("interval_seconds", 60),
                retry_interval_seconds=mo.get("retry_interval_seconds", 300),
            ),
            delivery=DeliveryConfig(
                retention_days=dl.get("retention_days", 30),
                grace_period_seconds=dl.get("grace_period_seconds", 5),
                queue_retry_seconds=dl.get("queue_retry_seconds", 30),
            ),
            database=DatabaseConfig(
                url=env.get("BACKBONE_DATABASE_URL") or db.get("url", ""),
                pool_size=db.get("pool_size", 5),
                pool_overflow=db.get("pool_overflow", 10),
                echo=db.get("echo", False),
            ),
            telegram=TelegramConfig(
                allowed_chat_ids=tuple(int(x) for x in tg.get("allowed_chat_ids", [])),
                topic_routes={int(k): v for k, v in tg.get("topic_routes", {}).items()},
                group_chat_id=tg.get("group_chat_id"),
                notification_chat_id=tg.get("notification_chat_id"),
                topic_discovery_file=tg.get("topic_discovery_file", ""),
            ),
            priority_scoring=PriorityScoringConfig(
                blocking_weight=ps.get("blocking_weight", defaults.blocking_weight),
                type_weights=ps.get("type_weights", dict(defaults.type_weights)),
                dependents_multiplier=ps.get(
                    "dependents_multiplier", defaults.dependents_multiplier
                ),
                age_tiebreaker_weight=ps.get(
                    "age_tiebreaker_weight", defaults.age_tiebreaker_weight
                ),
            ),
            escalation=EscalationConfig(
                target=es.get("target", ""),
                stall_threshold_seconds=es.get("stall_threshold_seconds", 5400),
                dedup_seconds=es.get("dedup_seconds", 1800),
            ),
            security=SecurityConfig(
                allow_remote_plan_control=bool(sec.get("allow_remote_plan_control", False)),
                allow_unauthenticated=bool(
                    env.get("BACKBONE_ALLOW_UNAUTHENTICATED", "").lower() in ("1", "true")
                    or sec.get("allow_unauthenticated", False)
                ),
            ),
            source_path=source_path,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def find_config_file(start: Path | None = None) -> Path | None:
    """Locate ``backbone.toml`` via env var, CWD ancestors, then the user config dir."""
    explicit = os.environ.get("BACKBONE_CONFIG", "")
    if explicit:
        return Path(explicit).expanduser()

    here = (start or Path.cwd()).resolve()
    for candidate_dir in (here, *here.parents):
        candidate = candidate_dir / CONFIG_FILENAME
        if candidate.is_file():
            return candidate

    user_config = Path.home() / ".config" / "agent-backbone" / CONFIG_FILENAME
    if user_config.is_file():
        return user_config
    return None


def _parse_agents(raw: dict) -> AgentsConfig:
    specs: dict[str, AgentSpec] = {}
    for name, data in raw.items():
        if not isinstance(data, dict):
            raise ValueError(f"[agents.{name}] must be a table")
        directory = data.get("dir", "")
        if not directory:
            raise ValueError(f"[agents.{name}] is missing required key 'dir'")
        model = data.get("model")
        specs[name] = AgentSpec(
            name=name,
            dir=directory,
            runtime=data.get("runtime", "claude"),
            model=str(model) if model else None,
            repo=data.get("repo", ""),
            tags=tuple(data.get("tags", [])),
            env={str(k): str(v) for k, v in data.get("env", {}).items()},
            description=data.get("description", ""),
        )
    return AgentsConfig(specs=specs)


def _optional_int(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None
