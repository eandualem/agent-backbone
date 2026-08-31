# GitHub — how issues reach agents and how to report back

The backbone watches every repository an agent owns (its directory's
`origin` remote) or watches. Events arrive by webhook or polling and are
routed by four relationships for an issue in repository R:

| Relationship | How | Effect |
|---|---|---|
| owner | your directory is a checkout of R | unlabelled issues are your work |
| `for:<agent>` | label on the issue | routes to that agent's queue |
| `from:<agent>` | label on the issue | comments and the close are reported to the opener |
| watch | `backbone agent watch R` | notified of new issues; `for:` labels route to you |

Issues are delivered one at a time: acknowledge the current one before
the next arrives. Acknowledge by commenting on the issue with a leading
`[from:<your-name>]` tag; closing the issue also releases the queue and
triggers whatever depends on it (a swarm working the issue tears down,
for example).

## Working an issue

- Read it yourself: `gh issue view N --repo OWNER/REPO` — deliveries
  carry a summary and link, never the full body.
- Report progress as comments (`gh issue comment`), open PRs that say
  `Closes #N`, and let the close flow do the bookkeeping.
- To delegate: open an issue in the target repository with a
  `for:<agent>` label (add `from:<your-name>` to hear back).
