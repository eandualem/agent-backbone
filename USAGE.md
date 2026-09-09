# Quick usage

**Open this guide in your terminal: `backbone usage`** (`ab usage` works too).
`backbone docs usage` opens the same page. Use `backbone --help` for all commands.

Prefer an agent to handle setup? Point it to [README.md](README.md) and ask it
to follow `backbone help setup`.

## Start Backbone manually

```bash
uv tool install "agent-backbone[github-app]"
backbone init
backbone service install          # start now and at login
backbone status
```

Without a login service (for example, in a container), use `backbone up --detach`.
Run `backbone doctor` if setup needs attention.

## Work with an agent

```bash
cd ~/code/app
backbone agent start --runtime codex --model gpt-6-astra:high
backbone agent attach app         # open its terminal; Ctrl-b d detaches
backbone agent start app          # continue its saved conversation if stopped
backbone agent start app --fresh  # new conversation, same CLI and model
backbone updates                  # latest progress from the team
```

An already running agent stays running. Starting from its directory also finds
its registered name. Choose `--runtime` and `--model` explicitly to change them;
changing CLI without a model clears the old CLI's model setting.

## Read agent progress

```bash
backbone updates                              # latest reports from the team
backbone updates --agent Feynman               # one agent's latest report
backbone updates --agent Feynman --history     # its earlier reports
backbone updates show 12                       # full report and links (use an ID shown above)
backbone updates --limit 20                    # a larger page
```

When a page has more results, it prints the command for the next page. Reports
show goals, progress, blockers and next steps. “No report yet” means that agent
has not published one. Reading reports does not interrupt agents or ask them to
produce one. `backbone status` shows whether agents are working; `backbone updates`
shows what they say they have accomplished.

In Telegram, use `/updates`, `/updates Feynman`, `/updates history Feynman`, or
`/updates show 12`. Each new report is also posted automatically to the configured
agents group and, for ordinary agents, the agent’s own topic, with buttons for the full report and team feed. Delivery retries
when Telegram is unavailable; older reports from before this feature are not sent.

## Publish your progress (for agents)

```bash
backbone report --example > progress.json
# Replace the example with your goal, progress, blockers and next steps.
backbone report --file progress.json --validate
backbone report --file progress.json
```

The author defaults to the current agent; use `--agent NAME` outside an agent
session. Reports must be short; oversized reports are rejected. See
`backbone help reports` for the section limits and when to publish.

## Rename an agent

Wait until the agent finishes its work, then:

```bash
backbone agent stop planner
backbone agent rename planner Feynman
backbone agent start Feynman --attach
```

The directory, settings, watches and saved conversation ID stay with the agent.
Use the new name in messages, scripts and external `for:` labels. Renaming requires
a stopped agent; active swarm members cannot be renamed.

## Find more

- `backbone agent start --help` — command options.
- `backbone templates list` — injected instructions you can inspect and edit.
- `backbone docs index` — the [documentation reading guide](docs/INDEX.md).
- `backbone docs usage` — this page from an installed package.

Ordinary agent reports appear in General and the agent's topic, with separate
delivery receipts. Swarm participants, including coordinators, cannot publish human-facing reports.
Only their repository agent reports consolidated progress to the owner. Optional full-report voice messages can be enabled with
`backbone config set telegram.report_audio true` after local speech setup.
See `backbone docs report-audio` for the model, service, voice and FFmpeg setup.


During long work, agents can read corrections themselves without waiting for
terminal delivery: `backbone inbox` (or `--agent NAME`). After applying or
superseding messages, use `backbone inbox --ack TOKEN ...`. Check between meaningful
steps and before a commit or handoff. Details: `backbone help messaging`.

For `--ack`, copy each complete `ack_token` from the inbox response. Numeric row
IDs alone cannot acknowledge work; tokens prevent stale acknowledgements from
consuming a different message after queue cleanup.
