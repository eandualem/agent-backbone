"""Application settings — Pydantic Settings entry point for configuration.

Loads secrets from environment variables (BACKBONE_ prefix), then delegates
structural config to BackboneConfig.from_toml(). This is the primary config
entry point for the FastAPI lifespan.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from agent_backbone.config import BackboneConfig


class AppSettings(BaseSettings):
    """Environment-sourced settings that wrap BackboneConfig."""

    model_config = SettingsConfigDict(env_prefix="BACKBONE_", env_nested_delimiter="__")

    github_token: str = ""
    webhook_secret: str = ""
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

        from agent_backbone.config import JarvisConfig

        toml_path = Path(self.config_path) if self.config_path else None
        config = BackboneConfig.from_toml(path=toml_path)

        # Overlay env-sourced secrets where provided
        overrides: dict = {}
        if self.github_token:
            overrides["github_token"] = self.github_token
        if self.webhook_secret:
            overrides["webhook_secret"] = self.webhook_secret
        if self.jarvis_inject_url or self.jarvis_sessions_url:
            overrides["jarvis"] = JarvisConfig(
                inject_url=self.jarvis_inject_url or config.jarvis.inject_url,
                sessions_url=self.jarvis_sessions_url or config.jarvis.sessions_url,
            )

        # Overlay database fields if any env var is provided
        db_overrides: dict = {}
        if self.database_host:
            db_overrides["host"] = self.database_host
        if self.database_port:
            db_overrides["port"] = self.database_port
        if self.database_user:
            db_overrides["user"] = self.database_user
        if self.database_password:
            db_overrides["password"] = self.database_password
        if self.database_name:
            db_overrides["name"] = self.database_name
        if db_overrides:
            overrides["database"] = config.database.model_copy(update=db_overrides)

        if overrides:
            config = replace(config, **overrides)
        return config
