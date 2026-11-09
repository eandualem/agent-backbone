import subprocess

import pytest

from agent_backbone.services.runtimes import _keychain, prepare_profile_keychain


@pytest.fixture
def security(monkeypatch):
    calls = []

    def run(argv, input, env, **kwargs):
        calls.append((argv, input.strip(), env["HOME"]))
        stdout = ""
        if input.strip() == "default-keychain -d user":
            stdout = f'    "{run.keychain}"\n'
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    run.keychain = ""
    monkeypatch.setattr(_keychain.sys, "platform", "darwin")
    monkeypatch.setattr(_keychain.subprocess, "run", run)
    return run, calls


def _profile(tmp_path):
    (tmp_path / ".env").write_text("BACKBONE_API_KEY=k\n")
    (tmp_path / ".env").chmod(0o600)
    return tmp_path, [tmp_path / "home", tmp_path / "native" / "claude" / "home"]


def test_every_home_selects_one_unlocked_profile_keychain(tmp_path, security):
    run, calls = security
    root, homes = _profile(tmp_path)
    run.keychain = root / "keychain" / "profile.keychain-db"
    path = prepare_profile_keychain(root, homes)
    assert path == run.keychain
    password = (root / ".env").read_text().split("BACKBONE_KEYCHAIN_PASSWORD=")[1].strip()
    commands = [command.split(" ")[0] for _, command, _ in calls]
    assert commands[:3] == ["create-keychain", "unlock-keychain", "set-keychain-settings"]
    assert {home for _, command, home in calls if "default-keychain -d user -s" in command} == {
        str(home) for home in homes
    }
    assert all(argv == ["/usr/bin/security", "-i"] for argv, _, _ in calls)
    assert all(password not in " ".join(argv) for argv, _, _ in calls)
    assert all((home / "Library" / "Preferences").is_dir() for home in homes)

    calls.clear()
    path.parent.mkdir(exist_ok=True)
    path.touch()
    prepare_profile_keychain(root, homes)
    assert (root / ".env").read_text().count("BACKBONE_KEYCHAIN_PASSWORD=") == 1
    assert f"unlock-keychain -p {password} " in calls[0][1]
    assert not any(command.startswith("create-keychain") for _, command, _ in calls)


def test_a_default_that_did_not_take_is_refused(tmp_path, security):
    run, _ = security
    run.keychain = "/elsewhere/login.keychain-db"
    with pytest.raises(ValueError, match="did not become the default"):
        prepare_profile_keychain(*_profile(tmp_path))


def test_the_password_gets_its_own_line_after_an_unterminated_value(tmp_path, security):
    run, _ = security
    root, homes = _profile(tmp_path)
    (root / ".env").write_text("BACKBONE_API_KEY=k")
    run.keychain = root / "keychain" / "profile.keychain-db"
    prepare_profile_keychain(root, homes)
    lines = (root / ".env").read_text().splitlines()
    assert lines[0] == "BACKBONE_API_KEY=k" and lines[1].startswith("BACKBONE_KEYCHAIN_PASSWORD=")


def test_failed_security_command_is_refused(tmp_path, monkeypatch, security):
    monkeypatch.setattr(
        _keychain.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 51, "", "not correct"),
    )
    with pytest.raises(ValueError, match="create-keychain failed"):
        prepare_profile_keychain(*_profile(tmp_path))


def test_other_platforms_need_no_keychain(tmp_path, monkeypatch):
    monkeypatch.setattr(_keychain.sys, "platform", "linux")
    root, homes = _profile(tmp_path)
    assert prepare_profile_keychain(root, homes) is None
    assert "KEYCHAIN" not in (root / ".env").read_text()
