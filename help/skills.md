# Skills — shared procedures, one copy, given to the agents tagged for them

A skill is a directory with a `SKILL.md` that your CLI loads on demand
(Claude Code, Codex and OpenCode all read the same format). The backbone keeps
one shared store of them — `backbone config get skills.store`, normally
`~/skills` — and at every launch links the ones tagged for you into the
directory your CLI reads (`.claude/skills` for Claude Code, `.agents/skills`
for Codex and OpenCode). You never copy a skill; you use it through the link.

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
name as a store skill wins.

## See what you have

```bash
backbone skills list                 # every store skill, its tags, who receives it
backbone skills preview NAME         # what NAME's next launch links, and where
backbone skills show SKILL           # print a store skill
```

## Share a skill you wrote

Write it in your own repository first — `.claude/skills/<name>/SKILL.md` or
`.agents/skills/<name>/SKILL.md`, whichever your CLI reads — and use it there.
A skill that only you need can stay there forever; nothing requires the store.

When it should reach other agents, hand it to the backbone with the tags of
the agents it is for:

```bash
backbone skills add .claude/skills/my-skill --tag coder --tag python
```

This **moves** the directory into the store (the store holds the only copy),
records the tags in its frontmatter, commits the store's history with your name,
and the link appears in your repository at your next launch. Pick the narrowest
tags that fit: `all` reaches every agent on the machine, and other agents will
follow what you wrote. Do not write into the store directory directly — the
command is the sanctioned path, and it works from inside a sandbox because the
backbone does the move.

## Change or retag a skill

How you edit depends on your sandbox — measured, not assumed:

- **Codex** refuses a write through the link (`operation not permitted`): the
  sandbox stops writes that resolve outside your repository. Copy the skill
  into your repository, edit the copy, and hand it back:

  ```bash
  cp -r .agents/skills/my-skill ./my-skill-draft
  # edit ./my-skill-draft/SKILL.md
  backbone skills add ./my-skill-draft --name my-skill --replace
  ```

  `--replace` without `--tag` keeps the existing tags: it is an update, not a
  re-tag. The draft is moved into the store, so nothing is left behind.
- **Claude Code** has no such sandbox by default: editing
  `.claude/skills/<name>/SKILL.md` through the link edits the store file
  directly. The copy-and-replace path above works there too and leaves a
  commit with your name, which the in-place edit does not.

Every agent tagged for the skill sees the change at its next launch. Change who
receives it with:

```bash
backbone skills tag my-skill coder python typescript   # replace its tags
backbone skills tag my-skill                            # no tags: reaches nobody
```

## When a launch reports a skills problem

`backbone agent start` fails if a selected skill's `SKILL.md` cannot be
resolved through its link — the store is checked at every launch, like a
missing policy. `backbone skills validate` runs the same checks over the whole
store and every registered agent, and names what to fix.
