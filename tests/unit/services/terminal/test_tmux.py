"""Tests for agent_backbone/services/terminal."""

from __future__ import annotations

import signal
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.services.terminal import (
    graceful_close,
    list_sessions,
    query_format_vars,
    send_keys,
    session_exists,
    start_session,
)


@pytest.fixture
def mock_subprocess():
    """Mock the single tmux subprocess entry point (`_run_tmux`)."""
    mock = AsyncMock()
    with patch("agent_backbone.services.terminal._core.asyncio.create_subprocess_exec", mock):
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
            "=ike:",
            stdout=-3,  # DEVNULL
            stderr=-1,  # PIPE
        )

    async def test_session_exists_false(self, mock_subprocess):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"no session"))
        mock_subprocess.return_value = proc

        assert await session_exists("nonexistent") is False


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
                "agent_backbone.services.terminal._core.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.terminal._core.asyncio.create_subprocess_exec",
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
            "agent_backbone.services.terminal._core.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            assert await send_keys("offline", "Escape") is False


class TestStartSession:
    async def test_start_with_working_dir_and_command(self, mock_subprocess):
        with patch(
            "agent_backbone.services.terminal._sessions.session_exists",
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
            )
            assert result is True
            call_args = mock_subprocess.call_args_list[0][0]  # first call positional args
            assert "-c" in call_args
            assert "/tmp/wd" in call_args
            assert "claude" in call_args

    async def test_start_already_exists(self, mock_subprocess):
        with patch(
            "agent_backbone.services.terminal._sessions.session_exists",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await start_session("ike")
            assert result is True
            mock_subprocess.assert_not_called()

    async def test_start_without_working_dir(self, mock_subprocess):
        with patch(
            "agent_backbone.services.terminal._sessions.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.wait = AsyncMock()
            mock_subprocess.return_value = proc
            result = await start_session("test")
            assert result is True
            call_args = mock_subprocess.call_args_list[0][0]
            assert "-c" not in call_args

    async def test_start_with_multi_arg_command(self, mock_subprocess):
        """Multi-arg command list extends into subprocess args individually."""
        with patch(
            "agent_backbone.services.terminal._sessions.session_exists",
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


class TestStartSessionEnvironment:
    async def test_initial_command_receives_env_vars(self, mock_subprocess):
        """The initial tmux command inherits env vars when a command is supplied."""
        with patch(
            "agent_backbone.services.terminal._sessions.session_exists",
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
                command=["claude", "--model", "opus"],
                environment={"BACKBONE_AGENT": "app"},
            )

            assert result is True
            new_session_call = mock_subprocess.call_args_list[0][0]
            assert "new-session" in new_session_call
            assert "env" in new_session_call
            assert "BACKBONE_AGENT=app" in new_session_call
            assert "claude" in new_session_call

    async def test_env_vars_are_session_environment_from_the_start(self, mock_subprocess):
        """`new-session -e` sets the session environment atomically with the session."""
        with patch(
            "agent_backbone.services.terminal._sessions.session_exists",
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
                environment={"MY_VAR": "hello", "OTHER": "world"},
            )
            assert result is True
            new_session = mock_subprocess.call_args_list[0][0]
            e_flags = [new_session[i + 1] for i, a in enumerate(new_session) if a == "-e"]
            assert e_flags == ["MY_VAR=hello", "OTHER=world"]
            assert not [c for c in mock_subprocess.call_args_list if "set-environment" in c[0]]

    async def test_no_env_no_calls(self, mock_subprocess):
        """No environment param means no set-environment calls."""
        with patch(
            "agent_backbone.services.terminal._sessions.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.wait = AsyncMock()
            mock_subprocess.return_value = proc

            result = await start_session("test")
            assert result is True
            set_env_calls = [c for c in mock_subprocess.call_args_list if "set-environment" in c[0]]
            assert len(set_env_calls) == 0


class TestGracefulClose:
    async def test_graceful_exit(self):
        """Process exits gracefully after SIGTERM."""
        with (
            patch(
                "agent_backbone.services.terminal._sessions.query_format_vars",
                new_callable=AsyncMock,
            ) as mock_qfv,
            patch(
                "agent_backbone.services.terminal._sessions.session_exists",
                new_callable=AsyncMock,
            ) as mock_exists,
            patch(
                "agent_backbone.services.terminal._sessions.stop_session",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_stop,
            patch("agent_backbone.services.terminal._sessions.os.kill") as mock_kill,
            patch(
                "agent_backbone.services.terminal._sessions.asyncio.sleep",
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
                "agent_backbone.services.terminal._sessions.query_format_vars",
                new_callable=AsyncMock,
            ) as mock_qfv,
            patch(
                "agent_backbone.services.terminal._sessions.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.terminal._sessions.stop_session",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_stop,
            patch("agent_backbone.services.terminal._sessions.os.kill"),
            patch(
                "agent_backbone.services.terminal._sessions.asyncio.sleep",
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
                "agent_backbone.services.terminal._sessions.query_format_vars",
                new_callable=AsyncMock,
            ) as mock_qfv,
            patch(
                "agent_backbone.services.terminal._sessions.session_exists",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "agent_backbone.services.terminal._sessions.stop_session",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "agent_backbone.services.terminal._sessions.os.kill",
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
                "agent_backbone.services.terminal._sessions.query_format_vars",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "agent_backbone.services.terminal._sessions.stop_session",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_stop,
        ):
            result = await graceful_close("ike")
            assert result is True
            mock_stop.assert_called_once_with("ike")


class TestSessionSecretScrub:
    """Agent sessions must not inherit the backbone's secrets (issue #81)."""

    @staticmethod
    def _ok_proc():
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.wait = AsyncMock()
        return proc

    async def test_scrubbed_vars_are_unset_for_the_launched_process(self, mock_subprocess):
        with patch(
            "agent_backbone.services.terminal._sessions.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            mock_subprocess.return_value = self._ok_proc()
            result = await start_session(
                "test",
                command=["claude"],
                environment={"BACKBONE_AGENT": "app"},
                scrub=["BACKBONE_API_KEY", "GITHUB_TOKEN"],
            )
            assert result is True
            new_session = mock_subprocess.call_args_list[0][0]
            assert "-uBACKBONE_API_KEY" in new_session
            assert "-uGITHUB_TOKEN" in new_session
            # `env -u` comes before the assignments and the command itself.
            assert new_session.index("env") < new_session.index("-uGITHUB_TOKEN")
            assert new_session.index("-uGITHUB_TOKEN") < new_session.index("claude")
            # ...and the session environment shadows the server's from the start,
            # so a pane opened before `set-environment -r` sees nothing either.
            e_flags = [new_session[i + 1] for i, a in enumerate(new_session) if a == "-e"]
            assert e_flags == ["BACKBONE_AGENT=app", "BACKBONE_API_KEY=", "GITHUB_TOKEN="]
            assert new_session.index("-e") < new_session.index("env")

    async def test_scrubbed_vars_are_removed_from_the_session_environment(self, mock_subprocess):
        with patch(
            "agent_backbone.services.terminal._sessions.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            mock_subprocess.return_value = self._ok_proc()
            await start_session("test", command=["claude"], scrub=["GITHUB_TOKEN"])
            calls = [c[0] for c in mock_subprocess.call_args_list]
            removals = [c for c in calls if "set-environment" in c and "-r" in c]
            assert len(removals) == 1
            assert removals[0][-1] == "GITHUB_TOKEN"

    async def test_session_is_killed_when_the_scrub_fails(self, mock_subprocess):
        """A session that may still leak a secret is not handed back as started."""
        calls: list[tuple[str, ...]] = []

        async def _exec(*args, **kwargs):
            calls.append(args)
            proc = self._ok_proc()
            if "set-environment" in args and "-r" in args:
                proc.returncode = 1
                proc.communicate = AsyncMock(return_value=(b"", b"no such option"))
            return proc

        with patch(
            "agent_backbone.services.terminal._sessions.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            mock_subprocess.side_effect = _exec
            result = await start_session("test", command=["claude"], scrub=["GITHUB_TOKEN"])
        assert result is False
        assert any("kill-session" in c for c in calls)

    async def test_agent_env_wins_over_the_scrub(self, mock_subprocess):
        """An agent configured with its own GITHUB_TOKEN keeps it."""
        with patch(
            "agent_backbone.services.terminal._sessions.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            mock_subprocess.return_value = self._ok_proc()
            await start_session(
                "test",
                command=["claude"],
                environment={"GITHUB_TOKEN": "agent-own"},
                scrub=["GITHUB_TOKEN", "BACKBONE_API_KEY"],
            )
            new_session = mock_subprocess.call_args_list[0][0]
            assert "GITHUB_TOKEN=agent-own" in new_session
            assert "-uGITHUB_TOKEN" not in new_session
            assert "-uBACKBONE_API_KEY" in new_session
            # The agent's value is session environment from the first instant,
            # so a pane opened before the cleanup sees it, not the server's.
            e_flags = [new_session[i + 1] for i, a in enumerate(new_session) if a == "-e"]
            assert e_flags == ["GITHUB_TOKEN=agent-own", "BACKBONE_API_KEY="]

    async def test_shell_session_is_scrubbed_too(self, mock_subprocess):
        """A shell-runtime agent gets no command, but still no secrets."""
        with patch(
            "agent_backbone.services.terminal._sessions.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            mock_subprocess.return_value = self._ok_proc()
            await start_session(
                "test",
                environment={"BACKBONE_AGENT": "app"},
                scrub=["BACKBONE_API_KEY"],
            )
            new_session = mock_subprocess.call_args_list[0][0]
            assert "-uBACKBONE_API_KEY" in new_session
            assert "BACKBONE_AGENT=app" in new_session
            # A login shell, so the user's profile (and PATH) still applies.
            assert new_session[-1] == "-l"

    async def test_no_scrub_leaves_the_command_untouched(self, mock_subprocess):
        with patch(
            "agent_backbone.services.terminal._sessions.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            mock_subprocess.return_value = self._ok_proc()
            await start_session("test", command=["claude"])
            new_session = mock_subprocess.call_args_list[0][0]
            assert "env" not in new_session
            assert new_session[-1] == "claude"
