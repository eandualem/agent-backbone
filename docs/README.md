# agent-backbone documentation

Start with **Concepts** for the mental model, then **Getting started** to run
it, then **How it works** for the end-to-end flows. Everything else is
reference.

| Read this | When you want to |
|---|---|
| [Concepts](concepts.md) | Understand the handful of words the whole system is built from |
| [Getting started](getting-started.md) | Install, start two agents from their directories, send the first message, add GitHub |
| [How it works](how-it-works.md) | Follow a start, a message, an issue and a Telegram command through the system |
| [Configuration](configuration.md) | Every setting (`backbone config`), every secret, the data directory |
| [CLI](cli.md) | `backbone init / doctor / up / status / config / agent / swarm / tell / hooks` |
| [Swarms](swarms.md) | A coordinator plus members on one worktree, one branch, one issue, one PR |
| [HTTP & Socket.IO API](api.md) | Build a dashboard or script against the backbone |
| [GitHub integration](github.md) | Repositories, labels, routing rules, intake modes, what an agent is expected to do |
| [GitHub App setup](github-app-setup.md) | The step-by-step production setup: App + webhook via Cloudflare Tunnel or ngrok, with checkpoints |
| [Integrations](integrations.md) | The contract every human-facing channel implements, and how to add one |
| [Telegram](telegram.md) | Bot setup, allowlist, forum topics, commands |
| [Security](security.md) | What is protected by default and what you opt into |
| [Status and roadmap](status-and-roadmap.md) | What works today, what is deliberately missing, what is next |

Conventions: `reviewer`, `builder`, `orch` are example agent names; `acme/app`
and `acme/web` are example repositories; `<data_dir>` is
`~/.local/share/agent-backbone` unless you changed it.
