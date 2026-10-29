"""Compare retained native notices with cached, pinned upstream source bytes.

No network, extraction to disk, source execution or dependency resolution. This
checks source correspondence, not how an upstream binary wheel was compiled.
"""

import argparse
import hashlib
import io
import json
import posixpath
import re
import runpy
import tarfile
import tomllib
import urllib.parse
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

builder = runpy.run_path(str(Path(__file__).with_name("build.py")))
native_notice_material = builder["native_notice_material"]
relative_path = builder["relative_path"]
require = builder["require"]


def verify_sources(cache, target="aarch64-apple-darwin"):
    require(target in builder["TARGETS"], "Unknown native notice target")
    lock = json.loads((builder["ROOT"] / (target + ".json")).read_text())
    material = native_notice_material(lock)
    document = json.loads(material["native-notices.json"])
    sources = {row["id"]: row for row in document["sources"]}
    snapshots = {}
    total = 0
    for source in sources.values():
        name = str(relative_path(source["filename"]))
        require(name == Path(name).name, "Source name must be a filename")
        path = cache / name
        require(path.is_file() and not path.is_symlink(), "Missing regular source input")
        with path.open("rb") as stream:
            data = stream.read(64 * 1024 * 1024 + 1)
        total += len(data)
        require(
            len(data) <= 64 * 1024 * 1024 and total <= 256 * 1024 * 1024,
            "Source verification memory budget exceeded",
        )
        require(hashlib.sha256(data).hexdigest() == source["sha256"], "Source input hash mismatch")
        snapshots[source["id"]] = data

    def open_archive(source_id):
        return tarfile.open(fileobj=io.BytesIO(snapshots[source_id]))

    def notice_members(archive, license_file=None):
        return {
            m.name
            for m in archive
            if m.isfile()
            and (
                Path(m.name)
                .name.upper()
                .startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE", "COPYRIGHT", "UNLICENSE"))
                or any(p.upper() == "LICENSES" for p in Path(m.name).parts[:-1])
                or m.name == license_file
            )
        }

    def read(source_id, member):
        source = sources[source_id]
        if source.get("format") == "raw":
            require(member is None, "Raw source must not have archive member")
            return snapshots[source_id]
        relative_path(member)
        with open_archive(source_id) as archive:
            entries = [entry for entry in archive if entry.name == member]
            require(len(entries) == 1 and entries[0].isfile(), "Invalid source member")
            require(entries[0].size <= 4 * 1024 * 1024, "Source notice member too large")
            return archive.extractfile(entries[0]).read()

    root_sources = {root["source_id"] for root in document["rust_roots"]}
    found_roots = set()

    def source_path(prefix, directory, value):
        require(
            isinstance(value, str)
            and value
            and not PurePosixPath(value).is_absolute()
            and "\\" not in value,
            "Invalid declared source notice path",
        )
        name = posixpath.normpath(directory + "/" + value)
        relative_path(name)
        require(name.startswith(prefix + "/"), "Declared source notice escapes source root")
        return name

    for wheel in document["wheels"]:
        source = sources[wheel["source_id"]]
        with open_archive(wheel["source_id"]) as archive:
            members = archive.getmembers()
            infos = [
                m
                for m in members
                if m.isfile() and len(m.name.split("/")) == 2 and m.name.endswith("/PKG-INFO")
            ]
            require(len(infos) == 1, "Missing source distribution identity")
            info = BytesParser().parsebytes(archive.extractfile(infos[0]).read())
            require(
                info["Name"].replace("_", "-").lower() == wheel["name"].replace("_", "-").lower()
                and info["Version"] == wheel["version"],
                "Source distribution identity mismatch",
            )
            prefix = infos[0].name.rsplit("/", 1)[0]
            required = notice_members(archive)
            names = {m.name for m in members if m.isfile()}
            # Maturin reports license paths relative to its selected crate,
            # which can live below the pyproject directory in an sdist.
            bases = {prefix}
            pyproject = prefix + "/pyproject.toml"
            if pyproject in names:
                project = tomllib.loads(read(wheel["source_id"], pyproject).decode())
                manifest = project.get("tool", {}).get("maturin", {}).get("manifest-path")
                if manifest:
                    bases.add(posixpath.dirname(source_path(prefix, prefix, manifest)))
            for declared in info.get_all("License-File", []):
                matches = {source_path(prefix, base, declared) for base in bases} & names
                require(matches, "Missing declared distribution notice")
                required.update(matches)
            cargo = {
                m.name: tomllib.loads(read(wheel["source_id"], m.name).decode())
                for m in members
                if m.isfile() and m.name.endswith("/Cargo.toml")
            }
            for path, manifest in cargo.items():
                value = manifest.get("package", {}).get("license-file")
                if value is None:
                    continue
                directory = posixpath.dirname(path)
                if value == {"workspace": True}:
                    ancestors = [
                        p
                        for p in cargo
                        if directory == posixpath.dirname(p)
                        or directory.startswith(posixpath.dirname(p) + "/")
                    ]
                    workspaces = [p for p in ancestors if "workspace" in cargo[p]]
                    require(workspaces, "Missing workspace for inherited license file")
                    workspace = max(workspaces, key=len)
                    directory = posixpath.dirname(workspace)
                    value = cargo[workspace]["workspace"].get("package", {}).get("license-file")
                required.add(source_path(prefix, directory, value))
            retained = {
                c.get("source_member")
                for c in document["components"]
                if c["source_id"] == wheel["source_id"]
            }
            require(
                required <= retained,
                f"Wheel source notice members missing: {sorted(required - retained)}",
            )
            if prefix + "/Cargo.lock" in {m.name for m in members}:
                found_roots.add(wheel["source_id"])
                require(
                    any(
                        root["source_id"] == wheel["source_id"]
                        and root["source_member"] == prefix + "/Cargo.lock"
                        for root in document["rust_roots"]
                    ),
                    "Missing Rust root source",
                )
    require(root_sources == found_roots, "Rust roots differ from source distributions")
    version = document["python_version"]
    header = read("cpython", "Python-" + version + "/Include/patchlevel.h").decode()
    numbers = [
        re.search(r"#define\s+PY_" + part + r"_VERSION\s+(\d+)", header)
        for part in ("MAJOR", "MINOR", "MICRO")
    ]
    require(
        all(numbers) and ".".join(n.group(1) for n in numbers) == version,
        "CPython source version mismatch",
    )

    notices = 0
    for component in document["components"]:
        if component.get("kind") == "rust-source-lock-package":
            sid = component["source_id"]
            source = sources[sid]
            prefix = source["name"] + "-" + source["version"]
            package = tomllib.loads(read(sid, prefix + "/Cargo.toml").decode())["package"]
            require(
                package["name"] == source["name"]
                and package["version"] == source["version"]
                and package.get("license") == component["declared_license"],
                "Crate package identity/license differs",
            )
            with open_archive(sid) as archive:
                required = notice_members(archive)
                if "license-file" in package:
                    declared = source_path(prefix, prefix, package["license-file"])
                    require(
                        declared in {m.name for m in archive.getmembers() if m.isfile()},
                        "Missing declared crate notice",
                    )
                    required.add(declared)
            retained = {n["source_member"] for n in component["notices"] if n["source_id"] == sid}
            require(
                required <= retained, f"Crate notice members missing: {sorted(required - retained)}"
            )
            for notice in component["notices"]:
                external = sources[notice["source_id"]]
                if external.get("format") != "raw":
                    continue
                vcs = json.loads(read(sid, prefix + "/.cargo_vcs_info.json"))
                repo = urllib.parse.urlsplit(package["repository"].removesuffix(".git").rstrip("/"))
                url = urllib.parse.urlsplit(external["url"])
                require(
                    repo.scheme == "https"
                    and repo.netloc == "github.com"
                    and external["git_commit"] == vcs["git"]["sha1"]
                    and url.scheme == "https"
                    and url.netloc == "raw.githubusercontent.com"
                    and url.path.startswith(repo.path + "/" + vcs["git"]["sha1"] + "/"),
                    "Upstream notice commit/repository differs from crate provenance",
                )
        for notice in component.get("notices", [component.get("notice")]):
            source_id = notice.get("source_id", component["source_id"])
            member = notice.get("source_member", component.get("source_member"))
            data = read(source_id, member)
            selection = notice.get("selection", component.get("selection", "file"))
            if selection == "initial_c_comment":
                require(data.startswith(b"/*") and b"*/" in data, "Missing initial C comment")
                data = data[: data.index(b"*/") + 2]
            else:
                require(selection == "file", "Unknown source selection")
            require(
                len(data) == notice["bytes"]
                and hashlib.sha256(data).hexdigest() == notice["sha256"],
                f"Source notice differs: {component['component']}",
            )
            notices += 1

    # Check full registry membership, including dependencies not linked on this
    # platform. No feature resolver or build script is run to guess inclusion.
    for root in document["rust_roots"]:
        data = read(root["source_id"], root["source_member"])
        require(hashlib.sha256(data).hexdigest() == root["sha256"], "Rust lock changed")
        packages = tomllib.loads(data.decode())["package"]
        registry = [p for p in packages if p.get("source")]
        require(
            sorted("crate:" + p["name"] + "-" + p["version"] for p in registry)
            == root["registry_packages"],
            "Rust source inventory membership differs from lock",
        )
        for package in registry:
            require(
                package["source"] == "registry+https://github.com/rust-lang/crates.io-index"
                and package["checksum"]
                == sources["crate:" + package["name"] + "-" + package["version"]]["sha256"],
                "Rust dependency source/checksum differs from lock",
            )
    return {
        "sources_verified": len(sources),
        "notices_verified": notices,
        "compiled_component_attestation": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cache", type=Path)
    parser.add_argument(
        "--target", choices=sorted(builder["TARGETS"]), default="aarch64-apple-darwin"
    )
    args = parser.parse_args()
    print(json.dumps(verify_sources(args.cache, args.target), indent=2))
