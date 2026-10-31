# Interactions

Interactions retain communication that passes through Backbone. One original
message has one immutable identity and operation, with separate observed
transport events. Attempts, queued waits and explicit acknowledgements do not
multiply the logical message count. They do not prove understanding, useful
work, task completion or that a particular message started a model turn.

The candidate read contract is in [contracts/interactions-v1](contracts/interactions-v1/README.md).
It is an operator read surface; reading it never claims inbox work. Existing
`POST /api/messages/inbox` continues to claim messages and must not be used as a
history feed. Capturing and projecting communication is independent of runtime;
actual terminal or hook delivery retains each runtime's existing coverage.

Future direct sends and steers retain their original body before transport
wrapping. Queue capture is transactional with insertion; immediate capture
commits before an external handoff. Original accepted records are not rewritten
by retries, name changes or later explanation. Owner questions and artifact
conversation messages also have recorded links to their source. Unrecorded links
stay absent. Owner attribution on API sends is explicit (`interaction.actor`);
managed CLI agents use registration credentials, while a human CLI without an
agent identity marks its operator-origin send. A claimed sender label is not an
authenticated identity. Unknown senders remain unknown.

History windows include messages whose recorded lifecycle overlaps the window,
including sends that began earlier. Observed state history remains separate and
partial. No historical message is reconstructed from a preview, transcript,
nearby timestamps or a current agent name. Name reuse never grants an old
message's participant rights to a new registration.

An interaction conversation keeps discussion anchored to its immutable message.
The owner selects a current verified participant; other recipients are refused.
Agents need registration-bound credentials and must be a recorded participant.
A participant that retires is not replaced by an agent reusing its name. Replies
explain the message now; they are not retroactive evidence of intent at send time.
The conversation never changes the original message or a shared policy.

Full-body retention follows `timing.delivery_retention_days` (30 by default).
Unanchored older bodies become explicitly retained excerpts; interaction context
referenced by a conversation remains intact. Coverage exposes the capture and
retention boundaries, snapshot and any projection truncation. Retained metadata
and excerpts are sensitive operator data, not automatically safe to export.
Missing old bodies cannot be recovered. Pending delivery storage and existing
conversation limits retain their own documented semantics.
