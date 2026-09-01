"""Configuration for agent-backbone.

The **data directory** is the configuration. Inside it:

- ``backbone.db`` — settings (with built-in defaults), the known agents,
  events, deliveries, queue, state. The single source of truth.
- ``.env`` — secrets only (API key, GitHub/Telegram tokens). Loaded into the
  process environment; never stored in the database.
- ``state/``, ``hooks/``, ``pids/`` — runtime files.

The only knobs that live outside the directory are environment variables:
``BACKBONE_DATA_DIR`` (where the directory is) and ``BACKBONE_DATABASE_URL``
(to use PostgreSQL instead of the SQLite file).

Code reads a frozen ``BackboneConfig`` snapshot built from the database by
``build_config``; ``backbone config set`` changes a setting and the running
backbone picks it up on its next refresh.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from agent_backbone.services.database.interface import sqlite_url

DEFAULT_DATA_DIR = "~/.local/share/agent-backbone"
DEFAULT_PORT = 7120


# ---------------------------------------------------------------------------
# Settings schema: key -> (default, type)
# ---------------------------------------------------------------------------

SETTINGS_DEFAULTS: dict[str, Any] = {
    "backbone.host": "127.0.0.1",
    "backbone.port": DEFAULT_PORT,
    "backbone.session_name": "backbone",
    "backbone.cors_origins": [],
    "backbone.max_delivery_ids": 100,
    "agents.default_runtime": "claude",
    "agents.pre_trust": True,
    "agents.inject_brief": True,
    "github.intake": "auto",  # auto | webhook | poll | off
    "github.poll_interval_seconds": 60,
    "github.backfill_on_start": True,
    "github.backfill_lookback_hours": 24,
    "routing.ignore_targets": [],
    "routing.notification_dedup_seconds": 10,
    "timing.stale_threshold_seconds": 300,
    "timing.snapshot_trust_seconds": 20,
    "timing.grace_period_seconds": 5,
    "timing.queue_expiry_minutes": 30,
    "timing.stall_threshold_seconds": 5400,
    "timing.escalation_dedup_seconds": 1800,
    "timing.monitor_interval_seconds": 60,
    "timing.retry_interval_seconds": 300,
    "timing.start_timeout_seconds": 60,
    "timing.delivery_retention_days": 30,
    "telegram.allowed_chat_ids": [],
    "telegram.notification_chat_id": None,
    "telegram.group_chat_id": None,
    "telegram.topic_routes": {},
    "escalation.target": "",
    "priority.blocking_weight": 1000.0,
    "priority.type_weights": {
        "spec-gap": 100.0,
        "bug": 90.0,
        "task": 50.0,
        "question": 20.0,
        "optimization": 10.0,
    },
    "priority.dependents_multiplier": 1.5,
    "priority.age_tiebreaker_weight": 0.01,
    "security.allow_remote_plan_control": False,
    "security.allow_unauthenticated": False,
}

SETTINGS_HELP: dict[str, str] = {
    "backbone.host": "Bind address for the API (keep 127.0.0.1 unless you add TLS+auth in front)",
    "backbone.port": "API port",
    "backbone.session_name": "tmux session used by `backbone up --detach`",
    "backbone.cors_origins": "Browser origins allowed to call the API (JSON list)",
    "agents.default_runtime": "Runtime used by `agent start` when none is given",
    "agents.pre_trust": (
        "Answer the runtime's folder-trust dialog before starting (claude, codex, gemini)"
    ),
    "agents.inject_brief": (
        "Give each agent the backbone's brief at launch (system prompt or initial prompt)"
    ),
    "github.intake": "auto | webhook | poll | off — how GitHub events arrive",
    "github.poll_interval_seconds": "Poll frequency when intake resolves to poll",
    "github.backfill_on_start": "Fetch events missed while the backbone was down",
    "github.backfill_lookback_hours": "How far back a first-ever backfill looks",
    "routing.ignore_targets": "for:/from: values that are people, not agents (JSON list)",
    "timing.stale_threshold_seconds": "Hook state older than this is verified against the terminal",
    "timing.snapshot_trust_seconds": "A stored state snapshot older than this is re-verified live",
    "timing.grace_period_seconds": "Settle time after an agent becomes idle before delivering",
    "timing.queue_expiry_minutes": "Queued messages older than this are dropped",
    "timing.stall_threshold_seconds": "Busy on one issue longer than this is a stall",
    "timing.escalation_dedup_seconds": "Do not repeat the same escalation within this window",
    "timing.monitor_interval_seconds": "agent-monitor job period",
    "timing.retry_interval_seconds": "delivery-retry job period",
    "timing.start_timeout_seconds": "How long `agent start` waits for the prompt",
    "timing.delivery_retention_days": "Delivery history retention",
    "telegram.allowed_chat_ids": "Chat ids allowed to control the backbone (JSON list) — required",
    "telegram.notification_chat_id": "Where alerts are sent",
    "telegram.group_chat_id": "Forum group for topic routing",
    "telegram.topic_routes": "JSON object thread_id -> agent name",
    "escalation.target": "Agent that receives stall/offline/plan escalations",
    "security.allow_remote_plan_control": "Allow approving plans via API/Telegram (sends keys)",
    "security.allow_unauthenticated": "Serve the API without an API key (dev only)",
}


_SETTING_CHOICES: dict[str, tuple[str, ...]] = {
    "github.intake": ("auto", "webhook", "poll", "off"),
    "agents.default_runtime": ("claude", "codex", "gemini", "opencode", "aider", "cursor", "shell"),
}
_INT_LIST_SETTINGS = frozenset({"telegram.allowed_chat_ids"})
# Scheduler job periods: a zero or negative value would make Scheduler.add()
# raise on the next startup, so reject it at write time.
_POSITIVE_SETTINGS = frozenset(
    {
        "github.poll_interval_seconds",
        "timing.monitor_interval_seconds",
        "timing.retry_interval_seconds",
    }
)


def validate_setting(key: str, value: Any) -> Any:
    """Check a key exists and coerce/validate the value against the default's type."""
    if key not in SETTINGS_DEFAULTS:
        raise KeyError(f"unknown setting {key!r}")
    default = SETTINGS_DEFAULTS[key]
    if key in _SETTING_CHOICES:
        if not isinstance(value, str) or value not in _SETTING_CHOICES[key]:
            raise ValueError(f"{key}: expected one of {', '.join(_SETTING_CHOICES[key])}")
        return value
    if key in _INT_LIST_SETTINGS:
        if not isinstance(value, list):
            raise ValueError(f"{key}: expected a JSON list of integers")
        try:
            return [int(item) for item in value]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key}: expected a JSON list of integers") from exc
    if default is None:
        if value is None or isinstance(value, int | str):
            return value
        raise ValueError(f"{key}: expected an integer, string or null")
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false", "1", "0", "yes", "no"):
            return value.lower() in ("true", "1", "yes")
        raise ValueError(f"{key}: expected true/false")
    if isinstance(default, int) and not isinstance(default, bool):
        if isinstance(value, bool):
            raise ValueError(f"{key}: expected an integer")
        try:
            coerced = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key}: expected an integer") from exc
        if key in _POSITIVE_SETTINGS and coerced <= 0:
            raise ValueError(f"{key}: expected a positive integer")
        return coerced
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key}: expected a number") from exc
    if isinstance(default, str):
        if not isinstance(value, str):
            raise ValueError(f"{key}: expected a string")
        return value
    if isinstance(default, list):
        if not isinstance(value, list):
            raise ValueError(f"{key}: expected a JSON list")
        return value
    if isinstance(default, dict):
        if not isinstance(value, dict):
            raise ValueError(f"{key}: expected a JSON object")
        # Validate the shapes build_config() will rely on, so one bad stored
        # object cannot break refresh or the next startup.
        if key == "telegram.topic_routes":
            if not all(isinstance(v, str) for v in value.values()):
                raise ValueError(f"{key}: expected integer thread-id keys and string agent values")
            try:
                return {str(int(k)): v for k, v in value.items()}
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{key}: expected integer thread-id keys and string agent values"
                ) from exc
        if key == "priority.type_weights":
            try:
                return {str(k): float(v) for k, v in value.items()}
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key}: expected numeric weight values") from exc
        return value
    return value


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentSpec:
    """A known agent.

    The name doubles as the tmux session name, the ``for:<name>`` label value
    and the ``from:`` identity. ``repo`` is the repository the agent's
    directory belongs to (owned); ``watches`` are repositories it follows
    without owning.
    """

    name: str
    dir: str
    runtime: str = "claude"
    model: str | None = None
    repo: str = ""
    watches: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    description: str = ""

    @property
    def path(self) -> Path:
        return Path(self.dir).expanduser()

    @property
    def repos(self) -> tuple[str, ...]:
        """Owned repo first, then watched repos, deduplicated."""
        seen: list[str] = []
        for candidate in (self.repo, *self.watches):
            if candidate and candidate not in seen:
                seen.append(candidate)
        return tuple(seen)


@dataclass(frozen=True)
class AgentsConfig:
    """Snapshot of the known agents, keyed by name."""

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

    def for_repo(self, repo_full_name: str) -> list[AgentSpec]:
        """Every agent that owns or watches a repository (owners first)."""
        return self.owners(repo_full_name) + self.watchers(repo_full_name)

    def owners(self, repo_full_name: str) -> list[AgentSpec]:
        """Agents whose directory *is* the repository."""
        key = repo_full_name.casefold()
        if not key:
            return []
        return [spec for spec in self.specs.values() if spec.repo.casefold() == key]

    def watchers(self, repo_full_name: str) -> list[AgentSpec]:
        """Agents that watch a repository without owning it."""
        key = repo_full_name.casefold()
        return [
            spec
            for spec in self.specs.values()
            if spec.repo.casefold() != key and any(w.casefold() == key for w in spec.watches)
        ]

    def with_tag(self, tag: str) -> list[AgentSpec]:
        return [spec for spec in self.specs.values() if tag in spec.tags]

    @property
    def repos(self) -> list[str]:
        """Every repository any agent owns or watches, deduplicated, casefold-unique."""
        out: list[str] = []
        seen: set[str] = set()
        for spec in self.specs.values():
            for repo in spec.repos:
                if repo.casefold() not in seen:
                    seen.add(repo.casefold())
                    out.append(repo)
        return out


# ---------------------------------------------------------------------------
# Sections (frozen snapshots built from settings)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackboneSection:
    data_dir: str = DEFAULT_DATA_DIR
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    session_name: str = "backbone"
    cors_origins: tuple[str, ...] = ()
    max_delivery_ids: int = 100

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir).expanduser()


@dataclass(frozen=True)
class AgentsSection:
    default_runtime: str = "claude"
    pre_trust: bool = True
    inject_brief: bool = True


@dataclass(frozen=True)
class GitHubConfig:
    """GitHub intake settings (non-secret). Credentials come from the environment."""

    intake: str = "auto"
    poll_interval_seconds: int = 60
    backfill_on_start: bool = True
    backfill_lookback_hours: int = 24


@dataclass(frozen=True)
class RoutingConfig:
    ignore_targets: frozenset[str] = frozenset()
    notification_dedup_seconds: int = 10


@dataclass(frozen=True)
class AgentStateConfig:
    stale_threshold_seconds: int = 300
    snapshot_trust_seconds: int = 20


@dataclass(frozen=True)
class MonitorConfig:
    interval_seconds: int = 60
    retry_interval_seconds: int = 300
    start_timeout_seconds: int = 60


@dataclass(frozen=True)
class DeliveryConfig:
    retention_days: int = 30
    grace_period_seconds: int = 5
    queue_expiry_minutes: int = 30


@dataclass(frozen=True)
class TelegramConfig:
    allowed_chat_ids: tuple[int, ...] = ()
    topic_routes: dict[int, str] = field(default_factory=dict)
    group_chat_id: int | None = None
    notification_chat_id: int | None = None


@dataclass(frozen=True)
class PriorityScoringConfig:
    blocking_weight: float = 1000.0
    type_weights: dict[str, float] = field(
        default_factory=lambda: dict(SETTINGS_DEFAULTS["priority.type_weights"])
    )
    dependents_multiplier: float = 1.5
    age_tiebreaker_weight: float = 0.01


@dataclass(frozen=True)
class EscalationConfig:
    target: str = ""
    stall_threshold_seconds: int = 5400
    dedup_seconds: int = 1800


@dataclass(frozen=True)
class SecurityConfig:
    allow_remote_plan_control: bool = False
    allow_unauthenticated: bool = False


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackboneConfig:
    """Frozen configuration snapshot: secrets from the environment, settings
    and agents from the database."""

    api_key: str = ""
    webhook_secret: str = ""
    github_token: str = ""
    github_app_id: int | None = None
    github_app_private_key_path: str = ""
    telegram_token: str = ""

    backbone: BackboneSection = field(default_factory=BackboneSection)
    agents: AgentsConfig = field(default_factory=AgentsConfig)
    agents_section: AgentsSection = field(default_factory=AgentsSection)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    agent_state: AgentStateConfig = field(default_factory=AgentStateConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)
    database_url_override: str = ""
    """``BACKBONE_DATABASE_URL`` when set (PostgreSQL); empty means SQLite in the data dir."""
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    priority_scoring: PriorityScoringConfig = field(default_factory=PriorityScoringConfig)
    escalation: EscalationConfig = field(default_factory=EscalationConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    settings: dict[str, Any] = field(default_factory=dict)
    """Raw effective settings (defaults overlaid with stored values)."""

    # --- Derived paths -----------------------------------------------------

    @property
    def data_dir(self) -> Path:
        return self.backbone.data_path

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def action_log_path(self) -> Path:
        return self.state_dir / "actions.jsonl"

    @property
    def telegram_topic_discovery_path(self) -> Path:
        return self.data_dir / "telegram-topics.json"

    @property
    def env_path(self) -> Path:
        return self.data_dir / ".env"

    @property
    def database_url(self) -> str:
        return self.database_url_override or sqlite_url(self.data_dir)

    # --- Derived flags -----------------------------------------------------

    @property
    def github_app_ready(self) -> bool:
        return bool(self.github_app_id and self.github_app_private_key_path)

    @property
    def github_ready(self) -> bool:
        return self.github.intake != "off" and (bool(self.github_token) or self.github_app_ready)

    @property
    def github_intake(self) -> str:
        """The effective intake mode: ``webhook``, ``poll`` or ``off``."""
        if not self.github_ready:
            return "off"
        mode = self.github.intake
        if mode == "auto":
            return "webhook" if self.webhook_secret else "poll"
        if mode == "webhook" and not self.webhook_secret:
            # Every webhook would be rejected without a secret; fall back to
            # polling so events keep flowing (startup logs the mismatch).
            return "poll"
        return mode

    @property
    def telegram_ready(self) -> bool:
        return bool(self.telegram_token)

    @property
    def webhook_secrets(self) -> tuple[str, ...]:
        return (self.webhook_secret,) if self.webhook_secret else ()


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


def resolve_data_dir(explicit: str | Path | None = None) -> Path:
    """Data directory from an explicit value, ``BACKBONE_DATA_DIR``, or the default."""
    raw = explicit or os.environ.get("BACKBONE_DATA_DIR") or DEFAULT_DATA_DIR
    return Path(raw).expanduser()


def load_secrets(data_dir: Path) -> None:
    """Load ``<data_dir>/.env`` into the environment (existing variables win).

    Only the data directory's ``.env`` is read — a stray ``.env`` in the
    current working directory must not inject secrets or security flags.
    """
    load_dotenv(data_dir / ".env")


def bootstrap_config(data_dir: str | Path | None = None) -> BackboneConfig:
    """Minimal config (paths + secrets + defaults) available before the database is open."""
    path = resolve_data_dir(data_dir)
    load_secrets(path)
    return build_config(path, settings={}, agents=AgentsConfig())


def effective_settings(stored: dict[str, Any]) -> dict[str, Any]:
    """Defaults overlaid with stored values (unknown keys are ignored)."""
    merged = dict(SETTINGS_DEFAULTS)
    for key, value in stored.items():
        if key in SETTINGS_DEFAULTS:
            try:
                merged[key] = validate_setting(key, value)
            except (ValueError, TypeError):
                continue
    return merged


def build_config(
    data_dir: Path,
    *,
    settings: dict[str, Any],
    agents: AgentsConfig,
) -> BackboneConfig:
    """Assemble a frozen snapshot from the data dir, environment and stored settings."""
    env = os.environ
    s = effective_settings(settings)

    def _opt_int(value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    return BackboneConfig(
        api_key=env.get("BACKBONE_API_KEY", ""),
        webhook_secret=env.get("GITHUB_WEBHOOK_SECRET", env.get("WEBHOOK_SECRET", "")),
        github_token=env.get("GITHUB_TOKEN", ""),
        github_app_id=_opt_int(env.get("GITHUB_APP_ID", "")),
        github_app_private_key_path=env.get("GITHUB_APP_PRIVATE_KEY_PATH", ""),
        telegram_token=env.get("TELEGRAM_TOKEN", ""),
        backbone=BackboneSection(
            data_dir=str(data_dir),
            host=s["backbone.host"],
            port=int(env.get("BACKBONE_PORT") or s["backbone.port"]),
            session_name=s["backbone.session_name"],
            cors_origins=tuple(s["backbone.cors_origins"]),
            max_delivery_ids=s["backbone.max_delivery_ids"],
        ),
        agents=agents,
        agents_section=AgentsSection(
            default_runtime=s["agents.default_runtime"],
            pre_trust=s["agents.pre_trust"],
            inject_brief=s["agents.inject_brief"],
        ),
        github=GitHubConfig(
            intake=s["github.intake"],
            poll_interval_seconds=s["github.poll_interval_seconds"],
            backfill_on_start=s["github.backfill_on_start"],
            backfill_lookback_hours=s["github.backfill_lookback_hours"],
        ),
        routing=RoutingConfig(
            ignore_targets=frozenset(s["routing.ignore_targets"]),
            notification_dedup_seconds=s["routing.notification_dedup_seconds"],
        ),
        agent_state=AgentStateConfig(
            stale_threshold_seconds=s["timing.stale_threshold_seconds"],
            snapshot_trust_seconds=s["timing.snapshot_trust_seconds"],
        ),
        monitor=MonitorConfig(
            interval_seconds=s["timing.monitor_interval_seconds"],
            retry_interval_seconds=s["timing.retry_interval_seconds"],
            start_timeout_seconds=s["timing.start_timeout_seconds"],
        ),
        delivery=DeliveryConfig(
            retention_days=s["timing.delivery_retention_days"],
            grace_period_seconds=s["timing.grace_period_seconds"],
            queue_expiry_minutes=s["timing.queue_expiry_minutes"],
        ),
        database_url_override=env.get("BACKBONE_DATABASE_URL", ""),
        telegram=TelegramConfig(
            allowed_chat_ids=tuple(int(x) for x in s["telegram.allowed_chat_ids"]),
            topic_routes={int(k): str(v) for k, v in s["telegram.topic_routes"].items()},
            group_chat_id=_opt_int(s["telegram.group_chat_id"]),
            notification_chat_id=_opt_int(s["telegram.notification_chat_id"]),
        ),
        priority_scoring=PriorityScoringConfig(
            blocking_weight=float(s["priority.blocking_weight"]),
            type_weights={k: float(v) for k, v in s["priority.type_weights"].items()},
            dependents_multiplier=float(s["priority.dependents_multiplier"]),
            age_tiebreaker_weight=float(s["priority.age_tiebreaker_weight"]),
        ),
        escalation=EscalationConfig(
            target=s["escalation.target"],
            stall_threshold_seconds=s["timing.stall_threshold_seconds"],
            dedup_seconds=s["timing.escalation_dedup_seconds"],
        ),
        security=SecurityConfig(
            allow_remote_plan_control=bool(s["security.allow_remote_plan_control"]),
            allow_unauthenticated=(
                env.get("BACKBONE_ALLOW_UNAUTHENTICATED", "").lower() in ("1", "true")
                or bool(s["security.allow_unauthenticated"])
            ),
        ),
        settings=s,
    )


def agents_from_rows(rows: list[dict]) -> AgentsConfig:
    """Build the agents snapshot from ``agents`` table rows."""
    specs: dict[str, AgentSpec] = {}
    for row in rows:
        specs[row["name"]] = AgentSpec(
            name=row["name"],
            dir=row["dir"],
            runtime=row.get("runtime") or "claude",
            model=row.get("model") or None,
            repo=row.get("repo") or "",
            watches=tuple(row.get("watches") or ()),
            tags=tuple(row.get("tags") or ()),
            env=dict(row.get("env") or {}),
            description=row.get("description") or "",
        )
    return AgentsConfig(specs=specs)
