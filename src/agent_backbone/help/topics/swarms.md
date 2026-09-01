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

   Member spec: `ROLE[*N][@RUNTIME[/MODEL]]`. Roles with shipped briefs:
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
