"""Tests for infrastructure tunnel management."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_backbone.config import BackboneConfig, GatewayConfig


@pytest.fixture
def tunnel_config():
    return BackboneConfig(gateway=GatewayConfig(port=7120))


class TestStartTunnel:
    @pytest.mark.asyncio
    async def test_skips_if_running(self, tunnel_config):
        """start_tunnel returns True when ngrok session already exists."""
        with patch(
            "agent_backbone.services.infrastructure._tunnel.session_exists",
            new_callable=AsyncMock,
            return_value=True,
        ):
            with patch(
                "agent_backbone.services.infrastructure._tunnel.get_tunnel_url",
                new_callable=AsyncMock,
                return_value="https://abc.ngrok.io",
            ):
                from agent_backbone.services.infrastructure._tunnel import (
                    start_tunnel,
                )

                result = await start_tunnel(tunnel_config)
        assert result is True

    @pytest.mark.asyncio
    async def test_creates_session_with_ngrok_command(self, tunnel_config):
        """start_tunnel creates tmux session with 'ngrok http <port>'."""
        with patch(
            "agent_backbone.services.infrastructure._tunnel.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            with patch(
                "agent_backbone.services.infrastructure._tunnel.start_session",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_start:
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    with patch(
                        "agent_backbone.services.infrastructure._tunnel.get_tunnel_url",
                        new_callable=AsyncMock,
                        return_value="https://abc.ngrok.io",
                    ):
                        from agent_backbone.services.infrastructure._tunnel import (
                            start_tunnel,
                        )

                        result = await start_tunnel(tunnel_config)
        assert result is True
        call_args = mock_start.call_args
        assert call_args[0][0] == "ngrok"
        assert call_args[1]["command"] == ["ngrok", "http", "7120"]

    @pytest.mark.asyncio
    async def test_returns_false_on_session_failure(self, tunnel_config):
        """start_tunnel returns False when start_session fails."""
        with patch(
            "agent_backbone.services.infrastructure._tunnel.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            with patch(
                "agent_backbone.services.infrastructure._tunnel.start_session",
                new_callable=AsyncMock,
                return_value=False,
            ):
                from agent_backbone.services.infrastructure._tunnel import (
                    start_tunnel,
                )

                result = await start_tunnel(tunnel_config)
        assert result is False


class TestGetTunnelUrl:
    @pytest.mark.asyncio
    async def test_parses_ngrok_api(self):
        """get_tunnel_url parses public_url from ngrok API response."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"tunnels": [{"public_url": "https://abc123.ngrok.io"}]}

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            from agent_backbone.services.infrastructure._tunnel import get_tunnel_url

            url = await get_tunnel_url()
        assert url == "https://abc123.ngrok.io"

    @pytest.mark.asyncio
    async def test_returns_none_on_connect_error(self):
        """get_tunnel_url returns None when ngrok API is unreachable."""
        import httpx

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get.side_effect = httpx.ConnectError("refused")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            from agent_backbone.services.infrastructure._tunnel import get_tunnel_url

            url = await get_tunnel_url()
        assert url is None

    @pytest.mark.asyncio
    async def test_returns_none_on_empty_tunnels(self):
        """get_tunnel_url returns None when API returns empty tunnels list."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"tunnels": []}

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            from agent_backbone.services.infrastructure._tunnel import get_tunnel_url

            url = await get_tunnel_url()
        assert url is None

    @pytest.mark.asyncio
    async def test_returns_none_on_non_200(self):
        """get_tunnel_url returns None on non-200 status code."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            from agent_backbone.services.infrastructure._tunnel import get_tunnel_url

            url = await get_tunnel_url()
        assert url is None


class TestStopTunnel:
    @pytest.mark.asyncio
    async def test_stops_session(self):
        """stop_tunnel kills the ngrok tmux session and API listener."""
        with patch(
            "agent_backbone.services.infrastructure._tunnel.stop_session",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_stop:
            with patch(
                "agent_backbone.services.infrastructure._tunnel.kill_port_process",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_kill_port:
                from agent_backbone.services.infrastructure._tunnel import stop_tunnel

                result = await stop_tunnel()
        assert result is True
        mock_stop.assert_called_once_with("ngrok")
        mock_kill_port.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_false_when_stop_fails(self):
        """stop_tunnel returns False when session shutdown fails."""
        with patch(
            "agent_backbone.services.infrastructure._tunnel.stop_session",
            new_callable=AsyncMock,
            return_value=False,
        ):
            with patch(
                "agent_backbone.services.infrastructure._tunnel.kill_port_process",
                new_callable=AsyncMock,
                return_value=True,
            ):
                from agent_backbone.services.infrastructure._tunnel import stop_tunnel

                result = await stop_tunnel()
        assert result is False
