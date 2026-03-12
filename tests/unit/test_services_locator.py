"""Tests for services/_locator.py — service locator module."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_backbone.services._locator import (
    ensure_initialized,
    get_config,
    get_db,
    get_gh,
    init,
    reset,
)


class TestServiceLocator:
    def test_uninitialized_config_raises(self):
        """get_config() raises RuntimeError before init()."""
        with pytest.raises(RuntimeError, match="not initialized"):
            get_config()

    def test_uninitialized_db_raises(self):
        """get_db() raises RuntimeError before init()."""
        with pytest.raises(RuntimeError, match="not initialized"):
            get_db()

    def test_uninitialized_gh_raises(self):
        """get_gh() raises RuntimeError before init()."""
        with pytest.raises(RuntimeError, match="not initialized"):
            get_gh()

    def test_init_and_get(self):
        """init() sets services, getters return them."""
        mock_config = MagicMock()
        mock_db = MagicMock()
        mock_gh = MagicMock()

        init(config=mock_config, db=mock_db, gh=mock_gh)

        assert get_config() is mock_config
        assert get_db() is mock_db
        assert get_gh() is mock_gh

    def test_reset_clears(self):
        """reset() clears all services."""
        init(config=MagicMock(), db=MagicMock(), gh=MagicMock())
        reset()

        with pytest.raises(RuntimeError):
            get_config()

    def test_init_reset_init_cycle(self):
        """Services can be re-initialized after reset."""
        mock1 = MagicMock()
        mock2 = MagicMock()

        init(config=mock1, db=MagicMock(), gh=MagicMock())
        assert get_config() is mock1

        reset()

        init(config=mock2, db=MagicMock(), gh=MagicMock())
        assert get_config() is mock2

    @pytest.mark.asyncio
    async def test_ensure_initialized_bootstraps_services(self):
        """ensure_initialized() bootstraps services from the env-aware settings path."""
        reset()
        mock_config = MagicMock()
        mock_db_service = MagicMock()
        mock_db_service.start = AsyncMock()
        mock_db = MagicMock()
        mock_db.start = AsyncMock()
        mock_gh = MagicMock()
        mock_gh.start = AsyncMock()
        fake_github_interface = ModuleType("agent_backbone.services.github.interface")
        fake_github_interface.GitHubClient = MagicMock(return_value=mock_gh)
        fake_github_package = ModuleType("agent_backbone.services.github")
        fake_github_package.interface = fake_github_interface

        with (
            patch(
                "agent_backbone.settings.AppSettings.build_config",
                return_value=mock_config,
            ),
            patch(
                "agent_backbone.services.database.interface.DatabaseService",
                return_value=mock_db_service,
            ),
            patch(
                "agent_backbone.services.database.backbone_db.BackboneDB",
                return_value=mock_db,
            ),
            patch.dict(
                sys.modules,
                {
                    "agent_backbone.services.github": fake_github_package,
                    "agent_backbone.services.github.interface": fake_github_interface,
                },
            ),
        ):
            await ensure_initialized()

        mock_db_service.start.assert_awaited_once()
        mock_db.start.assert_awaited_once()
        mock_gh.start.assert_awaited_once()
        assert get_config() is mock_config
        assert get_db() is mock_db
        assert get_gh() is mock_gh
