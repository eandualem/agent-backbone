"""Tests for tmux/process observation helpers."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent_backbone.services.agents._observation import (
    ProcessDescendant,
    SessionObservation,
    _extract_permission_request,
    _find_sub_agent_processes,
    _is_cli_process,
    _is_infrastructure_process,
    _list_descendant_processes,
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
        assert _is_infrastructure_process("caffeinate") is True

    def test_preserves_real_child_processes(self):
        assert _is_infrastructure_process("python") is False
        assert _is_infrastructure_process("/usr/bin/node") is False


class TestProcessClassification:
    def test_detects_codex_cli_process(self):
        process = ProcessDescendant(
            pid=101,
            ppid=42,
            comm="codex",
            command="/usr/local/bin/codex --model gpt-5",
        )
        assert _is_cli_process(process) is True

    def test_detects_claude_node_cli_process(self):
        process = ProcessDescendant(
            pid=101,
            ppid=42,
            comm="node",
            command="/usr/local/bin/node /opt/claude-code/cli.js",
        )
        assert _is_cli_process(process) is True

    def test_single_baseline_codex_process_stays_idle(self):
        descendants = [
            ProcessDescendant(
                pid=101,
                ppid=42,
                comm="codex",
                command="/usr/local/bin/codex --model gpt-5",
            )
        ]
        assert _find_sub_agent_processes(descendants) == []

    def test_claude_baseline_plus_infrastructure_stays_idle(self):
        descendants = [
            ProcessDescendant(
                pid=101,
                ppid=42,
                comm="node",
                command="/usr/local/bin/node /opt/claude-code/cli.js",
            ),
            ProcessDescendant(
                pid=102,
                ppid=42,
                comm="docker",
                command="docker desktop",
            ),
            ProcessDescendant(
                pid=103,
                ppid=42,
                comm="caffeinate",
                command="caffeinate -dims",
            ),
        ]
        assert _find_sub_agent_processes(descendants) == []

    def test_extra_codex_teammate_process_promotes_sub_agent_waiting(self):
        teammate = ProcessDescendant(
            pid=102,
            ppid=101,
            comm="codex",
            command=(
                "/usr/local/bin/codex --agent-id teammate-1 "
                "--parent-session-id bell-wf"
            ),
        )
        descendants = [
            ProcessDescendant(
                pid=101,
                ppid=42,
                comm="codex",
                command="/usr/local/bin/codex --model gpt-5",
            ),
            teammate,
        ]
        assert _find_sub_agent_processes(descendants) == [teammate]

    def test_extra_claude_teammate_process_promotes_sub_agent_waiting(self):
        teammate = ProcessDescendant(
            pid=102,
            ppid=101,
            comm="node",
            command=(
                "/usr/local/bin/node /opt/claude-code/cli.js "
                "--agent-id teammate-1 --parent-session-id bell-wf"
            ),
        )
        descendants = [
            ProcessDescendant(
                pid=101,
                ppid=42,
                comm="node",
                command="/usr/local/bin/node /opt/claude-code/cli.js",
            ),
            teammate,
        ]
        assert _find_sub_agent_processes(descendants) == [teammate]

    def test_multiple_cli_processes_without_teammate_markers_bias_idle(self):
        descendants = [
            ProcessDescendant(
                pid=101,
                ppid=42,
                comm="codex",
                command="/usr/local/bin/codex --model gpt-5",
            ),
            ProcessDescendant(
                pid=102,
                ppid=101,
                comm="codex",
                command="/usr/local/bin/codex --model gpt-5-mini",
            ),
        ]
        assert _find_sub_agent_processes(descendants) == []


class TestListDescendantProcesses:
    @pytest.mark.asyncio
    async def test_none_parent_pid_is_empty(self):
        assert await _list_descendant_processes(None) == []

    @pytest.mark.asyncio
    async def test_collects_recursive_descendants(self):
        procs = [
            _Proc(returncode=0, stdout=b"101\n"),
            _Proc(returncode=0, stdout=b"102\n"),
            _Proc(returncode=1, stdout=b""),
            _Proc(
                returncode=0,
                stdout=(
                    b"101 42 codex /usr/local/bin/codex --model gpt-5\n"
                    b"102 101 codex /usr/local/bin/codex --agent-id teammate-1\n"
                ),
            ),
        ]

        async def mock_exec(*args, **kwargs):
            return procs.pop(0)

        with patch(f"{_OBS}.asyncio.create_subprocess_exec", side_effect=mock_exec):
            descendants = await _list_descendant_processes(42)

        assert descendants == [
            ProcessDescendant(
                pid=101,
                ppid=42,
                comm="codex",
                command="/usr/local/bin/codex --model gpt-5",
            ),
            ProcessDescendant(
                pid=102,
                ppid=101,
                comm="codex",
                command="/usr/local/bin/codex --agent-id teammate-1",
            ),
        ]


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
    def test_promotes_sub_agent_process_to_sub_agent_waiting(self):
        snapshot = snapshot_from_observation(
            SessionObservation(
                session="agent-backbone",
                online=True,
                has_sub_agent_processes=True,
            ),
            timestamp=123.0,
        )

        assert snapshot.state == AgentState.SUB_AGENT_WAITING

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
                has_sub_agent_processes=False,
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
