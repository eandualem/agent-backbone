"""Tests for src/workflow_engine.py — workflow execution engine."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from src.workflow_engine import execute_workflow_steps

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_engine(
    start_ok: bool = True,
    stop_ok: bool = True,
    send_ok: bool = True,
    resolve_dir: str = "/resolved/dir",
):
    """Return a context-manager stack that patches all tmux operations
    imported into workflow_engine, with configurable return values."""
    return (
        patch("src.workflow_engine.start_session", new_callable=AsyncMock, return_value=start_ok),
        patch("src.workflow_engine.stop_session", new_callable=AsyncMock, return_value=stop_ok),
        patch("src.workflow_engine.send_message", new_callable=AsyncMock, return_value=send_ok),
        patch("src.workflow_engine.resolve_agent_dir", return_value=resolve_dir),
    )


# ---------------------------------------------------------------------------
# Start action
# ---------------------------------------------------------------------------


class TestStartStep:
    async def test_start_step_success(self, config):
        """Start action succeeds — result has expected structure."""
        with (
            patch(
                "src.workflow_engine.start_session",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_start,
            patch("src.workflow_engine.resolve_agent_dir", return_value="/home/agent"),
        ):
            steps = [{"action": "start", "session": "ike"}]
            result = await execute_workflow_steps(steps, config)

            assert result["ok"] is True
            assert len(result["steps"]) == 1

            step_result = result["steps"][0]
            assert step_result["action"] == "start"
            assert step_result["session"] == "ike"
            assert step_result["ok"] is True
            assert step_result["detail"] == "started"

            mock_start.assert_awaited_once()

    async def test_start_resolves_working_dir(self, config):
        """Start action calls resolve_agent_dir when no working_dir in step."""
        with (
            patch(
                "src.workflow_engine.start_session",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_start,
            patch(
                "src.workflow_engine.resolve_agent_dir",
                return_value="/resolved/path",
            ) as mock_resolve,
        ):
            steps = [{"action": "start", "session": "leo"}]
            await execute_workflow_steps(steps, config)

            mock_resolve.assert_called_once_with("leo")
            # start_session should receive the resolved dir
            call_kwargs = mock_start.call_args
            assert call_kwargs.kwargs.get("working_dir") == "/resolved/path"

    async def test_start_uses_explicit_working_dir(self, config):
        """Start action uses step-provided working_dir instead of resolving."""
        with (
            patch(
                "src.workflow_engine.start_session",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "src.workflow_engine.resolve_agent_dir",
                return_value="/resolved/path",
            ) as mock_resolve,
        ):
            steps = [{"action": "start", "session": "leo", "working_dir": "/custom/dir"}]
            await execute_workflow_steps(steps, config)

            # resolve_agent_dir should NOT be called when working_dir is provided
            mock_resolve.assert_not_called()


# ---------------------------------------------------------------------------
# Stop action
# ---------------------------------------------------------------------------


class TestStopStep:
    async def test_stop_step_success(self, config):
        """Stop action succeeds."""
        with patch(
            "src.workflow_engine.stop_session",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_stop:
            steps = [{"action": "stop", "session": "ike"}]
            result = await execute_workflow_steps(steps, config)

            assert result["ok"] is True
            assert len(result["steps"]) == 1
            assert result["steps"][0]["ok"] is True
            assert result["steps"][0]["detail"] == "stopped"
            mock_stop.assert_awaited_once_with("ike")

    async def test_stop_step_failure(self, config):
        """Stop action failure is reported correctly."""
        with patch("src.workflow_engine.stop_session", new_callable=AsyncMock, return_value=False):
            steps = [{"action": "stop", "session": "ike"}]
            result = await execute_workflow_steps(steps, config)

            assert result["ok"] is False
            assert result["steps"][0]["ok"] is False
            assert result["steps"][0]["detail"] == "failed to stop"


# ---------------------------------------------------------------------------
# Message action
# ---------------------------------------------------------------------------


class TestMessageStep:
    async def test_message_step_success(self, config):
        """Message action delivers successfully."""
        with patch(
            "src.workflow_engine.send_message",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_send:
            steps = [{"action": "message", "session": "ike", "message": "hello agent"}]
            result = await execute_workflow_steps(steps, config)

            assert result["ok"] is True
            assert result["steps"][0]["ok"] is True
            assert result["steps"][0]["detail"] == "sent"
            mock_send.assert_awaited_once_with("ike", "hello agent")

    async def test_message_step_missing_message(self, config):
        """Message with empty/no message field fails."""
        with patch("src.workflow_engine.send_message", new_callable=AsyncMock) as mock_send:
            # No message key at all
            steps = [{"action": "message", "session": "ike"}]
            result = await execute_workflow_steps(steps, config)

            assert result["ok"] is False
            assert result["steps"][0]["ok"] is False
            assert result["steps"][0]["detail"] == "No message provided"
            mock_send.assert_not_awaited()

    async def test_message_step_empty_string(self, config):
        """Message with empty string value fails."""
        with patch("src.workflow_engine.send_message", new_callable=AsyncMock) as mock_send:
            steps = [{"action": "message", "session": "ike", "message": ""}]
            result = await execute_workflow_steps(steps, config)

            assert result["ok"] is False
            assert result["steps"][0]["ok"] is False
            assert result["steps"][0]["detail"] == "No message provided"
            mock_send.assert_not_awaited()


# ---------------------------------------------------------------------------
# Unknown action
# ---------------------------------------------------------------------------


class TestUnknownAction:
    async def test_unknown_action(self, config):
        """Unknown action returns ok=False with descriptive detail."""
        steps = [{"action": "restart", "session": "ike"}]
        result = await execute_workflow_steps(steps, config)

        assert result["ok"] is False
        assert result["steps"][0]["ok"] is False
        assert "Unknown action" in result["steps"][0]["detail"]
        assert "restart" in result["steps"][0]["detail"]


# ---------------------------------------------------------------------------
# Missing fields
# ---------------------------------------------------------------------------


class TestMissingFields:
    async def test_missing_action(self, config):
        """Step without action field fails gracefully."""
        steps = [{"session": "ike"}]
        result = await execute_workflow_steps(steps, config)

        assert result["ok"] is False
        assert result["steps"][0]["ok"] is False
        assert "missing required" in result["steps"][0]["detail"].lower()

    async def test_missing_session(self, config):
        """Step without session field fails gracefully."""
        steps = [{"action": "start"}]
        result = await execute_workflow_steps(steps, config)

        assert result["ok"] is False
        assert result["steps"][0]["ok"] is False
        assert "missing required" in result["steps"][0]["detail"].lower()

    async def test_empty_step(self, config):
        """Completely empty step dict fails gracefully."""
        steps = [{}]
        result = await execute_workflow_steps(steps, config)

        assert result["ok"] is False
        assert len(result["steps"]) == 1
        assert result["steps"][0]["ok"] is False


# ---------------------------------------------------------------------------
# Multi-step scenarios
# ---------------------------------------------------------------------------


class TestMultiStep:
    async def test_partial_failure(self, config):
        """Mix of passing and failing steps — overall ok=False, all steps execute."""
        with (
            patch("src.workflow_engine.start_session", new_callable=AsyncMock, return_value=True),
            patch("src.workflow_engine.stop_session", new_callable=AsyncMock, return_value=False),
            patch("src.workflow_engine.resolve_agent_dir", return_value="/resolved"),
        ):
            steps = [
                {"action": "start", "session": "ike"},
                {"action": "stop", "session": "leo"},
            ]
            result = await execute_workflow_steps(steps, config)

            assert result["ok"] is False
            assert len(result["steps"]) == 2
            # First step succeeded
            assert result["steps"][0]["ok"] is True
            assert result["steps"][0]["action"] == "start"
            # Second step failed
            assert result["steps"][1]["ok"] is False
            assert result["steps"][1]["action"] == "stop"

    async def test_all_steps_succeed(self, config):
        """Multiple steps all pass — ok=True."""
        with (
            patch("src.workflow_engine.start_session", new_callable=AsyncMock, return_value=True),
            patch("src.workflow_engine.stop_session", new_callable=AsyncMock, return_value=True),
            patch("src.workflow_engine.send_message", new_callable=AsyncMock, return_value=True),
            patch("src.workflow_engine.resolve_agent_dir", return_value="/resolved"),
        ):
            steps = [
                {"action": "start", "session": "ike"},
                {"action": "message", "session": "ike", "message": "hello"},
                {"action": "stop", "session": "ike"},
            ]
            result = await execute_workflow_steps(steps, config)

            assert result["ok"] is True
            assert len(result["steps"]) == 3
            assert all(s["ok"] for s in result["steps"])

    async def test_empty_steps_list(self, config):
        """Empty steps list returns ok=True with no step results."""
        result = await execute_workflow_steps([], config)

        assert result["ok"] is True
        assert result["steps"] == []

    async def test_failure_does_not_stop_execution(self, config):
        """A failing step does not prevent subsequent steps from executing."""
        with (
            patch("src.workflow_engine.send_message", new_callable=AsyncMock, return_value=True),
        ):
            steps = [
                {"action": "bogus", "session": "ike"},  # will fail (unknown action)
                {"action": "message", "session": "ike", "message": "hi"},  # should still run
            ]
            result = await execute_workflow_steps(steps, config)

            assert result["ok"] is False
            assert len(result["steps"]) == 2
            assert result["steps"][0]["ok"] is False  # bogus failed
            assert result["steps"][1]["ok"] is True  # message succeeded
