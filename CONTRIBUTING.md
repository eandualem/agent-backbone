# Contributing

Thanks for your interest. agent-backbone is small on purpose; the bar for a
change is that it keeps the model in [docs/concepts.md](docs/concepts.md)
intact and is covered by tests.

## Setup

```bash
git clone https://github.com/eandualem/agent-backbone && cd agent-backbone
make install          # uv sync --all-extras
make check            # ruff lint + format check + pytest
```

Tests run against SQLite in memory and mock tmux; nothing external is needed.

## Working on it

- `make dev` runs the backbone with auto-reload; use a scratch data directory
  (`BACKBONE_DATA_DIR=/tmp/backbone-dev`) so your real one is untouched.
- Schema changes: edit `services/database/models.py`, then regenerate the
  single initial migration (`make db-migrate MSG="initial schema"` after
  deleting the old one) — the project is pre-1.0 and ships one migration.
- New settings go in `SETTINGS_DEFAULTS` (with help text) in `config.py` and
  in [docs/configuration.md](docs/configuration.md).
- Anything that pastes into an agent goes through `safe_deliver`; anything
  that decides agent state goes through `get_agent_state`. Keep it that way.

## Pull requests

- One logical change per PR, with a message that says why.
- `make check` must pass; CI runs the same on 3.11–3.13.
- Update the docs page that describes the behaviour you changed.

## Releasing (maintainers)

Releases are published to [PyPI](https://pypi.org/project/agent-backbone/)
by `.github/workflows/release.yml` when a version tag is pushed:

```bash
uv version 2.0.0a1            # bump pyproject.toml; commit it
git tag v2.0.0a1 && git push origin main v2.0.0a1
```

The workflow refuses a tag that does not match `pyproject.toml`, runs the
tests, builds with `uv build` and publishes with `uv publish`. It needs the
`PYPI_API_TOKEN` repository secret (Settings → Secrets and variables →
Actions; `gh secret set PYPI_API_TOKEN` prompts for it). The token is never
in the repository or in the data directory's `.env`.

To publish from a laptop instead: `uv build && UV_PUBLISH_TOKEN=… uv publish`.

`docs/` ships inside the wheel (`backbone docs`), so a docs-only change is
still worth a release.
