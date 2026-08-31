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
