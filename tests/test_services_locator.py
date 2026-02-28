"""Tests for services/_locator.py — service locator module."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent_backbone.services._locator import get_config, get_db, get_gh, init, reset


class TestServiceLocator:
    def test_uninitialized_config_raises(self):
        """get_config() raises AssertionError before init()."""
        with pytest.raises(AssertionError, match="not initialized"):
            get_config()

    def test_uninitialized_db_raises(self):
        """get_db() raises AssertionError before init()."""
        with pytest.raises(AssertionError, match="not initialized"):
            get_db()

    def test_uninitialized_gh_raises(self):
        """get_gh() raises AssertionError before init()."""
        with pytest.raises(AssertionError, match="not initialized"):
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

        with pytest.raises(AssertionError):
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
