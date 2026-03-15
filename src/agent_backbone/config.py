"""Configuration for the agent backbone.

Loads structural config from backbone.toml (committed), secrets from env vars.
Nested frozen dataclasses per section. Works without TOML file (falls back to defaults).
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from agent_backbone.services.database.config import DatabaseConfig
from agent_backbone.services.registry import EntityRegistry, build_registry

load_dotenv()

# Repo name pattern: "[type] repo-name: description" or "[type] org/repo-name: description"
REPO_NAME_PATTERN: re.Pattern[str] = re.compile(r"^\[[^\]]+\]\s+([\w/.-]+):")

# Default TOML path: backbone.toml in repo root
_DEFAULT_TOML_PATH = Path(__file__).resolve().parent.parent.parent / "backbone.toml"


@dataclass(frozen=True)
class GatewayConfig:
    """Gateway server settings."""

    port: int = 7120
    max_delivery_ids: int = 100


@dataclass(frozen=True)
class GitHubConfig:
    """GitHub API settings (non-secret)."""

    owner: str = "eandualem"
    repo: str = "orchestration"


@dataclass(frozen=True)
class RegistryConfig:
    """Registry file paths for entity and repo discovery."""

    path: str = "~/.claude/state/entity-registry.json"
    code_base_dir: str = "~/ws/core/code"

    @property
    def registry_path(self) -> Path:
        return Path(self.path).expanduser()

    @property
    def code_base_path(self) -> Path:
        return Path(self.code_base_dir).expanduser()


@dataclass(frozen=True)
class EntityConfig:
    """Entity routing configuration (non-registry parts)."""

    skip: frozenset[str] = frozenset({"elias"})
    fallback: dict[str, str] = field(default_factory=lambda: {"coding-agent": "ike"})
    service_sessions: frozenset[str] = frozenset(
        {
            "prefect",
            "prefect-worker",
            "prefect-server",
            "telegram-bot",
            "gateway",
            "backbone-worker",
        }
    )


@dataclass(frozen=True)
class DedupConfig:
    """Notification deduplication settings."""

    notification_window_seconds: int = 10


@dataclass(frozen=True)
class AgentStateConfig:
    """Agent state tracking settings."""

    state_dir: str = "~/.claude/state"
    stale_threshold_seconds: int = 300

    @property
    def state_path(self) -> Path:
        return Path(self.state_dir).expanduser()


@dataclass(frozen=True)
class SchedulingConfig:
    """Scheduling and retry settings."""

    monitor_interval_seconds: int = 60
    delivery_retry_interval_seconds: int = 300
    work_pool_name: str = "agent-pool"


@dataclass(frozen=True)
class DeliveryConfig:
    """Delivery tracking / persistence settings."""

    retention_days: int = 30


@dataclass(frozen=True)
class TelegramConfig:
    """Telegram bot settings."""

    allowed_chat_ids: list[int] = field(default_factory=list)
    topic_routes: dict[int, str] = field(default_factory=dict)
    group_chat_id: int | None = None
    notification_chat_id: int | None = None
    topic_discovery_file: str = "~/.claude/state/telegram-topics.json"

    @property
    def topic_discovery_path(self) -> Path:
        return Path(self.topic_discovery_file).expanduser()


@dataclass(frozen=True)
class DailyRoutineConfig:
    """Daily routine scheduling settings."""

    morning_time: str = "08:00"
    evening_time: str = "18:00"
    morning_agents: list[str] = field(default_factory=lambda: ["ike", "feynman"])
    timezone: str = "Africa/Addis_Ababa"


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
class CapacityRoutingConfig:
    """Capacity-aware routing settings."""

    busy_threshold_seconds: int = 1800


@dataclass(frozen=True)
class HeartbeatConfig:
    """Heartbeat scheduler settings."""

    schedule_file: str = "~/.claude/state/heartbeat-schedules.json"
    default_timezone: str = "Africa/Addis_Ababa"

    @property
    def schedule_path(self) -> Path:
        return Path(self.schedule_file).expanduser()


@dataclass(frozen=True)
class SessionBridgeConfig:
    """Session bridge settings."""

    grace_period_seconds: int = 5
    queue_retry_seconds: int = 30


@dataclass(frozen=True)
class EscalationConfig:
    """Escalation settings for stalled/offline agent detection."""

    stall_threshold_seconds: int = 5400  # 90 minutes
    escalation_target: str = "ike"
    escalation_dedup_seconds: int = 1800  # 30 minutes


@dataclass(frozen=True)
class JarvisConfig:
    """Jarvis injection endpoint settings."""

    inject_url: str = ""
    sessions_url: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.inject_url)


@dataclass(frozen=True)
class BackboneConfig:
    """Top-level configuration. Assembled from TOML + env vars."""

    # Secrets (env vars only)
    webhook_secret: str = field(default_factory=lambda: _load_webhook_secret())
    github_app_id: int | None = field(default_factory=lambda: _load_optional_int("GITHUB_APP_ID"))
    github_app_private_key_path: str = field(
        default_factory=lambda: os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH", "")
    )
    github_app_webhook_secret: str = field(
        default_factory=lambda: os.environ.get("GITHUB_APP_WEBHOOK_SECRET", "")
    )

    # Nested structural config
    gateway: GatewayConfig = field(default_factory=GatewayConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    entities: EntityConfig = field(default_factory=EntityConfig)
    registry: EntityRegistry = field(default_factory=EntityRegistry)
    dedup: DedupConfig = field(default_factory=DedupConfig)
    agent_state: AgentStateConfig = field(default_factory=AgentStateConfig)
    scheduling: SchedulingConfig = field(default_factory=SchedulingConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    daily_routines: DailyRoutineConfig = field(default_factory=DailyRoutineConfig)
    priority_scoring: PriorityScoringConfig = field(default_factory=PriorityScoringConfig)
    capacity_routing: CapacityRoutingConfig = field(default_factory=CapacityRoutingConfig)
    escalation: EscalationConfig = field(default_factory=EscalationConfig)
    heartbeat: HeartbeatConfig = field(default_factory=HeartbeatConfig)
    session_bridge: SessionBridgeConfig = field(default_factory=SessionBridgeConfig)
    jarvis: JarvisConfig = field(default_factory=JarvisConfig)

    @property
    def webhook_secrets(self) -> tuple[str, ...]:
        """Return the configured webhook secrets in validation order."""
        return tuple(
            dict.fromkeys(
                secret for secret in (self.webhook_secret, self.github_app_webhook_secret) if secret
            )
        )

    @property
    def github_app_ready(self) -> bool:
        """Whether GitHub App credentials are configured for REST API access."""
        return bool(self.github_app_id and self.github_app_private_key_path)

    @classmethod
    def from_toml(cls, path: Path | None = None) -> BackboneConfig:
        """Load config from TOML file + env var overrides.

        Falls back to defaults if TOML file doesn't exist.
        """
        toml_path = path or _DEFAULT_TOML_PATH
        raw: dict = {}
        if toml_path.exists():
            with open(toml_path, "rb") as f:
                raw = tomllib.load(f)

        gw = raw.get("gateway", {})
        gh = raw.get("github", {})
        ent = raw.get("entities", {})
        reg = raw.get("registry", {})
        dd = raw.get("dedup", {})
        ag = raw.get("agent_state", {})
        sc = raw.get("scheduling", {})
        db = raw.get("database", {})
        dl = raw.get("delivery", {})
        tg = raw.get("telegram", {})
        dr = raw.get("daily_routines", {})
        ps = raw.get("priority_scoring", {})
        cr = raw.get("capacity_routing", {})
        es = raw.get("escalation", {})
        hb = raw.get("heartbeat", {})
        sb = raw.get("session_bridge", {})

        # Build registry from JSON + filesystem
        import logging

        _log = logging.getLogger(__name__)
        reg_cfg = RegistryConfig(
            path=reg.get("path", "~/.claude/state/entity-registry.json"),
            code_base_dir=reg.get("code_base_dir", "~/ws/core/code"),
        )
        try:
            registry = build_registry(reg_cfg.registry_path, reg_cfg.code_base_path)
        except FileNotFoundError:
            _log.warning(
                "Entity registry not found at %s — using empty registry",
                reg_cfg.registry_path,
            )
            registry = EntityRegistry()

        return cls(
            gateway=GatewayConfig(
                port=gw.get("port", 7120),
                max_delivery_ids=gw.get("max_delivery_ids", 100),
            ),
            github=GitHubConfig(
                owner=gh.get("owner", "eandualem"),
                repo=gh.get("repo", "orchestration"),
            ),
            entities=EntityConfig(
                skip=frozenset(ent.get("skip", ["elias"])),
                fallback=ent.get("fallback", {"coding-agent": "ike"}),
                service_sessions=frozenset(
                    ent.get(
                        "service_sessions",
                        [
                            "prefect",
                            "prefect-worker",
                            "prefect-server",
                            "telegram-bot",
                            "gateway",
                            "backbone-worker",
                        ],
                    )
                ),
            ),
            registry=registry,
            dedup=DedupConfig(
                notification_window_seconds=dd.get("notification_window_seconds", 10),
            ),
            agent_state=AgentStateConfig(
                state_dir=ag.get("state_dir", "~/.claude/state"),
                stale_threshold_seconds=ag.get("stale_threshold_seconds", 300),
            ),
            scheduling=SchedulingConfig(
                monitor_interval_seconds=sc.get("monitor_interval_seconds", 60),
                delivery_retry_interval_seconds=sc.get("delivery_retry_interval_seconds", 300),
                work_pool_name=sc.get("work_pool_name", "agent-pool"),
            ),
            database=DatabaseConfig(
                host=db.get("host", "localhost"),
                port=db.get("port", 5435),
                user=db.get("user", "backbone"),
                password=db.get("password", "backbone"),
                name=db.get("name", "backbone"),
                pool_size=db.get("pool_size", 5),
                pool_overflow=db.get("pool_overflow", 10),
                echo=db.get("echo", False),
            ),
            delivery=DeliveryConfig(
                retention_days=dl.get("retention_days", 30),
            ),
            telegram=TelegramConfig(
                allowed_chat_ids=tg.get("allowed_chat_ids", []),
                topic_routes={int(k): v for k, v in tg.get("topic_routes", {}).items()},
                group_chat_id=tg.get("group_chat_id"),
                notification_chat_id=tg.get("notification_chat_id"),
                topic_discovery_file=tg.get(
                    "topic_discovery_file",
                    "~/.claude/state/telegram-topics.json",
                ),
            ),
            daily_routines=DailyRoutineConfig(
                morning_time=dr.get("morning_time", "08:00"),
                evening_time=dr.get("evening_time", "18:00"),
                morning_agents=dr.get("morning_agents", ["ike", "feynman"]),
                timezone=dr.get("timezone", "Africa/Addis_Ababa"),
            ),
            priority_scoring=PriorityScoringConfig(
                blocking_weight=ps.get("blocking_weight", 1000.0),
                type_weights=ps.get(
                    "type_weights",
                    {
                        "spec-gap": 100.0,
                        "bug": 90.0,
                        "task": 50.0,
                        "question": 20.0,
                        "optimization": 10.0,
                    },
                ),
                dependents_multiplier=ps.get("dependents_multiplier", 1.5),
                age_tiebreaker_weight=ps.get("age_tiebreaker_weight", 0.01),
            ),
            capacity_routing=CapacityRoutingConfig(
                busy_threshold_seconds=cr.get("busy_threshold_seconds", 1800),
            ),
            escalation=EscalationConfig(
                stall_threshold_seconds=es.get("stall_threshold_seconds", 5400),
                escalation_target=es.get("escalation_target", "ike"),
                escalation_dedup_seconds=es.get("escalation_dedup_seconds", 1800),
            ),
            heartbeat=HeartbeatConfig(
                schedule_file=hb.get("schedule_file", "~/.claude/state/heartbeat-schedules.json"),
                default_timezone=hb.get("default_timezone", "Africa/Addis_Ababa"),
            ),
            session_bridge=SessionBridgeConfig(
                grace_period_seconds=sb.get("grace_period_seconds", 5),
                queue_retry_seconds=sb.get("queue_retry_seconds", 30),
            ),
            jarvis=JarvisConfig(
                inject_url=os.environ.get("JARVIS_INJECT_URL", ""),
                sessions_url=os.environ.get("JARVIS_SESSIONS_URL", ""),
            ),
        )


def _load_webhook_secret() -> str:
    """Load webhook secret from env or fallback to file.

    Checks repo root first (.webhook-secret next to backbone.toml),
    then legacy path (~/.claude/services/.webhook-secret).
    """
    secret = os.environ.get("WEBHOOK_SECRET", "")
    if secret:
        return secret
    # Repo root (next to backbone.toml)
    repo_secret = _DEFAULT_TOML_PATH.parent / ".webhook-secret"
    try:
        return repo_secret.read_text().strip()
    except FileNotFoundError:
        pass
    # Legacy path
    legacy_secret = Path.home() / ".claude" / "services" / ".webhook-secret"
    try:
        return legacy_secret.read_text().strip()
    except FileNotFoundError:
        return ""


def _load_optional_int(env_var: str) -> int | None:
    """Load an optional integer from the environment."""
    value = os.environ.get(env_var, "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None
