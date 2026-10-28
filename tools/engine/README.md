# Portable engine builder

This native Apple Silicon builder produces the immutable engine consumed by the
combined application installer. It builds Backbone from an exact Git commit and
uses pinned CPython, runtime wheels, build-backend wheels and tmux inputs. It never
copies a development environment. Python 3.11+ and Git are build-host prerequisites.
No dependency resolution or download happens during `build`.

```sh
python3 -I -B tools/engine/build.py fetch --inputs /path/to/cache
# Supply the three separately built tmux inputs listed in aarch64-apple-darwin.json.
python3 -I -B tools/engine/build.py build --repository . --commit FULL_COMMIT_ID \
  --inputs /path/to/cache --output /path/to/new-engine
python3 -I -B tools/engine/build.py verify /path/to/new-engine
```

The cache is disposable. Fetch verifies SHA-256 before accepting files; existing
mismatched files fail rather than being silently overwritten.
Download redirects must retain HTTPS and use an allowed public host, including
GitHub's release asset host; validation happens before following each redirect.
The application builder supplies the locked tmux binary, its `BUILD.json` and license notices.
The lock names their exact filenames and hashes. CPython notices in `notices/`
come from the full upstream archive identified in `python-notices.json`, whose
install-only archive is also pinned. Runtime wheels follow `uv.lock`, including
the optional PostgreSQL driver; build tools are separately pinned.
The source commit must contain matching runtime wheel versions/hashes in `uv.lock`, with the complete
runtime dependency closure including selected extras.
The project's root requirements, version, extras and dependency edges must also
match the lock. Root environment markers and direct-URL requirements are refused
until this fixed-input tooling is updated to support them.
Dependencies across marker branches are retained conservatively; forked package versions are refused until
the input handling is explicitly updated. Updating inputs
requires reviewing the new hashes, origins, notices and native platform behavior.

The source commit must already exist in the repository. Uncommitted project files are
excluded. Builder code, input lock and retained notice metadata come from the
builder checkout; their hashes are recorded in the payload receipt.
The build runs a private snapshot of the builder, lock and notices, so changing the
checkout during a build cannot change the executed tooling or its receipt hashes.
Failed subprocesses print their captured diagnostics before staging is removed.
Git archive container bytes are not part of payload identity. Source files
are compared with Git blob identities so local archive filters cannot change them.
Source links and submodules are refused. Build tools run in a separately extracted interpreter, with an isolated
home and a minimal environment. Runtime installation is separate and offline.
Payload links are materialized into regular files; dependency console launchers
are relative, use isolated Python and disable bytecode writes. Local installation
provenance is removed and wheel records regenerated. Modes and wheel timestamps
are normalized. The output is published only after CLI and exact inventory checks
succeed; atomic exclusive rename refuses an existing file, directory or symlink.

`engine-manifest.json`, `engine-file-inventory-v1.json`,
`engine-license-inventory.json`, `bin/backbone`, `python/` and `libexec/tmux` retain
the installer contract. `engine-build-receipt.json` records the source tree identity,
built wheel, builder and lock hashes without local build paths. A final scan
refuses any retained build-directory prefix, including unnormalized wheel scripts. Invoke the launcher through
its installed path or payload `bin` on PATH; external symlinks are unsupported.
The installer owns activation and persistent data; this tool does neither.

Build twice from the same commit and lock into different locations and compare
both manifests and inventories before calling a candidate reproducible. Then run
relocated setup/replay, native spreadsheet calculation, service and installer
acceptance against the exact candidate. A successful build sets
`release_ready: false`: it does not establish minimum-OS, other-platform, real
first-agent or publication acceptance. Other Mac/Linux targets need their own
reviewed inputs and native verification; this lock does not claim their support.

## Rechecking CPython notice provenance

Download the full upstream archive using `full_archive_url` in
`python-notices.json` and keep its recorded filename. The native host's
`/usr/bin/tar` must support Zstandard (as does the tested build host).

```sh
python3 -I -B tools/engine/build.py verify-notices /path/to/full-archive.tar.zst
```

This verifies the entire archive hash, reads `python/PYTHON.json`, compares all
14 retained notice files byte-for-byte, and checks the three recorded installed
binary samples. The wheel notice inventory covers license texts provided by each
wheel. Additional native notices are retained in `licenses/native/`, with a
hashed `native-notices.json` index. This index is bound to the exact Python
archive and all 13 native wheel inputs; changing those inputs requires updating
the source correspondence. The builder validates each retained notice and each
component's byte range before taking its private tooling snapshot.

The additional notices include libuv (including its incorporated tree and network
code), llhttp, http-parser, Tk, Tcl Thread, Itcl, HACL* and CPython's incorporated
component notices, including mimalloc. The Rust inventory retains all 295 registry
packages in the four matching-version source locks for cryptography,
pydantic-core, watchfiles and ironcalc. It records declared and selected licenses,
including the additional Unicode terms where required, and retains all supplied
license/notice texts. Missing crate notice files are taken from the upstream
commit identified by that crate's `.cargo_vcs_info.json`; r-efi's notice is in
its `AUTHORS` file.

This is deliberately a conservative source inventory: build, development and
other-target packages are included. It is **not** proof that an upstream wheel
was built from that exact source archive, with those enabled features, or that
every listed crate is linked. Exact compiled closure and upstream compiler /
standard-library provenance remain unverified. These limits are also recorded
in the payload metadata; `release_ready` remains false.

Retain the cached upstream files named in `native-notices.json` to reproduce the
source comparison offline:

```sh
python3 -I -B tools/engine/verify_native_sources.py /path/to/source-cache
```

This checks all 320 source files by hash, all 574 notice excerpts byte-for-byte,
and every registry package/checksum against the four source Cargo locks. It
does not download, execute source, resolve dependencies or compile anything.
tmux provenance remains the supplied hashed build receipt and notices.

`verify` checks internal consistency, not authenticity of an untrusted download.
The installer must verify its trusted archive/manifest checksum and use the full
inventory digest, not just the shortened digest in the display payload ID.
