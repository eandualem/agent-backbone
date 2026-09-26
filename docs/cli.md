# CLI reference

`backbone help usage` opens the quick guide, including reading and publishing progress.
`backbone usage` now opens the [token usage overview](token-usage.md). Use
`--agent NAME --current` for running conversations, `session ID` for requests,
`--by model` for model totals, `--cost` for optional estimates, and `--json`
for structured data. `usage limits` shows supported allowance snapshots.
See [CLI presentation](cli-presentation.md) for the table/detail convention, narrow
terminal behavior and machine-readable output contracts.


Starting an existing agent reuses its saved CLI and model and begins a **new
conversation**. Continuing the previous one is your call: `backbone agent resume
NAME` (or `start --resume`; API `resume: true`) reopens the session the backbone
last saw for that runtime, or the runtime's own last conversation when no ID is
saved. A fresh start says when a previous conversation is available. Starts are
fresh by default because a resumed agent trusts its own context over whatever
happened in the checkout since — another CLI, a swarm, the shared memory.
The project's handoff and shared memory carry progress across fresh sessions,
including CLI changes. Read them according to the project's instructions. Use
explicit resume when you intend to return to a previous conversation, for example
after an unexpected closure; it cannot know what other sessions did in between.
Codex resume honors `--model`, including an effort suffix, while retaining the
saved conversation. Changing runtime without specifying a model clears the previous runtime's model.
Starting from a directory reuses its registered name, even after a rename;
if several agents share that directory, specify a name.

`backbone --help` lists everything; `ab` is the same command under a short
name; `-v` enables debug logging. Commands with offline support fall back to the local database and tmux when
the API is unavailable. Delivery, approvals, inbox operations, reports, replies
and diagnostics require the running API; their sections below describe failures.
Looking up the API address reads only existing
address settings; it never creates or repairs the database. All API clients use
the server’s port precedence: process `BACKBONE_PORT`, then the data directory’s
`.env`, then `backbone.port`. Commands that need local agent configuration load
it explicitly; offline database operations retain their normal initialization.

Resuming by saved ID requires an adapter that can address the session exactly.
Codex, Claude Code, Gemini and OpenCode pass the saved ID; for other adapters
`--resume` falls back to the runtime's own latest session, as it does for every
runtime when no ID is saved. Gemini's [session guide](https://geminicli.com/docs/cli/session-management/)
and OpenCode's [CLI guide](https://opencode.ai/docs/cli/) describe their ID flags.

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

Completion installers serialize updates to the same resolved rc file using a
`.backbone-completion.lock` sidecar, retained alongside the file. The backup and
conflict check protect existing content. Editors and dotfile managers that do not
use that lock must not write the rc file concurrently with installation.

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
known agent's directory and runtime binary, tmux on PATH, at least one agent
CLI on PATH (with none, it fails and names the supported CLIs; `agent start`
would fail next), API key, GitHub credentials and effective intake, Telegram
allowlist, whether the API is up, and any stored setting the current rules reject (an older release may have accepted it; it is ignored in favour of the default until fixed). Exit code 1 if anything failed. For each
installed agent CLI it also lists the capabilities that runtime lacks, with the
issue tracking each ([runtime capabilities](runtime-capabilities.md)); these are
reported, not failed. It also names each user-level instruction file an
installed CLI adds to its sessions, such as a non-empty `~/.codex/AGENTS.md`
or `~/.claude/CLAUDE.md`, so its content never reaches agents unnoticed; `agent
start` lists them for the agent it starts, with the agent's own environment
applied. These are notes, not failures.

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

On macOS the LaunchAgent is an interactive job (`ProcessType`). The tmux
server and every agent the service starts inherit its scheduling, and
launchd's default for a LaunchAgent would throttle them below ordinary
apps, so their screens fall behind when the machine is busy. An older
install lacks the key: run `backbone service install` again to rewrite it,
which restarts the service. A tmux server that is already running keeps its
old priority until it restarts, and all its sessions with it. To apply it
now: let your agents save their work, stop them (`backbone agent stop
NAME…`), end the old server with `tmux kill-server` (this closes every
session still on it), then start the same agents again by name
(`backbone agent start NAME…`, a fresh conversation; `backbone agent resume
NAME` continues the previous one); the service starts a new tmux server with
the new scheduling.

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

`upgrade --no-restart` obtains a hold from the running API before installing code.
The hold lasts until a manual service restart, even if installation fails after
changing files. It does not change `backbone.restart_on_upgrade`. If the running
API cannot acknowledge the hold (for example an older release), the CLI refuses
to install; update/restart that service first. If no API is reachable, there is
no running API to coordinate; do not start another service during the upgrade.

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

Bordered tables show Agent, State, CLI, Model, Tags and Work columns, grouped
into ordinary agents and swarms. At 80 columns each agent occupies one row. Wider
terminals reveal longer model names, more tags and full repository paths; below
80 columns CLI/model share a column and tags move to `--details`, and below 60
they all do. Long cells use an ellipsis. Compact views show the repository name
without its owner; `--details` includes full model/repository names, tags,
replies and activity times.

Model is the model the agent is **actually answering with** when its runtime's
hook could observe it (Claude Code: read from the transcript after each reply;
shown bold), otherwise the saved selection, otherwise `default` — the CLI's own
choice, which the backbone has not seen. `--details` names the source. Tags are
the group tags that select policies and skills (`backbone agent tag`); swarm
identity tags are implied by the grouping. Work shows the current issue,
repository, configured description or, failing those, the directory name; an
offline agent's saved issue is labelled `Last:`.

Agents needing attention appear first, then busy, starting and idle agents.
A divider separates offline agents. States have colors as well as text labels;
`waiting` is the compact label for `waiting_for_human`. Reasons, provider errors
and plan titles appear in aligned details below the tables. Component failures
are shown above the roster. State comes from
the same hook/terminal inference as `agent inspect`; an API authentication error
is an error, not an offline result. With the service down, local tmux and saved
state remain available. A state followed by `?` (`idle?`, `busy?`) repeats a
hook state older than `timing.stale_threshold_seconds` because the terminal was
inconclusive (`state_source` `stale` in `--json`); treat it as unconfirmed.
Other tmux sessions grouped with an agent's session (a terminal viewer's
`new-session -t NAME`) show that agent's windows and are not listed.

When a command gets no answer from the API it says why: nothing is listening
(start it with `backbone up`), the connection or answer timed out, or **this
process may not connect** (`Operation not permitted`). The last case is a
sandbox or permission boundary around the caller, such as a Codex task
without network access: the service may be running and its agents working.
If the tmux socket is refused too, `status` and `agent inspect` report the
agent states as unknown from that process instead of listing everyone as
offline. Run the command where local network and the tmux socket are
allowed (for Codex, a command approved to run outside its sandbox).

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
| `agent start [NAME…] [--dir D] [--runtime R] [--model M] [--resume \| --fresh] [--watch REPO]… [--no-wait] [--attach]` | Discover the agent from `--dir` (default: cwd), record it, start its tmux session and **wait until it is at its prompt**. A bare known `NAME` starts from its recorded directory; a bare unknown `NAME` registers the cwd under that name. A `NAME` that differs from a registered one only in letter case (`alfred` beside `Alfred`) is refused, naming the registered agent, rather than registered as a second agent that messages to the first never reach. Several names start a group of known agents (`ab agent start app web orch`). `--attach` opens a single session afterwards |
| `agent resume NAME… [--attach]` | Start known agents continuing their saved runtime conversations (the same as `agent start --resume`); a bare `agent start` is always a fresh conversation. An already running session is left running |
| `agent attach NAME [--read-only]` | Open a session in your terminal. Detach with Ctrl-b, then d. From inside tmux, switch the current client; read-only viewing requires a separate terminal |
| `agent list [--tag TAG] [--json]` | Known agents with runtime, model, directory and tags |
| `agent output [NAME] [--lines N] [--before OFFSET \| --since OFFSET [--end OFFSET]] [--screen] [--json]` | What the agent has **said**, **reads only**: its user-facing messages — progress, commentary and replies — complete and never shortened, out of the runtime's own transcript for Claude Code (`~/.claude/projects/*/<session>.jsonl`) and Codex (`~/.codex/sessions/**/rollout-*<session>.jsonl`), located from the session id its hook recorded. Tool calls, tool results and thinking are not messages and are not shown. A page is `--lines` messages (default 20, max 200): the last ones by default; `--before OFFSET` the ones ending at or before an offset (go back); `--since OFFSET` the ones starting at or after it (continue), `--end OFFSET` bounding a range. Every page prints its offsets and whether more lies before or after, so nothing is dropped silently; a page never cuts a message. Other runtimes, `--screen`, or a missing transcript show the visible terminal instead, ANSI stripped, and the first line says which you got. `NAME` defaults to `$BACKBONE_AGENT`; works through the API, or directly when the backbone is down |
| `agent inspect NAME [--json]` | State, reason, current issue, delivery condition, the runtime's session id and the agent's last reply (when its hook reports them), the evidence, the terminal tail, recent deliveries, watches and subscriptions |
| `agent stop NAME…` | Kill the session(s) |
| `agent restart [NAME] [--runtime R] [--model M] [--resume \| --fresh] [--in DURATION \| --at TIME] [--message TEXT] [--stop-only] [--from WHO] [--json]` | Stop the session and start its replacement after a wait. The backbone owns the transition — it accepts and validates the request (registered agent, installed runtime, model and effort, same-CLI resume), stops the session on its next tick and starts the replacement `DURATION` after the stop (default `1m`; `90s`, `20m`, `3h20m`) or `--at` an ISO 8601 time, through the same launch as `agent start` with the normal instructions — so an agent can ask for its own session and lose nothing. `--message` reaches the replacement once it is at its prompt and never expires while it waits. `--stop-only` stops and starts nothing. One open transition per agent. The result (`pending`, `completed` with the launch's readiness, `failed` with the reason) is in `agent inspect`. `NAME` defaults to `$BACKBONE_AGENT`; needs the running backbone. See [how it works](how-it-works.md#1-starting-an-agent) |
| `agent approve NAME [--from WHO]` | Answer the permission prompt the agent's runtime is showing (Claude Code, Codex, OpenCode — each verified against a live dialog; other runtimes report `unsupported`). Checks the terminal at the moment of the call and types only if the dialog is on screen *then* — otherwise reports `not_waiting` with the terminal tail. (tmux has no check-and-send: a dialog a human answers in that same instant can receive one extra key at an empty prompt; the response says whether the dialog actually cleared.) Needs the backbone running (`backbone up`): there is no direct-tmux fallback, so every approval goes through the API and is recorded as an `approval` event. Disable with `security.allow_remote_approval false` |
| `agent deny NAME [--from WHO]` | Refuse the prompt with the runtime's refusing key (Escape for Claude Code and Codex), under the same gate and audit as `approve`. The right answer to a *choice* dialog — Codex's rate-limit "switch to gpt-5.6-luna?" has Switch preselected, so `approve` refuses it (`not_permission`) and Escape keeps the model |
| `agent set NAME key=value…` | Change `dir`, `runtime`, `model`, `repo`, `description`, `tags` (JSON list), `env` (JSON object), `always_on`, `unattended` and `inbox_only` (`true`/`false`; `unattended` launches the runtime with its own no-approval switch, `inbox_only` marks a client that is never launched — see [configuration](configuration.md#agents)) |
| `agent watch [NAME] REPO…` / `agent unwatch [NAME] REPO…` | Add / remove watched repositories. Inside an agent session `NAME` defaults to the agent itself (`$BACKBONE_AGENT`), so an agent can subscribe on its own |
| `agent subscribe [NAME] SOURCE FILTER [--priority normal\|high]` / `agent unsubscribe [NAME] ID` | Subscribe to inbound events from a [source](sources.md) (`gmail`) matching a filter in the source's own query language (`"from:upwork.com subject:job"`); `high` reaches a working Claude Code or Codex agent on its next tool call through hook context (other runtimes: first when ready), `normal` waits for its prompt, batched. `agent inspect` lists subscriptions with their ids. `NAME` defaults to `$BACKBONE_AGENT` inside a session |
| `agent forget NAME` | Remove a stopped agent from the backbone (refuses while its session is still running) |
| `agent tag NAME TAG…` / `agent untag NAME TAG…` | Add/remove tags, retaining other tags. `swarm:`, `role:` and `task:` tags are managed by the swarm lifecycle |
| `agent rename NAME NEW_NAME` | Rename a stopped non-swarm agent, preserving its directory, settings, watches, resume ID, queue, pending restart and routing receipts. Refuses occupied names or names with existing history, active deliveries and agents participating in an active swarm |

`agent set` rejects unknown field names both online and in direct mode. A typo in
a mixed update rejects the entire request; no valid fields are partially applied.

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
| `--resume` | Continue the conversation the backbone last saw for this runtime (the runtime's own last conversation when no ID is saved); same as `agent resume` | no |
| `--fresh` | New conversation, keeping saved CLI and model — the default, spelled out | no |
| `--always-on` | Instead of names: start every agent marked `always_on` (after a reboot, with `--resume`) | — |
| `--no-wait` | Return immediately instead of waiting for the prompt | no |
| `--attach` | Open this one session after startup; an interactive terminal is required | no |
| `--inbox-only` | Register a client without a terminal instead of launching anything: it reads its messages with `backbone inbox --agent NAME` (see [configuration](configuration.md#agents)). Takes one agent and no launch options | yes |

Examples:

```bash
backbone agent start                                  # this repo, defaults
backbone agent start --runtime claude --model opus    # this repo, explicit Claude model
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

Startup instructions are Markdown files in `templates.dir` (by default
`<data_dir>/templates/`). Assignments are
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
assign it automatically. `use NAME...` replaces the ordered list for its scope; omit
policy names to inspect that scope, or pass `--clear` to empty it. Policies are
deduplicated after composing
global names, then matching tags in alphabetical order. Runtime changes retain
tags and assignments. Use `role:scout` or `swarm:audit` as a policy scope when
appropriate; membership in those tags is managed by the swarm.

`base` refers to the common environment brief. Editing it for the first time
copies the shipped template and adds `{shared_policy}`, where global/tag
instructions will be inserted. Overrides without that placeholder
have selected policies appended. A custom base does not suppress them.
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

## `backbone tell AGENT MESSAGE… [--from NAME] [--priority | --steer]`

Delivers `[via:backbone from:NAME] MESSAGE` through the running backbone
(`POST /api/messages`) and prints the outcome JSON. The sender defaults to
`$BACKBONE_AGENT` (set in every backbone-started session), so an agent's
messages are attributed to the agent, not the human account; `--from`
overrides it. `--priority` lets the
message through while a human is typing or the agent is settling; it never
interrupts a busy agent — that is an invariant, not a gap. A message that
cannot be delivered now (`agent_working`, `offline`, …) is stored in the
queue and the monitor delivers it when the agent is ready, oldest first;
ordinary queued messages expire after `timing.queue_expiry_minutes` (default 30);
active swarm coordination and inbox holds are retained, and nothing behind an
unconfirmed (`uncertain`) delivery expires while that hold stands. When messages
expire, the recipient and each registered agent that sent one get a
`[via:backbone]` notice listing them.
The reply prints one sentence saying what happened: delivered; stored
(`"queued": true`); the same message from you already waiting
(`"queue": "already_queued"` — nothing was added); or not stored
(`"queue": "failed"` — send again later). Exit code 0 if delivered, 2 if
the message is in the queue, 1 if it is not (API error or storage
failure). With no answer from the API the error says whether the request
was sent. If it was not, the message was not accepted; if it was (a read
timeout or a dropped answer), the outcome is unknown, so check `agent
inspect` before sending it again. Multi-line messages are pasted with bracketed paste and arrive
intact as a single message.

The JSON includes `operation_id`, `delivery_id` and `queue_id` when available.
A queued or unsuccessful reply also prints its operation ID after the explanation;
use `backbone diagnostics trace OPERATION_ID` to follow that message through
retries, submission, expiry or retirement. A retirement does not mean the message
was submitted.

`--steer` is a different thing from `--priority`: guidance for the agent's
**current task** — a clarification, a correction, context it lacks — not a new
task and not queue-jumping. It goes through `POST /api/steer` as a transient
offer to the agent's runtime hook, never as a queue row and never as a paste:
Claude Code and Codex hand it to the model as context on the agent's next tool
call, so it cannot overwrite a draft or answer a dialog. It is accepted only
while the agent is `agent_working` on one of those runtimes in a session the
backbone started; otherwise it is **refused with the reason** (`not_working`,
`unsupported_runtime`, `offline`, `no_launch_id`) and nothing is queued — send
an ordinary message instead. The guarantee is at-most-once handoff, not
incorporation: the delivery record (`kind` `steer`, in `agent inspect`) moves
from `offered` to `handed_off` when the hook took it, `not_taken` when the turn
ended first or no tool call took it within five minutes, or `cancelled` when the
session was replaced first. Exit 0 when offered, 1 otherwise.

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

## `backbone chrome install|uninstall`

Optional. Claude-in-Chrome titles every agent's tab group "Claude", so
agents sharing one Chrome are hard to tell apart. With this installed, a
group an agent's session opened is renamed after the agent (`contract-desk`
→ "Contract Desk"):

1. The Claude Code hook reads each `tabs_context_mcp` result (the Claude
   extension's own account of the session's tab group; page output from other
   tools is never trusted) and records the group and its tabs in
   `<state_dir>/chrome-groups/<agent>.<group>.json`. The agent does nothing
   different.
2. A small extension, loaded once, asks a native messaging host for those
   records (no network, no API key) and renames a group only while it still
   has Claude's default title and still holds one of the recorded tabs. A
   group a person renamed, and every other group, is left alone. Claude's
   own extension tracks its groups by id, so it keeps working, and it does
   not overwrite a title that is no longer its default.

```bash
backbone chrome install      # registers the host, copies the extension
# then in Chrome: chrome://extensions → Developer mode → Load unpacked → the printed folder
backbone chrome uninstall    # removes both; also remove the extension in Chrome
```

The extension needs `tabGroups`, `tabs`, `nativeMessaging` and `alarms`; it
never reads pages. A group is renamed within about 30 seconds of the agent's
first `tabs_context_mcp` result for it; a group a `navigate` call opened
first is named at the agent's next `tabs_context_mcp`. Only Claude Code agents are covered (Codex drives Chrome
through its own extension), and only Google Chrome's default user directory.

## `backbone templates`

Find, edit and preview injected instructions. `list [--json]` shows sources and
assignments; `show NAME` prints content; `path [NAME]` locates editable files;
`edit NAME` opens the editor; `init [NAME...]` copies defaults without overwriting.
`use [POLICY...] [--tag TAG] [--clear]` shows assignments without names,
replaces them with names, or empties them with `--clear`;
`preview AGENT [--json]` shows effective next-launch content;
`validate [AGENT]` checks required instructions. Names are `base`, `swarm:ROLE`,
or `policy:NAME`. See [Templates](templates.md) for examples and adoption rules.

## `backbone skills`

One shared store of skills (`skills.store`, default `~/skills`), each tagged
with the agents it is for; at launch the backbone links the right ones into the
directory the agent's CLI reads. `list [--tag TAG] [--json]` shows store skills,
tags and the agents each reaches; `show NAME` prints one; `path [NAME]` locates
the store or a skill; `add PATH [--name N] [--tag T]… [--replace]` moves a skill
directory into the store and tags it; `tag NAME [TAG…]` replaces its tags;
`preview AGENT [--json]` — or just `backbone skills AGENT` — shows what the next
launch links and where;
`validate [AGENT]` checks the store and every selection. See [Skills](skills.md).

`backbone agent tag NAME TAG...` adds persistent group tags;
`backbone agent untag NAME TAG...` removes them. Swarm/role identity tags cannot
be changed with these commands. Changes take effect at the next fresh launch.

## Cooperative message checkpoints

`backbone inbox [--agent NAME]` reads up to ten direct messages (for an inbox-only agent, escalation notices too) at a worker's own
checkpoint without terminal injection; `backbone inbox --ack TOKEN ...` confirms
application or deliberate supersession. Unacknowledged receipts are replayed on later
reads, including after a lost response/restart. Do not repeat work for an
`uncertain` message until checking whether its earlier paste already arrived.
Uncertain submissions pause automatic terminal delivery for that session until
resolved, preventing blind retries from duplicating accepted assignments.

The normal queue expiry remains `timing.queue_expiry_minutes` (30 minutes).
Messages to or from active swarm members are exempt while that swarm is active;
checkpoint and uncertain holds remain until acknowledged. Ending the swarm
removes the active-swarm exemption for ordinary pending rows. These are retention
rules, not a promise of timely steering: busy agents are never interrupted,
including by priority messages. Workers must check their inbox between meaningful
steps and before integration. See [messaging help](../help/messaging.md).


The CLI uses `BACKBONE_AGENT` inside a managed session; outside one, pass
`--agent NAME`. It requires the running API. An empty `messages` list means no rows were available to claim at that instant;
a queue drainer may temporarily hold a lease. Check again at the next checkpoint. A normal successful terminal submission
is not also copied here. Uncertain holds can include issue notifications;
ordinary pending issue notifications retain their GitHub acknowledgement flow.

For `--ack`, copy each complete `ack_token` from the inbox response. Numeric row
IDs alone cannot acknowledge work; tokens prevent stale acknowledgements from
consuming a different message after queue cleanup.
