# CLI reference

`backbone usage` opens the quick guide, including reading and publishing progress.


Starting an existing agent reuses its saved CLI and model, and resumes its saved
conversation when a matching runtime session ID is available. With no saved ID,
it starts fresh; `--resume` explicitly allows the runtime's own last-conversation
fallback. Use `backbone agent start NAME --fresh` for a new conversation with the
same settings (API: `resume: false`; omitted or `null` means automatic).
Changing runtime without specifying a model clears the previous runtime's model.
Starting from a directory reuses its registered name, even after a rename;
if several agents share that directory, specify a name.

`backbone --help` lists everything; `ab` is the same command under a short
name; `-v` enables debug logging. Commands go
through the running backbone's API when it is up and fall back to the
database (and tmux) directly when it is not — except `agent approve`,
`agent deny` and `tell`, which only work through the API so that every keystroke into an
agent is audited. `diagnostics` also requires the API and reports unavailable
when it cannot read a result.

## Quick reference and Tab completion

```bash
backbone agent start --attach       # start here and open the session
backbone agent resume app --attach  # return to a stopped agent's conversation
backbone status --watch             # follow agents and swarms; Ctrl-C exits
backbone help agent start           # recall this command's options
backbone help swarms                # read the capability playbook
```

Install completion once for the shell you use. Both `backbone` and `ab` complete
commands, options, agent names, swarm names, runtime names, settings, tags and
instruction names. Tab shows matching choices, extends a common prefix, and
fills in a unique match. At an empty argument it also offers available flags;
flags already supplied are omitted unless they can be repeated.

```bash
backbone completion zsh --install   # ~/.zshrc (respects ZDOTDIR)
backbone completion bash --install  # ~/.bashrc
backbone completion fish --install  # fish/conf.d under XDG_CONFIG_HOME or ~/.config
```

Setup adds one marked block, backs up an existing file, and is safe to run again.
Shell configuration outside that block is preserved, including dotfile symlinks.
The block loads the current completion wrapper on each shell start, so upgrades
do not require regenerating a saved script. Use `--rc-file PATH` for a different
startup file (for example, `~/.bash_profile` for a Bash login shell that does not
source `.bashrc`). `--uninstall` removes only the managed block; open a new shell
to unload its functions. Zsh initializes `compinit` only if it is not already
loaded and keeps existing key bindings, including FZF's Tab widget.

Open a new terminal, or use the activation command printed by the installer.
For Zsh/Bash that is `source <(backbone completion zsh)` or
`source <(backbone completion bash)`; for Fish, `backbone completion fish | source`.
Without `--install`, the command still prints a script for manual integration.

Try these without executing them (`<Tab>` means press the key):

```text
ab <Tab>                         # commands and global options
ab agent st<Tab>                 # start and stop
ab agent attach <Tab>            # known agents and attach options
ab status <Tab>                  # available status flags
ab agent start --runtime co<Tab> # completes codex
```

For inline suggestions in Zsh, load
[zsh-autosuggestions](https://github.com/zsh-users/zsh-autosuggestions) in your
shell, then enable Backbone's integration:

```zsh
backbone completion zsh --install --suggestions
source <(backbone completion zsh --suggestions)
```

Existing history suggestions retain their priority. When history has no match,
Backbone commands get a faint suggestion from the completion engine, including
agent names and flags, without pressing Tab. Right arrow at the end of the line
accepts the hint; it does not execute the command. Other commands retain their
existing suggestion strategies. The integration uses the plugin's asynchronous
fetching and installs no plugin itself. Fish provides inline suggestions natively;
Bash setup provides Tab completion only.

Names
are read directly from the selected SQLite database, including stopped agents;
the service need not be running. Completion never creates or migrates a database,
starts an agent or contacts a model provider. Missing or locked data produces no
names rather than an error in your prompt. PostgreSQL installations currently
get static command/option completion; name discovery uses local SQLite only.
Directory arguments complete directories, and `--rc-file` completes files.
Unknown agent names and free-text messages do not fall back to unrelated files.

## `backbone report` · `backbone updates`

Publish a short structured progress report with `backbone report --file report.json`
(or `--file -` for stdin). `--example` and `--schema` expose the authoring tool;
`--validate` checks a file without publishing. Managed agents use `$BACKBONE_AGENT`;
other callers supply `--agent NAME`. Oversized reports fail with field-specific errors.

`backbone updates` reads the latest report per agent, including missing reports
and report age. Use repeated `--agent NAME`, `--history`, `--members`, `--limit`,
or `--cursor` to select and page; `backbone updates show ID` opens a full report
and its titled links. `--json` supports scripts and orchestrators. These commands
require the running API for publishing/reading, and never interrupt an agent.
See [Agent progress reports](reports.md) for the format, bounds and reporting protocol.

## `backbone init [--data-dir DIR] [--force]`

Creates the data directory with `.env` (generated `BACKBONE_API_KEY`, mode
0600), the SQLite database (migrated), and `state/`. Keeps an existing
`.env` unless `--force`.

## `backbone secrets set KEY [VALUE] | unset KEY | list | path`

The one `.env` the backbone reads lives in the data directory
(`~/.local/share/agent-backbone/.env` by default), not in any repository:
the backbone runs `agent start` *inside* your projects, which have `.env`
files of their own, so it deliberately reads only its own. `set` writes a
value (prompted when omitted, so it never enters shell history; a `# KEY=`
placeholder from `init` is filled in place; mode stays 0600), `unset`
removes one, `list` shows which known secrets are present (names only),
`path` prints the file. The running backbone reads it at startup —
`backbone down && backbone up --detach` after a change.

## `backbone doctor`

Checks and prints ✓/✗ for: data dir and `.env`, database reachable, each
known agent's directory and runtime binary, tmux on PATH, installed
runtimes, API key, GitHub credentials and effective intake, Telegram
allowlist, whether the API is up. Exit code 1 if anything failed.

## `backbone up [--detach] [--reload]` · `backbone down`

Runs the backbone: API, Socket.IO, background jobs, Telegram bot, GitHub
intake — one process. `--detach` runs it inside a tmux session
(`backbone.session_name`); `down` stops it gracefully. `--reload` restarts
on code changes (development).

`backbone up --detach` is manual: a tmux session ends with a reboot. To
have the backbone start at login and restart if it dies, install the
login service once:

```bash
backbone service install     # macOS: a LaunchAgent; Linux: a systemd --user unit
backbone service status      # running | installed | not installed | unsupported
backbone service restart     # what `backbone upgrade` does after upgrading
backbone service uninstall
```

Where there is no launchd or `systemd --user` — a container, a minimal
image — `install` says so and leaves nothing behind; `status` reports
`unsupported`. Run `backbone up --detach` there instead (and again after
a restart).

The service runs `backbone up` in the foreground with the data directory
you installed it from; its log is `<data_dir>/backbone.log` on macOS and
`journalctl --user -u agent-backbone` on Linux. Agents are still tmux
sessions and still need `backbone agent start` after a reboot.

## `backbone upgrade [--check] [--no-restart]`

New code in, one restart, agents untouched. Upgrades the package through
the installer that put it there (`uv tool upgrade agent-backbone` or
`pipx upgrade agent-backbone`; a development checkout runs whatever is
checked out, so nothing is downloaded), then restarts the backbone: the
login service when it is installed, otherwise the `backbone up --detach`
tmux session. Waits for the API to answer and prints the running version.
Agents are tmux sessions and the queue is in the database, so the restart
loses nothing but a few seconds of API. `--check` only reports the
installed version and the newest on PyPI.

The running backbone also restarts itself: once a minute it compares the
code on disk with what it started as and, when they differ and nothing is
being routed, re-executes `backbone up` in place
(`backbone.restart_on_upgrade`, on by default). So `uv tool upgrade`, or
pulling the checkout, is enough; `backbone upgrade` is the same thing
done now. A development checkout switched to another branch is left
alone — that is development, not an upgrade; the restart happens when
the branch the backbone started on moves. `backbone service restart` is
the plain building block.

## `backbone runtimes`

Every runtime the backbone knows, whether its binary is on `PATH`, and
example model ids that work with `--model` (Claude Code's aliases, the id
Codex shows in its status line, Deep Code's two models). The list is a
starting point for agents choosing a model for another agent or a swarm
member; the runtime's own model picker is the authority.

Each line also prints the reasoning-effort levels that runtime accepts —
`low, medium, high, xhigh, max` for Claude Code, the same plus `ultra`
for Codex, `-` for a CLI with no effort setting. Unlike model ids, these
are checked: a level the runtime does not have is refused at start.

OpenCode and Aider use literal colon tags in model IDs. Their model values,
such as `ollama/qwen3:8b`, pass through unchanged at registration, update and
launch; a suffix like `:high` is also a model tag for those runtimes. The
`model:effort` convention continues to select reasoning effort for Codex and
Claude Code.

The `unattended:` line shows the no-approval switch and sandbox support;
`-` means unattended startup is unsupported. See the
[permission boundaries](security.md#unattended-agents-and-writable-directories)
before enabling it and [configuration](configuration.md#agents) for settings.

## `backbone status`

Bordered tables show Agent, State, CLI, Model and Work columns, grouped into
ordinary agents and swarms. At 80 columns each agent occupies one row. Wider
terminals reveal longer model names and full repository paths; below 80 columns
CLI/model share a column, and below 60 they move to `--details`. Long cells use
an ellipsis. Compact views show the repository name without its owner;
`--details` includes full model/repository names, tags, replies and activity times.
Work shows the current issue, repository or configured description; an offline
agent's saved issue is labelled `Last:`.

Agents needing attention appear first, then busy, starting and idle agents.
A divider separates offline agents. States have colors as well as text labels;
`waiting` is the compact label for `waiting_for_human`. Reasons, provider errors
and plan titles appear in aligned details below the tables. Component failures
are shown above the roster. State comes from
the same hook/terminal inference as `agent inspect`; an API authentication error
is an error, not an offline result. With the service down, local tmux and saved
state remain available.

```bash
backbone status --running           # only live sessions
backbone status --tag backend       # one group
backbone status --swarm audit       # one swarm
backbone status --details           # last replies and terminal activity times
backbone status --watch --interval 3
backbone status --json              # full snapshot, including state evidence
```

`--plain` uses ASCII borders and disables color; `NO_COLOR` disables color.
Redirected output contains no terminal escapes. `--watch` requires a terminal
with cursor support. It refreshes in the alternate screen, adapts to width
changes on the next refresh, and restores the previous screen and cursor on
Ctrl-C. This also applies with `--plain`. A roster taller than the window is
clipped with an ellipsis instead of scrolling repeated snapshots; use `--running`,
`--tag` or `--swarm` to focus the view, or omit `--watch` to see every row.
`--json` returns one snapshot for scripts. Activity is terminal activity, and a
last reply is labelled as such: neither implies a task completion percentage.
Use `agent inspect NAME` for the terminal tail and recent deliveries. Repository
owners/watchers and intake diagnostics remain available through `/api/status`.

## `backbone diagnostics [--since WHEN] [--agent NAME] [--limit N] [--json]`

Groups recorded operational warnings and errors, with occurrence counts,
first/last observation times and IDs for investigation. The default is the
last 24 hours and at most 20 groups. `--since` accepts positive durations
such as `30m`, `24h` and `7d`, or ISO timestamps with an explicit timezone.
`--limit` accepts 1–100. Normal waits while an agent is busy remain aggregate
delivery counts; message content and terminal tails are excluded.

`backbone diagnostics show ID [--json]` reads one record and at most 100
records with its exact operation identity.
`backbone diagnostics trace OPERATION_ID [--json]` accepts the operation ID
from a delivery receipt and reads up to 100 matching records. Its JSON
response includes `operation_id`, `count`, `truncated`, `items` and
`next_before_id`; use the records API to page through more. All forms require the API and
exit 1 when no result can be read. JSON failures carry
`{"error": "diagnostics_unavailable", "detail": "…"}`.

Diagnostic occurrence counts cover the retained lifetime of records last
seen in the interval. Delivery-history counts cover attempts timestamped
in the interval. An empty result does not establish system health. See
[Learning from local usage](diagnostics.md) for the evidence inventory,
retention, correlation limits and an investigator-agent brief.

## `backbone config list | get KEY | set KEY VALUE | unset KEY`

Settings live in the database with built-in defaults; see
[Configuration](configuration.md). `set` validates the value and applies
it to the running backbone at once. Values are JSON where needed:

```bash
backbone config set timing.grace_period_seconds 3
backbone config set telegram.allowed_chat_ids '[123456789]'
backbone config set escalation.target orch
```

## `backbone agent …`

| Command | Effect |
|---|---|
| `agent start [NAME…] [--dir D] [--runtime R] [--model M] [--resume \| --fresh] [--watch REPO]… [--no-wait] [--attach]` | Discover the agent from `--dir` (default: cwd), record it, start its tmux session and **wait until it is at its prompt**. A bare known `NAME` starts from its recorded directory; a bare unknown `NAME` registers the cwd under that name. Several names start a group of known agents (`ab agent start app web orch`). `--attach` opens a single session afterwards |
| `agent resume NAME… [--attach]` | Start known agents with their saved runtime conversations. An already running session is left running |
| `agent attach NAME [--read-only]` | Open a session in your terminal. Detach with Ctrl-b, then d. From inside tmux, switch the current client; read-only viewing requires a separate terminal |
| `agent list [--tag TAG] [--json]` | Known agents with runtime, model, directory and tags |
| `agent inspect NAME [--json]` | State, reason, current issue, delivery condition, the runtime's session id and the agent's last reply (when its hook reports them), the evidence, the terminal tail, recent deliveries |
| `agent stop NAME…` | Kill the session(s) |
| `agent approve NAME [--from WHO]` | Answer the permission prompt the agent's runtime is showing (Claude Code, Codex, OpenCode — each verified against a live dialog; other runtimes report `unsupported`). Checks the terminal at the moment of the call and types only if the dialog is on screen *then* — otherwise reports `not_waiting` with the terminal tail. (tmux has no check-and-send: a dialog a human answers in that same instant can receive one extra key at an empty prompt; the response says whether the dialog actually cleared.) Needs the backbone running (`backbone up`): there is no direct-tmux fallback, so every approval goes through the API and is recorded as an `approval` event. Disable with `security.allow_remote_approval false` |
| `agent deny NAME [--from WHO]` | Refuse the prompt with the runtime's refusing key (Escape for Claude Code and Codex), under the same gate and audit as `approve`. The right answer to a *choice* dialog — Codex's rate-limit "switch to gpt-5.6-luna?" has Switch preselected, so `approve` refuses it (`not_permission`) and Escape keeps the model |
| `agent set NAME key=value…` | Change `dir`, `runtime`, `model`, `repo`, `description`, `tags` (JSON list), `env` (JSON object), `always_on` and `unattended` (`true`/`false`; `unattended` launches the runtime with its own no-approval switch — see [configuration](configuration.md#agents)) |
| `agent watch [NAME] REPO…` / `agent unwatch [NAME] REPO…` | Add / remove watched repositories. Inside an agent session `NAME` defaults to the agent itself (`$BACKBONE_AGENT`), so an agent can subscribe on its own |
| `agent forget NAME` | Remove a stopped agent from the backbone (refuses while its session is still running) |
| `agent tag NAME TAG…` / `agent untag NAME TAG…` | Add/remove tags, retaining other tags. `swarm:` and `role:` tags are managed by the swarm lifecycle |
| `agent rename NAME NEW_NAME` | Rename a stopped non-swarm agent, preserving its directory, settings, watches, resume ID, queue and routing receipts. Refuses occupied names or names with existing history, active deliveries and agents participating in an active swarm |

Renaming also updates explicit Telegram routes and the escalation target.
Automatically provisioned Telegram topics follow the existing lifecycle: the
old topic closes with history retained and the new name gets a topic. Update
external `for:OLD_NAME` labels, scripts and saved command lines yourself; the
command prints a reminder. A rename never stops a running agent for you.

### Every `agent start` parameter

`agent start` is the whole declaration — there is no separate registration
step, and everything is optional except a directory to discover (given, or
the cwd). Anyone with a shell can drive it, **including another agent**:
an orchestrator that should spin up workers runs these commands itself.

| Parameter | Meaning | Recorded on the agent? |
|---|---|---|
| `NAME` (positional) | Agent name = tmux session = `for:` label. Known name: starts from its recorded directory. Unknown name: registers the cwd under it. Omitted: the folder name is the name — the usual case for single-repository agents | yes (the key) |
| `--dir D` | Project directory to discover (name defaults from its folder name; repo from its `origin` remote) | yes |
| `--runtime R` | Which CLI runs the agent: `claude` (default via `agents.default_runtime`), `codex`, `gemini`, `opencode`, `deepcode`, `aider`, or `shell` | yes — later bare starts reuse it |
| `--model M` | Passed to the runtime as `--model M` (e.g. `opus`, `sonnet`, or a full model id — whatever that CLI accepts). Use it to run cheaper models per agent. Write it as `M:EFFORT` (e.g. `gpt-6-astra:high`, `opus:max`) to set the reasoning effort too — such a spec is **not** passed verbatim: it is split, and the CLI gets the bare model plus its own effort switch. A level the runtime does not have, or an effort with no model (`:high`), is refused rather than dropped | yes — later bare starts reuse it |
| `--watch OWNER/REPO` | Also subscribe to a repository (repeatable) | yes |
| `--resume` | Allow the runtime's last-conversation fallback when no matching saved ID exists | automatic with a matching saved ID |
| `--fresh` | New conversation, keeping saved CLI and model | no |
| `--always-on` | Instead of names: start every agent marked `always_on` (after a reboot, with `--resume`) | — |
| `--no-wait` | Return immediately instead of waiting for the prompt | no |
| `--attach` | Open this one session after startup; an interactive terminal is required | no |

Examples:

```bash
backbone agent start                                  # this repo, defaults
backbone agent start --model opus                     # this repo, cheaper model
backbone agent start orch --dir ~/ws/orch --watch acme/app
backbone agent start --dir ~/ws/api --runtime codex --model gpt-5.2
backbone agent start recruiter-desk                   # known agent, recorded settings
```

Recorded settings are changed later with `agent set NAME model=sonnet`
(or `runtime=…`, `dir=…`), and a one-off override at start
(`agent start NAME --model haiku`) also updates the record.

Moving a project: registration is keyed by name (default: the folder name).
Starting a known name from a new directory updates the record **if the old
directory is gone** — the agent follows the move, keeping its watches and
settings. If the old directory still exists, the new one is a different
project that happens to share a folder name and is registered as `name-2`.
A changed folder name is a new agent; `agent forget` removes the old one.

`agent start` reports one of:

```
app: ready — claude repo acme/app                      # at its prompt
app: started, waiting for you — claude repo acme/app   # e.g. Claude's folder-trust question; tmux attach -t app
app: started but not at its prompt yet                 # timeout; the last terminal lines are shown
app: already running
```

## `backbone swarm create|list|status|disband`

Run a coordinator+members swarm on one existing issue — see
[Swarms](swarms.md). `create NAME --issue OWNER/REPO#N [--member SPEC]…`
starts the roster in a shared worktree; `disband NAME` stops the members
and removes the worktree (the branch is kept). The swarm's issue being
closed (normally by merging the coordinator's PR) tears it down
automatically. `backbone tell <swarm-name> …` reaches its coordinator.

`swarm status NAME --watch` follows a roster using the same layout and filters as
`status`; `--details`, `--json` and `--plain` work there too. `swarm list` includes
completed swarms. Open a member with `backbone agent attach MEMBER`.

## `backbone instructions …`

Startup instructions are Markdown files in the data directory. Assignments are
settings in the database. Preview an agent's next launch before changing it:

```bash
backbone instructions list
backbone instructions preview app
backbone instructions preview app --json
backbone instructions show base
backbone instructions show swarm:common
backbone instructions edit swarm:scout        # template for newly created swarms
backbone instructions path base
backbone instructions edit coding
backbone instructions use coding             # replace the global assignment
backbone agent tag app backend
backbone instructions edit python
backbone instructions use python --tag backend
backbone instructions validate
```

`edit` uses `$VISUAL`, then `$EDITOR`, on a temporary draft. Only a successful,
nonempty edit is saved atomically; editor failure or a concurrent edit leaves
the original untouched. Set the editor first, for example `export EDITOR=vi`.
`path` prints the file to edit with any other tool. Editing a policy does not
assign it automatically. `use` replaces the ordered list for its scope; omit
policy names to clear that scope. Policies are deduplicated after composing
global names, then matching tags in alphabetical order. Runtime changes retain
tags and assignments. Use `role:scout` or `swarm:audit` as a policy scope when
appropriate; membership in those tags is managed by the swarm.

`base` refers to the common environment brief. Editing it for the first time
copies the shipped template and adds `{shared_policy}`, where global/tag
instructions will be inserted. Existing overrides without that placeholder
continue to replace the whole brief; preview explicitly marks policies skipped.
Swarm members use their saved role brief plus selected policies on each fresh
start. Preview reports the sources, paths, injection mode and effective content;
it describes the next launch, not the contents of an existing conversation.
Project/runtime-owned instructions are still loaded by the runtime itself.
`instructions list` also lists shipped/overridden swarm template sources.
`show`, `path` and `edit` accept `swarm:common`, `swarm:coordinator`, `swarm:scout`
or a custom role. These edits affect newly created swarms; existing members
keep their saved role brief and compose the currently selected policies.
See [Shared policy](configuration.md#shared-policy-and-environment-facts).

## `backbone help [TOPIC]`

The backbone explains its own capabilities to agents: no argument lists
the topics (`setup`, `agents`, `messaging`, `swarms`, `github`, …), a
topic name prints the full playbook. Also served at
`GET /api/help[/{topic}]`. `setup` is the one written for an agent that
is installing the backbone for a person: install, run, first agent,
GitHub, Telegram, and exactly where a human is needed.
Every backbone-started Claude agent carries a short injected brief (see
`agents.inject_brief`) that points here, so the injected text stays
small while the capability surface can grow. Add or override topics by
dropping markdown files into `<data_dir>/help/`.

A command path such as `backbone help agent start` prints that command's parser
help. The plural topics (`agents`, `swarms`) remain the longer playbooks.

## `backbone docs [PAGE]`

The documentation in this directory, from the installed package: no
argument lists the pages with their titles, a page name
(`getting-started`, `concepts`, `how-it-works`, `cli`, …) prints it.
The wheel ships `docs/` inside the package so an agent that installed the
backbone from PyPI can read the reference without a checkout; a source
checkout reads the same files from the repository. Also served at
`GET /api/docs[/{page}]`.

## `backbone tell AGENT MESSAGE… [--from NAME] [--priority]`

Delivers `[via:backbone from:NAME] MESSAGE` through the running backbone
(`POST /api/messages`) and prints the outcome JSON. The sender defaults to
`$BACKBONE_AGENT` (set in every backbone-started session), so an agent's
messages are attributed to the agent, not the human account; `--from`
overrides it. `--priority` lets the
message through while a human is typing or the agent is settling; it never
interrupts a busy agent — that is an invariant, not a gap. A message that
cannot be delivered now (`agent_working`, `offline`, …) is stored in the
queue and the monitor delivers it when the agent is ready, oldest first;
queued messages expire after `timing.queue_expiry_minutes` (default 30).
The reply prints one sentence saying what happened: delivered; stored
(`"queued": true`); the same message from you already waiting
(`"queue": "already_queued"` — nothing was added); or not stored
(`"queue": "failed"` — send again later). Exit code 0 if delivered, 2 if
the message is in the queue, 1 if it is not (API error or storage
failure). Multi-line messages are pasted with bracketed paste and arrive
intact as a single message.

The JSON includes `operation_id`, `delivery_id` and `queue_id` when available.
A queued or unsuccessful reply also prints its operation ID after the explanation;
use `backbone diagnostics trace OPERATION_ID` to follow that message through
retries, submission, expiry or retirement. A retirement does not mean the message
was submitted.

## `backbone reply TEXT… [--agent NAME]`

The other direction: an agent answers the humans on the channel they use.
`POST /api/integrations/reply` posts the text into the agent's surface on
every enabled integration — on Telegram, the forum topic mapped to that
agent. Inside an agent session `--agent` defaults to `$BACKBONE_AGENT`.
Exit 1 with the reason when no integration is configured or none has a
surface for the agent yet. See [Integrations](integrations.md).

## `backbone hooks install|uninstall claude|codex|gemini [--dir PROJECT]`

Sessions started by `agent start` need no install: the backbone wires its
hooks into every launch it performs (Claude Code `--settings`, Codex `-c`
overrides, Gemini's system-settings path, OpenCode's inline config), from
files it owns under `<data_dir>/hooks/` and regenerates on every start,
without touching any repository or the CLI's own settings.

`hooks install` is for sessions started outside the backbone. It copies the
hook files into `<data_dir>/hooks/` and adds tagged entries to the CLI's
settings: `~/.claude/settings.json`, `~/.codex/hooks.json` or
`~/.gemini/settings.json` (with `--dir`, the project's `.claude/`,
`.codex/` or `.gemini/` file instead). Re-running is idempotent;
`uninstall` removes only the backbone's entries. Codex asks once to trust
hooks it has not seen: accept them with `/hooks` in a session. OpenCode
loads its plugin only through the launch wiring; there is no install.
The hooks prefer `$BACKBONE_STATE_DIR` (exported into every session the
backbone starts), so one global install serves any data directory. Restart
running sessions afterwards.

## `backbone templates`

Find, edit and preview injected instructions. `list [--json]` shows sources and
assignments; `show NAME` prints content; `path [NAME]` locates editable files;
`edit NAME` opens the editor; `init [NAME...]` copies defaults without overwriting.
`use [POLICY...] [--tag TAG]` replaces global or tag assignments;
`preview AGENT [--json]` shows effective next-launch content;
`validate [AGENT]` checks required instructions. Names are `base`, `swarm:ROLE`,
or `policy:NAME`. See [Templates](templates.md) for examples and adoption rules.

`backbone agent tag NAME TAG...` adds persistent group tags;
`backbone agent untag NAME TAG...` removes them. Swarm/role identity tags cannot
be changed with these commands. Changes take effect at the next fresh launch.

Completion installers serialize updates to the same resolved rc file using a
`.backbone-completion.lock` sidecar, retained alongside the file. The backup and
conflict check protect existing content. Editors and dotfile managers that do not
use that lock must not write the rc file concurrently with installation.

Automatic resume requires an adapter that can address the saved session exactly.
Codex, Claude Code, Gemini and OpenCode pass the saved ID; other adapters start
fresh automatically. Explicit `--resume` may select the runtime's latest session
when no saved ID is available. Gemini's [session guide](https://geminicli.com/docs/cli/session-management/)
and OpenCode's [CLI guide](https://opencode.ai/docs/cli/) describe their ID flags.

`upgrade --no-restart` obtains a hold from the running API before installing code.
The hold lasts until a manual service restart, even if installation fails after
changing files. It does not change `backbone.restart_on_upgrade`. If the running
API cannot acknowledge the hold (for example an older release), the CLI refuses
to install; update/restart that service first. If no API is reachable, there is
no running API to coordinate; do not start another service during the upgrade.
