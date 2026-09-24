"""The optional Chrome tab-group names: hook record, native host, installer, extension."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_backbone.hooks import chrome_tab_names_host as host
from agent_backbone.hooks import claude_hook as hook
from agent_backbone.hooks.install import (
    CHROME_EXTENSION_ID,
    hook_source,
    install_chrome_tab_names,
    uninstall_chrome_tab_names,
)


@pytest.fixture(autouse=True)
def _own_state_dir(monkeypatch):
    # In a backbone-started session BACKBONE_STATE_DIR points at the real
    # state directory and wins over --state-dir.
    monkeypatch.delenv("BACKBONE_STATE_DIR", raising=False)


# tabs_context_mcp's result as Claude Code hands it to PostToolUse (live shape).
CONTEXT_RESULT = [
    {
        "type": "text",
        "text": json.dumps(
            {"availableTabs": [{"tabId": 607118701, "title": "New Tab"}], "tabGroupId": 1351533637}
        ),
    },
    {"type": "text", "text": '\n\nTab Context:\n- Available tabs:\n  • tabId 607118701: "New Tab"'},
]


def _post(tool: str, response) -> dict:
    return {"hook_event_name": "PostToolUse", "tool_name": tool, "tool_response": response}


class TestHookRecord:
    def _run(self, tmp_path, payload: dict) -> None:
        with patch.object(hook.sys, "stdin", io.StringIO(json.dumps(payload))):
            assert hook.main(["--state-dir", str(tmp_path), "--agent", "contract-desk"]) == 0

    @pytest.mark.parametrize("wrap", [lambda r: r, lambda r: {"content": r}])
    def test_a_chrome_result_records_the_agents_group_and_tabs(self, tmp_path, wrap):
        self._run(tmp_path, _post("mcp__claude-in-chrome__tabs_context_mcp", wrap(CONTEXT_RESULT)))
        record = json.loads((tmp_path / "chrome-groups" / "contract-desk.json").read_text())
        assert record["group"] == 1351533637 and record["tabs"] == [607118701]

    @pytest.mark.parametrize(
        "payload",
        [
            _post("Bash", CONTEXT_RESULT),  # not a Chrome tool
            _post("mcp__claude-in-chrome__navigate", [{"type": "text", "text": "ok"}]),
            {
                **_post("mcp__claude-in-chrome__tabs_context_mcp", CONTEXT_RESULT),
                "hook_event_name": "PreToolUse",
            },
        ],
    )
    def test_nothing_else_is_recorded(self, tmp_path, payload):
        self._run(tmp_path, payload)
        assert not (tmp_path / "chrome-groups").exists()


class TestNativeHost:
    def _record(self, state_dir: Path, agent: str, group: int, tabs, age: float = 0) -> None:
        (state_dir / "chrome-groups").mkdir(parents=True, exist_ok=True)
        (state_dir / "chrome-groups" / f"{agent}.json").write_text(
            json.dumps({"group": group, "tabs": tabs, "ts": time.time() - age})
        )

    def test_titles_and_stale_records(self, tmp_path):
        self._record(tmp_path, "contract-desk", 7, [1, 2])
        self._record(tmp_path, "ada", 8, [3])
        self._record(tmp_path, "old-desk", 9, [4], age=host.MAX_AGE_SECONDS + 1)
        (tmp_path / "chrome-groups" / "broken.json").write_text("{")
        assert host.groups(tmp_path) == [
            {"group": 8, "tabs": [3], "title": "Ada"},
            {"group": 7, "tabs": [1, 2], "title": "Contract Desk"},
        ]

    def test_answers_chromes_length_prefixed_request(self, tmp_path):
        self._record(tmp_path, "founder-desk", 11, [5])
        body = json.dumps({"op": "groups"}).encode()
        done = subprocess.run(
            [
                sys.executable,
                str(hook_source("chrome_tab_names_host.py")),
                "--state-dir",
                str(tmp_path),
                "chrome-extension://x/",
            ],
            input=struct.pack("=I", len(body)) + body,
            capture_output=True,
            timeout=30,
            check=True,
        )
        (length,) = struct.unpack("=I", done.stdout[:4])
        assert json.loads(done.stdout[4 : 4 + length]) == {
            "groups": [{"group": 11, "tabs": [5], "title": "Founder Desk"}]
        }


class TestInstaller:
    def test_the_manifest_key_yields_the_allowed_extension_id(self):
        manifest = json.loads((hook_source("chrome_tab_names") / "manifest.json").read_text())
        digest = hashlib.sha256(base64.b64decode(manifest["key"])).hexdigest()[:32]
        assert "".join(chr(ord("a") + int(c, 16)) for c in digest) == CHROME_EXTENSION_ID

    def test_install_and_uninstall(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        extension, manifest = install_chrome_tab_names(tmp_path / "data", tmp_path / "state")
        assert (extension / "manifest.json").is_file() and (extension / "background.js").is_file()
        registered = json.loads(manifest.read_text())
        assert registered["allowed_origins"] == [f"chrome-extension://{CHROME_EXTENSION_ID}/"]
        wrapper = Path(registered["path"])
        assert wrapper.stat().st_mode & 0o111 and str(tmp_path / "state") in wrapper.read_text()
        assert set(uninstall_chrome_tab_names(tmp_path / "data")) == {
            manifest,
            tmp_path / "data" / "chrome-tab-names",
        }
        assert not manifest.exists()

    def test_relative_directories_register_absolute_paths(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        monkeypatch.chdir(tmp_path)
        _extension, manifest = install_chrome_tab_names(Path("data"), Path("state"))
        wrapper = Path(json.loads(manifest.read_text())["path"])
        assert wrapper.is_absolute()
        assert str((tmp_path / "state").resolve()) in wrapper.read_text()


NODE_TEST = r"""
const groups = {
  1: { title: "Claude" },          // an agent's, default title: rename
  2: { title: "My research" },     // renamed by a person: leave
  3: { title: "Claude" },          // default, but not proven to be the agent's: leave
  4: { title: "⌛Claude" },        // working prefix kept
};
const tabs = { 1: [{ id: 10 }], 2: [{ id: 20 }], 3: [{ id: 99 }], 4: [{ id: 40 }] };
const updates = [];
const api = {
  runtime: { sendNativeMessage: async () => ({ groups: [
    { group: 1, tabs: [10], title: "Contract Desk" },
    { group: 2, tabs: [20], title: "Ada" },
    { group: 3, tabs: [30], title: "Founder Desk" },
    { group: 4, tabs: [40], title: "Regional Desk" },
    { group: 5, tabs: [50], title: "Gone" },
  ] }) },
  tabGroups: {
    get: async (id) => { if (!groups[id]) throw new Error("no group"); return groups[id]; },
    update: async (id, change) => { updates.push([id, change.title]); },
  },
  tabs: { query: async ({ groupId }) => tabs[groupId] ?? [] },
};
const listeners = {};
const on = (name) => ({ addListener: (fn) => { listeners[name] = fn; } });
globalThis.chrome = {
  ...api,
  tabGroups: { ...api.tabGroups, onCreated: on("created"), onUpdated: on("groupUpdated") },
  tabs: { ...api.tabs, onUpdated: on("tabUpdated") },
  alarms: { create: () => {}, onAlarm: on("alarm") },
  runtime: { ...api.runtime, onStartup: on("startup"), onInstalled: on("installed") },
};
const { nameGroups: named } = await import("BACKGROUND?listeners");
listeners.alarm({ name: "name-groups", scheduledTime: 0 });  // Chrome passes an Alarm
await new Promise((resolve) => setTimeout(resolve, 50));
const fromAlarm = updates.length;
updates.length = 0;
const renamed = await named(api);
const noHost = async () => { throw new Error("no host"); };
const offline = await named({ runtime: { sendNativeMessage: noHost } });
console.log(JSON.stringify({ renamed, updates, offline, fromAlarm }));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_extension_renames_only_proven_default_groups(tmp_path):
    background = hook_source("chrome_tab_names") / "background.js"
    script = tmp_path / "check.mjs"
    script.write_text(NODE_TEST.replace("BACKGROUND", background.as_uri()))
    done = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60, check=True
    )
    assert json.loads(done.stdout) == {
        "renamed": 2,
        "updates": [[1, "Contract Desk"], [4, "⌛Regional Desk"]],
        "offline": 0,
        "fromAlarm": 2,
    }
