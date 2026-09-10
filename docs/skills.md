# Skills

Keep one copy of every skill your agents share, tag it, and let the backbone
put the right ones in front of each agent — in the directory its CLI actually
reads — at every launch.

## Why a store

A skill is a directory with a `SKILL.md` (the [Agent Skills](https://agentskills.io)
format). Claude Code reads `.claude/skills`, Codex reads `.agents/skills`, and a
skill both should have ends up copied twice per repository, drifting apart. The
store, `skills.store` (default `~/skills`), holds the only copy; agents use it
through symlinks. The store is a git repository — the backbone initialises it on
the first `skills add` and commits after each `add` and `tag` — so every change
to a shared skill has an author and a date.

## Tags decide who gets what

Each store skill names its audience in its own frontmatter, in the spec's
`metadata` slot, so the store is self-describing and a skill stays a plain skill:

```yaml
---
name: backend-module-pattern
description: Standard Python backend module layout. Use when creating a service.
metadata:
  backbone-tags: coder python
---
```

An agent receives a skill when one of the skill's tags is one of the agent's tags
(`backbone agent tag NAME coder python` — the same tags that select policies),
or when the skill is tagged `all` (every agent) or `agent:NAME` (that one agent).
"On top of" is set union: an agent tagged `coder python` gets the `all` skills,
the `coder` skills and the `python` skills; one tagged `coder typescript` never
sees the Python one. No hierarchy is needed in the filesystem, and a skill that
belongs to two unrelated groups just carries both tags.

## What happens at launch

For runtimes with a measured skills directory — Claude Code (`.claude/skills`),
Codex and OpenCode (`.agents/skills`) — the start path:

1. reads the store and selects the skills tagged for the agent;
2. creates `<repo>/<dir>/<name>` → `<store>/<name>` symlinks for them, records
   each in `<data_dir>/skills/materialized/<agent>.json`, and removes links from
   that manifest that are no longer selected (only while they still point into
   the store);
3. keeps the link names out of `git status` through a backbone-owned block in
   `.git/info/exclude` — git's per-clone ignore list, never a tracked file — so
   the repository's own skills stay tracked and only the links are hidden;
4. fails the start if a selected skill does not resolve to a `SKILL.md` through
   its link, the same way a missing policy fails it.

Everything the repository owns is left alone. A real directory with the same
name as a store skill is the repository's skill and wins; the start reports it
and continues. A repository that ships its own skills keeps them, with or
without the backbone; with the backbone absent, a project simply has fewer
skills and a few dangling links that git and the CLIs both ignore.

Gemini and Cursor are not materialised: their behaviour has not been measured
on a working install (see the evidence table in
[issue 213](https://github.com/eandualem/agent-backbone/issues/213)). Plain
shells, `aider` and `deepcode` receive nothing.

## Commands

```bash
backbone skills list [--tag TAG] [--json]   # store skills, tags, and the agents each reaches
backbone skills show NAME                   # print a store skill
backbone skills path [NAME]                 # the store, or one skill's directory
backbone skills add PATH [--name N] [--tag T]… [--replace]
backbone skills tag NAME [TAG…]             # replace tags; none = reaches nobody
backbone skills preview AGENT [--json]      # next launch: skills, directories, link state
backbone skills validate [AGENT]            # store entries and every agent's selection
```

`add` **moves** the directory into the store — the source is gone afterwards,
so no second copy survives — rewrites its frontmatter `name` if `--name` renames
it, writes the tags, and commits. It goes through the API when the backbone is
running, so an agent inside a sandbox that cannot write to `~/skills` still has
a sanctioned path; the backbone does the move. The API is
`GET /api/skills`, `POST /api/skills`, `PUT /api/skills/{name}/tags` and
`GET /api/skills/preview/{agent}`.

## An agent's private skill

Nothing requires the store. An agent that wants a skill only for itself creates
it in its own repository (`.claude/skills/<name>/` or `.agents/skills/<name>/`)
and uses it; the backbone never adopts or moves it. `backbone skills add` is the
one step that shares it, and the agent chooses the tags then. `backbone help
skills` is the agent-facing version of this page.

## Measured behaviour this relies on (2026-09-10)

| CLI | Reads | Symlinked skill directory | Symlinked `SKILL.md` |
|---|---|---|---|
| Claude Code 2.1.267 | `.claude/skills` | followed | followed |
| Codex 0.153.4 | `.agents/skills`, `.codex/skills` | followed | **silently ignored** |
| OpenCode 1.18.29 | `.claude/skills`, `.agents/skills`, `.opencode/skills` | followed | followed |

The backbone therefore links whole skill directories, never single files. The
Codex sandbox also refuses writes through such a link (measured the same day);
reads are fine.
Re-measure after a CLI upgrade: put a skill with an unguessable name behind a
link and ask the CLI to list its skills.
