"""Compare retained native notices with cached, pinned upstream source bytes.

No network, extraction to disk, source execution or dependency resolution. This
checks source correspondence, not how an upstream binary wheel was compiled.
"""

import argparse
import hashlib
import json
import runpy
import tarfile
import tomllib
from pathlib import Path

builder = runpy.run_path(str(Path(__file__).with_name("build.py")))
native_notice_material = builder["native_notice_material"]
relative_path = builder["relative_path"]
require = builder["require"]
verify_input = builder["verify_input"]


def verify_sources(cache):
    material = native_notice_material()
    document = json.loads(material["native-notices.json"])
    sources = {row["id"]: row for row in document["sources"]}
    for source in sources.values():
        name = str(relative_path(source["filename"]))
        require(name == Path(name).name, "Source name must be a filename")
        verify_input(cache / name, source)

    def read(source_id, member):
        source = sources[source_id]
        path = cache / source["filename"]
        if source.get("format") == "raw":
            require(member is None, "Raw source must not have archive member")
            return path.read_bytes()
        relative_path(member)
        with tarfile.open(path) as archive:
            entries = [entry for entry in archive if entry.name == member]
            require(len(entries) == 1 and entries[0].isfile(), "Invalid source member")
            require(entries[0].size <= 4 * 1024 * 1024, "Source notice member too large")
            return archive.extractfile(entries[0]).read()

    notices = 0
    for component in document["components"]:
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
    args = parser.parse_args()
    print(json.dumps(verify_sources(args.cache), indent=2))
