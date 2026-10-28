"""Integrity regressions for the offline portable engine builder (no network)."""

import hashlib
import importlib.util
import io
import json
import stat
import subprocess
import sys
import tarfile
import tomllib
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


@pytest.mark.parametrize(
    "url,allowed",
    [
        ("https://release-assets.githubusercontent.com/asset?signature=example", True),
        ("https://files.pythonhosted.org/package.whl", True),
        ("http://github.com/asset", False),
        ("https://example.com/asset", False),
        ("https://github.com:8443/asset", False),
        ("https://user:password@github.com/asset", False),
        ("https://github.com.example.com/asset", False),
    ],
)
def test_download_redirect_is_checked_before_request(url, allowed):
    handler = engine.DownloadRedirectHandler()
    request = engine.urllib.request.Request("https://github.com/release")
    if allowed:
        redirected = handler.redirect_request(request, None, 302, "Found", {}, url)
        assert redirected.full_url == url
    else:
        with pytest.raises(ValueError, match="Unexpected download origin"):
            handler.redirect_request(request, None, 302, "Found", {}, url)


@pytest.mark.parametrize("tampered", [False, True])
def test_download_keeps_hash_check_and_cleans_temporary_files(tmp_path, monkeypatch, tampered):
    item = {
        "filename": "input.whl",
        "url": "https://files.pythonhosted.org/input.whl",
        "sha256": hashlib.sha256(b"expected").hexdigest(),
    }
    monkeypatch.setattr(engine, "input_items", lambda lock: [item])

    class Opener:
        def open(self, url, timeout):
            assert url == item["url"] and timeout == 60
            return io.BytesIO(b"tampered" if tampered else b"expected")

    def opener(handler):
        assert isinstance(handler, engine.DownloadRedirectHandler)
        return Opener()

    monkeypatch.setattr(engine.urllib.request, "build_opener", opener)
    inputs = tmp_path / "inputs"
    if tampered:
        with pytest.raises(ValueError, match="Hash mismatch"):
            engine.fetch_inputs(inputs, {})
        assert list(inputs.iterdir()) == []
    else:
        engine.fetch_inputs(inputs, {})
        assert (inputs / item["filename"]).read_bytes() == b"expected"
        assert len(list(inputs.iterdir())) == 1


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


def test_manifest_link_is_rejected_before_reading(tmp_path):
    root = payload(tmp_path)
    manifest = root / "engine-manifest.json"
    external = tmp_path / "external.json"
    manifest.rename(external)
    manifest.symlink_to(external)
    with pytest.raises(ValueError, match="non-symlink"):
        engine.verify_payload(root)
    with pytest.raises(ValueError, match="symlinks"):
        engine.inventory(root)


@pytest.mark.parametrize("text", [True, False])
def test_failed_commands_preserve_both_diagnostic_streams(tmp_path, capsys, text):
    with pytest.raises(subprocess.CalledProcessError):
        engine.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import os;os.write(1,b'detail\\xff');os.write(2,b'cause\\xfe');"
                "raise SystemExit(9)",
            ],
            cwd=tmp_path,
            text=text,
        )
    captured = capsys.readouterr()
    assert "detail" in captured.err and "cause" in captured.err


def notice_fixture(tmp_path, monkeypatch, *, missing=False):
    source = tmp_path / "tooling"
    (source / "notices").mkdir(parents=True)
    (source / "notices/LICENSE.txt").write_text("notice")
    members = [
        ("python/PYTHON.json", tarfile.REGTYPE, '{"python_version":"3.13"}'),
        ("python/install/bin/python3", tarfile.REGTYPE, "binary"),
    ]
    if not missing:
        members.append(("python/licenses/LICENSE.txt", tarfile.REGTYPE, "notice"))
    path = archive(tmp_path, members)
    metadata = {
        "python_version": "3.13",
        "full_archive_sha256": engine.digest(path),
        "install_only_archive_sha256": "a" * 64,
        "files": [
            {
                "path": "LICENSE.txt",
                "source": "licenses/LICENSE.txt",
                "bytes": 6,
                "sha256": hashlib.sha256(b"notice").hexdigest(),
            }
        ],
        "components": [{"license_paths": ["licenses/LICENSE.txt"]}],
        "install_only_comparison": [
            {"path": "bin/python3", "sha256": hashlib.sha256(b"binary").hexdigest()}
        ],
    }
    engine.write_json(source / "python-notices.json", metadata)
    native = {
        "schema_version": 1,
        "complete_compiled_component_attestation": False,
        "target": "aarch64-apple-darwin",
        "python_archive_sha256": "a" * 64,
        "python_version": "3.13",
        "rust_roots": [],
        "wheels": [],
        "sources": [{"id": "cpython", "version": "3.13"}],
        "components": [],
        "files": [],
    }
    engine.write_json(source / "native-notices.json", native)
    monkeypatch.setattr(engine, "ROOT", source)
    return source, path, metadata


@pytest.mark.parametrize(
    "change", [None, "hash", "member", "bytes", "retained", "binary", "version"]
)
def test_upstream_notice_archive_verification(tmp_path, monkeypatch, capsys, change):
    source, path, metadata = notice_fixture(tmp_path, monkeypatch, missing=change == "member")
    if change == "hash":
        metadata["full_archive_sha256"] = "0" * 64
    elif change == "bytes":
        metadata["files"][0]["bytes"] = 1
    elif change == "retained":
        (source / "notices/LICENSE.txt").write_text("changed")
    elif change == "binary":
        metadata["install_only_comparison"][0]["sha256"] = "0" * 64
    elif change == "version":
        metadata["python_version"] = "different"
    engine.write_json(source / "python-notices.json", metadata)
    if change:
        with pytest.raises((ValueError, subprocess.CalledProcessError)):
            engine.verify_upstream_notices(path)
        if change == "member":
            assert "LICENSE.txt" in capsys.readouterr().err
    else:
        assert engine.verify_upstream_notices(path) == {
            "notices_verified": 1,
            "binary_samples_verified": 1,
        }


@pytest.mark.parametrize("missing", [False, True])
def test_package_notice_inventory_requires_notices(tmp_path, monkeypatch, missing):
    rows = [
        {
            "name": "demo",
            "version": "1",
            "license_expression": "MIT",
            "included_license_paths": [] if missing else [str(tmp_path / "licenses/LICENSE")],
        }
    ]
    monkeypatch.setattr(engine, "python_run", lambda *a, **kw: json.dumps(rows))
    if missing:
        with pytest.raises(ValueError, match="Missing package notices: demo"):
            engine.package_notices(tmp_path, Path("python"), {})
    else:
        assert engine.package_notices(tmp_path, Path("python"), {})[0][
            "included_license_paths"
        ] == ["licenses/LICENSE"]


@pytest.mark.parametrize("change", [None, "hash", "missing", "component"])
def test_notice_copy_checks_sources_and_component_paths(tmp_path, monkeypatch, change):
    source, _, metadata = notice_fixture(tmp_path, monkeypatch)
    if change == "hash":
        (source / "notices/LICENSE.txt").write_text("tampered")
    elif change == "missing":
        (source / "notices/LICENSE.txt").unlink()
    elif change == "component":
        metadata["components"][0]["license_paths"] = ["licenses/absent"]
        engine.write_json(source / "python-notices.json", metadata)
    root = tmp_path / "payload"
    root.mkdir()
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    for name in ("LICENSES.txt", "BUILD.json"):
        (inputs / name).write_text(name)
    lock = {
        "target": "aarch64-apple-darwin",
        "runtime_wheels": [],
        "python": {"sha256": "a" * 64, "version": "3.13"},
        "tmux": {n: {"filename": n} for n in ("LICENSES.txt", "BUILD.json")},
    }
    monkeypatch.setattr(engine, "package_notices", lambda *a: [])
    if change:
        with pytest.raises(ValueError):
            engine.add_notices(root, inputs, lock, Path("python"), {})
    else:
        engine.add_notices(root, inputs, lock, Path("python"), {})
        result = json.loads((root / "engine-license-inventory.json").read_text())
        assert result["upstream_python"]["components"][0]["license_paths"] == [
            "licenses/python/LICENSE.txt"
        ]
        assert (root / "licenses/python/LICENSE.txt").read_text() == "notice"
        assert (root / "licenses/tmux/LICENSES.txt").read_text() == "LICENSES.txt"


def test_build_executes_snapshot_when_checkout_changes(tmp_path, monkeypatch, capsys):
    source, _, _ = notice_fixture(tmp_path, monkeypatch)
    script = source / "build.py"
    script.write_text(
        "from pathlib import Path\ndef main(*, snapshot):\n assert snapshot\n"
        " print(Path(__file__).with_name('lock.json').read_text())\n"
    )
    lock = source / "lock.json"
    lock.write_text('{"version":"original"}')
    monkeypatch.setattr(engine, "__file__", str(script))
    monkeypatch.setattr(engine, "LOCK", lock)
    original_run = engine.run

    def changed_checkout(argv, **kwargs):
        script.write_text("raise RuntimeError('new checkout must not execute')")
        lock.write_text('{"version":"replacement"}')
        (source / "python-notices.json").write_text("changed")
        (source / "notices/LICENSE.txt").unlink()
        copied_script = Path(argv[5])
        assert (
            json.loads((copied_script.parent / "python-notices.json").read_text())["python_version"]
            == "3.13"
        )
        assert (copied_script.parent / "notices/LICENSE.txt").read_text() == "notice"
        return original_run(argv, **kwargs)

    monkeypatch.setattr(engine, "run", changed_checkout)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(script),
            "build",
            "--commit",
            "a" * 40,
            "--inputs",
            str(tmp_path),
            "--output",
            str(tmp_path / "out"),
        ],
    )
    engine.main()
    assert json.loads(capsys.readouterr().out) == {"version": "original"}


def test_wrong_host_does_not_create_output(tmp_path, monkeypatch):
    monkeypatch.setattr(engine.platform, "system", lambda: "Linux")
    with pytest.raises(ValueError, match="native Apple Silicon"):
        engine.build(tmp_path, "a" * 40, tmp_path, tmp_path / "output", {})
    assert not (tmp_path / "output").exists()


def test_retained_native_notices_cover_pinned_native_wheels():
    lock = json.loads(engine.LOCK.read_text())
    files = engine.native_notice_material(lock)
    metadata = json.loads(files["native-notices.json"])
    components = {item["component"] for item in metadata["components"]}
    assert {"libuv", "llhttp", "Tk", "Tcl Thread", "Itcl", "HACL*"} <= components
    assert metadata["complete_compiled_component_attestation"] is False
    assert {r["source_id"] for r in metadata["rust_roots"]} == {
        "cryptography",
        "pydantic-core",
        "watchfiles",
        "ironcalc",
    }


@pytest.mark.parametrize("change", ["wheel", "added", "target", "python"])
def test_native_notice_inventory_cannot_follow_changed_binary_inputs(change):
    lock = json.loads(engine.LOCK.read_text())
    if change == "wheel":
        next(w for w in lock["runtime_wheels"] if w["name"] == "uvloop")["sha256"] = "0" * 64
    elif change == "added":
        lock["runtime_wheels"].append({"name": "new-native", "filename": "new-native.so.whl"})
    elif change == "target":
        lock["target"] = "x86_64-apple-darwin"
    else:
        lock["python"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="Native notice"):
        engine.native_notice_material(lock)


@pytest.mark.parametrize(
    "change",
    [
        "bytes",
        "slice",
        "source",
        "attestation",
        "missing_hash",
        "source_version",
        "empty_notices",
        "missing_crate",
    ],
)
def test_native_notice_tampering_refused_before_snapshot(tmp_path, monkeypatch, change):
    import shutil

    metadata = json.loads((engine.ROOT / "native-notices.json").read_text())
    (tmp_path / "notices").mkdir()
    for item in metadata["files"]:
        shutil.copyfile(engine.ROOT / "notices" / item["path"], tmp_path / "notices" / item["path"])
    if change == "bytes":
        (tmp_path / "notices" / metadata["files"][0]["path"]).write_bytes(b"changed")
    elif change == "slice":
        metadata["components"][0]["notice"]["offset"] += 1
    elif change == "source":
        metadata["components"][0]["source_id"] = "absent"
    elif change == "attestation":
        metadata["complete_compiled_component_attestation"] = True
    elif change == "missing_hash":
        metadata["wheels"][0].pop("sha256")
    elif change == "source_version":
        metadata["wheels"][0]["version"] = "999"
    elif change == "empty_notices":
        metadata["components"][0] = {**metadata["components"][0], "notices": []}
    else:
        metadata["components"] = [
            c
            for c in metadata["components"]
            if c["source_id"] != metadata["rust_roots"][0]["registry_packages"][0]
        ]
    engine.write_json(tmp_path / "native-notices.json", metadata)
    monkeypatch.setattr(engine, "ROOT", tmp_path)
    with pytest.raises(ValueError):
        engine.native_notice_material()


@pytest.mark.parametrize("change", [None, "commit", "repository", "omitted_notice"])
def test_source_verifier_checks_upstream_crate_notice_correspondence(tmp_path, monkeypatch, change):
    import runpy

    verifier = runpy.run_path(str(PATH.with_name("verify_native_sources.py")))
    verify = verifier["verify_sources"]
    sha = "a" * 40
    raw = tmp_path / "upstream-license"
    raw.write_text("MIT license text")
    python_source = archive(
        tmp_path,
        [
            (
                "Python-3.13.18/Include/patchlevel.h",
                tarfile.REGTYPE,
                "#define PY_MAJOR_VERSION 3\n#define PY_MINOR_VERSION 13\n"
                "#define PY_MICRO_VERSION 18\n",
            )
        ],
    )
    python_source.rename(tmp_path / "python.tar")
    crate_members = [
        (
            "demo-1.0.0/Cargo.toml",
            tarfile.REGTYPE,
            '[package]\nname="demo"\nversion="1.0.0"\nlicense="MIT"\nrepository="https://github.com/acme/demo"\n',
        ),
        ("demo-1.0.0/.cargo_vcs_info.json", tarfile.REGTYPE, json.dumps({"git": {"sha1": sha}})),
    ]
    if change == "omitted_notice":
        crate_members.append(("demo-1.0.0/NOTICE", tarfile.REGTYPE, "required attribution"))
    crate = archive(tmp_path, crate_members)
    crate.rename(tmp_path / "crate.tar")
    data = {
        "wheels": [],
        "python_version": "3.13.18",
        "rust_roots": [],
        "sources": [
            {
                "id": "cpython",
                "filename": "python.tar",
                "sha256": engine.digest(tmp_path / "python.tar"),
            },
            {
                "id": "crate:demo-1.0.0",
                "filename": "crate.tar",
                "sha256": engine.digest(tmp_path / "crate.tar"),
                "name": "demo",
                "version": "1.0.0",
            },
            {
                "id": "raw",
                "filename": raw.name,
                "sha256": engine.digest(raw),
                "format": "raw",
                "git_commit": "b" * 40 if change == "commit" else sha,
                "url": "https://raw.githubusercontent.com/"
                + ("other/demo" if change == "repository" else "acme/demo")
                + "/"
                + sha
                + "/LICENSE",
            },
        ],
        "components": [
            {
                "kind": "rust-source-lock-package",
                "component": "demo-1.0.0",
                "source_id": "crate:demo-1.0.0",
                "declared_license": "MIT",
                "notices": [
                    {
                        "source_id": "raw",
                        "source_member": None,
                        "bytes": raw.stat().st_size,
                        "sha256": engine.digest(raw),
                    }
                ],
            }
        ],
    }
    monkeypatch.setitem(
        verify.__globals__,
        "native_notice_material",
        lambda lock: {"native-notices.json": json.dumps(data).encode()},
    )
    if change:
        with pytest.raises(ValueError, match="notice"):
            verify(tmp_path)
    else:
        assert verify(tmp_path)["notices_verified"] == 1


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


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS builder publication")
def test_atomic_publication_moves_source_to_vacant_destination(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "contents").write_text("candidate")
    target = tmp_path / "target"
    engine.publish_exclusively(source, target)
    assert not source.exists()
    assert (target / "contents").read_text() == "candidate"


@pytest.mark.parametrize(
    "change", [None, "extra_dependency", "constraint", "version", "edge", "marker"]
)
def test_project_requirement_changes_require_relocking(tmp_path, change):
    project = PATH.parents[2] / "pyproject.toml"
    lock = PATH.parents[2] / "uv.lock"
    project_text = project.read_text()
    lock_text = lock.read_text()
    if change == "extra_dependency":
        project_text = project_text.replace(
            'postgres = ["asyncpg>=0.29"]', 'postgres = ["asyncpg>=0.29", "new-driver>=1"]'
        )
    elif change == "constraint":
        project_text = project_text.replace('"asyncpg>=0.29"', '"asyncpg>=100"')
    elif change == "version":
        version = tomllib.loads(project_text)["project"]["version"]
        project_text = project_text.replace(f'version = "{version}"', 'version = "9.9.9"')
    elif change == "edge":
        lock_text = lock_text.replace('    { name = "asyncpg" },\n', "", 1)
    elif change == "marker":
        project_text = project_text.replace(
            '"asyncpg>=0.29"', "\"asyncpg>=0.29; python_version > '3.11'\""
        )
    (tmp_path / "pyproject.toml").write_text(project_text)
    (tmp_path / "uv.lock").write_text(lock_text)
    if change:
        with pytest.raises(ValueError):
            engine.validate_project_lock(tmp_path)
    else:
        engine.validate_project_lock(tmp_path)


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
