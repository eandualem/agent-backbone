"""Integrity regressions for the offline portable engine builder (no network)."""

import hashlib
import importlib.util
import io
import json
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[3] / "tools/engine/build.py"
spec = importlib.util.spec_from_file_location("engine_build", PATH)
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)


def archive(tmp_path, members):
    path = tmp_path / "input.tar"
    with tarfile.open(path, "w") as tar:
        for name, kind, content in members:
            item = tarfile.TarInfo(name)
            item.type = kind
            if kind == tarfile.REGTYPE:
                raw = content.encode()
                item.size = len(raw)
                tar.addfile(item, io.BytesIO(raw))
            else:
                item.linkname = content
                tar.addfile(item)
    return path


@pytest.mark.parametrize(
    "members",
    [
        [("../escape", tarfile.REGTYPE, "no")],
        [("/escape", tarfile.REGTYPE, "no")],
        [("x", tarfile.FIFOTYPE, "")],
        [("x", tarfile.REGTYPE, "a"), ("x", tarfile.REGTYPE, "b")],
        [("x", tarfile.SYMTYPE, "../escape")],
        [("x", tarfile.SYMTYPE, "/etc/passwd")],
        [("x", tarfile.SYMTYPE, "dir"), ("x/y", tarfile.REGTYPE, "no")],
        [("x", tarfile.REGTYPE, "a"), ("y", tarfile.SYMTYPE, "x"), ("z", tarfile.SYMTYPE, "y")],
    ],
)
def test_reject_unsafe_archive(tmp_path, members):
    with pytest.raises(ValueError):
        engine.extract_archive(archive(tmp_path, members), tmp_path / "output")
    assert not (tmp_path / "escape").exists()


def test_materializes_internal_link_without_symlink(tmp_path):
    path = archive(
        tmp_path,
        [
            ("python/bin/python3.13", tarfile.REGTYPE, "binary"),
            ("python/bin/python3", tarfile.SYMTYPE, "python3.13"),
        ],
    )
    output = tmp_path / "output"
    engine.extract_archive(path, output)
    link = output / "python/bin/python3"
    assert link.read_text() == "binary"
    assert not link.is_symlink()
    with pytest.raises(ValueError, match="already exists"):
        engine.extract_archive(path, output)


def test_isolation_drops_caller_configuration(tmp_path, monkeypatch):
    for key in (
        "BACKBONE_API_KEY",
        "TMUX",
        "PYTHONPATH",
        "PIP_CONFIG_FILE",
        "DYLD_LIBRARY_PATH",
        "GIT_DIR",
    ):
        monkeypatch.setenv(key, "untrusted")
    env = engine.isolated_env(tmp_path / "home", 123)
    assert not set(env.values()) & {"untrusted"}
    assert env["HOME"] == str(tmp_path / "home")
    assert env["SOURCE_DATE_EPOCH"] == "123"
    assert env["PIP_CONFIG_FILE"] == "/dev/null"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert stat.S_IMODE((tmp_path / "home").stat().st_mode) == 0o700


def test_input_tamper_and_symlinks_refused(tmp_path):
    path = tmp_path / "input"
    path.write_text("one")
    expected = {"sha256": hashlib.sha256(b"one").hexdigest()}
    engine.verify_input(path, expected)
    path.write_text("two")
    with pytest.raises(ValueError, match="Hash mismatch"):
        engine.verify_input(path, expected)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="regular"):
        engine.verify_input(link, expected)


def payload(tmp_path):
    root = tmp_path / "payload"
    root.mkdir()
    (root / "data").write_text("contents")
    (root / "bin").mkdir()
    (root / "bin/backbone").write_text("launcher")
    (root / "bin/backbone").chmod(0o755)
    (root / "libexec").mkdir()
    (root / "libexec/tmux").write_text("tmux")
    engine.write_json(root / "engine-build-receipt.json", {"source_commit": "a" * 40})
    metadata = root / "python/lib/python3.13/site-packages/agent_backbone-1.0.dist-info/METADATA"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("Name: agent-backbone\nVersion: 1.0\n")
    engine.write_json(
        root / "engine-license-inventory.json",
        {
            "upstream_python": {
                "python_version": "3.13.18",
                "upstream_release": "20261026",
                "install_only_archive_sha256": "b" * 64,
            }
        },
    )
    engine.write_json(root / "engine-file-inventory-v1.json", engine.inventory(root))
    checksum = engine.digest(root / "engine-file-inventory-v1.json")
    engine.write_json(
        root / "engine-manifest.json",
        {
            "schema_version": 1,
            "release_ready": False,
            "status": "private_integration",
            "backbone": {"source": "a" * 40, "version": "1.0"},
            "target": "aarch64-apple-darwin",
            "payload_id": "engine-" + "a" * 12 + "-aarch64-apple-darwin-" + checksum[:12],
            "entrypoint": "bin/backbone",
            "tmux": {"path": "libexec/tmux", "sha256": engine.digest(root / "libexec/tmux")},
            "protocols": {"runtime_discovery": 1, "setup": 1},
            "python": {
                "version": "3.13.18",
                "upstream_release": "20261026",
                "archive_sha256": "b" * 64,
            },
            "files": {"path": "engine-file-inventory-v1.json", "sha256": checksum},
            "licenses": {
                "path": "engine-license-inventory.json",
                "sha256": engine.digest(root / "engine-license-inventory.json"),
            },
        },
    )
    return root


@pytest.mark.parametrize("mutation", ["addition", "removal", "content", "mode", "link"])
def test_inventory_is_exact_in_both_directions(tmp_path, mutation):
    root = payload(tmp_path)
    engine.verify_payload(root)
    path = root / "data"
    if mutation == "addition":
        (root / "unexpected.pyc").write_bytes(b"cache")
    elif mutation == "removal":
        path.unlink()
    elif mutation == "content":
        path.write_text("changed")
    elif mutation == "mode":
        path.chmod(0o777)
    else:
        (root / "link").symlink_to(path)
    with pytest.raises(ValueError):
        engine.verify_payload(root)


def test_manifest_cannot_escape_payload(tmp_path):
    root = payload(tmp_path)
    path = root / "engine-manifest.json"
    manifest = json.loads(path.read_text())
    manifest["files"]["path"] = "../external"
    engine.write_json(path, manifest)
    with pytest.raises(ValueError, match="Invalid relative"):
        engine.verify_payload(root)


def test_wrong_host_does_not_create_output(tmp_path, monkeypatch):
    monkeypatch.setattr(engine.platform, "system", lambda: "Linux")
    with pytest.raises(ValueError, match="native Apple Silicon"):
        engine.build(tmp_path, "a" * 40, tmp_path, tmp_path / "output", {})
    assert not (tmp_path / "output").exists()


def test_build_refuses_runtime_input_drift_from_source_lock(tmp_path):
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname="agent-backbone"\nversion="1.0"\ndependencies=[{name="demo"}]\n'
        "[package.optional-dependencies]\npostgres=[]\n"
        '[[package]]\nname="demo"\nversion="1.0"\nwheels=[{hash="sha256:expected"}]\n'
    )
    item = {"name": "demo", "version": "1.0", "sha256": "expected"}
    engine.validate_source_lock(tmp_path, {"runtime_wheels": [item]})
    with pytest.raises(ValueError, match="differs from source"):
        engine.validate_source_lock(tmp_path, {"runtime_wheels": [{**item, "sha256": "different"}]})


@pytest.mark.parametrize("change", [None, "missing", "added", "changed"])
def test_installed_package_matches_built_wheel_exactly(tmp_path, change):
    wheel = tmp_path / "package.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("agent_backbone/__init__.py", "version = 1")
    package = tmp_path / "payload/python/lib/python3.13/site-packages/agent_backbone"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("version = 1")
    if change == "missing":
        (package / "__init__.py").unlink()
    elif change == "added":
        (package / "unexpected.py").write_text("unexpected")
    elif change == "changed":
        (package / "__init__.py").write_text("changed")
    if change:
        with pytest.raises(ValueError, match="differs from wheel"):
            engine.verify_wheel_package(tmp_path / "payload", wheel)
    else:
        assert engine.verify_wheel_package(tmp_path / "payload", wheel) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("release_ready", True),
        ("schema_version", 2),
        ("entrypoint", "data"),
        ("target", "another-platform"),
        ("payload_id", "forged"),
        ("tmux", {"path": "libexec/tmux", "sha256": "bad"}),
        ("python", {"version": "changed"}),
        ("backbone", {"source": "a" * 40, "version": "9.9.9"}),
    ],
)
def test_manifest_metadata_must_match_verified_candidate(tmp_path, field, value):
    root = payload(tmp_path)
    path = root / "engine-manifest.json"
    manifest = json.loads(path.read_text())
    manifest[field] = value
    engine.write_json(path, manifest)
    with pytest.raises(ValueError):
        engine.verify_payload(root)


@pytest.mark.parametrize("first,second", [("Name", "name"), ("caf\u00e9", "cafe\u0301")])
def test_archive_rejects_case_and_unicode_collisions(tmp_path, first, second):
    path = archive(tmp_path, [(first, tarfile.REGTYPE, "one"), (second, tarfile.SYMTYPE, first)])
    with pytest.raises(ValueError, match="Duplicate"):
        engine.extract_archive(path, tmp_path / "out")


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS builder publication")
@pytest.mark.parametrize("existing", ["directory", "symlink", "file"])
def test_atomic_publication_preserves_existing_destination(tmp_path, existing):
    source = tmp_path / "source"
    source.mkdir()
    (source / "contents").write_text("candidate")
    target = tmp_path / "target"
    if existing == "directory":
        target.mkdir()
    elif existing == "file":
        target.write_text("keep")
    else:
        target.symlink_to(source)
    before = target.lstat().st_ino
    with pytest.raises(FileExistsError):
        engine.publish_exclusively(source, target)
    assert target.lstat().st_ino == before
    assert (source / "contents").read_text() == "candidate"


def test_missing_record_file_fails_instead_of_disappearing(tmp_path, monkeypatch):
    payload = tmp_path / "payload"
    record = payload / "python/lib/python3.13/site-packages/demo.dist-info/RECORD"
    record.parent.mkdir(parents=True)
    record.write_text("missing.py,,\ndemo.dist-info/RECORD,,\n")
    monkeypatch.setattr(engine, "python_run", lambda *args, **kwargs: "{}")
    with pytest.raises(ValueError, match="Unexpected missing RECORD"):
        engine.normalize_python(payload, payload / "python/bin/python3", {})


def test_payload_rejects_build_path_leak(tmp_path):
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "script").write_text("#!" + str(tmp_path / "python"))
    with pytest.raises(ValueError, match="Local build path"):
        engine.reject_build_paths(payload, tmp_path)


def test_source_verification_detects_archive_transformations(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = engine.isolated_env(tmp_path / "home", 123)
    subprocess.run(["git", "init", str(repo)], env=env, capture_output=True, check=True)
    (repo / "file").write_text("committed")
    subprocess.run(["git", "-C", str(repo), "add", "file"], env=env, check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Example",
            "-c",
            "user.email=example@example.com",
            "commit",
            "-m",
            "source",
        ],
        env=env,
        capture_output=True,
        check=True,
    )
    commit = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], env=env, text=True
    ).strip()
    source = tmp_path / "source"
    source.mkdir()
    (source / "file").write_text("committed")
    engine.verify_source_tree(source, repo, commit, "git", env)
    (source / "file").write_text("transformed")
    with pytest.raises(ValueError, match="differs from Git blob"):
        engine.verify_source_tree(source, repo, commit, "git", env)


@pytest.mark.parametrize("missing", ["asyncpg", "greenlet", "httptools"])
def test_source_lock_requires_optional_runtime_dependency_closure(tmp_path, missing):
    rows = [
        '[[package]]\nname="agent-backbone"\nversion="1"\n'
        'dependencies=[{name="sqlalchemy",extra=["asyncio"]},{name="uvicorn",extra=["standard"]}]\n'
        '[package.optional-dependencies]\npostgres=[{name="asyncpg"}]\n'
    ]
    names = ["sqlalchemy", "uvicorn", "asyncpg", "greenlet", "httptools"]
    for name in names:
        row = f'[[package]]\nname="{name}"\nversion="1"\nwheels=[{{hash="sha256:{name}"}}]\n'
        if name == "sqlalchemy":
            row += '[package.optional-dependencies]\nasyncio=[{name="greenlet"}]\n'
        if name == "uvicorn":
            row += '[package.optional-dependencies]\nstandard=[{name="httptools"}]\n'
        rows.append(row)
    (tmp_path / "uv.lock").write_text("".join(rows))
    wheels = [{"name": name, "version": "1", "sha256": name} for name in names]
    engine.validate_source_lock(tmp_path, {"runtime_wheels": wheels})
    with pytest.raises(ValueError, match="dependency closure"):
        engine.validate_source_lock(
            tmp_path, {"runtime_wheels": [w for w in wheels if w["name"] != missing]}
        )
