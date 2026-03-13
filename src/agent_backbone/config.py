"""Configuration for the agent backbone.

Loads structural config from backbone.toml (committed), secrets from env vars.
Nested frozen dataclasses per section. Works without TOML file (falls back to defaults).
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import TypeVar, cast

from dotenv import load_dotenv

from agent_backbone.services.database.config import DatabaseConfig
from agent_backbone.services.registry import EntityRegistry, build_registry

load_dotenv()

# Repo name pattern: "[type] repo-name: description" or "[type] org/repo-name: description"
REPO_NAME_PATTERN: re.Pattern[str] = re.compile(r"^\[[^\]]+\]\s+([\w/.-]+):")

# Default TOML path: backbone.toml in repo root
_DEFAULT_TOML_PATH = Path(__file__).resolve().parent.parent.parent / "backbone.toml"
_ConfigT = TypeVar("_ConfigT")
_Converter = Callable[[object], object]
log = logging.getLogger(__name__)


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
            "ngrok",
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
                secret
                for secret in (self.webhook_secret, self.github_app_webhook_secret)
                if secret
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
        raw = _load_raw_toml(path or _DEFAULT_TOML_PATH)
        registry_config = _build_dataclass_config(
            RegistryConfig(),
            _section(raw, "registry"),
        )

        return cls(
            gateway=_build_dataclass_config(
                GatewayConfig(),
                _section(raw, "gateway"),
            ),
            github=_build_dataclass_config(
                GitHubConfig(),
                _section(raw, "github"),
            ),
            entities=_build_dataclass_config(
                EntityConfig(),
                _section(raw, "entities"),
                converters={
                    "skip": frozenset,
                    "service_sessions": frozenset,
                },
            ),
            registry=_load_registry(registry_config),
            dedup=_build_dataclass_config(
                DedupConfig(),
                _section(raw, "dedup"),
            ),
            agent_state=_build_dataclass_config(
                AgentStateConfig(),
                _section(raw, "agent_state"),
            ),
            scheduling=_build_dataclass_config(
                SchedulingConfig(),
                _section(raw, "scheduling"),
            ),
            database=_build_database_config(_section(raw, "database")),
            delivery=_build_dataclass_config(
                DeliveryConfig(),
                _section(raw, "delivery"),
            ),
            telegram=_build_dataclass_config(
                TelegramConfig(),
                _section(raw, "telegram"),
                converters={"topic_routes": _coerce_topic_routes},
            ),
            daily_routines=_build_dataclass_config(
                DailyRoutineConfig(),
                _section(raw, "daily_routines"),
            ),
            priority_scoring=_build_dataclass_config(
                PriorityScoringConfig(),
                _section(raw, "priority_scoring"),
            ),
            capacity_routing=_build_dataclass_config(
                CapacityRoutingConfig(),
                _section(raw, "capacity_routing"),
            ),
            escalation=_build_dataclass_config(
                EscalationConfig(),
                _section(raw, "escalation"),
            ),
            heartbeat=_build_dataclass_config(
                HeartbeatConfig(),
                _section(raw, "heartbeat"),
            ),
            session_bridge=_build_dataclass_config(
                SessionBridgeConfig(),
                _section(raw, "session_bridge"),
            ),
            jarvis=_build_jarvis_config(),
        )


def _load_raw_toml(path: Path) -> dict[str, object]:
    """Load a TOML file if present, otherwise return an empty mapping."""
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _section(raw: Mapping[str, object], name: str) -> Mapping[str, object]:
    """Return one TOML section as a mapping."""
    section = raw.get(name, {})
    if isinstance(section, Mapping):
        return cast(Mapping[str, object], section)
    return {}


def _build_dataclass_config(
    defaults: _ConfigT,
    section: Mapping[str, object],
    *,
    converters: Mapping[str, _Converter] | None = None,
) -> _ConfigT:
    """Apply known TOML keys onto a frozen dataclass instance."""
    updates: dict[str, object] = {}
    for dataclass_field in fields(defaults):
        if dataclass_field.name not in section:
            continue
        value = section[dataclass_field.name]
        if converters and dataclass_field.name in converters:
            value = converters[dataclass_field.name](value)
        updates[dataclass_field.name] = value
    return replace(defaults, **updates) if updates else defaults


def _build_database_config(section: Mapping[str, object]) -> DatabaseConfig:
    """Apply known TOML keys onto the database model defaults."""
    defaults = DatabaseConfig()
    updates = {
        field_name: section[field_name]
        for field_name in DatabaseConfig.model_fields
        if field_name in section
    }
    if not updates:
        return defaults
    return DatabaseConfig.model_validate({**defaults.model_dump(), **updates})


def _coerce_topic_routes(value: object) -> dict[int, str]:
    """Convert TOML topic route keys to integer thread IDs."""
    return {
        int(key): route
        for key, route in cast(Mapping[object, str], value).items()
    }


def _build_jarvis_config() -> JarvisConfig:
    """Build the env-only Jarvis integration config."""
    return JarvisConfig(
        inject_url=os.environ.get("JARVIS_INJECT_URL", ""),
        sessions_url=os.environ.get("JARVIS_SESSIONS_URL", ""),
    )


def _load_registry(config: RegistryConfig) -> EntityRegistry:
    """Build the entity registry from disk with the configured fallback."""
    try:
        return build_registry(config.registry_path, config.code_base_path)
    except FileNotFoundError:
        log.warning(
            "Entity registry not found at %s — using empty registry",
            config.registry_path,
        )
        return EntityRegistry()


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
