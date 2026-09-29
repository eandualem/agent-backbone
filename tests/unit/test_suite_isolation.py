"""The suite run from an agent's own session stays off the real installation (#351)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_backbone.config import resolve_data_dir

_SESSION = {
    "BACKBONE_STATE_DIR": "/real/data/state",
    "BACKBONE_DATA_DIR": "/real/data",
    "BACKBONE_AGENT": "app",
    "TMUX": "/real/tmux-501/default,1,0",
    "TMUX_PANE": "%1",
}


@pytest.fixture(scope="module")
def session_environment():
    """What a backbone-started session exports, set before each test's isolation."""
    with pytest.MonkeyPatch.context() as patch:
        for key, value in _SESSION.items():
            patch.setenv(key, value)
        yield


def test_a_session_environment_does_not_reach_a_test(session_environment):
    assert not set(_SESSION) & set(os.environ)
    assert resolve_data_dir() == Path.home() / ".local" / "share" / "agent-backbone"
