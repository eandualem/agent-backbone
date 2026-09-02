"""Settings endpoints — the database-backed configuration."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from agent_backbone.api.deps import get_agent_store, get_config
from agent_backbone.config import SETTINGS_DEFAULTS, SETTINGS_HELP, BackboneConfig
from agent_backbone.services.agents import AgentStore

router = APIRouter(prefix="/api", tags=["config"])


class SettingValue(BaseModel):
    value: Any


@router.get("/config")
async def list_settings(config: BackboneConfig = Depends(get_config)):
    """Effective settings with defaults, help text and whether each is overridden."""
    stored = getattr(config, "settings", {})
    return {
        key: {
            "value": stored.get(key, default),
            "default": default,
            "help": SETTINGS_HELP.get(key, ""),
        }
        for key, default in SETTINGS_DEFAULTS.items()
    }


@router.get("/config/{key}")
async def get_setting(key: str, config: BackboneConfig = Depends(get_config)):
    if key not in SETTINGS_DEFAULTS:
        raise HTTPException(status_code=404, detail=f"unknown setting {key!r}")
    return {"key": key, "value": config.settings.get(key, SETTINGS_DEFAULTS[key])}


@router.put("/config/{key}")
async def set_setting(key: str, body: SettingValue, store: AgentStore = Depends(get_agent_store)):
    try:
        new_config = await store.set_setting(key, body.value)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"key": key, "value": new_config.settings[key]}


@router.delete("/config/{key}")
async def unset_setting(key: str, store: AgentStore = Depends(get_agent_store)):
    if key not in SETTINGS_DEFAULTS:
        raise HTTPException(status_code=404, detail=f"unknown setting {key!r}")
    new_config = await store.unset_setting(key)
    return {"key": key, "value": new_config.settings[key], "default": True}
