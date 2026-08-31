# agent-backbone documentation

Start with **Concepts** to get the mental model, then **How it works** for the
end-to-end flows. Everything else is reference.

| Read this | When you want to |
|---|---|
| [Concepts](concepts.md) | Understand the eight words the whole system is built from |
| [Getting started](getting-started.md) | Install, configure two agents, send the first message |
| [How it works](how-it-works.md) | Follow a message, an issue, and a Telegram command through the system, step by step |
| [Configuration](configuration.md) | Every `backbone.toml` key and environment variable |
| [CLI](cli.md) | `backbone init / doctor / up / status / agent / tell / hooks` |
| [HTTP & Socket.IO API](api.md) | Build a dashboard or script against the backbone |
| [GitHub integration](github.md) | Labels, routing rules, close-then-next, what an agent is expected to do |
| [Telegram](telegram.md) | Bot setup, allowlist, forum topics, commands |
| [Security](security.md) | What is protected by default and what you opt into |
| [Status and roadmap](status-and-roadmap.md) | What works today, what is deliberately missing, what is next |
| [Architecture record](design/00-architecture-proposal.md) | Why v2 looks the way it does (decision record, not a user guide) |

Conventions used in these pages: `reviewer`, `builder`, `planner` are example
agent names; `acme/app` is an example repository; `<data_dir>` is
`~/.local/share/agent-backbone` unless you changed it.
