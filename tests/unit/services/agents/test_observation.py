"""Tests for tmux/process observation helpers."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent_backbone.services.agents._observation import (
    _has_child_processes,
    _is_infrastructure_process,
)

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
