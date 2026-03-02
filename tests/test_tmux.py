"""Tests for agent_backbone/services/tmux."""

from __future__ import annotations

import signal
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.services.registry import EntityEntry, EntityRegistry, RepoInfo
from agent_backbone.services.tmux import (
    close_pane,
    close_window,
    create_layout,
    create_window,
    graceful_close,
    list_panes,
    list_sessions,
    list_windows,
    query_format_vars,
    rename_window,
    resize_pane,
    resolve_agent_dir,
    select_window,
    send_keys,
    send_message,
    session_exists,
    set_layout,
    split_pane,
    start_pipe_pane,
    start_session,
    stop_pipe_pane,
    swap_panes,
)


@pytest.fixture
def mock_subprocess():
    """Mock asyncio.create_subprocess_exec across all tmux private modules."""
    mock = AsyncMock()
    with (
        patch("agent_backbone.services.tmux._core.asyncio.create_subprocess_exec", mock),
        patch("agent_backbone.services.tmux._sessions.asyncio.create_subprocess_exec", mock),
        patch("agent_backbone.services.tmux._panes.asyncio.create_subprocess_exec", mock),
        patch("agent_backbone.services.tmux._windows.asyncio.create_subprocess_exec", mock),
        patch("agent_backbone.services.tmux._streaming.asyncio.create_subprocess_exec", mock),
    ):
        yield mock


class TestSessionExists:
    async def test_session_exists_true(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        assert await session_exists("ike") is True
        mock_subprocess.assert_called_once_with(
            "tmux",
            "has-session",
            "-t",
            "ike",
            stdout=-3,  # DEVNULL
            stderr=-1,  # PIPE
        )

    async def test_session_exists_false(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"no session"))
        mock_subprocess.return_value = proc

        assert await session_exists("nonexistent") is False


class TestSendMessage:
    async def test_send_success(self):
        with (
            patch(
                "agent_backbone.services.tmux._core.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.tmux._core.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
            ) as mock_exec,
        ):
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_exec.return_value = proc

            assert await send_message("ike", "hello") is True
            # Called twice: once for -l message, once for Enter
            assert mock_exec.call_count == 2

    async def test_send_session_offline(self):
        with patch(
            "agent_backbone.services.tmux._core.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            assert await send_message("offline", "hello") is False

    async def test_send_keys_failure(self):
        with (
            patch(
                "agent_backbone.services.tmux._core.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.tmux._core.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
            ) as mock_exec,
        ):
            proc = AsyncMock()
            proc.returncode = 1
            proc.communicate = AsyncMock(return_value=(b"", b"error"))
            mock_exec.return_value = proc

            assert await send_message("ike", "hello") is False


class TestListSessions:
    async def test_list_sessions(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"feynman\nike\nleo\n", b""))
        mock_subprocess.return_value = proc

        result = await list_sessions()
        assert result == ["feynman", "ike", "leo"]

    async def test_list_sessions_no_server(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await list_sessions()
        assert result == []


class TestSendKeys:
    async def test_send_keys_success(self):
        with (
            patch(
                "agent_backbone.services.tmux._core.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.tmux._core.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
            ) as mock_exec,
        ):
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_exec.return_value = proc

            assert await send_keys("ike", "Escape") is True
            # Only called once (no Enter, no -l flag)
            assert mock_exec.call_count == 1
            call_args = mock_exec.call_args[0]
            assert "-l" not in call_args
            assert "Enter" not in call_args
            assert "Escape" in call_args

    async def test_send_keys_session_offline(self):
        with patch(
            "agent_backbone.services.tmux._core.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            assert await send_keys("offline", "Escape") is False


class TestResolveAgentDir:
    def _make_registry(self, entities=None, repos=None):
        return EntityRegistry(
            entities=entities or {},
            repos=repos or [],
        )

    def test_named_entity_feynman(self):
        registry = self._make_registry(
            entities={
                "feynman": EntityEntry(
                    session="feynman",
                    home="~/orchestration",
                    groups=[],
                    figure="",
                    role="",
                ),
            }
        )
        result = resolve_agent_dir("feynman", registry)
        assert result.endswith("orchestration")

    def test_named_entity_ike(self):
        registry = self._make_registry(
            entities={
                "ike": EntityEntry(
                    session="ike", home="~/ws/core/ike", groups=[], figure="", role=""
                ),
            }
        )
        result = resolve_agent_dir("ike", registry)
        assert "ws/core/ike" in result

    def test_coding_repo_found(self, tmp_path):
        repo_dir = tmp_path / "my-repo"
        repo_dir.mkdir()
        registry = self._make_registry(
            repos=[
                RepoInfo(org="WF", name="my-repo", path=str(repo_dir)),
            ]
        )
        result = resolve_agent_dir("my-repo", registry)
        assert result == str(repo_dir)

    def test_unknown_returns_empty(self):
        registry = self._make_registry()
        result = resolve_agent_dir("nonexistent-xyz", registry)
        assert result == ""

    def test_no_registry_returns_empty(self):
        result = resolve_agent_dir("feynman")
        assert result == ""


class TestStartSession:
    async def test_start_with_working_dir_and_command(self, mock_subprocess):
        with patch(
            "agent_backbone.services.tmux._sessions.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.wait = AsyncMock()
            mock_subprocess.return_value = proc
            result = await start_session(
                "test",
                working_dir="/tmp/wd",
                command=["claude"],
                apply_theme=False,
            )
            assert result is True
            call_args = mock_subprocess.call_args_list[0][0]  # first call positional args
            assert "-c" in call_args
            assert "/tmp/wd" in call_args
            assert "claude" in call_args

    async def test_start_already_exists(self, mock_subprocess):
        with patch(
            "agent_backbone.services.tmux._sessions.session_exists",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await start_session("ike", apply_theme=False)
            assert result is True
            mock_subprocess.assert_not_called()

    async def test_start_without_working_dir(self, mock_subprocess):
        with patch(
            "agent_backbone.services.tmux._sessions.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.wait = AsyncMock()
            mock_subprocess.return_value = proc
            result = await start_session("test", apply_theme=False)
            assert result is True
            call_args = mock_subprocess.call_args_list[0][0]
            assert "-c" not in call_args

    async def test_start_with_multi_arg_command(self, mock_subprocess):
        """Multi-arg command list extends into subprocess args individually."""
        with patch(
            "agent_backbone.services.tmux._sessions.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.wait = AsyncMock()
            mock_subprocess.return_value = proc
            result = await start_session(
                "test",
                working_dir="/tmp/wd",
                command=["claude", "--model", "opus"],
                apply_theme=False,
            )
            assert result is True
            call_args = mock_subprocess.call_args_list[0][0]
            assert "claude" in call_args
            assert "--model" in call_args
            assert "opus" in call_args


class TestQueryFormatVars:
    async def test_returns_parsed_vars(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(
            return_value=(b"pane_in_mode=0\nclient_activity=1234567890\n", b"")
        )
        mock_subprocess.return_value = proc

        result = await query_format_vars("ike")
        assert result == {"pane_in_mode": "0", "client_activity": "1234567890"}

    async def test_returns_empty_on_failure(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await query_format_vars("nonexistent")
        assert result == {}

    async def test_custom_format_string(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"foo=bar\n", b""))
        mock_subprocess.return_value = proc

        result = await query_format_vars("ike", "foo=#{foo}")
        assert result == {"foo": "bar"}

    async def test_handles_empty_output(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await query_format_vars("ike")
        assert result == {}


class TestPipePane:
    async def test_start_pipe_pane_success(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await start_pipe_pane("ike", "/tmp/ike.log")
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "pipe-pane" in call_args
        assert "-t" in call_args
        assert "ike" in call_args
        assert "-o" in call_args
        assert "cat >> /tmp/ike.log" in call_args

    async def test_start_pipe_pane_failure(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_subprocess.return_value = proc

        result = await start_pipe_pane("ike", "/tmp/ike.log")
        assert result is False

    async def test_stop_pipe_pane_success(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await stop_pipe_pane("ike")
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "pipe-pane" in call_args
        assert "-t" in call_args
        assert "ike" in call_args
        assert "-o" not in call_args

    async def test_stop_pipe_pane_failure(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_subprocess.return_value = proc

        result = await stop_pipe_pane("ike")
        assert result is False


class TestListPanes:
    async def test_success(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"%1\t0\t120\t40\t1\n%2\t1\t120\t40\t0\n", b""))
        mock_subprocess.return_value = proc

        result = await list_panes("ike")
        assert len(result) == 2
        assert result[0]["pane_id"] == "%1"
        assert result[0]["pane_index"] == "0"
        assert result[0]["pane_width"] == "120"
        assert result[0]["pane_height"] == "40"
        assert result[0]["pane_active"] is True
        assert result[1]["pane_active"] is False

    async def test_empty_result(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await list_panes("ike")
        assert result == []

    async def test_failure(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await list_panes("nonexistent")
        assert result == []


class TestSplitPane:
    async def test_vertical(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await split_pane("ike", direction="vertical")
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "-v" in call_args
        assert "-h" not in call_args

    async def test_horizontal(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await split_pane("ike", direction="horizontal")
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "-h" in call_args
        assert "-v" not in call_args

    async def test_with_size(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await split_pane("ike", size="50%")
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "-l" in call_args
        assert "50%" in call_args

    async def test_failure(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_subprocess.return_value = proc

        result = await split_pane("ike")
        assert result is False


class TestResizePane:
    async def test_width(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await resize_pane("ike", "0", width=80)
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "-x" in call_args
        assert "80" in call_args

    async def test_height(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await resize_pane("ike", "0", height=24)
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "-y" in call_args
        assert "24" in call_args

    async def test_failure(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_subprocess.return_value = proc

        result = await resize_pane("ike", "0", width=80)
        assert result is False


class TestSwapPanes:
    async def test_success(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await swap_panes("ike", "0", "1")
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "-s" in call_args
        assert "ike:0" in call_args
        assert "-t" in call_args
        assert "ike:1" in call_args

    async def test_failure(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_subprocess.return_value = proc

        result = await swap_panes("ike", "0", "1")
        assert result is False


class TestClosePane:
    async def test_success(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await close_pane("ike", "1")
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "kill-pane" in call_args
        assert "ike:1" in call_args

    async def test_failure(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_subprocess.return_value = proc

        result = await close_pane("ike", "1")
        assert result is False


class TestSetLayout:
    async def test_tiled(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await set_layout("ike", "tiled")
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "select-layout" in call_args
        assert "tiled" in call_args

    async def test_even_horizontal(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await set_layout("ike", "even-horizontal")
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "even-horizontal" in call_args

    async def test_failure(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_subprocess.return_value = proc

        result = await set_layout("ike", "tiled")
        assert result is False


class TestCreateLayout:
    async def test_correct_split_count(self):
        """3 panes = 2 splits."""
        with (
            patch(
                "agent_backbone.services.tmux._windows.split_pane",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_split,
            patch(
                "agent_backbone.services.tmux._windows.set_layout",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.tmux._windows.list_panes",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "agent_backbone.services.tmux._windows.send_message",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await create_layout("ike", {"panes": 3, "layout": "tiled"})
            assert result is True
            assert mock_split.call_count == 2

    async def test_layout_applied(self):
        with (
            patch(
                "agent_backbone.services.tmux._windows.split_pane",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.tmux._windows.set_layout",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_layout,
            patch(
                "agent_backbone.services.tmux._windows.list_panes",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "agent_backbone.services.tmux._windows.send_message",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await create_layout("ike", {"panes": 2, "layout": "even-horizontal"})
            assert result is True
            mock_layout.assert_called_once_with("ike", "even-horizontal")

    async def test_split_failure_aborts(self):
        with (
            patch(
                "agent_backbone.services.tmux._windows.split_pane",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "agent_backbone.services.tmux._windows.set_layout",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_layout,
        ):
            result = await create_layout("ike", {"panes": 3, "layout": "tiled"})
            assert result is False
            mock_layout.assert_not_called()


class TestCreateWindow:
    async def test_minimal(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await create_window("ike")
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "new-window" in call_args
        assert "-n" not in call_args

    async def test_with_name(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await create_window("ike", name="logs")
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "-n" in call_args
        assert "logs" in call_args

    async def test_failure(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_subprocess.return_value = proc

        result = await create_window("ike")
        assert result is False


class TestCloseWindow:
    async def test_success(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await close_window("ike", "1")
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "kill-window" in call_args
        assert "ike:1" in call_args

    async def test_failure(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_subprocess.return_value = proc

        result = await close_window("ike", "1")
        assert result is False


class TestRenameWindow:
    async def test_success(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await rename_window("ike", "0", "new-name")
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "rename-window" in call_args
        assert "ike:0" in call_args
        assert "new-name" in call_args

    async def test_failure(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_subprocess.return_value = proc

        result = await rename_window("ike", "0", "new-name")
        assert result is False


class TestListWindows:
    async def test_success(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"@0\t0\tbash\t1\t1\n@1\t1\tlogs\t0\t2\n", b""))
        mock_subprocess.return_value = proc

        result = await list_windows("ike")
        assert len(result) == 2
        assert result[0]["window_id"] == "@0"
        assert result[0]["window_index"] == "0"
        assert result[0]["window_name"] == "bash"
        assert result[0]["window_active"] is True
        assert result[0]["window_panes"] == 1
        assert result[1]["window_active"] is False
        assert result[1]["window_panes"] == 2

    async def test_empty(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await list_windows("ike")
        assert result == []

    async def test_failure(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await list_windows("nonexistent")
        assert result == []


class TestSelectWindow:
    async def test_success(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subprocess.return_value = proc

        result = await select_window("ike", "1")
        assert result is True
        call_args = mock_subprocess.call_args[0]
        assert "select-window" in call_args
        assert "ike:1" in call_args

    async def test_failure(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_subprocess.return_value = proc

        result = await select_window("ike", "1")
        assert result is False


class TestStartSessionEnvironment:
    async def test_env_vars_set(self, mock_subprocess):
        """Environment variables are set via tmux set-environment."""
        with patch(
            "agent_backbone.services.tmux._sessions.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.wait = AsyncMock()
            mock_subprocess.return_value = proc

            result = await start_session(
                "test",
                apply_theme=False,
                environment={"MY_VAR": "hello", "OTHER": "world"},
            )
            assert result is True
            # First call: new-session, then 2 set-environment calls
            set_env_calls = [c for c in mock_subprocess.call_args_list if "set-environment" in c[0]]
            assert len(set_env_calls) == 2

    async def test_no_env_no_calls(self, mock_subprocess):
        """No environment param means no set-environment calls."""
        with patch(
            "agent_backbone.services.tmux._sessions.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.wait = AsyncMock()
            mock_subprocess.return_value = proc

            result = await start_session("test", apply_theme=False)
            assert result is True
            set_env_calls = [c for c in mock_subprocess.call_args_list if "set-environment" in c[0]]
            assert len(set_env_calls) == 0

    async def test_partial_failure_logs_warning(self, mock_subprocess):
        """If one env var fails, session still returns True."""
        with patch(
            "agent_backbone.services.tmux._sessions.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            # First call (new-session) succeeds, subsequent calls alternate
            success_proc = AsyncMock()
            success_proc.returncode = 0
            success_proc.communicate = AsyncMock(return_value=(b"", b""))
            success_proc.wait = AsyncMock()

            fail_proc = AsyncMock()
            fail_proc.returncode = 1
            fail_proc.communicate = AsyncMock(return_value=(b"", b"env error"))
            fail_proc.wait = AsyncMock()

            mock_subprocess.side_effect = [success_proc, fail_proc]

            result = await start_session(
                "test",
                apply_theme=False,
                environment={"BAD_VAR": "value"},
            )
            # Session creation succeeded, env var failure is non-fatal
            assert result is True


class TestGracefulClose:
    async def test_graceful_exit(self):
        """Process exits gracefully after SIGTERM."""
        with (
            patch(
                "agent_backbone.services.tmux._sessions.query_format_vars",
                new_callable=AsyncMock,
            ) as mock_qfv,
            patch(
                "agent_backbone.services.tmux._sessions.session_exists",
                new_callable=AsyncMock,
            ) as mock_exists,
            patch(
                "agent_backbone.services.tmux._sessions.stop_session",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_stop,
            patch("agent_backbone.services.tmux._sessions.os.kill") as mock_kill,
            patch(
                "agent_backbone.services.tmux._sessions.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            # First call: get pane_pid. Second call: pane_dead=1
            mock_qfv.side_effect = [
                {"pane_pid": "12345"},
                {"pane_dead": "1"},
            ]
            mock_exists.return_value = True

            result = await graceful_close("ike", timeout=5.0)
            assert result is True
            mock_kill.assert_called_once_with(12345, signal.SIGTERM)
            mock_stop.assert_called_once_with("ike")

    async def test_timeout_fallback(self):
        """Process doesn't exit within timeout, falls back to kill-session."""
        with (
            patch(
                "agent_backbone.services.tmux._sessions.query_format_vars",
                new_callable=AsyncMock,
            ) as mock_qfv,
            patch(
                "agent_backbone.services.tmux._sessions.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.tmux._sessions.stop_session",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_stop,
            patch("agent_backbone.services.tmux._sessions.os.kill"),
            patch(
                "agent_backbone.services.tmux._sessions.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            # pane_pid OK, then pane_dead always 0
            mock_qfv.side_effect = [
                {"pane_pid": "12345"},
                {"pane_dead": "0"},
                {"pane_dead": "0"},
            ]

            result = await graceful_close("ike", timeout=0.5)
            assert result is True
            # stop_session is the fallback
            mock_stop.assert_called()

    async def test_process_already_gone(self):
        """ProcessLookupError on kill means session cleanup."""
        with (
            patch(
                "agent_backbone.services.tmux._sessions.query_format_vars",
                new_callable=AsyncMock,
            ) as mock_qfv,
            patch(
                "agent_backbone.services.tmux._sessions.session_exists",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "agent_backbone.services.tmux._sessions.stop_session",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.tmux._sessions.os.kill",
                side_effect=ProcessLookupError,
            ),
        ):
            mock_qfv.return_value = {"pane_pid": "12345"}

            result = await graceful_close("ike")
            # Session doesn't exist either, so True
            assert result is True

    async def test_no_pid_fallback(self):
        """No pane_pid available means falls back to stop_session."""
        with (
            patch(
                "agent_backbone.services.tmux._sessions.query_format_vars",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "agent_backbone.services.tmux._sessions.stop_session",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_stop,
        ):
            result = await graceful_close("ike")
            assert result is True
            mock_stop.assert_called_once_with("ike")
