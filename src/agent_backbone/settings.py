"""Application settings — Pydantic Settings entry point for configuration.

Loads secrets from environment variables (BACKBONE_ prefix), then delegates
structural config to BackboneConfig.from_toml(). This is the primary config
entry point for the FastAPI lifespan.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from agent_backbone.config import BackboneConfig

_DIRECT_OVERRIDE_FIELDS = (
    "webhook_secret",
    "github_app_private_key_path",
    "github_app_webhook_secret",
)
_DATABASE_OVERRIDE_FIELDS = (
    ("host", "database_host"),
    ("port", "database_port"),
    ("user", "database_user"),
    ("password", "database_password"),
    ("name", "database_name"),
)


class AppSettings(BaseSettings):
    """Environment-sourced settings that wrap BackboneConfig."""

    model_config = SettingsConfigDict(env_prefix="BACKBONE_", env_nested_delimiter="__")

    webhook_secret: str = ""
    github_app_id: int | None = None
    github_app_private_key_path: str = ""
    github_app_webhook_secret: str = ""
    jarvis_inject_url: str = ""
    jarvis_sessions_url: str = ""
    telegram_token: str = ""
    config_path: str = ""
    database_host: str = ""
    database_port: int = 0
    database_user: str = ""
    database_password: str = ""
    database_name: str = ""

    def build_config(self) -> BackboneConfig:
        """Build BackboneConfig from TOML + env settings.

        Calls existing from_toml() for structural config, then
        overlays env-sourced secrets via dataclass replace.
        """
        from dataclasses import replace
        config = BackboneConfig.from_toml(path=self._config_path())
        overrides = self._build_overrides(config)
        return replace(config, **overrides) if overrides else config

    def _config_path(self) -> Path | None:
        """Return an explicit TOML path when configured."""
        return Path(self.config_path) if self.config_path else None

    def _build_overrides(self, config: BackboneConfig) -> dict[str, object]:
        """Collect env-derived overrides for the frozen backbone config."""
        overrides = {
            field_name: value
            for field_name in _DIRECT_OVERRIDE_FIELDS
            if (value := getattr(self, field_name))
        }
        if self.github_app_id is not None:
            overrides["github_app_id"] = self.github_app_id

        jarvis_override = self._jarvis_override(config)
        if jarvis_override is not None:
            overrides["jarvis"] = jarvis_override

        database_override = self._database_override(config)
        if database_override is not None:
            overrides["database"] = database_override

        return overrides

    def _jarvis_override(self, config: BackboneConfig) -> object | None:
        """Build a Jarvis override only when env settings are present."""
        if not (self.jarvis_inject_url or self.jarvis_sessions_url):
            return None

        from agent_backbone.config import JarvisConfig

        return JarvisConfig(
            inject_url=self.jarvis_inject_url or config.jarvis.inject_url,
            sessions_url=self.jarvis_sessions_url or config.jarvis.sessions_url,
        )

    def _database_override(self, config: BackboneConfig) -> object | None:
        """Build a database model override only when env settings are present."""
        updates = {
            field_name: value
            for field_name, settings_field in _DATABASE_OVERRIDE_FIELDS
            if (value := getattr(self, settings_field))
        }
        if not updates:
            return None
        return config.database.model_copy(update=updates)
