"""Native session selection is metadata-only and checked before TUI launch."""

import json
from types import SimpleNamespace

import pytest

from agent_backbone.services.runtimes import RUNTIMES, _opencode_v2_launch


def test_explicit_effort_preserves_literal_model_tags():
    runtime = RUNTIMES["opencode"]
    assert runtime.split_model("provider/model:high") == ("provider/model:high", None)
    assert runtime.split_model("provider/model:high::high") == ("provider/model:high", "high")
    with pytest.raises(ValueError, match="requires an effort"):
        runtime.split_model("provider/model::")


@pytest.mark.parametrize("resume", [False, True])
def test_native_selection_is_verified_before_returning_session(monkeypatch, resume):
    target = {"providerID": "provider", "id": "model:high", "variant": "high"}
    session = {"id": "ses_example", "model": target}
    catalog = {"data": [{**target, "variants": [{"id": "high"}]}]}
    operations = []

    def run(command, **kwargs):
        assert command[:3] == ["/bin/opencode", "api", "--standalone"]
        operation = command[3]
        operations.append(operation)
        if operation in ("session.create", "session.switchModel"):
            assert json.loads(command[command.index("--data") + 1])["model"] == target
        result = catalog if operation == "model.list" else {"data": session}
        return SimpleNamespace(returncode=0, stdout=json.dumps(result))

    monkeypatch.setattr(_opencode_v2_launch.subprocess, "run", run)
    assert (
        _opencode_v2_launch.prepare_session(
            "/bin/opencode",
            "provider/model:high",
            "high",
            session_id="ses_example" if resume else None,
        )
        == "ses_example"
    )
    assert operations == (
        ["model.list", "session.get", "session.switchModel", "session.get"]
        if resume
        else ["model.list", "session.create", "session.get"]
    )


def test_unknown_variant_does_not_mutate_a_session(monkeypatch):
    operations = []

    def api(binary, operation, **kwargs):
        operations.append(operation)
        return {"data": [{"providerID": "provider", "id": "model", "variants": [{"id": "low"}]}]}

    monkeypatch.setattr(_opencode_v2_launch, "api", api)
    with pytest.raises(RuntimeError, match="has no variant 'high'"):
        _opencode_v2_launch.prepare_session("/bin/opencode", "provider/model", "high")
    assert operations == ["model.list"]


def test_changed_selection_is_not_reported_as_success(monkeypatch):
    responses = iter(
        [
            {"data": [{"providerID": "provider", "id": "model", "variants": [{"id": "high"}]}]},
            {"data": {"id": "ses_example"}},
            {
                "data": {
                    "id": "ses_example",
                    "model": {"providerID": "provider", "id": "model", "variant": "low"},
                }
            },
        ]
    )
    monkeypatch.setattr(_opencode_v2_launch, "api", lambda *args, **kwargs: next(responses))
    with pytest.raises(RuntimeError, match="did not retain"):
        _opencode_v2_launch.prepare_session("/bin/opencode", "provider/model", "high")


@pytest.mark.parametrize("resume", [False, "ses_existing"])
def test_v2_command_uses_selected_session_and_keeps_resume_brief_suppressed(
    monkeypatch, tmp_path, resume
):
    runtime = RUNTIMES["opencode"]
    monkeypatch.setattr(type(runtime), "installed_version", lambda self: "2.1.4")
    monkeypatch.setattr(
        "agent_backbone.services.runtimes.base.resolve_command", lambda name: "/bin/opencode"
    )
    brief = tmp_path / "brief"
    brief.write_text("A fresh brief")
    command = runtime.build_command(
        model="provider/model:high::high",
        resume=resume,
        brief_file=brief,
        data_dir=tmp_path,
        state_dir=tmp_path / "state",
        unattended=True,
    )
    assert command[2] == "--v2"
    plugin = tmp_path / "hooks/opencode-v2/index.js"
    assert plugin.exists()
    selection = next(arg for arg in command if arg.startswith('{"model"'))
    assert json.loads(selection) == {
        "model": "provider/model:high",
        "effort": "high",
        "resume": resume,
    }
    native = command[command.index("/bin/opencode") :]
    assert native[:3] == ["/bin/opencode", "--standalone", "--auto"]
    assert "--model" not in native and "--session" not in native
    assert ("--prompt" in native) is (not resume)


def test_legacy_version_refuses_explicit_effort(monkeypatch):
    runtime = RUNTIMES["opencode"]
    monkeypatch.setattr(type(runtime), "installed_version", lambda self: "1.19.4")
    with pytest.raises(RuntimeError, match="requires @opencode/cli 2.1.4"):
        runtime.check_effort("high")


def test_exact_resume_cannot_silently_create_a_missing_session(monkeypatch):
    monkeypatch.setattr(_opencode_v2_launch, "api", lambda *args, **kwargs: {"data": None})
    executions = []
    monkeypatch.setattr(_opencode_v2_launch.os, "execv", lambda *args: executions.append(args))
    with pytest.raises(RuntimeError, match="requested session"):
        _opencode_v2_launch.launch({"resume": "ses_missing"}, ["/bin/opencode", "--standalone"])
    assert executions == []
