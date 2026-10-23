"""Native session selection is metadata-only and checked before TUI launch."""

import json
from contextlib import nullcontext
from functools import partial
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
        assert command[:4] == ["/bin/opencode", "api", "--server", "http://127.0.0.1:1234"]
        assert "timeout" not in kwargs
        operation = command[4]
        if operation == "model.list":
            assert any(arg.startswith("location[directory]=") for arg in command)
        operations.append(operation)
        if operation in ("session.create", "session.switchModel"):
            assert json.loads(command[command.index("--data") + 1])["model"] == target
        result = catalog if operation == "model.list" else {"data": session}
        return SimpleNamespace(returncode=0, stdout=json.dumps(result))

    monkeypatch.setattr(_opencode_v2_launch.subprocess, "run", run)
    assert (
        _opencode_v2_launch.prepare_session(
            partial(
                _opencode_v2_launch.api, "/bin/opencode", server="http://127.0.0.1:1234", env={}
            ),
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

    def api(operation, **kwargs):
        operations.append(operation)
        return {"data": [{"providerID": "provider", "id": "model", "variants": [{"id": "low"}]}]}

    with pytest.raises(RuntimeError, match="has no variant 'high'"):
        _opencode_v2_launch.prepare_session(api, "provider/model", "high")
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
    removed = []

    def request(operation, **kwargs):
        if operation == "session.remove":
            removed.append(kwargs["session_id"])
            return None
        return next(responses)

    with pytest.raises(RuntimeError, match="did not retain"):
        _opencode_v2_launch.prepare_session(request, "provider/model", "high")
    assert removed == ["ses_example"]


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
    monkeypatch.setattr(
        _opencode_v2_launch,
        "standalone",
        lambda binary: nullcontext(lambda *args, **kwargs: {"data": None}),
    )
    executions = []
    monkeypatch.setattr(_opencode_v2_launch.os, "execv", lambda *args: executions.append(args))
    with pytest.raises(RuntimeError, match="requested session"):
        _opencode_v2_launch.launch({"resume": "ses_missing"}, ["/bin/opencode", "--standalone"])
    assert executions == []


def test_v2_does_not_claim_legacy_flag_enables_plan_approval(monkeypatch):
    runtime = RUNTIMES["opencode"]
    monkeypatch.setattr(type(runtime), "installed_version", lambda self: "2.1.4")
    reason = runtime.plan_approval_needs({"OPENCODE_EXPERIMENTAL_PLAN_MODE": "1"})
    assert "unavailable on OpenCode v2" in reason


@pytest.mark.parametrize("version", [None, "unrecognized version"])
def test_unknown_version_refuses_to_guess_launch_protocol(monkeypatch, version):
    runtime = RUNTIMES["opencode"]
    monkeypatch.setattr(type(runtime), "installed_version", lambda self: version)
    monkeypatch.setattr(type(runtime), "available", lambda self: True)
    with pytest.raises(RuntimeError, match="Could not determine the OpenCode version"):
        runtime.build_command()


def test_continue_without_history_starts_fresh(monkeypatch):
    monkeypatch.setattr(
        _opencode_v2_launch,
        "standalone",
        lambda binary: nullcontext(lambda *args, **kwargs: {"data": []}),
    )
    selected = []

    def prepare(request, model, effort, *, session_id):
        selected.append(session_id)
        return "ses_new"

    monkeypatch.setattr(_opencode_v2_launch, "prepare_session", prepare)
    executed = []
    monkeypatch.setattr(_opencode_v2_launch.os, "execv", lambda *args: executed.append(args))
    _opencode_v2_launch.launch({"resume": True, "model": "provider/model"}, ["/bin/opencode"])
    assert selected == [None]
    assert executed == [("/bin/opencode", ["/bin/opencode", "--session", "ses_new"])]


@pytest.mark.parametrize("final", ["completed", "error"])
def test_metadata_waits_for_native_migration_on_one_server(monkeypatch, final):
    import io

    monkeypatch.setenv("BACKBONE_AGENT", "app")
    monkeypatch.setenv("BACKBONE_STATE_DIR", "/example/state")
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", '{"plugins":["example-provider"]}')
    server = SimpleNamespace(
        stdin=io.StringIO(),
        stdout=io.StringIO('{"url":"http://127.0.0.1:1234"}\n'),
        wait=lambda **kwargs: 0,
    )
    launched = []

    def start(command, **kwargs):
        assert command == [
            "/bin/opencode",
            "serve",
            "--stdio",
            "--hostname",
            "127.0.0.1",
            "--port",
            "0",
        ]
        assert kwargs["env"]["OPENCODE_PASSWORD"]
        assert "BACKBONE_AGENT" not in kwargs["env"]
        assert "BACKBONE_STATE_DIR" not in kwargs["env"]
        assert kwargs["env"]["OPENCODE_CONFIG_CONTENT"] == '{"plugins":["example-provider"]}'
        launched.append(command)
        return server

    monkeypatch.setattr(_opencode_v2_launch.subprocess, "Popen", start)
    monkeypatch.setattr(_opencode_v2_launch.time, "sleep", lambda delay: None)
    states = iter(["required", "running", final])
    calls = []

    def status(endpoint, password):
        assert endpoint == "http://127.0.0.1:1234" and password
        calls.append("migration")
        return {"status": next(states)}

    monkeypatch.setattr(_opencode_v2_launch, "migration_status", status)
    entered = False
    try:
        with _opencode_v2_launch.standalone("/bin/opencode"):
            entered = True
            assert len(calls) == 3
    except RuntimeError as exc:
        assert final == "error" and "migration failed" in str(exc)
    assert entered is (final == "completed")
    assert len(launched) == 1
    assert server.stdin.closed


def test_missing_binary_is_distinct_from_unknown_version(monkeypatch):
    runtime = RUNTIMES["opencode"]
    monkeypatch.setattr("agent_backbone.services.runtimes.base.resolve_command", lambda name: None)

    def version(self):
        pytest.fail("a missing executable must not be queried for its version")

    monkeypatch.setattr(type(runtime), "installed_version", version)
    with pytest.raises(RuntimeError, match="binary not found: opencode"):
        runtime.build_command()
