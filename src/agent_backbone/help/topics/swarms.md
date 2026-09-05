# Swarms — parallel workers on one issue

A swarm is one coordinator plus members sharing a single git worktree
and branch, created to complete **one existing GitHub issue** and ending
in a single PR. Use one only for breadth-first work: research fan-outs,
competing-hypothesis debugging, features whose pieces can be owned
separately. For sequential or tightly coupled work, stay single-agent —
a swarm costs several times the tokens.

## The flow, end to end

1. **Write the issue first — in YOUR OWN repository.** The swarm never
   creates the issue, and it runs in the initiator's checkout, so an
   agent may only swarm on a repository it owns (creating the issue
   elsewhere is rejected with guidance). Describe the task, constraints
   and the definition of done:
   `gh issue create --repo OWNER/REPO --title … --body …`
2. **Create the swarm on it** (from inside your session; you become the
   initiator):

   ```bash
   backbone swarm create NAME --issue OWNER/REPO#N \
       --member 'scout*2@claude/sonnet' --member coder@claude/opus
   ```

   Member spec: `ROLE[*N][@RUNTIME[/MODEL[:EFFORT]]]`. Model ids and the
   effort levels each runtime accepts come from `backbone runtimes` (see
   `backbone help agents`, "Choosing a model" and "Choosing an effort");
   do not ask the human for either. The effort rides on the model, so a
   roster can mix levels — the expensive one where the judgement is:

   ```bash
   backbone swarm create review --issue OWNER/REPO#7 \
       --member coordinator@codex/gpt-6-astra:high \
       --member 'scout*2@codex/gpt-6-astra'
   ```

   Leave the suffix off and the CLI's own default applies, which may be
   its cheapest level. OpenCode names models `provider/model`
   (`scout@opencode/google/gemini-3-flash-preview`): the runtime is the
   word after `@`, the model everything after the first `/`; `backbone
   runtimes` lists no ids for it — read `opencode models`. Roles with
   shipped briefs:
   `coordinator` (added automatically; at most one), `scout` (read-only
   research), `coder` (implements an owned slice), `reviewer` (verifies
   by running). Any other role gets a generic brief. 3–5 members total
   is the sweet spot; prefer cheap fast models for scouts.
3. **Talk only to the coordinator**: `backbone tell NAME "…"` reaches it.
   Members report to their coordinator; the coordinator reports to you
   on the issue (and may `tell` you directly).
4. **Completion**: the coordinator opens a PR from `swarm/NAME` with
   `Closes #N`. Merging it closes the issue, and the closed issue tears
   the swarm down automatically — members stopped, worktree removed,
   branch kept.
5. `backbone swarm status NAME` shows the roster;
   `backbone swarm disband NAME` is the manual teardown.

## What the backbone handles for you

The worktree and branch, member registration and startup, role briefs
(injected, never written into the repository), folder trust, and the
teardown. Everything a member does runs through the normal delivery and
audit pipeline, so `backbone status` and `agent inspect` work on swarm
members like any other agent.

## Members and permission prompts

Codex members never ask: Codex's sandbox confines them to the worktree
(plus temp and the network, so `backbone tell` and `gh` work), and inside
that wall the backbone launches them with `-a never`
(`swarm.unattended_members`, on by default). A write outside fails and
the model is told; what a project's tooling needs outside is opened once
with `agents.writable_dirs` (this repository: `["~/.cache/uv"]`).

Members on a runtime without a sandbox (OpenCode, Claude Code, Gemini)
keep their dialogs — unattended there would be trust on the whole
machine, the owner's explicit call per agent, never the swarm's.

When a member does show one, `backbone agent inspect <member>` reports
`waiting_for_human (permission)` with the prompt in the evidence and
`backbone agent approve <member>` answers it: the runtime's affirmative
key, only while the dialog is actually on screen, recorded with who
approved (`GET /api/events`). Check what is being approved — the evidence
quotes the command; approving is your decision, the backbone just types it.
A *choice* dialog (Codex's rate-limit "switch model?") is never approved;
`backbone agent deny <member>` keeps the model.
