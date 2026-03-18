"""Tests for infrastructure agent group operations."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import BackboneConfig, EntityConfig
from agent_backbone.services.registry import EntityEntry, EntityRegistry, RepoInfo


@pytest.fixture
def agent_config(tmp_path):
    """Config with a test registry using real tmp directories."""
    registry = EntityRegistry(
        entities={
            "ike": EntityEntry(
                session="ike",
                home=str(tmp_path / "ike"),
                groups=["orchestrators"],
                figure="Ike",
                role="Orchestrator",
            ),
            "leo": EntityEntry(
                session="leo",
                home=str(tmp_path / "leo"),
                groups=["orchestrators"],
                figure="Leo",
                role="Strategy",
            ),
            "ada": EntityEntry(
                session="ada",
                home=str(tmp_path / "ada"),
                groups=["standalone"],
                figure="Ada",
                role="Spec",
            ),
        },
        repos=[
            RepoInfo(
                org="Arclio",
                name="platform-api",
                path=str(tmp_path / "platform-api"),
            ),
            RepoInfo(
                org="WF",
                name="agent-backbone",
                path=str(tmp_path / "agent-backbone"),
            ),
        ],
    )
    # Create directories so resolve_agent_dir + Path.is_dir() work
    for d in ["ike", "leo", "ada", "platform-api", "agent-backbone"]:
        (tmp_path / d).mkdir()

    return BackboneConfig(
        registry=registry,
        entities=EntityConfig(
            service_sessions=frozenset(
                {
                    "prefect",
                    "gateway",
                    "backbone-worker",
                    "telegram-bot",
                }
            ),
        ),
    )


class TestStartAgent:
    @pytest.mark.asyncio
    async def test_resolves_dir_and_starts(self, agent_config):
        """start_agent resolves working dir from registry and creates session."""
        with patch(
            "agent_backbone.services.infrastructure._agents.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            with patch(
                "agent_backbone.services.infrastructure._agents.start_session",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_start:
                from agent_backbone.services.infrastructure._agents import start_agent

                result = await start_agent("ike", agent_config)
        assert result is True
        call_args = mock_start.call_args
        assert call_args[0][0] == "ike"
        assert "ike" in call_args[1]["working_dir"]
        assert call_args[1]["command"] == ["claude"]
        assert call_args[1]["environment"] == {"BACKBONE_RUNTIME": "claude"}

    @pytest.mark.asyncio
    async def test_already_running(self, agent_config):
        """start_agent returns True when session already exists."""
        with patch(
            "agent_backbone.services.infrastructure._agents.session_exists",
            new_callable=AsyncMock,
            return_value=True,
        ):
            from agent_backbone.services.infrastructure._agents import start_agent

            result = await start_agent("ike", agent_config)
        assert result is True

    @pytest.mark.asyncio
    async def test_unknown_agent(self, agent_config):
        """start_agent returns False for agent not in registry."""
        with patch(
            "agent_backbone.services.infrastructure._agents.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            from agent_backbone.services.infrastructure._agents import start_agent

            result = await start_agent("nonexistent", agent_config)
        assert result is False

    @pytest.mark.asyncio
    async def test_custom_cli_and_model(self, agent_config):
        """start_agent passes cli and model to session command."""
        with patch(
            "agent_backbone.services.infrastructure._agents.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            with patch(
                "agent_backbone.services.infrastructure._agents.start_session",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_start:
                from agent_backbone.services.infrastructure._agents import start_agent

                await start_agent("ike", agent_config, cli="gemini", model="pro")
        cmd = mock_start.call_args[1]["command"]
        assert cmd == ["gemini", "--model", "pro"]
        assert mock_start.call_args[1]["environment"] == {"BACKBONE_RUNTIME": "gemini"}

    @pytest.mark.asyncio
    async def test_dir_not_exists(self, agent_config, tmp_path):
        """start_agent returns False when the resolved directory doesn't exist."""
        # Remove the ike directory so Path.is_dir() returns False
        import shutil

        shutil.rmtree(tmp_path / "ike")
        with patch(
            "agent_backbone.services.infrastructure._agents.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            from agent_backbone.services.infrastructure._agents import start_agent

            result = await start_agent("ike", agent_config)
        assert result is False


class TestStopAgent:
    @pytest.mark.asyncio
    async def test_calls_stop_session(self):
        """stop_agent delegates to stop_session."""
        with patch(
            "agent_backbone.services.infrastructure._agents.stop_session",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_stop:
            from agent_backbone.services.infrastructure._agents import stop_agent

            result = await stop_agent("ike")
        assert result is True
        mock_stop.assert_called_once_with("ike")


class TestStartGroup:
    @pytest.mark.asyncio
    async def test_starts_non_running_agents(self, agent_config):
        """start_group starts only agents that are not already running."""

        async def mock_exists(name):
            return name == "ike"  # ike already running

        with patch(
            "agent_backbone.services.infrastructure._agents.session_exists",
            new_callable=AsyncMock,
            side_effect=mock_exists,
        ):
            with patch(
                "agent_backbone.services.infrastructure._agents.start_session",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_start:
                from agent_backbone.services.infrastructure._agents import start_group

                count = await start_group("Test", ["ike", "leo"], agent_config)
        assert count == 1  # Only leo started
        assert mock_start.call_args[0][0] == "leo"
        assert mock_start.call_args[1]["environment"] == {"BACKBONE_RUNTIME": "claude"}

    @pytest.mark.asyncio
    async def test_skips_agents_with_missing_dirs(self, agent_config, tmp_path):
        """start_group skips agents whose directory does not exist."""
        import shutil

        shutil.rmtree(tmp_path / "leo")

        with patch(
            "agent_backbone.services.infrastructure._agents.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            with patch(
                "agent_backbone.services.infrastructure._agents.start_session",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_start:
                from agent_backbone.services.infrastructure._agents import start_group

                count = await start_group("Test", ["ike", "leo"], agent_config)
        # ike started (dir exists), leo skipped (dir removed)
        assert count == 1
        assert mock_start.call_args[0][0] == "ike"


class TestStopAllAgents:
    @pytest.mark.asyncio
    async def test_skips_service_sessions(self, agent_config):
        """stop_all_agents stops agent sessions but skips service sessions."""
        with patch(
            "agent_backbone.services.infrastructure._agents.list_sessions",
            new_callable=AsyncMock,
            return_value=["ike", "gateway", "prefect"],
        ):
            with patch(
                "agent_backbone.services.infrastructure._agents.stop_session",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_stop:
                from agent_backbone.services.infrastructure._agents import (
                    stop_all_agents,
                )

                count = await stop_all_agents(agent_config)
        assert count == 1  # Only ike stopped
        mock_stop.assert_called_once_with("ike")

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_agents(self, agent_config):
        """stop_all_agents returns 0 when no agent sessions are running."""
        with patch(
            "agent_backbone.services.infrastructure._agents.list_sessions",
            new_callable=AsyncMock,
            return_value=["gateway", "prefect"],
        ):
            with patch(
                "agent_backbone.services.infrastructure._agents.stop_session",
                new_callable=AsyncMock,
            ) as mock_stop:
                from agent_backbone.services.infrastructure._agents import (
                    stop_all_agents,
                )

                count = await stop_all_agents(agent_config)
        assert count == 0
        mock_stop.assert_not_called()


class TestStartOrchestrators:
    @pytest.mark.asyncio
    async def test_uses_registry_group_filtering(self, agent_config):
        """start_orchestrators starts all entities in the 'orchestrators' group."""
        started_names = []

        async def mock_exists(name):
            return False

        async def mock_start(name, **kwargs):
            started_names.append(name)
            return True

        with patch(
            "agent_backbone.services.infrastructure._agents.session_exists",
            new_callable=AsyncMock,
            side_effect=mock_exists,
        ):
            with patch(
                "agent_backbone.services.infrastructure._agents.start_session",
                new_callable=AsyncMock,
                side_effect=mock_start,
            ):
                from agent_backbone.services.infrastructure._agents import (
                    start_orchestrators,
                )

                count = await start_orchestrators(agent_config)
        assert count == 2  # ike and leo are orchestrators
        assert set(started_names) == {"ike", "leo"}


class TestStartOrg:
    @pytest.mark.asyncio
    async def test_starts_org_repos(self, agent_config):
        """start_org starts all coding agents for the given organization."""
        with patch(
            "agent_backbone.services.infrastructure._agents.session_exists",
            new_callable=AsyncMock,
            return_value=False,
        ):
            with patch(
                "agent_backbone.services.infrastructure._agents.start_session",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_start:
                from agent_backbone.services.infrastructure._agents import start_org

                count = await start_org("WF", agent_config)
        assert count == 1
        assert mock_start.call_args[0][0] == "agent-backbone"

    @pytest.mark.asyncio
    async def test_returns_zero_for_unknown_org(self, agent_config):
        """start_org returns 0 for an organization with no repos."""
        from agent_backbone.services.infrastructure._agents import start_org

        count = await start_org("UnknownOrg", agent_config)
        assert count == 0


class TestStartAll:
    @pytest.mark.asyncio
    async def test_starts_all_groups(self, agent_config):
        """start_all starts orchestrators, standalone agents, and all orgs."""
        started_names = []

        async def mock_exists(name):
            return False

        async def mock_start(name, **kwargs):
            started_names.append(name)
            return True

        with patch(
            "agent_backbone.services.infrastructure._agents.session_exists",
            new_callable=AsyncMock,
            side_effect=mock_exists,
        ):
            with patch(
                "agent_backbone.services.infrastructure._agents.start_session",
                new_callable=AsyncMock,
                side_effect=mock_start,
            ):
                from agent_backbone.services.infrastructure._agents import start_all

                total = await start_all(agent_config)
        # 2 orchestrators (ike, leo) + 1 standalone (ada) + 2 repos (platform-api, agent-backbone)
        assert total == 5
        assert set(started_names) == {
            "ike",
            "leo",
            "ada",
            "platform-api",
            "agent-backbone",
        }


class TestListAgents:
    def test_formats_output(self, agent_config):
        """list_agents produces formatted text with entities and repos."""
        from agent_backbone.services.infrastructure._agents import list_agents

        output = list_agents(agent_config)
        assert "Named Entities" in output
        assert "ike" in output
        assert "leo" in output
        assert "ada" in output
        assert "Coding Agents" in output
        assert "platform-api" in output
        assert "agent-backbone" in output

    def test_groups_repos_by_org(self, agent_config):
        """list_agents groups coding agents under their org headers."""
        from agent_backbone.services.infrastructure._agents import list_agents

        output = list_agents(agent_config)
        assert "Coding Agents (Arclio)" in output
        assert "Coding Agents (WF)" in output
