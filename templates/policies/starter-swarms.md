# Use swarms for parallel work

Keep one continuing agent responsible for each repository or task. That agent owns
the context, decisions and integrated result. Do not start a second independent
owner on the same repository to increase capacity. Consult an existing specialist
for focused expertise; use a swarm when temporary parallel work will help.

A swarm is a coordinator and role-specific members working on one existing issue
in a shared worktree and branch. Use it for independent research, competing
debugging hypotheses, or implementation slices with clear file ownership. Keep
sequential or tightly coupled work with one agent when coordination would add more
cost than value. Consider the additional model usage within the user's authority.

Before creating one, read `backbone help swarms`. Check existing agents and swarms
for overlapping work, establish the issue in the repository you own, and give the
coordinator the goal, constraints, owned slices and evidence needed for completion.
Follow the project's issue-creation, review and permission rules. A swarm does not
grant additional authority or bypass runtime boundaries.

The repository or task owner may manage multiple swarms with distinct scopes.
Keep their work from competing, decide the integration order and verify the combined
result. Members follow the swarm's common and role instructions; the coordinator
assigns work and reports back to the initiating owner. A member does not become a
second repository owner or start another swarm on its own.

Track actual findings and results, not merely started processes or delivered
messages. Keep responsibility through review, integration and the swarm's documented
teardown. The continuing owner retains the useful context and answers the user.
