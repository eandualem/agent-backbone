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

1. verifies `acme/app#42` exists and is open — GitHub must be
   configured, one issue can have at most one active swarm, and the
   swarm's name must not collide with an agent's (the issue is a
   prerequisite — an agent or human writes it first; the swarm never
   creates it, and an agent may only swarm on its **own** repository —
   the swarm runs in the initiator's checkout, which must be on a real
   branch, not a detached `HEAD`),
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
branch with `Closes #N` in the body. When the PR's base is the
repository's default branch, merging it closes the issue automatically;
for any other base branch GitHub does not auto-close, so the
coordinator (or you) closes the issue after the merge. Either way, the
backbone sees the issue-closed event and tears the swarm down:
members stopped and forgotten, worktree removed, branch kept.
`backbone swarm disband <name>` is the manual teardown. The branch —
and every commit on it — always survives teardown, but **uncommitted
edits in the worktree are removed with it**, so make sure members have
committed before disbanding.

```bash
backbone swarm list              # every swarm with roster and status
backbone swarm status research   # one swarm
backbone swarm disband research  # manual teardown
```

## Roster syntax

`--member ROLE[*N][@RUNTIME[/MODEL[:EFFORT]]]`, repeatable:

| Example | Meaning |
|---|---|
| `'scout*3@claude/sonnet'` | three scouts on Claude Code with the Sonnet model |
| `coder@codex` | one coder on Codex, its default model |
| `reviewer` | one reviewer on the default runtime |
| `coordinator@claude/opus` | the coordinator (at most one; added automatically if omitted) |
| `coordinator@codex/gpt-6-astra:high` | the coordinator on Codex at `high` reasoning effort |

The effort rides on the model, so a roster can spend where the judgement
is and stay cheap elsewhere — a coordinator that validates and implements
at `high`, scouts that only read at the CLI's default:

```bash
backbone swarm create review --issue OWNER/REPO#7 \
    --member coordinator@codex/gpt-6-astra:high \
    --member 'scout*2@codex/gpt-6-astra'
```

Omitting the suffix means the CLI's own default, which is not always the
cheap end — GPT-6-Astra defaults to `low`. `backbone runtimes` lists the
levels each runtime accepts.

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

## What a swarm is, and is not

A swarm is **lifecycle scaffolding around ordinary agents**. The backbone
does exactly six things: it validates the issue and the roster, registers
the members, creates the worktree and branch, injects each role brief,
delivers the kickoff, and tears everything down when the issue closes.

Everything that looks like coordination is the **coordinator agent
behaving as its brief tells it to** — a prompt, not a mechanism. In
particular the backbone does not:

- **assign tasks** — the coordinator does, by `backbone tell`;
- **prevent file conflicts** — members share one worktree and one branch;
  file ownership is a rule in the briefs, and nothing enforces it;
- **track progress or verify completion** — there is no task state, no
  per-member progress, and no check that a member did what it was asked;
- **retry or reassign a member that stalls** — the backbone will *report*
  it (`agent inspect`, dead-session and stall alerts), and you or the
  coordinator decide what to do.

This is a deliberate trade: members are ordinary agents, so every state,
delivery and audit mechanism applies to them unchanged, and you can attach
to any member's session and take over by hand.

**Teardown is destructive to uncommitted work.** Disbanding runs
`git worktree remove --force`, which deletes any edits members had not
committed. The branch and its commits always survive. Make sure members
have committed before you disband.

## When to use a swarm

Parallel, breadth-first work: research fan-outs, competing-hypothesis
debugging, features whose pieces can be owned separately. A swarm costs
a multiple of a single agent's tokens — for sequential or tightly
coupled work, one agent is faster and cheaper. Start with 3–5 members.

## Members stuck on permission prompts

Codex and OpenCode members (and Claude Code outside auto mode) stop on
approval dialogs. The backbone shows this as `waiting_for_human
(permission)` in `backbone agent inspect <member>` with the prompt quoted
in the evidence, and `backbone agent approve <member>` answers it — the
runtime's affirmative key, sent only while the dialog is on screen, and
recorded as an `approval` event. The coordinator's brief tells it to do
this instead of reaching into tmux directly.
