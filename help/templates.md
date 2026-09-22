# Inspect and customize injected instructions

Help is an on-demand playbook. Templates are the instructions Backbone supplies
when an agent starts. Read `backbone docs templates` for the full reference.

Before changing instructions, inspect the effective content and its source:

```bash
backbone templates list
backbone templates show base
backbone templates show swarm:scout
backbone templates preview AGENT
```

To customize with the owner's authorization, use `backbone templates edit NAME`
with `$VISUAL` or `$EDITOR`, or edit `backbone templates path NAME`. Names are
`base`, `swarm:ROLE`, and `policy:NAME`. `init` copies defaults without overwriting
local edits. Files default to `<data_dir>/templates/`; `templates.dir` can relocate them.

Assign short policies globally with `backbone templates use NAME...`, or to a
specific group with `backbone templates use NAME... --tag TAG`. Apply a group tag
with `backbone agent tag AGENT TAG`; remove it with `backbone agent untag AGENT TAG`.
Names replace that scope’s list; no names shows it. Use `--clear` to empty it. Global rules come first,
then matching tags alphabetically; each policy appears once. Required rules apply
even with a custom base. Missing or empty selected policies fail launch clearly.

Run `backbone templates validate` and preview affected agents before their next
fresh start. Existing conversations are not updated by changing files. Swarm
restarts reuse saved role briefs; changed role templates apply to new swarms.
Do not interrupt busy agents or broaden their task to adopt new instructions.
