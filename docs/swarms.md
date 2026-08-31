# Swarms

A swarm is one **coordinator** plus a configurable set of **members**
that share a single git worktree and branch to complete **one existing
GitHub issue**. Members are ordinary backbone agents — every state,
delivery, and audit mechanism applies to them unchanged — so you can
attach to any member's tmux session and watch it work.

```bash
backbone swarm create research --issue acme/app#42 \
    --member 'scout*3@claude/sonnet' --member coder@claude/opus
```

That single command:

1. verifies `acme/app#42` exists and is open (the issue is a
   prerequisite — an agent or human writes it first; the swarm never
   creates it),
2. creates a worktree at `<checkout>/.backbone/swarms/research` on the
   branch `swarm/research`, inside the repository checkout of the
   initiating agent (`--initiator`, defaulting to `$BACKBONE_AGENT`, or
   the repo's owner agent),
3. registers and starts each member in that worktree — here a
   `research-coordinator` (added automatically), `research-scout-1..3`
   on Sonnet, and a `research-coder` on Opus — each with a **role brief**
   injected as a system prompt (Claude Code) or first message (other
   runtimes); nothing is ever written into the repository,
4. delivers the kickoff to the coordinator, which reads the issue and
   starts assigning work.

## The communication model

- **Inside the swarm**: members and the coordinator message each other
  with `backbone tell` — the ordinary agent-to-agent pipeline (queued
  when busy, audited, provenance-labeled). The issue plays no role here.
- **Swarm ↔ initiator**: the coordinator is the swarm's only outside
  voice. It posts progress and questions as comments on the issue, and
  may also `tell` the initiating agent directly.
- **You ↔ swarm**: `backbone tell research "..."` — a swarm's name
  resolves to its coordinator.

## The lifecycle

Work ends when the coordinator opens a pull request from the swarm
branch with `Closes #N` in the body. Merging the PR closes the issue;
the backbone sees the issue-closed event and tears the swarm down
automatically: members stopped and forgotten, worktree removed, branch
kept. `backbone swarm disband <name>` is the manual teardown (also
keeps the branch, so no work is ever destroyed).

```bash
backbone swarm list              # every swarm with roster and status
backbone swarm status research   # one swarm
backbone swarm disband research  # manual teardown
```

## Roster syntax

`--member ROLE[*N][@RUNTIME[/MODEL]]`, repeatable:

| Example | Meaning |
|---|---|
| `'scout*3@claude/sonnet'` | three scouts on Claude Code with the Sonnet model |
| `coder@codex` | one coder on Codex, its default model |
| `reviewer` | one reviewer on the default runtime |
| `coordinator@claude/opus` | the coordinator (at most one; added automatically if omitted) |

## Roles and briefs

Shipped role briefs: `coordinator` (plans, assigns file ownership,
tracks, reports on the issue, opens the PR), `scout` (read-only
research), `coder` (implements an owned vertical slice), `reviewer`
(verifies by running, never fixes). Any other role name gets a generic
worker brief driven by the coordinator's instructions.

Every brief starts from a common preamble covering the shared-worktree
rules (one branch, file ownership, no scope creep) and the exact
communication commands. To customize, copy a template from the package
(`services/swarm/templates/`) into `<data_dir>/swarm-templates/<role>.md`
— files there override the shipped ones, and you can add new roles the
same way.

## When to use a swarm

Parallel, breadth-first work: research fan-outs, competing-hypothesis
debugging, features whose pieces can be owned separately. A swarm costs
a multiple of a single agent's tokens — for sequential or tightly
coupled work, one agent is faster and cheaper. Start with 3–5 members.
