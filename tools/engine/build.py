"""Build an offline, relocatable engine from a Git commit and pinned binary inputs.

This is release tooling, not an installed command. It does not activate an engine,
install a service, create credentials or launch agents.
"""

from __future__ import annotations

import argparse
import base64
import csv
import ctypes
import hashlib
import io
import json
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unicodedata
import urllib.parse
import urllib.request
import zipfile
from email.parser import Parser
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent
LOCK = ROOT / "aarch64-apple-darwin.json"
EXCLUDED = {"engine-manifest.json", "engine-file-inventory-v1.json"}


def require(condition, message):
    """Fail validation even when Python is run with optimization enabled."""
    if not condition:
        raise ValueError(message)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def relative_path(value):
    """Accept canonical relative POSIX paths, including for archive members."""
    path = PurePosixPath(value)
    require(
        isinstance(value, str)
        and value
        and not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in value
        and str(path) == value
        and value != ".",
        f"Invalid relative path: {value!r}",
    )
    return path


def verify_input(path, item):
    require(path.is_file() and not path.is_symlink(), f"Missing regular input: {path.name}")
    require(digest(path) == item["sha256"], f"Hash mismatch: {path.name}")


def extract_archive(archive, destination):
    """Extract only regular files/directories; materialize safe internal file links.

    Links are resolved after regular files, never used as extraction parents.
    Duplicate paths, special files and symlink chains are rejected.
    """
    require(not destination.exists(), "Extraction destination already exists")
    destination.mkdir(parents=True)
    links = []
    seen = set()
    with tarfile.open(archive, "r:*") as source:
        members = source.getmembers()
        for member in members:
            name = member.name.rstrip("/") if member.isdir() else member.name
            path = relative_path(name)
            normalized = unicodedata.normalize("NFD", name).casefold()
            require(normalized not in seen, f"Duplicate archive member: {name}")
            seen.add(normalized)
            require(
                member.isdir() or member.isfile() or member.issym(), "Unsupported archive member"
            )
            if member.issym():
                links.append((path, member.linkname))
        link_paths = {path for path, _ in links}
        for member in members:
            name = member.name.rstrip("/") if member.isdir() else member.name
            path = relative_path(name)
            require(not any(p in link_paths for p in path.parents), "Archive path traverses link")
            target = destination / str(path)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as incoming, target.open("xb") as out:
                    shutil.copyfileobj(incoming, out)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
        for path, link in links:
            require(not PurePosixPath(link).is_absolute(), "Absolute archive link")
            target = destination / str(path)
            referent = (target.parent / link).resolve()
            require(referent.is_relative_to(destination.resolve()), "Escaping archive link")
            require(
                referent.is_file() and not referent.is_symlink(), "Link must target regular file"
            )
            require(
                unicodedata.normalize(
                    "NFD", referent.relative_to(destination.resolve()).as_posix()
                ).casefold()
                in seen,
                "Unknown link target",
            )
            # Reject link chains even if a previous link was already materialized.
            require(
                PurePosixPath(referent.relative_to(destination.resolve())) not in link_paths,
                "Archive link chains are unsupported",
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            with referent.open("rb") as incoming, target.open("xb") as out:
                shutil.copyfileobj(incoming, out)
            target.chmod(stat.S_IMODE(referent.stat().st_mode))
    for path in destination.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
    destination.chmod(0o755)


def isolated_env(home, epoch):
    """Use an allowlist: no caller agent identity, Python, pip, Git or loader settings."""
    home.mkdir(mode=0o700)
    temp = home / "tmp"
    temp.mkdir(mode=0o700)
    return {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "TMPDIR": str(temp),
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
        "PYTHONHASHSEED": "0",
        "PIP_CONFIG_FILE": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_REPLACE_OBJECTS": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "SOURCE_DATE_EPOCH": str(epoch),
    }


def run(argv, *, cwd=None, env=None, text=True):
    try:
        result = subprocess.run(
            [str(a) for a in argv],
            cwd=cwd,
            env=env,
            check=True,
            text=text,
            errors="replace" if text else None,
            capture_output=True,
        )
    except subprocess.CalledProcessError as error:
        # Keep native/pip/Git diagnostics visible after temporary staging cleanup.
        for output in (error.stdout, error.stderr):
            if output:
                if isinstance(output, bytes):
                    output = output.decode("utf-8", errors="replace")
                print(output, file=sys.stderr, end="" if output.endswith("\n") else "\n")
        raise
    return result.stdout


def python_run(python, *args, cwd, env):
    return run([python, "-I", "-B", *args], cwd=cwd, env=env)


def install_wheels(python, items, inputs, stage, env):
    """Install exact locked wheels without an index or dependency resolution."""
    requirements = stage / "requirements.txt"
    requirements.write_text(
        "".join(
            f"{item['name']}=={item['version']} --hash=sha256:{item['sha256']}\n" for item in items
        )
    )
    python_run(
        python,
        "-m",
        "pip",
        "--isolated",
        "install",
        "--no-index",
        "--no-deps",
        "--no-compile",
        "--no-cache-dir",
        "--only-binary=:all:",
        "--require-hashes",
        "--find-links",
        inputs,
        "-r",
        requirements,
        cwd=stage,
        env=env,
    )


def normalize_python(payload, python, env):
    entrypoints = json.loads(
        python_run(
            python,
            "-c",
            "import importlib.metadata as m,json; "
            "print(json.dumps({e.name:e.value for e in m.entry_points(group='console_scripts')}))",
            cwd=payload,
            env=env,
        )
    )
    for name, value in entrypoints.items():
        require(
            re.fullmatch(r"[A-Za-z0-9_.-]+", name) and name not in {".", ".."},
            "Invalid console entrypoint name",
        )
        path = payload / "python/bin" / name
        if not path.is_file():
            continue
        code = (
            f"import sys; from importlib.metadata import EntryPoint; sys.argv[0]={name!r}; "
            f"sys.exit(EntryPoint(name={name!r},value={value!r},group='console_scripts').load()())"
        )
        path.write_text(
            '#!/bin/sh\nexec "${0%/*}/python3" -I -B -c ' + shlex.quote(code) + ' "$@"\n'
        )
        path.chmod(0o755)
    (payload / "bin").mkdir()
    launcher = payload / "bin/backbone"
    launcher.write_text(
        '#!/bin/sh\nexec "${0%/*}/../python/bin/python3" -I -B -m agent_backbone.cli "$@"\n'
    )
    launcher.chmod(0o755)
    for path in list(payload.rglob("__pycache__")):
        shutil.rmtree(path)
    for pattern in ("*.pyc", "direct_url.json"):
        for path in payload.rglob(pattern):
            path.unlink()
    for record in payload.glob("python/lib/python*/site-packages/*.dist-info/RECORD"):
        rows = []
        for name, _, _ in csv.reader(io.StringIO(record.read_text(encoding="utf-8"))):
            path = (record.parent.parent / name).resolve()
            require(path.is_relative_to(payload), "RECORD path escapes payload")
            if path == record:
                rows.append([name, "", ""])
            elif path.is_file():
                value = base64.urlsafe_b64encode(bytes.fromhex(digest(path))).decode().rstrip("=")
                rows.append([name, "sha256=" + value, str(path.stat().st_size)])
            else:
                require(
                    path.name == "direct_url.json"
                    or path.suffix == ".pyc"
                    or "__pycache__" in path.parts,
                    f"Unexpected missing RECORD file: {name}",
                )
        with record.open("w", newline="", encoding="utf-8") as stream:
            csv.writer(stream).writerows(sorted(rows))


def verify_wheel_package(payload, wheel):
    """Require the exact built package, with no missing, added or changed files."""
    site = payload / "python/lib/python3.13/site-packages"
    with zipfile.ZipFile(wheel) as source:
        names = {
            name
            for name in source.namelist()
            if name.startswith("agent_backbone/") and not name.endswith("/")
        }
        require(bool(names), "Built wheel contains no Backbone package")
        actual = {
            p.relative_to(site).as_posix()
            for p in (site / "agent_backbone").rglob("*")
            if p.is_file()
        }
        require(actual == names, "Installed Backbone package file set differs from wheel")
        for name in names:
            require(
                (site / name).read_bytes() == source.read(name),
                f"Installed package differs from wheel: {name}",
            )
    return len(names)


def inventory(payload):
    entries = []
    for path in sorted(payload.rglob("*")):
        require(not path.is_symlink(), "Payload must not contain symlinks")
        if path.parent == payload and path.name in EXCLUDED:
            require(path.is_file(), "Payload metadata must be regular files")
            continue
        row = {
            "path": path.relative_to(payload).as_posix(),
            "mode": stat.S_IMODE(path.stat().st_mode),
        }
        if path.is_file():
            row.update(kind="file", size=path.stat().st_size, sha256=digest(path))
        else:
            require(path.is_dir(), "Payload contains a special file")
            row.update(kind="directory")
        entries.append(row)
    return entries


def verify_payload(payload):
    manifest_path = payload / "engine-manifest.json"
    require(
        manifest_path.is_file() and not manifest_path.is_symlink(),
        "Manifest must be a regular non-symlink file",
    )
    manifest = json.loads(manifest_path.read_text())
    for key in ("files", "licenses"):
        item = manifest[key]
        verify_input(payload / str(relative_path(item["path"])), item)
    expected = json.loads((payload / manifest["files"]["path"]).read_text())
    require(inventory(payload) == expected, "Payload inventory membership/content/modes changed")
    require(
        manifest.get("schema_version") == 1
        and manifest.get("release_ready") is False
        and manifest.get("status") == "private_integration",
        "Invalid candidate manifest status",
    )
    commit = manifest["backbone"]["source"]
    target = manifest["target"]
    checksum = manifest["files"]["sha256"]
    require(
        re.fullmatch(r"[0-9a-f]{40}", commit) and target == "aarch64-apple-darwin",
        "Invalid source commit or target",
    )
    require(
        manifest["payload_id"] == "engine-" + commit[:12] + "-" + target + "-" + checksum[:12],
        "Payload identity does not match inventory",
    )
    rows = {row["path"]: row for row in expected}
    require(
        manifest["entrypoint"] == "bin/backbone"
        and rows["bin/backbone"]["kind"] == "file"
        and rows["bin/backbone"]["mode"] == 0o755,
        "Invalid engine entrypoint",
    )
    require(
        manifest["tmux"]["path"] == "libexec/tmux"
        and manifest["tmux"]["sha256"] == rows["libexec/tmux"]["sha256"],
        "tmux metadata does not match inventory",
    )
    receipt = json.loads((payload / "engine-build-receipt.json").read_text(encoding="utf-8"))
    require(receipt["source_commit"] == commit, "Source receipt does not match manifest")
    metadata_files = list(
        payload.glob("python/lib/python*/site-packages/agent_backbone-*.dist-info/METADATA")
    )
    require(len(metadata_files) == 1, "Expected one Backbone distribution")
    metadata = Parser().parsestr(metadata_files[0].read_text(encoding="utf-8"))
    require(
        metadata["Name"] == "agent-backbone"
        and metadata["Version"] == manifest["backbone"]["version"],
        "Backbone version does not match installed metadata",
    )
    require(
        manifest["protocols"] == {"runtime_discovery": 1, "setup": 1}, "Invalid protocol versions"
    )
    licenses = json.loads((payload / manifest["licenses"]["path"]).read_text(encoding="utf-8"))
    upstream = licenses["upstream_python"]
    require(
        manifest["python"]
        == {
            "version": upstream["python_version"],
            "upstream_release": upstream["upstream_release"],
            "archive_sha256": upstream["install_only_archive_sha256"],
        },
        "Python metadata does not match provenance",
    )
    return manifest


def package_notices(payload, python, env):
    code = """import importlib.metadata as m,json,pathlib
rows=[]
for d in m.distributions():
 paths=[]
 for f in d.files or []:
  name=pathlib.PurePath(str(f)).name.upper()
  if name.startswith(('LICENSE','COPYING','NOTICE')) and not name.endswith(('.PY','.PYC')):
   path=pathlib.Path(d.locate_file(f)).resolve()
   if path.is_file():paths.append(str(path))
 rows.append({'name':d.metadata['Name'],'version':d.version,
              'license_expression':d.metadata.get('License-Expression'),
              'included_license_paths':sorted(paths)})
print(json.dumps(rows))"""
    rows = json.loads(python_run(python, "-c", code, cwd=payload, env=env))
    for row in rows:
        require(row["included_license_paths"], f"Missing package notices: {row['name']}")
        row["included_license_paths"] = [
            Path(p).relative_to(payload).as_posix() for p in row["included_license_paths"]
        ]
    return sorted(rows, key=lambda row: row["name"].lower())


def native_notice_material(lock=None):
    """Validate source attribution and retained bytes before copying any notices.

    This is a conservative source inventory, not a compiled dependency SBOM.
    Binding it to exact inputs prevents silently reusing it after wheel upgrades.
    """
    metadata = (ROOT / "native-notices.json").read_bytes()
    document = json.loads(metadata)
    require(document["schema_version"] == 1, "Unsupported native notice schema")
    require(
        document["complete_compiled_component_attestation"] is False,
        "Source inventory must not claim compiled component attestation",
    )
    if lock is not None:
        require(
            document["target"] == lock["target"]
            and document["python_archive_sha256"] == lock["python"]["sha256"],
            "Native notice target/Python input mismatch",
        )
        wheels = {item["name"]: item for item in lock["runtime_wheels"]}
        require(
            {item["name"] for item in document["wheels"]}
            == {
                item["name"]
                for item in wheels.values()
                if not item["filename"].endswith("-none-any.whl")
            },
            "Native notice wheel membership mismatch",
        )
        for item in document["wheels"]:
            require(
                item["name"] in wheels
                and all(wheels[item["name"]].get(k) == v for k, v in item.items()),
                "Native notice wheel input mismatch",
            )
    files = {"native-notices.json": metadata}
    for item in document["files"]:
        name = str(relative_path(item["path"]))
        require(name == Path(name).name and name not in files, "Invalid native notice filename")
        source = ROOT / "notices" / name
        verify_input(source, item)
        data = source.read_bytes()
        require(
            len(data) == item["bytes"] and hashlib.sha256(data).hexdigest() == item["sha256"],
            "Native notice changed while reading",
        )
        files[name] = data
    sources = {item["id"] for item in document["sources"]}
    require(len(sources) == len(document["sources"]), "Duplicate native notice source")
    for component in document["components"]:
        require(component["source_id"] in sources, "Missing native notice source")
        for notice in component.get("notices", [component.get("notice")]):
            require(isinstance(notice, dict), "Missing native component notice")
            require(
                notice.get("source_id", component["source_id"]) in sources,
                "Missing native notice source",
            )
            name, offset, size = notice["file"], notice["offset"], notice["bytes"]
            require(
                name in files
                and name != "native-notices.json"
                and type(offset) is int
                and type(size) is int
                and offset >= 0
                and size > 0,
                "Invalid native notice location",
            )
            data = files[name][offset : offset + size]
            require(
                len(data) == size and hashlib.sha256(data).hexdigest() == notice["sha256"],
                "Native component notice mismatch",
            )
    return files


def add_notices(payload, inputs, lock, python, env):
    upstream = json.loads((ROOT / "python-notices.json").read_text())
    require(
        upstream["install_only_archive_sha256"] == lock["python"]["sha256"],
        "Python notice source does not match archive",
    )
    notices = payload / "licenses/python"
    notices.mkdir(parents=True)
    for item in upstream["files"]:
        name = str(relative_path(item["path"]))
        source = ROOT / "notices" / name
        verify_input(source, item)
        shutil.copyfile(source, notices / name)
        item["path"] = "licenses/python/" + name
    notice_paths = {item["path"] for item in upstream["files"]}
    for component in upstream["components"]:
        component["archive_license_paths"] = component["license_paths"]
        component["license_paths"] = [
            "licenses/python/" + Path(p).name for p in component["license_paths"]
        ]
        require(
            all(p in notice_paths for p in component["license_paths"]), "Missing component notice"
        )
    tmux_notices = payload / "licenses/tmux"
    tmux_notices.mkdir()
    for name in ("LICENSES.txt", "BUILD.json"):
        shutil.copyfile(inputs / lock["tmux"][name]["filename"], tmux_notices / name)
    native = payload / "licenses/native"
    native.mkdir()
    for name, data in native_notice_material(lock).items():
        (native / name).write_bytes(data)
    write_json(
        payload / "engine-license-inventory.json",
        {
            "schema_version": 1,
            "packages": package_notices(payload, python, env),
            "python_notices": "licenses/python",
            "tmux_notices": "licenses/tmux",
            "upstream_python": upstream,
            "native_components": {
                "path": "licenses/native/native-notices.json",
                "sha256": digest(native / "native-notices.json"),
                "scope": "conservative source notices; not a compiled-component attestation",
            },
        },
    )


def input_items(lock):
    return [
        lock["python"],
        *lock["build_wheels"],
        *lock["runtime_wheels"],
        *(lock["tmux"][key] for key in ("binary", "LICENSES.txt", "BUILD.json")),
    ]


def validate_inputs(inputs, lock):
    seen = set()
    for item in input_items(lock):
        name = item["filename"]
        require(str(relative_path(name)) == Path(name).name, "Input name must be a filename")
        require(name not in seen, "Duplicate input filename")
        seen.add(name)
        verify_input(inputs / name, item)


def validate_download_url(url, *, redirect=False):
    parsed = urllib.parse.urlsplit(url)
    hosts = {"github.com", "files.pythonhosted.org"}
    if redirect:
        hosts.add("release-assets.githubusercontent.com")
    require(
        parsed.scheme == "https"
        and parsed.hostname in hosts
        and parsed.port in {None, 443}
        and parsed.username is None
        and parsed.password is None,
        "Unexpected download origin",
    )


class DownloadRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_download_url(newurl, redirect=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_inputs(inputs, lock):
    """Download public locked archives/wheels; tmux is supplied by the app builder."""
    inputs.mkdir(parents=True, exist_ok=True)
    for item in input_items(lock):
        name = str(relative_path(item["filename"]))
        target = inputs / name
        if target.exists():
            verify_input(target, item)
            continue
        url = item.get("url")
        if not url:
            print(f"Supply separately: {name}")
            continue
        validate_download_url(url)
        with tempfile.NamedTemporaryFile(dir=inputs, prefix=".download-", delete=False) as out:
            temporary = Path(out.name)
            try:
                opener = urllib.request.build_opener(DownloadRedirectHandler())
                with opener.open(url, timeout=60) as incoming:
                    shutil.copyfileobj(incoming, out)
                out.close()
                verify_input(temporary, item)
                require(not target.exists(), "Input appeared during download")
                os.link(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        print(f"Verified: {name}")


def validate_project_lock(source):
    """Refuse stale project requirements before trusting the resolved closure.

    This fixed-input builder supports the project's plain requirements/extras.
    New direct URLs or root environment markers need an explicit tooling update.
    """
    project = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    packages = tomllib.loads((source / "uv.lock").read_text(encoding="utf-8"))["package"]
    roots = [p for p in packages if p["name"] == project["name"]]
    require(
        len(roots) == 1 and roots[0]["version"] == project["version"], "Stale project lock identity"
    )
    root = roots[0]

    def key(name, extras, specifier, group):
        def normalize(value):
            return re.sub(r"[-_.]+", "-", value).lower()

        return (
            normalize(name),
            tuple(sorted(normalize(e) for e in extras)),
            tuple(sorted(specifier.replace(" ", "").split(","))) if specifier else (),
            group,
        )

    declared = set()
    groups = {"": project.get("dependencies", []), **project.get("optional-dependencies", {})}
    for group, requirements in groups.items():
        for requirement in requirements:
            match = re.fullmatch(
                r"([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[([A-Za-z0-9_,.-]+)\])?([<>=!~][^;@\s]+)?",
                requirement.replace(" ", ""),
            )
            require(match is not None, "Unsupported root requirement; update portable tooling")
            name, extras, specifier = match.groups()
            declared.add(key(name, extras.split(",") if extras else [], specifier or "", group))
    recorded = set()
    for requirement in root["metadata"]["requires-dist"]:
        marker = requirement.get("marker", "")
        match = re.fullmatch(r"extra == ['\"]([^'\"]+)['\"]", marker) if marker else None
        require(not marker or match is not None, "Unsupported root lock marker")
        recorded.add(
            key(
                requirement["name"],
                requirement.get("extras", []),
                requirement.get("specifier", ""),
                match[1] if match else "",
            )
        )
    require(declared == recorded, "Project requirements differ from source lock metadata")
    require(
        set(groups) - {""} == set(root.get("optional-dependencies", {})),
        "Project extras differ from source lock",
    )
    for group in groups:
        dependencies = (
            root.get("dependencies", []) if not group else root["optional-dependencies"][group]
        )
        edges = {key(d["name"], d.get("extra", []), "", group) for d in dependencies}
        expected = {(name, extras, (), g) for name, extras, _, g in declared if g == group}
        require(edges == expected, "Project dependencies differ from source lock edges")


def validate_source_lock(source, lock):
    """Require the complete conservative runtime closure, including selected extras.

    Retain dependencies across marker branches rather than silently dropping an
    optional native feature. Forked package versions need an explicit lock update.
    """
    packages = tomllib.loads((source / "uv.lock").read_text(encoding="utf-8"))["package"]
    pending = [{"name": "agent-backbone", "extra": ["postgres"]}]
    visited = set()
    expected = set()
    while pending:
        dependency = pending.pop()
        name = dependency["name"]
        extras = tuple(sorted(dependency.get("extra", [])))
        if (name, extras) in visited:
            continue
        visited.add((name, extras))
        matching = [p for p in packages if p["name"] == name]
        require(len(matching) == 1, f"Missing or forked source lock package: {name}")
        package = matching[0]
        expected.add((name, package["version"]))
        pending.extend(package.get("dependencies", []))
        for extra in extras:
            require(
                extra in package.get("optional-dependencies", {}),
                f"Missing source lock extra: {name}[{extra}]",
            )
            pending.extend(package["optional-dependencies"][extra])
    expected = {pair for pair in expected if pair[0] != "agent-backbone"}
    actual = {(item["name"], item["version"]) for item in lock["runtime_wheels"]}
    require(
        len(actual) == len(lock["runtime_wheels"]) and actual == expected,
        "Runtime input set differs from source dependency closure (including extras)",
    )
    for item in lock["runtime_wheels"]:
        matching = [
            p for p in packages if p["name"] == item["name"] and p["version"] == item["version"]
        ]
        require(
            any(
                w["hash"] == "sha256:" + item["sha256"]
                for p in matching
                for w in p.get("wheels", [])
            ),
            f"Runtime input differs from source uv.lock: {item['name']}",
        )


def verify_source_tree(source, repository, commit, git, env):
    """Detect archive filters/attributes or local configuration changing Git blobs."""
    tree = run(
        [git, "-C", str(repository), "ls-tree", "-rz", "--full-tree", commit],
        cwd=source,
        env=env,
        text=False,
    )
    expected = set()
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        metadata, raw_name = entry.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split()
        require(
            kind == "blob" and mode in {"100644", "100755"},
            "Source tree must contain regular files; links/submodules are unsupported",
        )
        name = raw_name.decode("utf-8")
        path = source / str(relative_path(name))
        require(path.is_file() and not path.is_symlink(), "Source tree file missing")
        data = path.read_bytes()
        algorithm = "sha256" if len(oid) == 64 else "sha1"
        actual = hashlib.new(algorithm, f"blob {len(data)}\0".encode() + data).hexdigest()
        require(actual == oid, f"Archive content differs from Git blob: {name}")
        require(
            bool(path.stat().st_mode & 0o111) == (mode == "100755"),
            f"Archive executable mode differs from Git tree: {name}",
        )
        expected.add(name)
    actual_files = {p.relative_to(source).as_posix() for p in source.rglob("*") if p.is_file()}
    require(actual_files == expected, "Archive file membership differs from source commit")


def reject_build_paths(payload, stage):
    prefix = os.fsencode(stage)
    for path in payload.rglob("*"):
        if path.is_file():
            require(
                prefix not in path.read_bytes(),
                f"Local build path remains in payload: {path.relative_to(payload)}",
            )


def publish_exclusively(source, destination):
    """Atomically refuse every pre-existing destination on the native build host."""
    require(platform.system() == "Darwin", "Exclusive publication requires macOS")
    rename = ctypes.CDLL(None, use_errno=True).renamex_np
    rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    # macOS sys/stdio.h: RENAME_EXCL; available since macOS 10.12.
    if rename(os.fsencode(source), os.fsencode(destination), 0x00000004) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def verify_upstream_notices(archive):
    """Compare retained notices and binary samples with the pinned full archive."""
    upstream = json.loads((ROOT / "python-notices.json").read_text(encoding="utf-8"))
    verify_input(archive, {"sha256": upstream["full_archive_sha256"]})

    def member(name):
        relative_path(name)
        return run(
            ["/usr/bin/tar", "-xOf", str(archive), "python/" + name],
            text=False,
        )

    metadata = json.loads(member("PYTHON.json"))
    require(
        metadata["python_version"] == upstream["python_version"], "Notice Python version mismatch"
    )
    for item in upstream["files"]:
        data = member(item["source"])
        require(
            len(data) == item["bytes"] and hashlib.sha256(data).hexdigest() == item["sha256"],
            "Upstream notice mismatch",
        )
        require(
            (ROOT / "notices" / str(relative_path(item["path"]))).read_bytes() == data,
            "Retained notice differs from upstream",
        )
    for sample in upstream["install_only_comparison"]:
        require(
            hashlib.sha256(member("install/" + sample["path"])).hexdigest() == sample["sha256"],
            "Upstream binary sample mismatch",
        )
    return {
        "notices_verified": len(upstream["files"]),
        "binary_samples_verified": len(upstream["install_only_comparison"]),
    }


def build(repository, commit, inputs, output, lock):
    """Build in a fresh sibling directory and publish only after all checks pass."""
    require(re.fullmatch(r"[0-9a-f]{40}", commit), "Source must be a full commit ID")
    require(
        (platform.system(), platform.machine()) == ("Darwin", "arm64"),
        "This lock requires a native Apple Silicon build host",
    )
    require(not output.exists() and not output.is_symlink(), "Output already exists")
    validate_inputs(inputs, lock)
    output.parent.mkdir(parents=True, exist_ok=True)
    git = "/usr/bin/git"
    require(Path(git).is_file(), "System Git is required")
    with tempfile.TemporaryDirectory(prefix=".engine-build-", dir=output.parent) as directory:
        stage = Path(directory).resolve()
        # Snapshot checked inputs so later changes to the caller's cache cannot
        # alter the bytes consumed after validation.
        cached = stage / "inputs"
        cached.mkdir()
        for item in input_items(lock):
            shutil.copyfile(inputs / item["filename"], cached / item["filename"])
        validate_inputs(cached, lock)
        inputs = cached
        env = isolated_env(stage / "home", 315532800)
        resolved = run(
            [git, "-C", repository, "rev-parse", commit + "^{commit}"], cwd=stage, env=env
        ).strip()
        require(resolved == commit, "Source commit mismatch")
        epoch = run(
            [git, "-C", repository, "show", "-s", "--format=%ct", commit], cwd=stage, env=env
        ).strip()
        env["SOURCE_DATE_EPOCH"] = epoch
        source_archive = stage / "source.tar"
        run(
            [
                git,
                "-c",
                "tar.umask=0000",
                "-c",
                "core.autocrlf=false",
                "-c",
                "core.eol=lf",
                "-C",
                repository,
                "archive",
                "--format=tar",
                "--output",
                source_archive,
                commit,
            ],
            cwd=stage,
            env=env,
        )
        source = stage / "source"
        extract_archive(source_archive, source)
        verify_source_tree(source, repository, commit, git, env)
        validate_project_lock(source)
        validate_source_lock(source, lock)
        project = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        require(project["name"] == "agent-backbone", "Wrong source project")
        version = project["version"]
        runtime_archive = inputs / lock["python"]["filename"]
        build_root = stage / "builder"
        extract_archive(runtime_archive, build_root)
        builder = build_root / "python/bin/python3"
        install_wheels(builder, lock["build_wheels"], inputs, stage, env)
        wheels = stage / "wheels"
        wheels.mkdir()
        python_run(
            builder,
            "-m",
            "pip",
            "--isolated",
            "wheel",
            "--no-index",
            "--no-deps",
            "--no-build-isolation",
            "--no-cache-dir",
            "--wheel-dir",
            wheels,
            source,
            cwd=stage,
            env=env,
        )
        built = list(wheels.glob("*.whl"))
        require(len(built) == 1, "Expected exactly one project wheel")
        wheel = built[0]
        payload = stage / "payload"
        extract_archive(runtime_archive, payload)
        python = payload / "python/bin/python3"
        upstream = json.loads((ROOT / "python-notices.json").read_text(encoding="utf-8"))
        for sample in upstream["install_only_comparison"]:
            verify_input(payload / "python" / str(relative_path(sample["path"])), sample)
        identity = json.loads(
            python_run(
                python,
                "-c",
                "import sys,platform,json;"
                "print(json.dumps([platform.machine(),platform.python_version()]))",
                cwd=stage,
                env=env,
            )
        )
        require(identity == ["arm64", lock["python"]["version"]], "Python identity mismatch")
        install_wheels(python, lock["runtime_wheels"], inputs, stage, env)
        python_run(
            python,
            "-m",
            "pip",
            "--isolated",
            "install",
            "--no-index",
            "--no-deps",
            "--no-compile",
            "--no-cache-dir",
            wheel,
            cwd=stage,
            env=env,
        )
        python_run(python, "-m", "pip", "--isolated", "check", cwd=stage, env=env)
        normalize_python(payload, python, env)
        package_files = verify_wheel_package(payload, wheel)
        (payload / "libexec").mkdir()
        tmux = payload / "libexec/tmux"
        shutil.copyfile(inputs / lock["tmux"]["binary"]["filename"], tmux)
        tmux.chmod(0o755)
        require(
            run(["/usr/bin/lipo", "-archs", tmux], cwd=stage, env=env).split() == ["arm64"],
            "tmux architecture mismatch",
        )
        linkage = run(["/usr/bin/otool", "-L", tmux], cwd=stage, env=env).splitlines()[1:]
        require(
            linkage
            and all(line.strip().startswith(("/usr/lib/", "/System/Library/")) for line in linkage),
            "tmux has non-system dylibs",
        )
        require(
            run([tmux, "-V"], cwd=stage, env=env).strip() == "tmux " + lock["tmux"]["version"],
            "tmux version mismatch",
        )
        add_notices(payload, inputs, lock, python, env)
        write_json(
            payload / "engine-build-receipt.json",
            {
                "schema_version": 1,
                "source_commit": commit,
                "source_tree": run(
                    [git, "-C", repository, "rev-parse", commit + "^{tree}"], cwd=stage, env=env
                ).strip(),
                "source_date_epoch": int(epoch),
                "input_lock_sha256": digest(LOCK),
                "builder_sha256": digest(Path(__file__)),
                "python_notice_metadata_sha256": digest(ROOT / "python-notices.json"),
                "native_notice_metadata_sha256": digest(ROOT / "native-notices.json"),
                "wheel": {
                    "filename": wheel.name,
                    "sha256": digest(wheel),
                    "verified_package_files": package_files,
                },
            },
        )
        # Canonical modes make output independent of caller umask. No bytecode or
        # absolute local source provenance survives installation.
        for path in payload.rglob("*"):
            path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
        reject_build_paths(payload, stage)
        write_json(payload / "engine-file-inventory-v1.json", inventory(payload))
        checksum = digest(payload / "engine-file-inventory-v1.json")
        manifest = {
            "schema_version": 1,
            "payload_id": "engine-" + commit[:12] + "-" + lock["target"] + "-" + checksum[:12],
            "target": lock["target"],
            "entrypoint": "bin/backbone",
            "python": {
                "version": lock["python"]["version"],
                "upstream_release": lock["python"]["upstream_release"],
                "archive_sha256": lock["python"]["sha256"],
            },
            "backbone": {"version": version, "source": commit},
            "tmux": {"path": "libexec/tmux", "sha256": lock["tmux"]["binary"]["sha256"]},
            "protocols": {"runtime_discovery": 1, "setup": 1},
            "files": {"path": "engine-file-inventory-v1.json", "sha256": checksum},
            "licenses": {
                "path": "engine-license-inventory.json",
                "sha256": digest(payload / "engine-license-inventory.json"),
            },
            "status": "private_integration",
            "release_ready": False,
            "limitations": [
                "invoke entrypoint by its installed path or payload bin PATH; "
                "external launcher symlinks are unsupported",
                "candidate installer/service acceptance required",
                "actual minimum-OS run pending",
                "other platform targets pending",
                "package notices cover wheel-provided license files; "
                "bundled native component audit pending",
                "tmux provenance is the supplied hash-pinned build receipt and notices",
            ],
        }
        write_json(payload / "engine-manifest.json", manifest)
        for name in EXCLUDED:
            (payload / name).chmod(0o644)
        launcher = payload / manifest["entrypoint"]
        require(
            run([launcher, "--version"], cwd=stage, env=env).strip() == "backbone " + version,
            "CLI version mismatch",
        )
        require(
            json.loads(run([launcher, "runtimes", "--json"], cwd=stage, env=env))["schema_version"]
            == 1,
            "Runtime discovery mismatch",
        )
        require(
            json.loads(run([launcher, "setup", "--schema"], cwd=stage, env=env))["discriminator"][
                "propertyName"
            ]
            == "action",
            "Setup schema mismatch",
        )
        verify_payload(payload)
        publish_exclusively(payload, output)
        return manifest


def snapshot_builder(destination):
    """Copy the executed builder and all local inputs into private immutable staging."""
    destination.mkdir(mode=0o700)
    metadata = (ROOT / "python-notices.json").read_bytes()
    files = {
        "build.py": Path(__file__).read_bytes(),
        LOCK.name: LOCK.read_bytes(),
        "python-notices.json": metadata,
    }
    for item in json.loads(metadata)["files"]:
        name = str(relative_path(item["path"]))
        source = ROOT / "notices" / name
        verify_input(source, item)
        data = source.read_bytes()
        require(hashlib.sha256(data).hexdigest() == item["sha256"], "Notice changed while copying")
        files["notices/" + name] = data
    for name, data in native_notice_material().items():
        files[name if name == "native-notices.json" else "notices/" + name] = data
    for name, data in files.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return destination / "build.py"


def main(*, snapshot=False):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="fetch hash-locked public binary inputs")
    fetch.add_argument("--inputs", type=Path, required=True)
    build_command = commands.add_parser("build", help="build offline from an exact Git commit")
    build_command.add_argument("--repository", type=Path, default=Path.cwd())
    build_command.add_argument("--commit", required=True)
    build_command.add_argument("--inputs", type=Path, required=True)
    build_command.add_argument("--output", type=Path, required=True)
    notices = commands.add_parser(
        "verify-notices", help="compare notices with pinned full CPython archive"
    )
    notices.add_argument("archive", type=Path)
    verify = commands.add_parser("verify", help="verify exact immutable payload inventory")
    verify.add_argument("payload", type=Path)
    args = parser.parse_args()
    if args.command == "build" and not snapshot:
        # Execute the copied script itself: the receipt hashes exactly the code,
        # lock and notices used even if the caller checks out another revision.
        with tempfile.TemporaryDirectory(prefix="engine-tooling-") as directory:
            script = snapshot_builder(Path(directory) / "tooling")
            output = run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    "import runpy,sys; runpy.run_path(sys.argv.pop(1))['main'](snapshot=True)",
                    script,
                    *sys.argv[1:],
                ]
            )
            print(output, end="")
        return
    lock = json.loads(LOCK.read_text())
    if args.command == "fetch":
        fetch_inputs(args.inputs.resolve(), lock)
    elif args.command == "build":
        print(
            json.dumps(
                build(
                    args.repository.resolve(),
                    args.commit,
                    args.inputs.resolve(),
                    args.output.absolute(),
                    lock,
                ),
                indent=2,
            )
        )
    elif args.command == "verify-notices":
        print(json.dumps(verify_upstream_notices(args.archive.resolve()), indent=2))
    else:
        print(json.dumps(verify_payload(args.payload.resolve()), indent=2))


if __name__ == "__main__":
    main()
