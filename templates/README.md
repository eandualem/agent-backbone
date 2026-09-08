# Injected agent instructions

These Markdown files are the defaults Backbone supplies to new agent conversations.
They are shipped with the package; Python loads them from here in a checkout.

- `base.md`: ordinary agents' environment and collaboration brief.
- `swarm/common.md`: instructions shared by every swarm role.
- `swarm/{coordinator,scout,coder,reviewer,worker}.md`: role instructions.
- `swarm/kickoff.md`: the coordinator's initial task message.
- `policies/`: optional rules assigned globally or to agent tags; none by default.

Use `backbone templates list`, `show`, `edit`, and `preview AGENT` to inspect and
customize an installation. `backbone templates init` makes editable copies that
survive upgrades. See [the templates guide](../docs/templates.md) for paths,
composition order, placeholders, tags, and safe adoption.

On-demand agent playbooks are in [help/](../help/); user reference is in [docs/](../docs/).
