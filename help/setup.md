# Setup — start a team with working practices you can shape

You are setting agent-backbone up for the person you work for. Do the steps
in order; each ends with a check. Use the person’s known goals and preferences;
ask them for missing choices, accounts and secrets where needed.

Backbone connects agents across CLIs and projects. Setup should leave the person
with a useful collaboration and editable starting practices, not just running
sessions. Keep their project instructions and chosen permissions in force.

Requirements: macOS or Linux, Python 3.11+, `tmux`, and at least one agent
CLI on the PATH (`claude`, `codex`, `opencode`, `deepcode`, `gemini`, …),
already signed in. Use the runtime’s own setup if authentication is missing.

## 1. Install

```bash
uv tool install agent-backbone     # or: pipx install agent-backbone
backbone --help
```

If `backbone` is not found, run `uv tool update-shell` and use the full path
(`~/.local/bin/backbone`) until the human reloads their shell. `ab` is the
same command (macOS: `/usr/sbin/ab` may shadow it — use `backbone`).

## 2. Initialise and run

```bash
backbone init                 # data dir (~/.local/share/agent-backbone), .env with an API key, database
backbone service install      # runs now and at every login (launchd / systemd --user)
backbone doctor               # tmux, runtimes, credentials, API reachable
```

Check: `backbone doctor` reports `API: up`. Without a login service,
`backbone up --detach` runs it inside a tmux session named `backbone`.

To restart after changing secrets: `backbone service restart`, or `backbone down && backbone up --detach`
when you run it by hand.

If your CLI refuses `backbone init` or a later step on its own, with no
dialog (Claude Code's auto mode can refuse it as creating agents), a yes in
chat does not change that. Do not retry or work around it: ask the human to
switch you to asking before each step (Shift+Tab in Claude Code) and to
approve the steps, then continue.

### Choose starting practices

Before starting agents, read `backbone docs team` and the three starter policies:

```bash
backbone templates show starter-collaboration
backbone templates show starter-continuity
backbone templates show starter-learning
backbone templates use
```

Explain the starting model: a continuing repository agent owns context and the
integrated result, consults specialists and brings in temporary help as useful.
It is an editable recommendation, not a required hierarchy. The practices preserve
reasoning, check whether lessons transfer, and revise them after feedback.

For a new team, select the practices as part of setup:

```bash
backbone templates use starter-collaboration starter-continuity starter-learning
```

This replaces the global assignment. If policies are already selected, keep the
person's choices and discuss any additions rather than replacing them blindly.
Show how `templates edit NAME`, `templates use NAME...` and `templates use --clear`
change or remove the practices. Do not create repository memory files or a private
fleet structure by assumption. Policies do not grant permission or change sandboxes.

## 3. Start the first agent from its repository

If you are yourself running in the repository the human is working in, do
not start an agent there: it would be a second conversation, and a later
`--resume` would bring that one back instead of this one. Skip starting another agent,
finish the others, then tell the human how to move this session in: exit it,
then in this folder run `backbone agent start --resume --runtime <this CLI>
--no-wait` and `backbone agent attach <name>` (the name `start` prints). That
continues the folder's most recent conversation, this one (Claude Code lists
its conversations first; pick the top one). If Backbone already has an agent
for this folder from an earlier attempt, `--resume` opens that agent's saved
conversation instead: after attaching, switch to this one with the CLI's own
command (`/resume` in Claude Code and Codex). Later starts and restarts
continue it only with `--resume`.

```bash
cd <repository> && backbone agent start            # default runtime: claude
cd <repository> && backbone agent start --runtime codex
```

The agent is named after the directory and owns the repository in
`git remote origin`. `start` waits for readiness, or reports a prompt or timeout needing attention.
`backbone runtimes` lists runtimes, whether each is installed, and example
model ids — pick from there; never ask the human for a model id.

Check:

```bash
backbone agent inspect <name>          # state: idle, delivery: ready, and the evidence
backbone templates preview <name>      # next launch content and selected policies
backbone tell <name> "Summarise this repository in three sentences."   # → "outcome": "delivered"
```

Folder-trust dialogs are answered for you (`agents.pre_trust`). What you
cannot do is grant an agent shell permissions: for Claude Code the human adds
an allowlist once to `~/.claude/settings.json` so agents can run the
lifecycle commands unattended — give them this snippet:

```json
{
  "permissions": {
    "allow": [
      "Bash(backbone help)",
      "Bash(backbone help *)",
      "Bash(backbone docs *)",
      "Bash(backbone status)",
      "Bash(backbone agent list)",
      "Bash(backbone agent start)",
      "Bash(backbone agent start *)",
      "Bash(backbone agent stop *)",
      "Bash(backbone agent restart)",
      "Bash(backbone agent restart *)",
      "Bash(backbone agent inspect *)",
      "Bash(backbone agent approve *)",
      "Bash(backbone agent watch *)",
      "Bash(backbone agent unwatch *)",
      "Bash(backbone agent subscribe *)",
      "Bash(backbone agent unsubscribe *)",
      "Bash(backbone swarm *)",
      "Bash(backbone tell *)",
      "Bash(backbone reply *)"
    ]
  }
}
```

`backbone agent set` is left out on purpose: it can make an agent run
without prompts (`unattended=true`), and that is the human's decision.

Agents use their own GitHub CLI credentials to push and merge pull requests.
The backbone's credentials support issue intake and its issue API; they do not
sign an agent into `gh`. Check that separately before assigning GitHub work.
Merge and permission rules come from the repository and the operator.

## 4. Connect GitHub (issues become the agents' task list)

```bash
gh auth token | backbone secrets set GITHUB_TOKEN    # piped: never a token in a command argument
```

If `gh` is not logged in, ask the human for a token with `repo` scope and
run `backbone secrets set GITHUB_TOKEN` without a value — it prompts, so
the token stays out of shell history and out of this conversation. Restart
(step 2). Check: `backbone doctor` reports GitHub credentials and poll intake.

That is poll intake, every 60 s, for every repository an agent owns or
watches. Webhooks (instant, plus every repository the human ever creates)
need a GitHub App and a tunnel — a human task with checkpoints:
`backbone docs github-app-setup`.

## 5. Connect Telegram (optional; the human creates the bot)

The human creates a bot with @BotFather and gives you the token and their
chat id. Then:

```bash
backbone secrets set TELEGRAM_TOKEN                   # prompts
backbone config set telegram.allowed_chat_ids '[<chat id>]'
```

Restart (step 2). In a group with Topics enabled, every agent gets its own
topic. Details: `backbone docs telegram`.

## 6. A useful collaboration

Use the person's actual work to try a collaboration, such as the README example
in `backbone docs team`. Discover existing peers before starting another. Give the
specialist a useful purpose and scoped context; keep the repository agent responsible
for integrating and checking the result. Wait for the actual reply, not just the
delivery receipt. Capture a useful correction and its reasoning in the location the
person chooses, then check what would transfer to a second project.

Start further agents from their own directories (step 3). A specialist does not
need a GitHub repository. If coordination across repositories helps, an orchestrator
is an ordinary agent that watches them:

```bash
cd <orchestrator repository> && backbone agent start --watch acme/app --watch acme/web
```

Playbooks: `backbone help agents`, `backbone help messaging`,
`backbone help github`, `backbone help swarms`. Reference pages:
`backbone docs` (getting-started, concepts, how-it-works, configuration,
cli, api, github, telegram, security, …).

## 7. Report back

Tell the human what is running (`backbone status`), which agents you
started, and exactly which steps still need them: the permissions
allowlist (step 3), a GitHub token or App (step 4), a Telegram bot (step 5).
Name the selected policies, show where to edit or remove them, and explain where
the team will retain context. Report what the first collaboration produced and
what remains untested. If it has not happened yet, leave it as the next step rather
than claiming that running agents or installing policies demonstrates a working team.
