# Skills — shared procedures, one copy, given to the agents tagged for them

A skill is a directory with a `SKILL.md` that your CLI loads on demand
(Claude Code, Codex and OpenCode all read the same format). The backbone keeps
one shared store of them — `backbone config get skills.store`, normally
`~/skills` — and at every launch links the ones tagged for you into the
directory your CLI reads (`.claude/skills` for Claude Code, `.agents/skills`
for Codex, Gemini and OpenCode). You never copy a skill; you use it through
the link.

**Shared skill changes require an operator credential.** Agents can prepare a
local draft and hand it to the operator for `skills add` or `skills tag`.

**The store is written only through the backbone.** The operator runs
`backbone skills add` or `backbone skills tag`; agents provide the reviewed
draft, intended tags and exact command through the existing approval route.
A direct-write refusal does not grant permission to edit the store or use an
operator credential. Keep the draft in your repository while the handoff is pending.

Which skills reach you is decided by tags. Each store skill names its tags in
its frontmatter:

```yaml
---
name: backend-module-pattern
description: Standard Python backend module layout. Use when creating a service.
metadata:
  backbone-tags: coder python
---
```

You receive a skill when one of its tags is one of yours (`backbone agent
inspect NAME` shows your tags), or when it is tagged `all` (everyone) or
`agent:YOUR-NAME` (only you). Your repository's own skills — real directories
under `.claude/skills` or `.agents/skills` — stay exactly as they are; the
backbone only adds links beside them, and a repository skill with the same
name as a store skill wins. One kept only where another CLI looks is linked
where yours does, so the repository's skills reach you on any of Claude Code,
Codex or OpenCode.

## See what you have

```bash
backbone skills list                 # every store skill, its tags, who receives it
backbone skills NAME                 # every skill NAME's CLI loads: shared, repository, user-level
backbone skills show SKILL           # print a store skill
```

## Share a skill you wrote

Write it in your own repository first — `.claude/skills/<name>/SKILL.md` or
`.agents/skills/<name>/SKILL.md`, whichever your CLI reads — and use it there.
A skill that only you need can stay there forever; nothing requires the store.

When it should reach other agents, give the operator the draft and the tags
of the agents it is for. The operator runs:

```bash
backbone skills add .claude/skills/my-skill --tag coder --tag python
```

This **moves** the directory into the store (the store holds the only copy),
records the tags in its frontmatter, commits the store's history with the recorded requester,
and the link appears in your repository at once. Pick the narrowest
tags that fit: `all` reaches every agent on the machine, and other agents will
follow what you wrote. Do not write into the store directory directly — the
operator command is the sanctioned path; the backbone does the move.

## Change or retag a skill

Prepare a local draft, then ask the operator to apply it. Copy from your CLI’s
skill directory: `.claude/skills` for Claude Code, `.agents/skills` for Codex
and OpenCode. This example uses `.agents/skills`:

```bash
cp -RL .agents/skills/my-skill ./my-skill-draft
# edit ./my-skill-draft/SKILL.md
```

Give the operator this command with the reviewed draft:

```bash
backbone skills add ./my-skill-draft --name my-skill --replace
```

`--replace` without `--tag` keeps the existing tags. The operator's command
moves the draft into the store and records the change. A runtime allowing a
write through the shared link does not replace this publication route.

Running agents tagged for the skill get the change at once, and the command
names them: Claude Code and Codex load it during the session, and OpenCode
reloads its skills at its next pause between turns. Agents not running get it
at their next launch. To change who receives it, give the operator the intended
tags and the corresponding command:

```bash
backbone skills tag my-skill coder python typescript   # replace its tags
backbone skills tag my-skill                            # no tags: reaches nobody
```

## When a launch reports a skills problem

`backbone agent start` fails if a selected skill's `SKILL.md` cannot be
resolved through its link — the store is checked at every launch, like a
missing policy. `backbone skills validate` runs the same checks over the whole
store and every registered agent, and names what to fix.
