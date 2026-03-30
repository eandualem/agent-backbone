"""Tests for tmux/process observation helpers."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent_backbone.services.agents._observation import (
    SessionObservation,
    _extract_permission_request,
    _has_child_processes,
    _is_infrastructure_process,
    enrich_idle_state,
    snapshot_from_observation,
)
from agent_backbone.services.agents.models import AgentState, StateSnapshot

_OBS = "agent_backbone.services.agents._observation"


class _Proc:
    def __init__(self, *, returncode: int, stdout: bytes):
        self.returncode = returncode
        self._stdout = stdout

    async def communicate(self):
        return self._stdout, b""


class TestInfrastructureProcessFilter:
    def test_matches_docker_processes(self):
        assert _is_infrastructure_process("docker") is True
        assert _is_infrastructure_process("/usr/local/bin/docker-init") is True
        assert _is_infrastructure_process("containerd-shim") is True
        assert _is_infrastructure_process("com.docker.backend") is True

    def test_preserves_real_child_processes(self):
        assert _is_infrastructure_process("python") is False
        assert _is_infrastructure_process("/usr/bin/node") is False


class TestHasChildProcesses:
    @pytest.mark.asyncio
    async def test_none_parent_pid_is_false(self):
        assert await _has_child_processes(None) is False

    @pytest.mark.asyncio
    async def test_infrastructure_only_children_are_ignored(self):
        procs = [
            _Proc(returncode=0, stdout=b"101\n102\n"),
            _Proc(returncode=0, stdout=b"docker\ncontainerd-shim\n"),
        ]

        async def mock_exec(*args, **kwargs):
            return procs.pop(0)

        with patch(f"{_OBS}.asyncio.create_subprocess_exec", side_effect=mock_exec):
            assert await _has_child_processes(42) is False

    @pytest.mark.asyncio
    async def test_non_infrastructure_child_marks_session_busy(self):
        procs = [
            _Proc(returncode=0, stdout=b"101\n102\n"),
            _Proc(returncode=0, stdout=b"docker\npython\n"),
        ]

        async def mock_exec(*args, **kwargs):
            return procs.pop(0)

        with patch(f"{_OBS}.asyncio.create_subprocess_exec", side_effect=mock_exec):
            assert await _has_child_processes(42) is True


class TestPermissionPromptParsing:
    def test_extracts_claude_permission_request(self):
        pane = """
        │ Read
        │ /Users/elias/ws/core/code/WF/agent-orchestration-dashboard/.claude/
        │ Do you want to proceed?
        │ Yes, proceed
        """

        assert _extract_permission_request(pane) == {
            "tool": "Read",
            "target": "/Users/elias/ws/core/code/WF/agent-orchestration-dashboard/.claude/",
            "prompt": "Do you want to proceed?",
        }

    def test_extracts_codex_permission_request_with_bash_command(self):
        pane = """
        Bash
        $ git restore src/agent_backbone/api/routes/agents.py
        Working directory: /Users/elias/ws/core/code/WF/agent-backbone
        Press enter to confirm or Esc to cancel
        """

        assert _extract_permission_request(pane) == {
            "tool": "Bash",
            "target": "git restore src/agent_backbone/api/routes/agents.py",
            "command": "git restore src/agent_backbone/api/routes/agents.py",
            "cwd": "/Users/elias/ws/core/code/WF/agent-backbone",
            "prompt": "Press enter to confirm or Esc to cancel",
        }

    def test_ignores_regular_runtime_status_chrome(self):
        pane = """
        › Explain this codebase

          gpt-5.4 xhigh · 88% left · ~/ws/core/code/WF/agent-backbone
        """

        assert _extract_permission_request(pane) is None


class TestSnapshotFromObservation:
    def test_promotes_permission_prompt_to_permission_waiting(self):
        snapshot = snapshot_from_observation(
            SessionObservation(
                session="agent-backbone",
                online=True,
                permission_request={
                    "tool": "Read",
                    "target": "/tmp/example",
                    "prompt": "Do you want to proceed?",
                },
            ),
            timestamp=123.0,
        )

        assert snapshot.state == AgentState.PERMISSION_WAITING
        assert snapshot.context == {
            "tool": "Read",
            "target": "/tmp/example",
            "prompt": "Do you want to proceed?",
        }


class TestEnrichIdleState:
    @pytest.mark.asyncio
    async def test_upgrades_idle_to_permission_waiting(self):
        snapshot = StateSnapshot(state=AgentState.IDLE, source="hook", timestamp=55.0)

        with patch(
            f"{_OBS}.observe_session",
            return_value=SessionObservation(
                session="agent-backbone",
                online=True,
                permission_request={
                    "tool": "Bash",
                    "target": "git restore src/file.py",
                    "prompt": "Press enter to confirm",
                },
            ),
        ):
            enriched = await enrich_idle_state("agent-backbone", snapshot)

        assert enriched.state == AgentState.PERMISSION_WAITING
        assert enriched.timestamp == 55.0
        assert enriched.source == "hook"
        assert enriched.context == {
            "tool": "Bash",
            "target": "git restore src/file.py",
            "prompt": "Press enter to confirm",
        }
