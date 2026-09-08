# Agent instruction templates

See what Backbone tells your agents, change it without editing Python, and share
short required rules with a group of tagged agents.

## Three different kinds of text

- `templates/` contains injected instructions: the ordinary agent's base brief,
  swarm common and role briefs, the coordinator's kickoff, and optional policies.
- `help/` contains on-demand playbooks, read with `backbone help TOPIC`.
  Reading or editing help does not automatically inject it into an agent.
- `docs/` contains the user reference, read with `backbone docs PAGE`.

These directories are at the repository root and ship in the installed package.
Historical review snapshots live in Git history; their actionable work is tracked
in issues and pull requests, rather than mixed into the current reference.

## Find and edit

```bash
backbone templates list
backbone templates show base
backbone templates show swarm:scout
backbone templates path                       # installed editable directory
backbone templates edit base                 # uses $VISUAL, then $EDITOR
backbone templates edit swarm:coordinator
backbone templates preview api               # registered agent's next launch
backbone templates preview api --json         # content, sources and notices
backbone templates validate                  # files and registered agents
```

Names are `base`, `swarm:ROLE`, or `policy:NAME`. Bare names also select policies.
`swarm:common` is included before every role; an unknown role falls back to
`swarm:worker`. `swarm:kickoff` is the coordinator's initial work message; its
provenance envelope is supplied by code. `instructions` remains a command alias.

Edits go to `<data_dir>/templates/`, normally
`~/.local/share/agent-backbone/templates/`. `path NAME` prints the editable target;
`list` shows the effective source and any legacy override. Edit an individual file,
or use `backbone templates init` to copy all available templates there. You can
also name specific templates, such as `backbone templates init base swarm:scout`.
Existing copies are never overwritten. Copies are pinned: future package updates
change bundled defaults, not your local instructions. Delete an override to follow
the bundled default again (or a legacy override, if one still exists).

The editor works on a temporary copy. An empty result, failed editor, or concurrent
change leaves the active file untouched. An editor that launches a window should
wait until you close it; configure its wait option in `$VISUAL` or `$EDITOR`.

## Global rules and tagged groups

Create a policy and assign it to everyone, or to a particular tag:

```bash
backbone templates edit policy:team-rules
backbone templates use team-rules
backbone templates edit policy:python
backbone templates use python --tag python
backbone agent tag api python
backbone templates preview api
```

Assignments replace that scope's current list; pass several names to keep several
policies. `backbone templates use --tag python` clears that tag's assignment;
`backbone templates use` clears global assignments. `backbone agent untag api python`
removes the tag from that agent. Tags persist across runtime switches. The
`python-example` policy ships as an optional starting point and is **not assigned**.

For swarm roles, assign policies with `--tag role:scout` or `--tag role:coordinator`.
Swarm members automatically receive their role and `swarm:NAME` tags. The tag/untag
commands cannot change these identity tags. New rules apply when a new conversation
starts; tagging does not interrupt or send messages to an existing session.

Composition is deterministic:

1. The base brief, or the swarm's common preamble plus role brief.
2. Global policies, in their configured order.
3. Policies for matching tags, with tags sorted alphabetically and each tag's
   policy order preserved. A policy selected more than once is included once.

Assignments are database settings: `agents.shared_policy` and `agents.tag_policy`.
Markdown stays in files. Selected missing, unreadable or empty policies fail launch
with their path. Custom base and explicit role briefs also receive selected policies;
they cannot silently suppress required rules. Policies contain literal text, including
braces in examples. Keep them short and review them with `preview`.

## Placeholders and launch behavior

The base brief supports `{agent_name}` and `{repo}`. Swarm role/common templates
support `{swarm_name}`, `{agent_name}`, `{role}`, `{repo}`, `{issue_number}`, `{issue_url}`,
`{branch}`, `{worktree}`, `{base_branch}`, `{coordinator}`, `{initiator}`,
and `{members}`. The kickoff supports `{swarm}`, `{repo}`, `{issue_number}`, `{title}`,
and `{issue_url}`. Other braces remain literal.

`{shared_policy}` optionally chooses where policies appear in the base or combined
role brief; use it at most once. Without it, policies are appended automatically.
The actual launch and `preview` use the same composition. Project and runtime-owned
instructions can also load and remain outside Backbone's template management.

`agents.inject_brief=false` disables ordinary base and policy injection. Explicit
swarm role briefs still apply with their policies. Plain shell agents receive no
instructions. Resuming an initial-prompt or message-based runtime preserves its
conversation and does not resend the brief. Existing swarms reuse saved role briefs;
role template edits apply to newly created swarms. Updating files does not change
what an already-running agent has read. Plan fresh starts at safe stopping points;
never interrupt busy agents to adopt a template.

## Existing installations

Canonical overrides take precedence, then legacy files, then bundled defaults:
`agent-brief.md` maps to `templates/base.md`, `swarm-templates/ROLE.md` to
`templates/swarm/ROLE.md`, and `policies/NAME.md` to `templates/policies/NAME.md`.
`edit` and `init` copy the effective legacy text into the new location without
removing the old file. Legacy `help-topics/` remains readable; new help overrides
belong in `<data_dir>/help/`.

Behavior change: legacy full base overrides now receive configured global and tag
policies too. Previously they could suppress shared policy. Preview existing agents
before their next fresh launch; clear an assignment explicitly if it is no longer
wanted. No new policy is enabled by this update.
