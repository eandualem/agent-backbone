"""Starts keep the registered identity, runtime and model; conversations resume only on request."""

from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.api.models import AgentStartRequest
from agent_backbone.cli import build_parser
from agent_backbone.config import AgentSpec
from agent_backbone.services.agents import AgentStore, write_state_file
from agent_backbone.services.agents.launch import StartResult
from agent_backbone.services.agents.operations import StartRequest, resolve_agent, start_resolved


async def store_with_agent(db, tmp_path):
    store = AgentStore(db, tmp_path / "data")
    await store.start()
    await store.register(
        AgentSpec(name="Feynman", dir=str(tmp_path), runtime="codex", model="gpt-6-astra:high")
    )
    return store


async def test_directory_start_finds_custom_name_after_reload(db, tmp_path):
    store = await store_with_agent(db, tmp_path)
    reloaded = AgentStore(db, tmp_path / "data")
    await reloaded.start()
    with patch("agent_backbone.services.agents.store.detect_repo", AsyncMock(return_value="")):
        spec = await resolve_agent(reloaded, StartRequest(directory=str(tmp_path)))
    assert (spec.name, spec.runtime, spec.model) == ("Feynman", "codex", "gpt-6-astra:high")
    await store.refresh()
    assert store.agents.names == ["Feynman"]


async def test_shared_directory_requires_explicit_name(db, tmp_path):
    store = await store_with_agent(db, tmp_path)
    await store.register(AgentSpec(name="other", dir=str(tmp_path), runtime="shell"))
    with pytest.raises(ValueError, match="specify an agent name"):
        await resolve_agent(store, StartRequest(directory=str(tmp_path)))
    assert (await resolve_agent(store, StartRequest(name="Feynman"))).runtime == "codex"


@pytest.mark.parametrize("directory", [False, True])
@pytest.mark.parametrize("model", [None, "gpt-6-astra:high"])
async def test_runtime_switch_only_keeps_an_explicit_model(db, tmp_path, directory, model):
    store = await store_with_agent(db, tmp_path)
    with patch("agent_backbone.services.agents.store.detect_repo", AsyncMock(return_value="")):
        spec = await resolve_agent(
            store,
            StartRequest(
                name="Feynman",
                directory=str(tmp_path) if directory else None,
                runtime="claude",
                model=model,
            ),
        )
    assert (spec.runtime, spec.model) == ("claude", model)


@pytest.mark.parametrize(
    ("record", "requested", "expected"),
    [
        ({"runtime": "codex", "session_id": "saved"}, False, False),
        ({"session_id": "legacy"}, False, False),
        ({"runtime": "codex", "session_id": "saved"}, True, True),
        ({"runtime": "claude", "session_id": "other-cli"}, True, True),
        (None, True, True),
        (None, False, False),
    ],
)
async def test_a_start_is_fresh_unless_resume_is_asked_for(
    db, tmp_path, record, requested, expected
):
    """A saved conversation never resumes on its own; runtime and model are reused."""
    store = await store_with_agent(db, tmp_path)
    if record:
        write_state_file(store.config.state_dir, "Feynman", {"state": "idle", "ts": 1, **record})
    req = StartRequest(name="Feynman", resume=requested)
    spec = await resolve_agent(store, req)
    with (
        patch("agent_backbone.services.runtimes.base.Runtime.available", return_value=True),
        patch(
            "agent_backbone.services.agents.operations.launch.start_agent",
            AsyncMock(return_value=StartResult(ok=True)),
        ) as launch,
    ):
        await start_resolved(store, store.config, spec, req, db=db)
    assert launch.await_args.kwargs["resume"] is expected
    assert launch.await_args.kwargs["runtime"] == "codex"
    assert launch.await_args.kwargs["model"] == "gpt-6-astra:high"


def test_cli_and_api_default_to_fresh_and_resume_only_explicitly():
    parser = build_parser()
    assert parser.parse_args(["agent", "start", "app"]).resume is False
    assert parser.parse_args(["agent", "start", "app", "--fresh"]).resume is False
    assert parser.parse_args(["agent", "start", "app", "--resume"]).resume is True
    assert parser.parse_args(["agent", "resume", "app"]).resume is True
    with pytest.raises(SystemExit):
        parser.parse_args(["agent", "start", "app", "--fresh", "--resume"])
    assert AgentStartRequest().resume is False
    assert AgentStartRequest(resume=True).resume is True
    assert StartRequest().resume is False


@pytest.mark.parametrize("runtime", ["gemini", "opencode", "aider"])
async def test_explicit_resume_uses_the_saved_id_only_with_exact_capability(db, tmp_path, runtime):
    from agent_backbone.services.runtimes import RUNTIMES

    store = await store_with_agent(db, tmp_path)
    await store.update("Feynman", runtime=runtime)
    write_state_file(
        store.config.state_dir, "Feynman", {"runtime": runtime, "session_id": "own-session"}
    )
    req = StartRequest(name="Feynman", resume=True)
    spec = await resolve_agent(store, req)
    with (
        patch("agent_backbone.services.runtimes.base.Runtime.available", return_value=True),
        patch(
            "agent_backbone.services.agents.operations.launch.start_agent",
            AsyncMock(return_value=StartResult(ok=True)),
        ) as launch,
    ):
        await start_resolved(store, store.config, spec, req, db=db)
    assert launch.await_args.kwargs["resume"] is True
    if runtime != "aider":
        args = RUNTIMES[runtime].launch_args(
            model=None,
            resume="own-session",
            brief_file=None,
            pre_trust=False,
            data_dir=None,
            state_dir=None,
        )
        assert "own-session" in args and "latest" not in args and "--continue" not in args
