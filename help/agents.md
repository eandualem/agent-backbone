# Agents — starting, configuring and managing them


`backbone agent start NAME` reuses the agent's saved CLI and model but starts a
**fresh conversation**. `--fresh` makes that default explicit. The API also starts
fresh when `resume` is omitted or `false`.

Continuation across fresh sessions comes from the project's handoff and shared
memory, read according to its instructions. For example, after work in Codex,
then Claude Code, a new Codex session should read the latest handoff. Automatically
resuming the old Codex conversation would miss the work done in between.

Use `backbone agent resume NAME` (or `start --resume`; API `resume: true`) only
when you explicitly want to reopen a previous conversation, such as after an
unexpected closure. It uses the saved conversation ID for the selected runtime
when supported, or that runtime's latest conversation when no usable ID is
available. A fresh start does not replay a previous conversation.
Changing runtime without specifying a model clears the previous runtime's model.
Starting from a directory reuses its registered name, even after a rename;
if several agents share that directory, specify a name.

Any agent may start other agents; you do not need a human for this.

```bash
backbone agent start                       # this directory becomes an agent
backbone agent start NAME                  # known agent: its recorded settings
backbone agent start NAME --dir D          # register a directory under a name
backbone agent start --runtime codex
backbone agent start --runtime claude --model opus          # model recorded, reused next start
backbone agent start --runtime codex --model gpt-6-astra:high   # model *and* reasoning effort
backbone agent stop NAME…                  # kill sessions
backbone agent forget NAME                 # remove a stopped agent's record
backbone agent resume NAME --attach        # reopen a stopped conversation
backbone agent attach NAME                 # interactive terminal; Ctrl-b d detaches
backbone agent rename NAME NEW_NAME        # stopped agents only
backbone agent tag NAME backend            # grouping and shared instruction scope
backbone status --tag backend --watch      # follow a group
backbone templates preview NAME         # effective startup content and sources
backbone help agent start                  # recall command options
```

- Runtimes: `claude` (default), `codex`, `gemini`, `opencode`,
  `deepcode`, `aider`, `shell` — the binary must be installed.
  `backbone runtimes` shows which are installed.
- `--model` is recorded on the agent, so later bare starts reuse it.
  Change with `backbone agent set NAME model=…` (also `runtime=`, `dir=`).
  A plain model id is passed to the runtime CLI verbatim; a `model:effort`
  spec is split first — the CLI gets the bare model plus that runtime's own
  effort switch (see "Choosing an effort").
- The name defaults to the folder name; only name agents that need an
  identity (coordinators). Starting a known name from a new directory
  follows a move when the old directory is gone; a same-named directory
  that still exists elsewhere registers as `name-2`.
- Claude Code's folder-trust dialog is answered automatically for
  directories you start agents in (`agents.pre_trust`).

## Reading what an agent has said

```bash
backbone agent output NAME                  # its last 20 messages, complete
backbone agent output NAME --lines 50
backbone agent output NAME --before 5120    # earlier messages (offset printed by the last page)
backbone agent output NAME --since 6301     # later messages (continue from the last page)
backbone agent output NAME --since 5120 --end 9000   # a range
backbone agent output NAME --screen         # the visible terminal instead
```

Reads only: nothing is typed, pasted or attached. Claude Code and Codex keep
their own transcript; the backbone shows the agent's user-facing messages
from it — progress, commentary, replies — complete, never shortened. Tool
calls, results and thinking are not messages and are not shown. Each page
prints the commands for the earlier and later pages and says when more
exists, so read as far back as you need. Other runtimes show the visible
terminal; the first line says which you got. `agent inspect` remains the
place for state, delivery readiness and the last reply.

## Handing over to a fresh session — including your own

`backbone agent restart` is how you switch CLI or model, start over with a
clean context, or stop now and continue later. The backbone owns the
transition: it accepts the request first, stops the session on its next
tick and starts the replacement when the wait is over, so asking for your
own session is safe. `backbone agent stop` on yourself is not: it kills the
shell that would run your next command.

Before you ask, write your canonical memory — the current objective, the
decisions made, what is unfinished and the next concrete step — where the
project keeps it (its `.backbone/memory/` handoff, or whatever its
instructions prescribe). The replacement is a fresh session with the normal
startup instructions; it knows nothing you did not write down. `--message`
is a short pointer to that context, not a substitute for it.

```bash
backbone agent restart                                 # stop me; fresh session one minute later
backbone agent restart --runtime claude --model opus   # continue in Claude Code with Opus
backbone agent restart --in 3h20m --message "Resume from HANDOFF.md, section 'PR 12'"
backbone agent restart --at 2026-09-23T09:00           # local time; an offset is honoured
backbone agent restart --resume                        # same CLI, previous conversation
backbone agent restart --stop-only                     # save and stop; nothing starts
backbone agent inspect $BACKBONE_AGENT                 # the transition's status and result
```

- The wait is between the stop and the start; the stop itself is immediate.
- `--resume` needs the same CLI; a cross-CLI resume is refused with the reason.
- `--runtime` and `--model` are recorded on the agent, as with `agent start`.
- One open transition per agent; a second request is refused until it ends.
- The result is `pending`, `completed` (with the launch's readiness and the
  message outcome) or `failed` with the reason. A session someone started by
  hand during the wait counts as failed — it is never claimed as the
  transition's work.

## Choosing a model — never ask a human for an id

`backbone runtimes` prints example model ids per runtime: Claude Code
takes its aliases (`opus`, `sonnet`, `haiku`), Codex the id shown in its
own status line (e.g. `gpt-5.6-sol`), Deep Code `deepseek-v4-flash` /
`deepseek-v4-pro`. When a person names a model informally ("Sol",
"Opus"), map it to the id from that list; when the runtime is not listed
there, start it once and read its `/model` picker. Asking the human for
a model id is a usability failure, not a clarification.

## Choosing an effort — write it into the model

Reasoning effort is part of the model spec, `model:effort`, everywhere a
model can be named. There is no separate flag to forget:

```bash
backbone agent start --runtime codex --model gpt-6-astra:high     # one agent
backbone agent set NAME model=opus:max            # change it later
backbone swarm create rev --issue O/R#7 \
    --member coordinator@codex/gpt-6-astra:high \
    --member 'scout*2@codex/gpt-6-astra'          # no suffix -> the CLI's default
```

`backbone runtimes` prints the levels each runtime accepts, because they
differ: Claude Code takes `low, medium, high, xhigh, max`; Codex also
takes `ultra`. The backbone translates the level into whatever that CLI
wants (`--effort high` for Claude Code, `-c model_reasoning_effort=high`
for Codex) — never write those flags yourself.

Omit the suffix and the CLI uses its own default. Set an effort explicitly
when review depth or resource use matters; defaults can vary by CLI version.

OpenCode and Aider interpret colons as literal model tags, so
`ollama/qwen3:8b` is passed through unchanged; `:high` would also be part of
the model ID for those runtimes. They do not accept the `model:effort`
convention. Gemini and Deep Code reject an effort suffix because they have
no effort setting.

## Watching repositories

Subscribe yourself to a repository's issues (your name is implied inside
your own session):

```bash
backbone agent watch OWNER/REPO
backbone agent unwatch OWNER/REPO
```

You will be notified of new issues there, and `for:<your-name>` labels
route issues to your queue.

## Subscribing to inbound events

Sources (Gmail today) deliver references to new items that match a filter
written in the source's own query language. Several subscriptions per agent;
`high` reaches you mid-task on your next tool call as hook context (Claude
Code and Codex; on Gemini CLI, OpenCode, Aider and shell it waits, queued,
until you are ready and then comes first), `normal` waits for your prompt and
arrives as one batch:

```bash
backbone agent subscribe gmail "from:upwork.com subject:job" --priority high
backbone agent subscribe gmail "from:linkedin.com"
backbone agent inspect $BACKBONE_AGENT      # lists subscriptions with ids
backbone agent unsubscribe ID
```

A delivery names the message id, sender, subject and a link, never the
body: read it through your own Gmail connector. `backbone docs sources`.
