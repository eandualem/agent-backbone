"""Tests for backbone process orchestration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_backbone.config import BackboneConfig, GatewayConfig, SchedulingConfig


@pytest.fixture
def bb_config():
    return BackboneConfig(gateway=GatewayConfig(port=7120))


class TestStartPrefect:
    @pytest.mark.asyncio
    async def test_skips_if_already_running(self, bb_config):
        """start_prefect returns True immediately when session exists."""
        with patch(
            "agent_backbone.services.infrastructure._backbone.session_exists",
            new_callable=AsyncMock,
            return_value=True,
        ):
            from agent_backbone.services.infrastructure._backbone import start_prefect

            result = await start_prefect(bb_config)
        assert result is True

    @pytest.mark.asyncio
    async def test_fails_if_port_occupied(self, bb_config):
        """start_prefect returns False when port 4200 is in use."""
        with patch(
            "agent_backbone.services.infrastructure._backbone.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            with patch(
                "agent_backbone.services.infrastructure._backbone.stop_by_pid",
                new_callable=AsyncMock,
                return_value=True,
            ):
                with patch(
                    "agent_backbone.services.infrastructure._backbone.check_port_free",
                    new_callable=AsyncMock,
                    return_value=False,
                ):
                    from agent_backbone.services.infrastructure._backbone import (
                        start_prefect,
                    )

                    result = await start_prefect(bb_config)
        assert result is False

    @pytest.mark.asyncio
    async def test_creates_session_with_prefect_command(self, bb_config):
        """start_prefect creates tmux session with 'uv run prefect server start'."""
        with patch(
            "agent_backbone.services.infrastructure._backbone.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            with patch(
                "agent_backbone.services.infrastructure._backbone.stop_by_pid",
                new_callable=AsyncMock,
                return_value=True,
            ):
                with patch(
                    "agent_backbone.services.infrastructure._backbone.check_port_free",
                    new_callable=AsyncMock,
                    return_value=True,
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
                                start_prefect,
                            )

                            result = await start_prefect(bb_config)
        assert result is True
        mock_start.assert_called_once()
        call_args = mock_start.call_args
        assert call_args[0][0] == "prefect"
        assert call_args[1]["command"] == [
            "uv",
            "run",
            "prefect",
            "server",
            "start",
        ]


class TestStopPrefect:
    @pytest.mark.asyncio
    async def test_stops_all_components(self, bb_config):
        """stop_prefect calls stop_session, stop_by_pid, kill_port_process, remove_pid."""
        with patch(
            "agent_backbone.services.infrastructure._backbone.stop_session",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_stop_session:
            with patch(
                "agent_backbone.services.infrastructure._backbone.stop_by_pid",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_stop_pid:
                with patch(
                    "agent_backbone.services.infrastructure._backbone.kill_port_process",
                    new_callable=AsyncMock,
                    return_value=True,
                ) as mock_kill:
                    with patch(
                        "agent_backbone.services.infrastructure._backbone.remove_pid"
                    ) as mock_remove:
                        from agent_backbone.services.infrastructure._backbone import (
                            stop_prefect,
                        )

                        result = await stop_prefect(bb_config)
        assert result is True
        mock_stop_session.assert_called_once_with("prefect")
        mock_stop_pid.assert_called_once_with("prefect")
        mock_kill.assert_called_once_with(4200)
        mock_remove.assert_called_once_with("prefect")


class TestStartGateway:
    @pytest.mark.asyncio
    async def test_creates_session_with_uvicorn(self, bb_config):
        """start_gateway creates tmux session with uvicorn --reload on configured port."""
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
        """start_gateway returns True if session already exists."""
        with patch(
            "agent_backbone.services.infrastructure._backbone.session_exists",
            new_callable=AsyncMock,
            return_value=True,
        ):
            from agent_backbone.services.infrastructure._backbone import start_gateway

            result = await start_gateway(bb_config)
        assert result is True


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


class TestStartWorker:
    @pytest.mark.asyncio
    async def test_creates_pool_deploys_and_starts(self, bb_config):
        """start_worker creates work-pool, deploys, and starts worker session."""
        mock_subprocess = AsyncMock()
        mock_subprocess.wait.return_value = 0
        bb_config = BackboneConfig(
            gateway=GatewayConfig(port=7120),
            scheduling=SchedulingConfig(work_pool_name="custom-pool"),
        )

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
                    "asyncio.create_subprocess_exec", return_value=mock_subprocess
                ) as mock_create_subprocess:
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
                                PREFECT_API_URL,
                                start_worker,
                            )

                            result = await start_worker(bb_config)
        assert result is True
        call_args = mock_start.call_args
        assert call_args[0][0] == "backbone-worker"
        assert "custom-pool" in call_args[1]["command"]
        assert call_args[1]["environment"] == {"PREFECT_API_URL": PREFECT_API_URL}
        pool_create_call = mock_create_subprocess.call_args_list[0]
        assert pool_create_call.args[5] == "custom-pool"
        assert pool_create_call.kwargs["env"]["PREFECT_API_URL"] == PREFECT_API_URL

    @pytest.mark.asyncio
    async def test_skips_if_already_running(self, bb_config):
        """start_worker returns True if session already exists."""
        with patch(
            "agent_backbone.services.infrastructure._backbone.session_exists",
            new_callable=AsyncMock,
            return_value=True,
        ):
            from agent_backbone.services.infrastructure._backbone import start_worker

            result = await start_worker(bb_config)
        assert result is True


class TestStartTelegram:
    @pytest.mark.asyncio
    async def test_creates_session(self, bb_config):
        """start_telegram starts telegram-bot session."""
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


class TestStartBackbone:
    @pytest.mark.asyncio
    async def test_correct_start_order(self, bb_config):
        """Starts in order: Prefect -> Gateway -> Worker -> Telegram."""
        order = []

        async def mock_start_prefect(config):
            order.append("prefect")
            return True

        async def mock_start_gateway(config):
            order.append("gateway")
            return True

        async def mock_start_worker(config):
            order.append("worker")
            return True

        async def mock_start_telegram(config):
            order.append("telegram")
            return True

        with patch(
            "agent_backbone.services.infrastructure._backbone.start_prefect",
            side_effect=mock_start_prefect,
        ):
            with patch(
                "agent_backbone.services.infrastructure._backbone.wait_for_health",
                new_callable=AsyncMock,
                return_value=True,
            ):
                with patch(
                    "agent_backbone.services.infrastructure._backbone.start_gateway",
                    side_effect=mock_start_gateway,
                ):
                    with patch(
                        "agent_backbone.services.infrastructure._backbone.start_worker",
                        side_effect=mock_start_worker,
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
        assert order == ["prefect", "gateway", "worker", "telegram"]

    @pytest.mark.asyncio
    async def test_fails_when_prefect_fails(self, bb_config):
        """start_backbone returns False immediately when start_prefect fails."""
        with patch(
            "agent_backbone.services.infrastructure._backbone.start_prefect",
            new_callable=AsyncMock,
            return_value=False,
        ):
            from agent_backbone.services.infrastructure._backbone import start_backbone

            result = await start_backbone(bb_config)
        assert result is False

    @pytest.mark.asyncio
    async def test_uses_short_probe_retry_interval(self, bb_config):
        """start_backbone probes Prefect health with a 100ms retry interval."""
        with patch(
            "agent_backbone.services.infrastructure._backbone.start_prefect",
            new_callable=AsyncMock,
            return_value=True,
        ):
            with patch(
                "agent_backbone.services.infrastructure._backbone.wait_for_health",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_wait:
                with patch(
                    "agent_backbone.services.infrastructure._backbone.start_gateway",
                    new_callable=AsyncMock,
                    return_value=True,
                ):
                    with patch(
                        "agent_backbone.services.infrastructure._backbone.start_worker",
                        new_callable=AsyncMock,
                        return_value=True,
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
        assert result is True
        mock_wait.assert_awaited_once_with(
            "http://127.0.0.1:4200/api/health",
            interval=0.1,
        )

    @pytest.mark.asyncio
    async def test_continues_when_health_fails(self, bb_config):
        """start_backbone continues even when health check fails (logs warning)."""
        with patch(
            "agent_backbone.services.infrastructure._backbone.start_prefect",
            new_callable=AsyncMock,
            return_value=True,
        ):
            with patch(
                "agent_backbone.services.infrastructure._backbone.wait_for_health",
                new_callable=AsyncMock,
                return_value=False,
            ):
                with patch(
                    "agent_backbone.services.infrastructure._backbone.start_gateway",
                    new_callable=AsyncMock,
                    return_value=True,
                ):
                    with patch(
                        "agent_backbone.services.infrastructure._backbone.start_worker",
                        new_callable=AsyncMock,
                        return_value=True,
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
        assert result is True


class TestStopBackbone:
    @pytest.mark.asyncio
    async def test_reverse_stop_order(self, bb_config):
        """stop_backbone stops services in reverse order."""
        order = []

        async def mock_stop_telegram(config):
            order.append("telegram")
            return True

        async def mock_stop_gateway(config):
            order.append("gateway")
            return True

        async def mock_stop_worker(config):
            order.append("worker")
            return True

        async def mock_stop_prefect(config):
            order.append("prefect")
            return True

        with patch(
            "agent_backbone.services.infrastructure._backbone.stop_telegram",
            side_effect=mock_stop_telegram,
        ):
            with patch(
                "agent_backbone.services.infrastructure._backbone.stop_gateway",
                side_effect=mock_stop_gateway,
            ):
                with patch(
                    "agent_backbone.services.infrastructure._backbone.stop_worker",
                    side_effect=mock_stop_worker,
                ):
                    with patch(
                        "agent_backbone.services.infrastructure._backbone.stop_prefect",
                        side_effect=mock_stop_prefect,
                    ):
                        from agent_backbone.services.infrastructure._backbone import (
                            stop_backbone,
                        )

                        await stop_backbone(bb_config)

        assert order == ["telegram", "gateway", "worker", "prefect"]


class TestRestartBackbone:
    @pytest.mark.asyncio
    async def test_fails_if_prefect_port_occupied(self, bb_config):
        """restart_backbone returns False when Prefect port is still occupied after stop."""
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
    async def test_fails_if_gateway_port_occupied(self, bb_config):
        """restart_backbone returns False when gateway port is still occupied."""

        async def mock_port_free(port):
            # Prefect port (4200) is free, gateway port (7120) is not
            return port == 4200

        with patch(
            "agent_backbone.services.infrastructure._backbone.stop_backbone",
            new_callable=AsyncMock,
            return_value=True,
        ):
            with patch(
                "agent_backbone.services.infrastructure._backbone.check_port_free",
                new_callable=AsyncMock,
                side_effect=mock_port_free,
            ):
                from agent_backbone.services.infrastructure._backbone import (
                    restart_backbone,
                )

                result = await restart_backbone(bb_config)
        assert result is False

    @pytest.mark.asyncio
    async def test_restarts_when_ports_free(self, bb_config):
        """restart_backbone calls start_backbone when both ports are free."""
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

            result = await wait_for_health("http://localhost:4200/api/health", retries=1)
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
                    "http://localhost:4200/api/health",
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

                result = await wait_for_health("http://localhost:4200/api/health", retries=2)
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
                    "http://localhost:4200/api/health",
                    retries=1,
                    interval=0.01,
                )
        assert result is False


    @pytest.mark.asyncio
    async def test_returns_false_on_401(self):
        """wait_for_health rejects 401 — not a healthy endpoint (#782 finding)."""
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
                    "http://localhost:4200/api/health",
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
