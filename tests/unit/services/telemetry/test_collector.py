"""Tests for CLI-native telemetry collection."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agent_backbone.services.database import BackboneDB
from agent_backbone.services.telemetry import (
    ClaudeTelemetryAdapter,
    CodexTelemetryAdapter,
    CursorTelemetryAdapter,
    GeminiTelemetryAdapter,
    OpenCodeTelemetryAdapter,
    collect_session_telemetry,
)
from agent_backbone.services.telemetry import _adapters as telemetry_adapters
from agent_backbone.services.terminal import TerminalRuntime


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(json.dumps(record) for record in records) + "\n"
    path.write_text(payload, encoding="utf-8")


def _append_jsonl(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record))
        handle.write("\n")


async def test_collects_claude_transcript_incrementally(monkeypatch, tmp_path):
    cwd = tmp_path / "workspace" / "repo"
    cwd.mkdir(parents=True)
    projects_dir = tmp_path / "claude" / "projects"
    project_dir = projects_dir / str(cwd).replace("/", "-")
    transcript = project_dir / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "progress",
                "uuid": "progress-1",
                "sessionId": "claude-session-id",
                "timestamp": "2026-03-13T12:00:00Z",
                "data": {
                    "type": "hook_progress",
                    "hookEvent": "SessionStart",
                    "hookName": "SessionStart:startup",
                    "command": "~/.claude/hooks/session-context.sh",
                },
            },
            {
                "type": "user",
                "uuid": "user-1",
                "sessionId": "claude-session-id",
                "timestamp": "2026-03-13T12:00:01Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Check delivery routing."}],
                },
            },
            {
                "type": "assistant",
                "uuid": "assistant-1",
                "sessionId": "claude-session-id",
                "timestamp": "2026-03-13T12:00:02Z",
                "requestId": "req-1",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-1",
                    "content": [
                        {"type": "thinking", "thinking": "Planning verification."},
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "exec_command",
                            "input": {"cmd": "pytest"},
                        },
                        {"type": "text", "text": "Running tests now."},
                    ],
                    "usage": {"input_tokens": 42, "output_tokens": 21},
                    "stop_reason": "tool_use",
                },
            },
            {
                "type": "user",
                "uuid": "user-2",
                "sessionId": "claude-session-id",
                "toolUseID": "tool-1",
                "timestamp": "2026-03-13T12:00:03Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "content": "32 passed",
                        }
                    ],
                },
            },
        ],
    )

    adapter = ClaudeTelemetryAdapter(projects_dir=projects_dir)
    monkeypatch.setitem(telemetry_adapters._ADAPTERS, TerminalRuntime.CLAUDE, adapter)

    async with BackboneDB.connect("sqlite+aiosqlite:///:memory:") as db:
        summary = await collect_session_telemetry(
            db=db,
            session_name="claude-session",
            runtime=TerminalRuntime.CLAUDE,
            cwd=cwd,
            entity="coding-agent",
        )
        assert summary.sources_scanned == 1
        assert summary.events_recorded == 8

        rows = await db.get_activity("claude-session", limit=20)
        names = {row["event"] for row in rows}
        assert "session.started" in names
        assert "message.assistant" in names
        assert "tool.started" in names
        assert "tool.finished" in names
        assert "token.usage" in names
        assert rows[0]["runtime"] == "claude"

        checkpoint = await db.get_telemetry_checkpoint("claude-session", str(transcript))
        assert checkpoint is not None
        assert checkpoint["checkpoint"]["offset"] > 0

        repeat = await collect_session_telemetry(
            db=db,
            session_name="claude-session",
            runtime=TerminalRuntime.CLAUDE,
            cwd=cwd,
            entity="coding-agent",
        )
        assert repeat.events_recorded == 0

        _append_jsonl(
            transcript,
            {
                "type": "assistant",
                "uuid": "assistant-2",
                "sessionId": "claude-session-id",
                "timestamp": "2026-03-13T12:00:04Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-1",
                    "content": [{"type": "text", "text": "Done."}],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            },
        )
        delta = await collect_session_telemetry(
            db=db,
            session_name="claude-session",
            runtime=TerminalRuntime.CLAUDE,
            cwd=cwd,
            entity="coding-agent",
        )
        assert delta.events_recorded == 2


async def test_collects_codex_session_incrementally(monkeypatch, tmp_path):
    cwd = tmp_path / "workspace" / "codex-repo"
    cwd.mkdir(parents=True)
    sessions_dir = tmp_path / "codex" / "sessions" / "2026" / "03" / "13"
    transcript = sessions_dir / "rollout-test.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "timestamp": "2026-03-13T13:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "codex-session-id",
                    "cwd": str(cwd),
                    "originator": "codex_cli_rs",
                    "cli_version": "0.112.0",
                    "source": "cli",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-03-13T13:00:01Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-1"},
            },
            {
                "timestamp": "2026-03-13T13:00:02Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "Please inspect the repo."},
            },
            {
                "timestamp": "2026-03-13T13:00:03Z",
                "type": "response_item",
                "payload": {"type": "reasoning", "summary": ["Need to inspect files."]},
            },
            {
                "timestamp": "2026-03-13T13:00:04Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "exec_command",
                    "arguments": '{"cmd":"rg telemetry"}',
                },
            },
            {
                "timestamp": "2026-03-13T13:00:05Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "src/agent_backbone/services/telemetry",
                },
            },
            {
                "timestamp": "2026-03-13T13:00:06Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 100,
                            "output_tokens": 50,
                            "reasoning_output_tokens": 10,
                        },
                        "last_token_usage": {"input_tokens": 100, "output_tokens": 50},
                    },
                },
            },
            {
                "timestamp": "2026-03-13T13:00:07Z",
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": "Telemetry implementation is underway.",
                    "phase": "commentary",
                },
            },
        ],
    )

    adapter = CodexTelemetryAdapter(sessions_dir=tmp_path / "codex" / "sessions")
    monkeypatch.setitem(telemetry_adapters._ADAPTERS, TerminalRuntime.CODEX, adapter)

    async with BackboneDB.connect("sqlite+aiosqlite:///:memory:") as db:
        summary = await collect_session_telemetry(
            db=db,
            session_name="codex-session",
            runtime=TerminalRuntime.CODEX,
            cwd=cwd,
            entity="coding-agent",
        )
        assert summary.sources_scanned == 1
        assert summary.events_recorded == 8

        rows = await db.get_activity("codex-session", limit=20)
        names = {row["event"] for row in rows}
        assert "session.started" in names
        assert "task.started" in names
        assert "reasoning" in names
        assert "tool.started" in names
        assert "tool.finished" in names
        assert "token.usage" in names
        assert "message.assistant" in names

        repeat = await collect_session_telemetry(
            db=db,
            session_name="codex-session",
            runtime=TerminalRuntime.CODEX,
            cwd=cwd,
            entity="coding-agent",
        )
        assert repeat.events_recorded == 0

        _append_jsonl(
            transcript,
            {
                "timestamp": "2026-03-13T13:00:08Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "turn-1"},
            },
        )
        delta = await collect_session_telemetry(
            db=db,
            session_name="codex-session",
            runtime=TerminalRuntime.CODEX,
            cwd=cwd,
            entity="coding-agent",
        )
        assert delta.events_recorded == 1


async def test_collects_gemini_snapshot_and_logs_incrementally(monkeypatch, tmp_path):
    cwd = tmp_path / "workspace" / "agent-backbone"
    cwd.mkdir(parents=True)
    tmp_dir = tmp_path / "gemini" / "tmp"
    project_dir = tmp_dir / cwd.name
    chats_dir = project_dir / "chats"
    chats_dir.mkdir(parents=True)
    snapshot = chats_dir / "session-1.json"
    snapshot.write_text(
        json.dumps(
            {
                "sessionId": "gemini-session-id",
                "projectHash": "hash-1",
                "startTime": "2026-03-13T14:00:00Z",
                "lastUpdated": "2026-03-13T14:00:03Z",
                "kind": "main",
                "messages": [
                    {
                        "id": "msg-1",
                        "timestamp": "2026-03-13T14:00:00Z",
                        "type": "user",
                        "content": [{"text": "Please inspect the adapter."}],
                    },
                    {
                        "id": "msg-2",
                        "timestamp": "2026-03-13T14:00:01Z",
                        "type": "gemini",
                        "content": "Working on it.",
                        "thoughts": [
                            {
                                "subject": "Plan",
                                "description": "Inspecting files first.",
                                "timestamp": "2026-03-13T14:00:01Z",
                            }
                        ],
                        "tokens": {"input": 10, "output": 5, "total": 15},
                        "model": "gemini-3.1-pro",
                        "toolCalls": [
                            {
                                "id": "tool-1",
                                "name": "run_shell_command",
                                "args": {"command": "rg telemetry"},
                                "result": {"ok": True},
                                "status": "success",
                                "timestamp": "2026-03-13T14:00:02Z",
                            }
                        ],
                    },
                    {
                        "id": "msg-3",
                        "timestamp": "2026-03-13T14:00:03Z",
                        "type": "error",
                        "content": "Temporary runtime failure",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    logs = project_dir / "logs.json"
    logs.write_text(
        json.dumps(
            [
                {
                    "sessionId": "gemini-session-id",
                    "messageId": 0,
                    "type": "user",
                    "message": "Please inspect the adapter.",
                    "timestamp": "2026-03-13T14:00:00Z",
                }
            ]
        ),
        encoding="utf-8",
    )

    adapter = GeminiTelemetryAdapter(tmp_dir=tmp_dir)
    monkeypatch.setitem(telemetry_adapters._ADAPTERS, TerminalRuntime.GEMINI, adapter)

    async with BackboneDB.connect("sqlite+aiosqlite:///:memory:") as db:
        summary = await collect_session_telemetry(
            db=db,
            session_name="gemini-session",
            runtime=TerminalRuntime.GEMINI,
            cwd=cwd,
            entity="bell-wf",
        )
        assert summary.sources_scanned == 2
        assert summary.events_recorded == 9

        rows = await db.get_activity("gemini-session", limit=20)
        names = {row["event"] for row in rows}
        assert "session.started" in names
        assert "message.user" in names
        assert "message.assistant" in names
        assert "reasoning" in names
        assert "tool.started" in names
        assert "tool.finished" in names
        assert "token.usage" in names
        assert "runtime.error" in names

        repeat = await collect_session_telemetry(
            db=db,
            session_name="gemini-session",
            runtime=TerminalRuntime.GEMINI,
            cwd=cwd,
            entity="bell-wf",
        )
        assert repeat.events_recorded == 0

        snapshot_payload = json.loads(snapshot.read_text(encoding="utf-8"))
        snapshot_payload["messages"].append(
            {
                "id": "msg-4",
                "timestamp": "2026-03-13T14:00:04Z",
                "type": "gemini",
                "content": "Done.",
                "tokens": {"input": 2, "output": 1, "total": 3},
                "model": "gemini-3.1-pro",
            }
        )
        snapshot_payload["lastUpdated"] = "2026-03-13T14:00:04Z"
        snapshot.write_text(json.dumps(snapshot_payload), encoding="utf-8")
        logs_payload = json.loads(logs.read_text(encoding="utf-8"))
        logs_payload.append(
            {
                "sessionId": "gemini-session-id",
                "messageId": 1,
                "type": "assistant",
                "message": "Done.",
                "timestamp": "2026-03-13T14:00:04Z",
            }
        )
        logs.write_text(json.dumps(logs_payload), encoding="utf-8")

        delta = await collect_session_telemetry(
            db=db,
            session_name="gemini-session",
            runtime=TerminalRuntime.GEMINI,
            cwd=cwd,
            entity="bell-wf",
        )
        assert delta.events_recorded == 3


async def test_claimed_sources_prevents_cross_contamination(monkeypatch, tmp_path):
    """In shared-worktree scenarios, claimed_sources prevents duplicate ingestion (#35)."""
    cwd = tmp_path / "workspace" / "shared-worktree"
    cwd.mkdir(parents=True)
    projects_dir = tmp_path / "claude" / "projects"
    project_dir = projects_dir / str(cwd).replace("/", "-")
    transcript = project_dir / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "user",
                "uuid": "user-1",
                "sessionId": "shared-session-id",
                "timestamp": "2026-03-13T12:00:01Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Do the work."}],
                },
            },
            {
                "type": "assistant",
                "uuid": "assistant-1",
                "sessionId": "shared-session-id",
                "timestamp": "2026-03-13T12:00:02Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-1",
                    "content": [{"type": "text", "text": "Done."}],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            },
        ],
    )

    adapter = ClaudeTelemetryAdapter(projects_dir=projects_dir)
    monkeypatch.setitem(telemetry_adapters._ADAPTERS, TerminalRuntime.CLAUDE, adapter)

    async with BackboneDB.connect("sqlite+aiosqlite:///:memory:") as db:
        claimed: set[str] = set()

        # Session A collects first — claims the source file
        summary_a = await collect_session_telemetry(
            db=db,
            session_name="session-a",
            runtime=TerminalRuntime.CLAUDE,
            cwd=cwd,
            entity="coder-a",
            claimed_sources=claimed,
        )
        assert summary_a.sources_scanned == 1
        assert summary_a.events_recorded > 0

        # Session B shares the same cwd — source already claimed
        summary_b = await collect_session_telemetry(
            db=db,
            session_name="session-b",
            runtime=TerminalRuntime.CLAUDE,
            cwd=cwd,
            entity="coder-b",
            claimed_sources=claimed,
        )
        assert summary_b.sources_scanned == 0
        assert summary_b.events_recorded == 0


def _create_opencode_db(path: Path, cwd: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE session (
                id TEXT PRIMARY KEY,
                directory TEXT NOT NULL,
                title TEXT,
                time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL
            );
            CREATE TABLE message (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                time_created INTEGER NOT NULL,
                data TEXT NOT NULL
            );
            CREATE TABLE part (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                time_created INTEGER NOT NULL,
                data TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
            ("ses-1", str(cwd), "Agent Backbone", 1773400963000, 1773400969000),
        )
        conn.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            (
                "msg-user",
                "ses-1",
                1773400963100,
                json.dumps({"role": "user", "agent": "build", "summary": {"diffs": []}}),
            ),
        )
        conn.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            (
                "msg-assistant",
                "ses-1",
                1773400963200,
                json.dumps(
                    {
                        "role": "assistant",
                        "agent": "build",
                        "mode": "build",
                        "finish": "stop",
                        "providerID": "opencode",
                        "modelID": "big-pickle",
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            (
                "part-user",
                "ses-1",
                "msg-user",
                1773400963150,
                json.dumps({"type": "text", "text": "Probe request."}),
            ),
        )
        conn.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            (
                "part-reasoning",
                "ses-1",
                "msg-assistant",
                1773400963250,
                json.dumps({"type": "reasoning", "text": "Analyzing request."}),
            ),
        )
        conn.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            (
                "part-text",
                "ses-1",
                "msg-assistant",
                1773400963300,
                json.dumps({"type": "text", "text": "ACK-OPENCODE"}),
            ),
        )
        conn.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            (
                "part-step-start",
                "ses-1",
                "msg-assistant",
                1773400963350,
                json.dumps({"type": "step-start", "snapshot": "abc"}),
            ),
        )
        conn.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            (
                "part-step-finish",
                "ses-1",
                "msg-assistant",
                1773400963400,
                json.dumps(
                    {
                        "type": "step-finish",
                        "reason": "stop",
                        "snapshot": "abc",
                        "tokens": {"total": 30, "input": 10, "output": 20},
                    }
                ),
            ),
        )
        conn.commit()


async def test_collects_opencode_sqlite_incrementally(monkeypatch, tmp_path):
    cwd = tmp_path / "workspace" / "agent-backbone"
    cwd.mkdir(parents=True)
    db_path = tmp_path / "opencode" / "opencode.db"
    _create_opencode_db(db_path, cwd)

    adapter = OpenCodeTelemetryAdapter(db_path=db_path)
    monkeypatch.setitem(telemetry_adapters._ADAPTERS, TerminalRuntime.OPENCODE, adapter)

    async with BackboneDB.connect("sqlite+aiosqlite:///:memory:") as db:
        summary = await collect_session_telemetry(
            db=db,
            session_name="opencode-session",
            runtime=TerminalRuntime.OPENCODE,
            cwd=cwd,
            entity="bell-wf",
        )
        assert summary.sources_scanned == 1
        assert summary.events_recorded == 9

        rows = await db.get_activity("opencode-session", limit=20)
        names = {row["event"] for row in rows}
        assert "session.started" in names
        assert "message.user" in names
        assert "message.assistant" in names
        assert "reasoning" in names
        assert "task.started" in names
        assert "task.completed" in names
        assert "token.usage" in names

        repeat = await collect_session_telemetry(
            db=db,
            session_name="opencode-session",
            runtime=TerminalRuntime.OPENCODE,
            cwd=cwd,
            entity="bell-wf",
        )
        assert repeat.events_recorded == 0

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
                (
                    "part-new-text",
                    "ses-1",
                    "msg-assistant",
                    1773400963500,
                    json.dumps({"type": "text", "text": "Second response"}),
                ),
            )
            conn.commit()

        delta = await collect_session_telemetry(
            db=db,
            session_name="opencode-session",
            runtime=TerminalRuntime.OPENCODE,
            cwd=cwd,
            entity="bell-wf",
        )
        assert delta.events_recorded == 1


async def test_collects_cursor_sources_incrementally(monkeypatch, tmp_path):
    cwd = tmp_path / "workspace" / "agent-backbone"
    cwd.mkdir(parents=True)
    project_slug = str(cwd).lstrip("/").replace("/", "-")
    project_dir = tmp_path / "cursor" / "projects" / project_slug
    transcript_dir = project_dir / "agent-transcripts"
    tool_dir = project_dir / "agent-tools"
    terminal_dir = project_dir / "terminals"
    transcript_dir.mkdir(parents=True)
    tool_dir.mkdir(parents=True)
    terminal_dir.mkdir(parents=True)

    transcript = transcript_dir / "subagent.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "role": "user",
                "message": {
                    "content": [{"type": "text", "text": "Inspect the repo."}],
                },
            },
            {
                "role": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Working on it."}],
                },
            },
        ],
    )
    tool_log = tool_dir / "tool.txt"
    tool_log.write_text("rg telemetry\n", encoding="utf-8")
    terminal_log = terminal_dir / "1.txt"
    terminal_log.write_text("prompt>\n", encoding="utf-8")

    adapter = CursorTelemetryAdapter(projects_dir=tmp_path / "cursor" / "projects")
    monkeypatch.setitem(telemetry_adapters._ADAPTERS, TerminalRuntime.CURSOR, adapter)

    async with BackboneDB.connect("sqlite+aiosqlite:///:memory:") as db:
        summary = await collect_session_telemetry(
            db=db,
            session_name="cursor-session",
            runtime=TerminalRuntime.CURSOR,
            cwd=cwd,
            entity="cursor-agent",
        )
        assert summary.sources_scanned == 3
        assert summary.events_recorded == 4

        rows = await db.get_activity("cursor-session", limit=20)
        names = {row["event"] for row in rows}
        assert "message.user" in names
        assert "message.assistant" in names
        assert "tool.finished" in names
        assert "runtime.event" in names

        repeat = await collect_session_telemetry(
            db=db,
            session_name="cursor-session",
            runtime=TerminalRuntime.CURSOR,
            cwd=cwd,
            entity="cursor-agent",
        )
        assert repeat.events_recorded == 0

        _append_jsonl(
            transcript,
            {
                "role": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Done."}],
                },
            },
        )
        terminal_log.write_text("prompt>\nACK-CURSOR\n", encoding="utf-8")

        delta = await collect_session_telemetry(
            db=db,
            session_name="cursor-session",
            runtime=TerminalRuntime.CURSOR,
            cwd=cwd,
            entity="cursor-agent",
        )
        assert delta.events_recorded == 2
