# Injected agent instructions

These Markdown files are the defaults Backbone supplies to new agent conversations.
They are shipped with the package; Python loads them from here in a checkout.

- `base.md`: ordinary agents' environment and collaboration brief.
- `swarm/common.md`: instructions shared by every swarm role.
- `swarm/{coordinator,scout,coder,reviewer,worker}.md`: role instructions.
- `swarm/kickoff.md`: the coordinator's initial task message.
- `policies/`: optional rules assigned globally or to agent tags; none by default.

These files are generic. What you write for your own fleet — edited copies of
them and your policies — is your configuration, not Backbone code: it lives in
`templates.dir` (default `<data_dir>/templates`), a directory you can make a git
repository of its own. Nothing user-specific (agent names, repositories, team
rules) belongs in this repository.

## What kind of text goes where

- **Base brief** (`base.md`): one agent's environment — what Backbone gives it
  and how to use it. Written to the agent in the second person; supports
  `{agent_name}`, `{repo}` and `{policy_maintainer}`. Edit it to change what
  every ordinary agent is told; it is not the place for team rules.
- **Swarm briefs** (`swarm/`): the same for swarm members, split into the common
  preamble and one file per role.
- **Policy** (`policies/NAME.md`): one short rule set shared by a group, injected
  after the brief as `## Shared policy: NAME`. A policy is *global* when assigned
  with `backbone templates use NAME`, or *tag-scoped* when assigned with
  `--tag TAG` to the agents carrying that tag. Write it as rules, not a
  description; keep it to what the whole group must do the same way; name no
  agent. `policies/python-example.md` is the one example and is not assigned.
- **Agent instruction** (a repository's `AGENTS.md`/`CLAUDE.md`): the project's
  own rules, outside Backbone. A rule that several projects share is a policy,
  proposed to the maintainer named in `templates.maintainer`, not copied into
  each repository.

Use `backbone templates list`, `show`, `edit`, and `preview AGENT` to inspect and
customize an installation. `backbone templates init` makes editable copies that
survive upgrades. See [the templates guide](../docs/templates.md) for paths,
composition order, placeholders, tags, and safe adoption.

On-demand agent playbooks are in [help/](../help/); user reference is in [docs/](../docs/).
