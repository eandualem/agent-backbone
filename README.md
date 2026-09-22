# agent-backbone

[![PyPI](https://img.shields.io/pypi/v/agent-backbone)](https://pypi.org/project/agent-backbone/)
[![Downloads](https://img.shields.io/pypi/dm/agent-backbone)](https://pypistats.org/packages/agent-backbone)
[![CI](https://github.com/eandualem/agent-backbone/actions/workflows/ci.yml/badge.svg)](https://github.com/eandualem/agent-backbone/actions/workflows/ci.yml)
[![Visitors](https://api.visitorbadge.io/api/visitors?path=eandualem%2Fagent-backbone&label=visitors&countColor=%23263238)](https://github.com/eandualem/agent-backbone)

Run terminal coding agents as a team on your machine.

agent-backbone manages persistent tmux sessions for Claude Code, Codex, OpenCode
and other agent CLIs. Agents can message each other, delegate work through GitHub
Issues and form teams across runtimes and repositories. You can inspect their
state, join their terminals, or talk to them through Telegram.

It uses your existing agent logins and model access. No repository configuration
is required for core use; optional shared skills and swarms create their own
links and worktrees.

![Two agents started through Backbone review each other's work](docs/media/demo.gif)

*Installation from PyPI and collaboration between Claude Code and OpenCode on
Linux. Unedited recording at 3× speed.*

## What it enables

- **Communication:** agents address each other by name. Messages wait in a
  durable queue while the recipient is busy or needs a person.
- **Management:** start, stop, inspect and attach to sessions. State readings
  include the evidence behind them.
- **Delegation:** GitHub issues reach the agent responsible for a repository;
  labels route work to specific agents. An orchestrator is an ordinary agent
  that watches several repositories.
- **Teams:** a swarm puts a coordinator and workers on one issue, sharing a
  worktree and branch. Closing the issue tears down its sessions.
- **Usage visibility:** inspect tokens by agent, CLI conversation and model,
  with optional API-equivalent estimates. [Coverage and limits](docs/token-usage.md).

New sessions receive a brief explaining these tools. Agents choose how to
collaborate; Backbone tracks delivery and runtime state, not task completion.
Resumed sessions keep their conversation without receiving another brief.

## Getting started

You need macOS or Linux, Python 3.11+, tmux, [uv](https://docs.astral.sh/uv/)
(or pipx), and an agent CLI installed and signed in.

```bash
uv tool install "agent-backbone[github-app]"
backbone init
backbone service install           # start now and at login
backbone doctor                    # check dependencies and API access
```

If `backbone` is not on your PATH, run `uv tool update-shell` and open a new
terminal. In a container without a login service, use `backbone up --detach`.
`ab` is a short alias; on macOS, use `backbone` if Apache Bench shadows it.

From a repository you want an agent to read:

```bash
cd ~/code/app
backbone agent start --runtime claude   # or --runtime codex, --runtime opencode
backbone tell app "Summarise what this repository does in three sentences."
backbone agent attach app               # read its reply; Ctrl-b d detaches
```

The directory supplies the agent name. If startup reports a prompt or a timeout,
use `backbone agent inspect app` to see what needs attention. `tell` reports
whether the message was delivered or queued; delivery does not mean the agent
has finished answering. Do not resend a message accepted into the queue.

[Getting started](docs/getting-started.md) walks through setup, a second agent
and optional integrations. [Quick usage](USAGE.md) is the everyday command guide.
You can also ask an existing agent with shell access to install the package and
follow `backbone help setup`.

## Going further

- [GitHub](docs/github.md): route issues and review notifications, starting with
  token-based polling. [GitHub App setup](docs/github-app-setup.md) adds webhooks.
- [Swarms](docs/swarms.md): put several specialists on one task.
- [Telegram](docs/telegram.md): send messages and read reports from your phone.
- [API](docs/api.md): build a dashboard or automation; Backbone ships no UI.
- [Runtime support and limits](docs/status-and-roadmap.md): checked capabilities,
  unsupported environments and known limitations.
- [How it works](docs/how-it-works.md): state detection, delivery and routing.

The [documentation index](docs/INDEX.md) covers configuration, diagnostics,
skills and the remaining references. Installed copies are available through
`backbone docs`; `backbone help` contains short agent playbooks.

## Security model

Backbone runs as one trusted OS user on one machine. Agents are not isolated
from each other. The API binds to localhost by default and uses one full-admin
key; local sessions can read that key through the CLI. Backbone keeps its
control-plane secrets out of launched environments, but this is not a filesystem
security boundary. Read [Security](docs/security.md) before granting access or
exposing the API.

## Development

```bash
git clone https://github.com/eandualem/agent-backbone
cd agent-backbone
make install
make check
BACKBONE_DATA_DIR=/tmp/backbone-dev make dev
```

See [Contributing](CONTRIBUTING.md) for branches, checks and releases.

## License

[MIT](LICENSE).
