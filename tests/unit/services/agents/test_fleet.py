"""Fleet snapshots: save the running agents, then resume exactly their conversations."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import AgentSpec
from agent_backbone.services.agents import AgentStore, StateSnapshot, write_state_file
from agent_backbone.services.agents._file_reader import write_starting_marker
from agent_backbone.services.agents.fleet import resume_fleet, save_fleet
from agent_backbone.services.agents.launch import start_agent
from agent_backbone.services.agents.models import AgentState

_FLEET = "agent_backbone.services.agents.fleet"
_LAUNCH = "agent_backbone.services.agents.launch"
_BASE = "agent_backbone.services.runtimes.base"


@pytest.fixture(autouse=True)
def _binaries():
    with (
        patch(f"{_BASE}.Runtime.available", return_value=True),
        patch(f"{_BASE}.resolve_command", side_effect=lambda name, *a, **k: f"/usr/bin/{name}"),
    ):
        yield


async def _store(db, tmp_path, *specs: tuple[str, str]) -> AgentStore:
    store = AgentStore(db, tmp_path / "data")
    await store.start()
    for name, runtime in specs:
        project = tmp_path / name
        project.mkdir(exist_ok=True)
        await store.register(spec=AgentSpec(name=name, dir=str(project), runtime=runtime))
    return store


def _states(**states: str):
    async def state(config, name, **kwargs):
        return StateSnapshot(state=AgentState(states.get(name, "idle")))

    return state


def _report(store, name: str, session_id: str, runtime: str, *, launch_id: str = "L1") -> None:
    """The running session's hook reported its conversation."""
    write_starting_marker(store.config.state_dir, name, 1.0, launch_id=launch_id)
    write_state_file(
        store.config.state_dir,
        name,
        {
            "state": "idle",
            "ts": 2.0,
            "session_id": session_id,
            "runtime": runtime,
            "launch_id": launch_id,
        },
    )


async def _save(store, db, *, running=("app",), states=None, stop=False, **kwargs):
    with (
        patch(f"{_FLEET}.session_exists", AsyncMock(side_effect=lambda n: n in running)),
        patch(f"{_FLEET}.agent_state", _states(**(states or {}))),
        patch(f"{_FLEET}.stop_agent_session", AsyncMock(return_value=True)) as stops,
    ):
        snapshot = await save_fleet(store, store.config, db, stop=stop, **kwargs)
    return snapshot, [call.args[1] for call in stops.await_args_list]


class TestSave:
    async def test_nothing_running_saves_nothing(self, db, tmp_path):
        store = await _store(db, tmp_path, ("app", "claude"))
        snapshot, _ = await _save(store, db, running=())
        assert snapshot is None
        assert await db.fleet.latest() is None

    async def test_saves_the_current_sessions_id_durably(self, db, tmp_path):
        store = await _store(db, tmp_path, ("app", "claude"), ("idle-one", "codex"))
        _report(store, "app", "sess-1", "claude")
        snapshot, stopped = await _save(store, db)
        assert stopped == []
        (entry,) = snapshot["agents"]
        assert entry["name"] == "app" and entry["session_id"] == "sess-1"
        assert entry["resumable"] and entry["stop"] == "not_requested"
        assert (await db.fleet.latest())["agents"] == snapshot["agents"]

    async def test_an_earlier_sessions_id_is_never_saved(self, db, tmp_path):
        """The running session has not reported yet: the file still holds the
        previous launch's id, which would resume the wrong conversation."""
        store = await _store(db, tmp_path, ("app", "claude"))
        _report(store, "app", "old-sess", "claude", launch_id="L1")
        write_starting_marker(store.config.state_dir, "app", 5.0, launch_id="L2")
        snapshot, _ = await _save(store, db)
        (entry,) = snapshot["agents"]
        assert entry["session_id"] is None
        assert entry["not_resumable_reason"] == "no_session_reported"

    async def test_a_runtime_without_exact_resume_is_flagged(self, db, tmp_path):
        store = await _store(db, tmp_path, ("app", "deepcode"))
        _report(store, "app", "sess-1", "deepcode")
        snapshot, _ = await _save(store, db)
        assert snapshot["agents"][0]["not_resumable_reason"] == "exact_resume_unsupported"

    async def test_busy_agents_are_saved_but_not_stopped_unless_forced(self, db, tmp_path):
        store = await _store(db, tmp_path, ("app", "claude"), ("web", "codex"), ("orch", "claude"))
        running = ("app", "web", "orch")
        states = {"web": "busy", "orch": "waiting_for_human"}
        snapshot, stopped = await _save(store, db, running=running, states=states, stop=True)
        assert stopped == ["app"]
        stops = {entry["name"]: entry["stop"] for entry in snapshot["agents"]}
        assert stops == {"app": "stopped", "web": "skipped_busy", "orch": "skipped_busy"}
        saved = (await db.fleet.get(snapshot["id"]))["agents"]
        assert {entry["name"]: entry["stop"] for entry in saved} == stops

        _, stopped = await _save(store, db, running=running, states=states, stop=True, force=True)
        assert sorted(stopped) == ["app", "orch", "web"]

    async def test_an_agent_that_turns_busy_before_its_stop_is_left_running(self, db, tmp_path):
        store = await _store(db, tmp_path, ("app", "claude"))
        seen = iter(["idle", "busy"])  # at the save, then right before the stop

        async def state(config, name, **kwargs):
            return StateSnapshot(state=AgentState(next(seen)))

        with (
            patch(f"{_FLEET}.session_exists", AsyncMock(return_value=True)),
            patch(f"{_FLEET}.agent_state", state),
            patch(f"{_FLEET}.stop_agent_session", AsyncMock(return_value=True)) as stop,
        ):
            snapshot = await save_fleet(store, store.config, db, stop=True)
        assert snapshot["agents"][0]["stop"] == "skipped_busy"
        stop.assert_not_awaited()

    async def test_the_caller_is_stopped_last(self, db, tmp_path):
        store = await _store(db, tmp_path, ("app", "claude"), ("web", "codex"), ("zed", "claude"))
        _, stopped = await _save(
            store, db, running=("app", "web", "zed"), stop=True, from_entity="app"
        )
        assert stopped[-1] == "app"


async def _snapshot(db, *entries: dict) -> dict:
    agents = [
        {
            "name": "app",
            "runtime": "claude",
            "model": None,
            "session_id": "sess-1",
            "resumable": True,
            "not_resumable_reason": None,
            "state_at_save": "idle",
            "stop": "stopped",
            "stop_error": None,
            "session_reported_at": None,
            **entry,
        }
        for entry in entries
    ]
    return await db.fleet.create(created_by="", note=None, stop=True, force=False, agents=agents)


async def _resume(store, db, snapshot, *, running=(), started=True):
    with (
        patch(f"{_FLEET}.session_exists", AsyncMock(side_effect=lambda n: n in running)),
        patch(f"{_LAUNCH}.session_exists", AsyncMock(return_value=False)),
        patch(f"{_LAUNCH}.start_session", AsyncMock(return_value=started)) as start,
    ):
        run = await resume_fleet(store, store.config, db, snapshot, wait=False)
    commands = {call.args[0]: call.kwargs["command"] for call in start.await_args_list}
    return run, commands


class TestResume:
    @pytest.mark.parametrize("runtime", ["claude", "codex", "opencode", "gemini"])
    async def test_exactly_the_saved_conversation_is_opened(self, db, tmp_path, runtime):
        store = await _store(db, tmp_path, ("app", runtime))
        # A newer conversation on record must not win over the saved one.
        _report(store, "app", "newer-sess", runtime)
        snapshot = await _snapshot(
            db, {"runtime": runtime, "dir": str(store.agents.get("app").path)}
        )
        run, commands = await _resume(store, db, snapshot)
        (agent,) = run["agents"]
        assert agent["outcome"] == "resumed_known_session"
        assert "sess-1" in commands["app"] and "newer-sess" not in commands["app"]
        assert "--last" not in commands["app"] and "--continue" not in commands["app"]
        assert (await db.fleet.get(snapshot["id"]))["resumes"][0]["agents"] == run["agents"]

    async def test_the_saved_model_is_restored(self, db, tmp_path):
        store = await _store(db, tmp_path, ("app", "claude"))
        path = str(store.agents.get("app").path)
        snapshot = await _snapshot(db, {"dir": path, "model": "opus"})
        await _resume(store, db, snapshot)
        assert store.agents.get("app").model == "opus"

    async def test_a_saved_default_model_clears_one_set_since(self, db, tmp_path):
        store = await _store(db, tmp_path, ("app", "claude"))
        await store.update("app", model="opus")
        snapshot = await _snapshot(db, {"dir": str(store.agents.get("app").path), "model": None})
        _, commands = await _resume(store, db, snapshot)
        assert store.agents.get("app").model is None
        assert "opus" not in commands["app"]

    async def test_concurrent_resumes_both_stay_in_the_history(self, db, tmp_path):
        snapshot = await _snapshot(db, {"dir": "/x"})
        import asyncio

        await asyncio.gather(*(db.fleet.add_resume(snapshot["id"], {"run": n}) for n in range(5)))
        runs = (await db.fleet.get(snapshot["id"]))["resumes"]
        assert sorted(run["run"] for run in runs) == list(range(5))

    async def test_session_confirmed_compares_the_reported_id(self, db, tmp_path):
        store = await _store(db, tmp_path, ("app", "claude"))
        snapshot = await _snapshot(db, {"dir": str(store.agents.get("app").path)})
        for reported, confirmed in (("sess-1", True), ("forked", False)):
            hook = StateSnapshot(state=AgentState.IDLE, session_id=reported)
            with patch(f"{_FLEET}.read_state_file", return_value=hook):
                run, _ = await _resume(store, db, snapshot)
            assert run["agents"][0]["session_confirmed"] is confirmed

    async def test_reasons_an_agent_is_not_resumed(self, db, tmp_path):
        store = await _store(
            db,
            tmp_path,
            ("app", "claude"),
            ("web", "codex"),
            ("moved", "claude"),
            ("dc", "deepcode"),
            ("nosess", "claude"),
            ("up", "claude"),
        )
        path = lambda name: str(store.agents.get(name).path)  # noqa: E731
        snapshot = await _snapshot(
            db,
            {"name": "gone", "dir": "/nowhere"},
            {"name": "web", "dir": path("web"), "runtime": "claude"},
            {"name": "moved", "dir": "/old/place"},
            {"name": "dc", "dir": path("dc"), "runtime": "deepcode"},
            {"name": "nosess", "dir": path("nosess"), "session_id": None},
            {"name": "up", "dir": path("up")},
        )
        run, commands = await _resume(store, db, snapshot, running=("up",))
        outcomes = {a["name"]: (a["outcome"], a["reason"]) for a in run["agents"]}
        assert outcomes == {
            "gone": ("not_resumed", "agent_unknown"),
            "web": ("not_resumed", "runtime_changed"),
            "moved": ("not_resumed", "dir_changed"),
            "dc": ("not_resumed", "exact_resume_unsupported"),
            "nosess": ("not_resumed", "no_saved_session"),
            "up": ("already_running", None),
        }
        assert commands == {}
        assert run["counts"] == {"not_resumed": 5, "already_running": 1}

    async def test_a_launch_that_fails_is_reported(self, db, tmp_path):
        store = await _store(db, tmp_path, ("app", "claude"))
        snapshot = await _snapshot(db, {"dir": str(store.agents.get("app").path)})
        run, _ = await _resume(store, db, snapshot, started=False)
        assert run["agents"][0]["outcome"] == "failed"


@pytest.mark.parametrize("runtime", ["deepcode", "aider"])
async def test_an_exact_resume_is_refused_where_the_runtime_cannot_open_an_id(tmp_path, runtime):
    """Never a silent fallback to the directory's latest conversation."""
    from agent_backbone.config import bootstrap_config

    config = bootstrap_config(tmp_path / "data")
    spec = AgentSpec(name="app", dir=str(tmp_path), runtime=runtime)
    with (
        patch(f"{_LAUNCH}.session_exists", AsyncMock(return_value=False)),
        patch(f"{_LAUNCH}.start_session", AsyncMock(return_value=True)) as start,
    ):
        result = await start_agent(spec, config, session_id="sess-1", wait=False)
    assert not result.ok
    start.assert_not_awaited()


async def test_a_failed_resume_keeps_the_agents_model(db, tmp_path):
    store = await _store(db, tmp_path, ("app", "claude"))
    await store.update("app", model="sonnet")
    snapshot = await _snapshot(db, {"dir": str(store.agents.get("app").path), "model": "opus"})
    run, _ = await _resume(store, db, snapshot, started=False)
    assert run["agents"][0]["outcome"] == "failed"
    assert store.agents.get("app").model == "sonnet"


async def test_an_unexpected_error_is_one_agents_failure(db, tmp_path):
    store = await _store(db, tmp_path, ("app", "claude"), ("web", "claude"))
    snapshot = await _snapshot(
        db,
        {"dir": str(store.agents.get("app").path)},
        {"name": "web", "dir": str(store.agents.get("web").path)},
    )

    async def exists(name):
        if name == "app":
            raise FileNotFoundError("tmux")
        return True

    with patch(f"{_FLEET}.session_exists", exists):
        run = await resume_fleet(store, store.config, db, snapshot, wait=False)
    outcomes = {a["name"]: a["outcome"] for a in run["agents"]}
    assert outcomes == {"app": "failed", "web": "already_running"}
    assert (await db.fleet.get(snapshot["id"]))["resumes"]


async def test_a_model_change_during_a_failed_resume_is_kept(db, tmp_path):
    """The owner sets a model while the resume launches: the rollback must not undo it."""
    import asyncio

    store = await _store(db, tmp_path, ("app", "claude"))
    await store.update("app", model="sonnet")
    snapshot = await _snapshot(db, {"dir": str(store.agents.get("app").path), "model": "opus"})
    launched = asyncio.Event()

    async def slow_failure(*args, **kwargs):
        launched.set()
        await asyncio.sleep(0.05)
        return False

    async def owner():
        await launched.wait()
        await store.update("app", model="haiku")

    with (
        patch(f"{_FLEET}.session_exists", AsyncMock(return_value=False)),
        patch(f"{_LAUNCH}.session_exists", AsyncMock(return_value=False)),
        patch(f"{_LAUNCH}.start_session", slow_failure),
    ):
        await asyncio.gather(resume_fleet(store, store.config, db, snapshot, wait=False), owner())
    assert store.agents.get("app").model == "haiku"
