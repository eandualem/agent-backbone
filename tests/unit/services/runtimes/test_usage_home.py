"""Transcript discovery uses the same agent home as the launched runtime."""

import json
import sqlite3
from pathlib import Path

import pytest

from agent_backbone.services.runtimes import get_runtime


def _transcript(runtime, root, session_id):
    if runtime == "claude":
        path = root / "projects" / "example" / f"{session_id}.jsonl"
    elif runtime == "codex":
        path = root / "sessions" / "2026" / "10" / "12" / f"rollout-{session_id}.jsonl"
    else:
        path = root / "opencode" / "opencode.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    if runtime == "opencode":
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE session (id TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO session VALUES (?)", (session_id,))
    else:
        path.write_text("{}\n")
    return path


@pytest.mark.parametrize(
    ("runtime", "config_key", "relative"),
    [
        ("claude", "CLAUDE_CONFIG_DIR", ".claude"),
        ("codex", "CODEX_HOME", ".codex"),
        ("opencode", "XDG_DATA_HOME", ".local/share"),
    ],
)
@pytest.mark.parametrize("override", ["none", "process", "agent"])
def test_transcripts_use_agent_home_and_preserve_config_precedence(
    tmp_path, monkeypatch, runtime, config_key, relative, override
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "server-home")
    monkeypatch.delenv(config_key, raising=False)
    env = {"HOME": str(tmp_path / "agent-home")}
    roots = {
        "none": tmp_path / "agent-home" / relative,
        "process": tmp_path / "process-config",
        "agent": tmp_path / "agent-config",
    }
    sources = {key: _transcript(runtime, root, "session-a") for key, root in roots.items()}
    if override in ("process", "agent"):
        monkeypatch.setenv(config_key, str(roots["process"]))
    if override == "agent":
        env[config_key] = str(roots["agent"])
    assert get_runtime(runtime).usage_paths("session-a", env) == [sources[override]]


@pytest.mark.parametrize("override", ["none", "process", "agent"])
def test_codex_child_usage_uses_the_same_home(tmp_path, monkeypatch, override):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "server-home")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    env = {"HOME": str(tmp_path / "agent-home")}
    roots = {
        "none": tmp_path / "agent-home" / ".codex",
        "process": tmp_path / "process-config",
        "agent": tmp_path / "agent-config",
    }
    children = {}
    for key, root in roots.items():
        child = _transcript("codex", root, f"child-{key}")
        child.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {
                        "id": f"child-{key}",
                        "source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}},
                    },
                }
            )
            + "\n"
        )
        children[key] = child
    if override in ("process", "agent"):
        monkeypatch.setenv("CODEX_HOME", str(roots["process"]))
    if override == "agent":
        env["CODEX_HOME"] = str(roots["agent"])
    parent = _transcript("codex", roots[override], "parent")
    assert get_runtime("codex").usage_children(parent, "parent", env) == [
        (f"child-{override}", children[override])
    ]
