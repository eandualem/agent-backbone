# Agents — starting, configuring and managing them

Any agent may start other agents; you do not need a human for this.

```bash
backbone agent start                       # this directory becomes an agent
backbone agent start NAME                  # known agent: its recorded settings
backbone agent start NAME --dir D          # register a directory under a name
backbone agent start --runtime codex --model gpt-5.2
backbone agent start --model opus          # model recorded, reused next start
backbone agent stop NAME…                  # kill sessions
backbone agent forget NAME                 # remove a stopped agent's record
```

- Runtimes: `claude` (default), `codex`, `gemini`, `opencode`, `aider`,
  `cursor`, `shell` — the binary must be installed.
- `--model` is passed to the runtime CLI verbatim and recorded on the
  agent, so later bare starts reuse it. Change with
  `backbone agent set NAME model=…` (also `runtime=`, `dir=`).
- The name defaults to the folder name; only name agents that need an
  identity (coordinators). Starting a known name from a new directory
  follows a move when the old directory is gone; a same-named directory
  that still exists elsewhere registers as `name-2`.
- Claude Code's folder-trust dialog is answered automatically for
  directories you start agents in (`agents.pre_trust`).

## Watching repositories

Subscribe yourself to a repository's issues (your name is implied inside
your own session):

```bash
backbone agent watch OWNER/REPO
backbone agent unwatch OWNER/REPO
```

You will be notified of new issues there, and `for:<your-name>` labels
route issues to your queue.
