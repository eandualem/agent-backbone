"""A replacement must never inherit the previous launch's delivery condition."""

import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from agent_backbone.config import AgentSpec, bootstrap_config
from agent_backbone.hooks.backbone_state import prompt_digest
from agent_backbone.models import DeliveryOutcome
from agent_backbone.services.agents import get_agent_state, read_state_file, write_state_file
from agent_backbone.services.agents._file_reader import clear_starting_marker, write_starting_marker
from agent_backbone.services.agents.launch import start_agent
from agent_backbone.services.routing import safe_deliver
from agent_backbone.services.routing._delivery import prompt_hook_after
from agent_backbone.services.runtimes import RUNTIMES

LAUNCH = "agent_backbone.services.agents.launch"
INTEL = "agent_backbone.services.routing._intelligence"
DELIVERY = "agent_backbone.services.routing._delivery"
PROMPTS = {
    "claude": "❯ \n  ? for shortcuts\n",
    "codex": "› \n  ? for shortcuts\n",
    "opencode": "Ask anything...\n  tab agents  ctrl+p commands\n",
    "gemini": "*   Type your message or @path/to/file\n",
    "deepcode": (
        "status: completed · 14/1M · deepseek-v4-flash max\n>   Type your message...\n"
        "enter send · shift+enter newline · / commands · ctrl+d exit\n"
    ),
    "aider": "> ",
    "shell": "$ ",
}


@pytest.mark.parametrize("runtime", list(PROMPTS))
@pytest.mark.parametrize("tagged", [False, True])
async def test_terminal_ready_replacement_delivers_despite_previous_busy_hook(
    tmp_path, runtime, tagged
):
    config = bootstrap_config(tmp_path / "data")
    spec = AgentSpec(name="app", dir=str(tmp_path), runtime=runtime)
    old = {
        "state": "busy",
        "ts": time.time() - 65,
        "runtime": runtime,
        "session_id": "previous-conversation",
        "launch_id": "previous-launch",
    }
    if not tagged:
        old.pop("launch_id")
    write_state_file(config.state_dir, "app", old)
    before = (config.state_dir / "app.json").read_bytes()
    prompt = PROMPTS[runtime]
    with (
        patch.object(type(RUNTIMES[runtime]), "build_command", return_value="stub"),
        patch(f"{LAUNCH}.start_session", AsyncMock(return_value=True)),
        patch(f"{LAUNCH}.capture_pane", AsyncMock(return_value=prompt)),
        patch(f"{LAUNCH}.session_exists", AsyncMock(side_effect=[False, True])),
        patch(f"{LAUNCH}._queue_brief", AsyncMock(return_value=True)),
        patch(f"{INTEL}.list_sessions", AsyncMock(return_value=["app"])),
        patch(f"{INTEL}.capture_pane", AsyncMock(return_value=prompt)),
        patch(f"{INTEL}.resolve_runtime", AsyncMock(return_value=RUNTIMES[runtime])),
        patch(f"{INTEL}.in_copy_mode", AsyncMock(return_value=False)),
        patch(f"{DELIVERY}.send_message", AsyncMock(return_value=True)) as send,
    ):
        result = await start_agent(spec, config)
        assert result.ready == "ready"
        assert not (config.state_dir / "app.starting").exists()
        report = await safe_deliver("app", "continue", config, delivery_kind="direct")
    assert report.outcome == DeliveryOutcome.DELIVERED
    send.assert_awaited_once()
    assert (config.state_dir / "app.json").read_bytes() == before
    assert read_state_file(config.state_dir, "app").session_id == "previous-conversation"


@pytest.mark.parametrize("runtime", ["claude", "codex", "opencode"])
@pytest.mark.parametrize("late", [False, True])
async def test_previous_idle_never_allows_delivery_to_busy_replacement(tmp_path, runtime, late):
    config = bootstrap_config(tmp_path / "data")
    launched = time.time() - 2
    write_starting_marker(config.state_dir, "app", launched, launch_id="new")
    clear_starting_marker(config.state_dir, "app")
    write_state_file(
        config.state_dir,
        "app",
        {
            "state": "idle",
            "ts": time.time() if late else launched - 1,
            "launch_id": "old",
            "runtime": runtime,
        },
    )
    with (
        patch(f"{INTEL}.list_sessions", AsyncMock(return_value=["app"])),
        patch(
            f"{INTEL}.capture_pane",
            AsyncMock(return_value="Working (esc to interrupt)\nesc interrupt"),
        ),
        patch(f"{INTEL}.resolve_runtime", AsyncMock(return_value=RUNTIMES[runtime])),
        patch(f"{DELIVERY}.send_message", AsyncMock()) as send,
    ):
        result = await safe_deliver(
            "app", "continue", config, delivery_kind="direct", priority=True
        )
    assert result.outcome == DeliveryOutcome.AGENT_WORKING
    send.assert_not_awaited()


async def test_current_hook_authority_and_submission_boundary(tmp_path):
    launched = time.time() - 1
    write_starting_marker(tmp_path, "app", launched, launch_id="new")
    clear_starting_marker(tmp_path, "app")
    (tmp_path / "app.submitted").write_text(str(launched - 0.1))

    async def state():
        return await get_agent_state(
            tmp_path, "app", runtime_hint="codex", pane_content=PROMPTS["codex"]
        )

    assert (await state()).state.value == "idle"
    (tmp_path / "app.submitted").write_text(str(time.time()))
    assert (await state()).state.value == "busy"
    write_state_file(tmp_path, "app", {"state": "idle", "ts": time.time(), "launch_id": "new"})
    assert (await state()).state.value == "idle"
    write_state_file(tmp_path, "app", {"state": "busy", "ts": time.time(), "launch_id": "new"})
    assert (await state()).state.value == "busy"


async def test_late_old_hook_cannot_acknowledge_replacement_prompt(tmp_path):
    launched = time.time() - 1
    write_starting_marker(tmp_path, "app", launched, launch_id="new")
    clear_starting_marker(tmp_path, "app")
    record = {
        "state": "busy",
        "ts": time.time(),
        "prompted_at": time.time(),
        "prompt_digest": prompt_digest("continue"),
        "launch_id": "old",
    }
    with patch(f"{DELIVERY}.PROMPT_HOOK_WAIT_SECONDS", 0):
        write_state_file(tmp_path, "app", record)
        assert not await prompt_hook_after(tmp_path, "app", launched, "continue")
        write_state_file(tmp_path, "app", {**record, "launch_id": "new"})
        assert await prompt_hook_after(tmp_path, "app", launched, "continue")


async def test_late_old_hook_cannot_clear_starting_marker(tmp_path):
    write_starting_marker(tmp_path, "app", time.time() - 1, launch_id="new")
    write_state_file(tmp_path, "app", {"state": "idle", "ts": time.time(), "launch_id": "old"})
    assert read_state_file(tmp_path, "app", current_launch=True).state.value == "starting"
    assert json.loads((tmp_path / "app.launch").read_text())["launch_id"] == "new"


@pytest.mark.parametrize("runtime", ["claude", "codex", "opencode"])
async def test_fresh_restart_delivers_brief_then_continuation(db, tmp_path, runtime):
    from agent_backbone.services.agents import AgentStore
    from agent_backbone.services.jobs.retry import drain_message_queue
    from agent_backbone.services.jobs.transitions import run_transitions

    await db.settings.set("timing.grace_period_seconds", 0)
    store = AgentStore(db, tmp_path / "data")
    await store.start()
    await store.register(AgentSpec(name="app", dir=str(tmp_path), runtime=runtime))
    config = store.config
    write_state_file(
        config.state_dir,
        "app",
        {
            "state": "busy",
            "ts": time.time() - 65,
            "runtime": runtime,
            "session_id": "previous-conversation",
            "launch_id": "previous-launch",
        },
    )
    row = await db.transitions.create(agent_name="app", delay_seconds=0, message="continue")
    sent = []
    launch_env = {}

    async def start(name, **kwargs):
        launch_env.update(kwargs["environment"])
        return True

    async def send(name, message, **kwargs):
        sent.append(message)
        # A real hook acknowledging this paste, after the terminal-only readiness.
        write_state_file(
            config.state_dir,
            name,
            {
                "state": "idle",
                "ts": time.time(),
                "runtime": runtime,
                "session_id": "replacement-conversation",
                "launch_id": launch_env["BACKBONE_LAUNCH_ID"],
            },
        )
        return True

    with (
        patch.object(type(RUNTIMES[runtime]), "build_command", return_value="stub"),
        patch(f"{LAUNCH}.stop_agent", AsyncMock(return_value=True)),
        patch(f"{LAUNCH}.start_session", side_effect=start),
        patch(f"{LAUNCH}.capture_pane", AsyncMock(return_value=PROMPTS[runtime])),
        patch(f"{LAUNCH}.session_exists", AsyncMock(side_effect=[False, True])),
        patch(f"{INTEL}.list_sessions", AsyncMock(return_value=["app"])),
        patch(f"{INTEL}.capture_pane", AsyncMock(return_value=PROMPTS[runtime])),
        patch(f"{INTEL}.resolve_runtime", AsyncMock(return_value=RUNTIMES[runtime])),
        patch(f"{INTEL}.in_copy_mode", AsyncMock(return_value=False)),
        patch(f"{DELIVERY}.send_message", side_effect=send),
    ):
        assert await run_transitions(lambda: config, store, db) == {"app": "started"}
        done = await db.transitions.get(row["id"])
        assert done["result"]["ready"] == "ready"
        assert done["result"]["message"] != "agent_working"
        if runtime == "codex":
            assert sent == []  # The startup brief must arrive first.
            await drain_message_queue(config, db, None, active_sessions=["app"])
            assert len(sent) == 2
            assert "continue" not in sent[0]
        assert sent[-1] == "[via:backbone from:backbone] continue"
