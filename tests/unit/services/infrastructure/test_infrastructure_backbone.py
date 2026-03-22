"""Tests for backbone process orchestration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_backbone.config import BackboneConfig, GatewayConfig


@pytest.fixture
def bb_config():
    return BackboneConfig(gateway=GatewayConfig(port=7120))


class TestStartGateway:
    @pytest.mark.asyncio
    async def test_creates_session_with_uvicorn(self, bb_config):
        """start_gateway creates tmux session with uvicorn --reload on configured port."""
        with patch(
            "agent_backbone.services.infrastructure._backbone.pid_for_port",
            new_callable=AsyncMock,
            return_value=None,
        ):
            with patch(
                "agent_backbone.services.infrastructure._backbone.session_exists",
                new_callable=AsyncMock,
                return_value=False,
            ):
                with patch(
                    "agent_backbone.services.infrastructure._backbone.stop_by_pid",
                    new_callable=AsyncMock,
                ):
                    with patch(
                        "agent_backbone.services.infrastructure._backbone.kill_port_process",
                        new_callable=AsyncMock,
                    ):
                        with patch(
                            "agent_backbone.services.infrastructure._backbone.start_session",
                            new_callable=AsyncMock,
                            return_value=True,
                        ) as mock_start:
                            with patch(
                                "agent_backbone.services.infrastructure._backbone.record_tmux_pid",
                                new_callable=AsyncMock,
                            ):
                                from agent_backbone.services.infrastructure._backbone import (
                                    start_gateway,
                                )

                                result = await start_gateway(bb_config)
        assert result is True
        call_args = mock_start.call_args
        cmd = call_args[1]["command"]
        assert "uvicorn" in cmd
        assert "--reload" in cmd
        assert "7120" in cmd

    @pytest.mark.asyncio
    async def test_skips_if_already_running(self, bb_config):
        """start_gateway returns True if port is bound (pid exists)."""
        with patch(
            "agent_backbone.services.infrastructure._backbone.pid_for_port",
            new_callable=AsyncMock,
            return_value=54321,
        ):
            from agent_backbone.services.infrastructure._backbone import start_gateway

            result = await start_gateway(bb_config)
        assert result is True

    @pytest.mark.asyncio
    async def test_cleans_stale_session(self, bb_config):
        """start_gateway cleans up stale session when port not bound but session exists."""
        with patch(
            "agent_backbone.services.infrastructure._backbone.pid_for_port",
            new_callable=AsyncMock,
            return_value=None,
        ):
            with patch(
                "agent_backbone.services.infrastructure._backbone.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ):
                with patch(
                    "agent_backbone.services.infrastructure._backbone.stop_session",
                    new_callable=AsyncMock,
                    return_value=True,
                ) as mock_stop:
                    with patch(
                        "agent_backbone.services.infrastructure._backbone.stop_by_pid",
                        new_callable=AsyncMock,
                    ):
                        with patch(
                            "agent_backbone.services.infrastructure._backbone.kill_port_process",
                            new_callable=AsyncMock,
                        ):
                            with patch(
                                "agent_backbone.services.infrastructure._backbone.start_session",
                                new_callable=AsyncMock,
                                return_value=True,
                            ):
                                with patch(
                                    "agent_backbone.services.infrastructure._backbone.record_tmux_pid",
                                    new_callable=AsyncMock,
                                ):
                                    from agent_backbone.services.infrastructure._backbone import (
                                        start_gateway,
                                    )

                                    result = await start_gateway(bb_config)
        assert result is True
        mock_stop.assert_called_once_with("gateway")


class TestStopGateway:
    @pytest.mark.asyncio
    async def test_stops_session_pid_and_port(self, bb_config):
        """stop_gateway cleans up session, PID file, and port."""
        with patch(
            "agent_backbone.services.infrastructure._backbone.stop_session",
            new_callable=AsyncMock,
        ) as mock_stop:
            with patch(
                "agent_backbone.services.infrastructure._backbone.stop_by_pid",
                new_callable=AsyncMock,
            ):
                with patch(
                    "agent_backbone.services.infrastructure._backbone.kill_port_process",
                    new_callable=AsyncMock,
                ):
                    with patch("agent_backbone.services.infrastructure._backbone.remove_pid"):
                        from agent_backbone.services.infrastructure._backbone import (
                            stop_gateway,
                        )

                        result = await stop_gateway(bb_config)
        assert result is True
        mock_stop.assert_called_once_with("gateway")


class TestStartTelegram:
    @pytest.mark.asyncio
    async def test_creates_session(self, bb_config):
        """start_telegram starts telegram-bot session."""
        with patch(
            "agent_backbone.services.infrastructure._backbone.read_pid",
            return_value=None,
        ):
            with patch(
                "agent_backbone.services.infrastructure._backbone.session_exists",
                new_callable=AsyncMock,
                return_value=False,
            ):
                with patch(
                    "agent_backbone.services.infrastructure._backbone.stop_by_pid",
                    new_callable=AsyncMock,
                ):
                    with patch(
                        "agent_backbone.services.infrastructure._backbone.start_session",
                        new_callable=AsyncMock,
                        return_value=True,
                    ) as mock_start:
                        with patch(
                            "agent_backbone.services.infrastructure._backbone.record_tmux_pid",
                            new_callable=AsyncMock,
                        ):
                            from agent_backbone.services.infrastructure._backbone import (
                                start_telegram,
                            )

                            result = await start_telegram(bb_config)
        assert result is True
        assert mock_start.call_args[0][0] == "telegram-bot"

    @pytest.mark.asyncio
    async def test_skips_if_already_running(self, bb_config):
        """start_telegram returns True if PID exists and session is alive."""
        with patch(
            "agent_backbone.services.infrastructure._backbone.read_pid",
            return_value=88888,
        ):
            with patch(
                "agent_backbone.services.infrastructure._backbone.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ):
                from agent_backbone.services.infrastructure._backbone import (
                    start_telegram,
                )

                result = await start_telegram(bb_config)
        assert result is True

    @pytest.mark.asyncio
    async def test_cleans_stale_session(self, bb_config):
        """start_telegram cleans up stale session when PID is dead but session exists."""
        with patch(
            "agent_backbone.services.infrastructure._backbone.read_pid",
            return_value=None,
        ):
            with patch(
                "agent_backbone.services.infrastructure._backbone.session_exists",
                new_callable=AsyncMock,
                return_value=True,
            ):
                with patch(
                    "agent_backbone.services.infrastructure._backbone.stop_session",
                    new_callable=AsyncMock,
                    return_value=True,
                ) as mock_stop:
                    with patch(
                        "agent_backbone.services.infrastructure._backbone.stop_by_pid",
                        new_callable=AsyncMock,
                    ):
                        with patch(
                            "agent_backbone.services.infrastructure._backbone.start_session",
                            new_callable=AsyncMock,
                            return_value=True,
                        ):
                            with patch(
                                "agent_backbone.services.infrastructure._backbone.record_tmux_pid",
                                new_callable=AsyncMock,
                            ):
                                from agent_backbone.services.infrastructure._backbone import (
                                    start_telegram,
                                )

                                result = await start_telegram(bb_config)
        assert result is True
        mock_stop.assert_called_once_with("telegram-bot")


class TestStartBackbone:
    @pytest.mark.asyncio
    async def test_correct_start_order(self, bb_config):
        """Starts in order: Gateway -> Telegram."""
        order = []

        async def mock_start_gateway(config):
            order.append("gateway")
            return True

        async def mock_start_telegram(config):
            order.append("telegram")
            return True

        with patch(
            "agent_backbone.services.infrastructure._backbone.start_gateway",
            side_effect=mock_start_gateway,
        ):
            with patch(
                "agent_backbone.services.infrastructure._backbone.start_telegram",
                side_effect=mock_start_telegram,
            ):
                from agent_backbone.services.infrastructure._backbone import (
                    start_backbone,
                )

                result = await start_backbone(bb_config)

        assert result is True
        assert order == ["gateway", "telegram"]

    @pytest.mark.asyncio
    async def test_fails_when_gateway_fails(self, bb_config):
        """start_backbone returns False when start_gateway fails."""
        with patch(
            "agent_backbone.services.infrastructure._backbone.start_gateway",
            new_callable=AsyncMock,
            return_value=False,
        ):
            with patch(
                "agent_backbone.services.infrastructure._backbone.start_telegram",
                new_callable=AsyncMock,
                return_value=True,
            ):
                from agent_backbone.services.infrastructure._backbone import (
                    start_backbone,
                )

                result = await start_backbone(bb_config)
        assert result is False


class TestStopBackbone:
    @pytest.mark.asyncio
    async def test_reverse_stop_order(self, bb_config):
        """stop_backbone stops services in reverse order: Telegram -> Gateway."""
        order = []

        async def mock_stop_telegram(config):
            order.append("telegram")
            return True

        async def mock_stop_gateway(config):
            order.append("gateway")
            return True

        with patch(
            "agent_backbone.services.infrastructure._backbone.stop_telegram",
            side_effect=mock_stop_telegram,
        ):
            with patch(
                "agent_backbone.services.infrastructure._backbone.stop_gateway",
                side_effect=mock_stop_gateway,
            ):
                from agent_backbone.services.infrastructure._backbone import (
                    stop_backbone,
                )

                await stop_backbone(bb_config)

        assert order == ["telegram", "gateway"]


class TestRestartBackbone:
    @pytest.mark.asyncio
    async def test_fails_if_port_occupied(self, bb_config):
        """restart_backbone returns False when gateway port is still occupied."""
        with patch(
            "agent_backbone.services.infrastructure._backbone.stop_backbone",
            new_callable=AsyncMock,
            return_value=True,
        ):
            with patch(
                "agent_backbone.services.infrastructure._backbone.check_port_free",
                new_callable=AsyncMock,
                return_value=False,
            ):
                from agent_backbone.services.infrastructure._backbone import (
                    restart_backbone,
                )

                result = await restart_backbone(bb_config)
        assert result is False

    @pytest.mark.asyncio
    async def test_restarts_when_ports_free(self, bb_config):
        """restart_backbone calls start_backbone when port is free."""
        with patch(
            "agent_backbone.services.infrastructure._backbone.stop_backbone",
            new_callable=AsyncMock,
            return_value=True,
        ):
            with patch(
                "agent_backbone.services.infrastructure._backbone.check_port_free",
                new_callable=AsyncMock,
                return_value=True,
            ):
                with patch(
                    "agent_backbone.services.infrastructure._backbone.start_backbone",
                    new_callable=AsyncMock,
                    return_value=True,
                ) as mock_start:
                    from agent_backbone.services.infrastructure._backbone import (
                        restart_backbone,
                    )

                    result = await restart_backbone(bb_config)
        assert result is True
        mock_start.assert_called_once_with(bb_config)


class TestWaitForHealth:
    @pytest.mark.asyncio
    async def test_succeeds_on_healthy_response(self):
        """wait_for_health returns True on 2xx response."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.is_success = True

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            from agent_backbone.services.infrastructure._backbone import (
                wait_for_health,
            )

            result = await wait_for_health(
                "http://localhost:7120/health", retries=1
            )
        assert result is True

    @pytest.mark.asyncio
    async def test_retries_on_failure(self):
        """wait_for_health retries and returns False after exhausting retries."""
        import httpx

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.side_effect = httpx.ConnectError("refused")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with patch("asyncio.sleep", new_callable=AsyncMock):
                from agent_backbone.services.infrastructure._backbone import (
                    wait_for_health,
                )

                result = await wait_for_health(
                    "http://localhost:7120/health",
                    retries=2,
                    interval=0.01,
                )
        assert result is False

    @pytest.mark.asyncio
    async def test_uses_default_retry_interval(self):
        """wait_for_health uses the default retry interval when none is provided."""
        import httpx

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.side_effect = httpx.ConnectError("refused")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                from agent_backbone.services.infrastructure._backbone import (
                    wait_for_health,
                )

                result = await wait_for_health(
                    "http://localhost:7120/health", retries=2
                )
        assert result is False
        mock_sleep.assert_awaited_once_with(0.4)

    @pytest.mark.asyncio
    async def test_returns_false_on_500(self):
        """wait_for_health retries on HTTP 500 responses."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.is_success = False

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with patch("asyncio.sleep", new_callable=AsyncMock):
                from agent_backbone.services.infrastructure._backbone import (
                    wait_for_health,
                )

                result = await wait_for_health(
                    "http://localhost:7120/health",
                    retries=1,
                    interval=0.01,
                )
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_401(self):
        """wait_for_health rejects 401 -- not a healthy endpoint."""
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.is_success = False

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with patch("asyncio.sleep", new_callable=AsyncMock):
                from agent_backbone.services.infrastructure._backbone import (
                    wait_for_health,
                )

                result = await wait_for_health(
                    "http://localhost:7120/health",
                    retries=1,
                    interval=0.01,
                )
        assert result is False


class TestRestartGateway:
    @pytest.mark.asyncio
    async def test_stops_then_starts(self, bb_config):
        """restart_gateway calls stop_gateway then start_gateway."""
        with patch(
            "agent_backbone.services.infrastructure._backbone.stop_gateway",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_stop:
            with patch(
                "agent_backbone.services.infrastructure._backbone.start_gateway",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_start:
                from agent_backbone.services.infrastructure._backbone import (
                    restart_gateway,
                )

                result = await restart_gateway(bb_config)
        assert result is True
        mock_stop.assert_called_once_with(bb_config)
        mock_start.assert_called_once_with(bb_config)
