"""Interactive attachment uses exact targets without sending input to an agent."""

from unittest.mock import patch

import pytest

from agent_backbone.services.terminal import attach_session


@pytest.mark.parametrize(
    ("inside", "read_only", "expected"),
    [
        (False, False, ["tmux", "attach-session", "-t", "=api:"]),
        (False, True, ["tmux", "attach-session", "-t", "=api:", "-r"]),
        (True, False, ["tmux", "switch-client", "-t", "=api:"]),
    ],
)
def test_attach_argv(monkeypatch, inside, read_only, expected):
    if inside:
        monkeypatch.setenv("TMUX", "/example")
    else:
        monkeypatch.delenv("TMUX", raising=False)
    with patch("agent_backbone.services.terminal._attach.subprocess.call", return_value=0) as run:
        assert attach_session("api", read_only=read_only) == 0
    run.assert_called_once_with(expected)


def test_readonly_never_mutates_existing_client(monkeypatch):
    monkeypatch.setenv("TMUX", "/example")
    with (
        patch("agent_backbone.services.terminal._attach.subprocess.call") as run,
        pytest.raises(ValueError, match="another terminal"),
    ):
        attach_session("api", read_only=True)
    run.assert_not_called()
