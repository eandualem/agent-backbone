# Documents — retain HTML people can return to

Use Documents for an authored explanation, analysis or interactive report that
should survive local file cleanup. A Document is separate from progress reports
and Responses. Publication retains it without sending a notification.

```bash
backbone documents publish --file explanation.html --title "Explanation" --validate
backbone documents publish --file explanation.html --title "Explanation" --json
backbone documents --agent app --json
backbone documents show 12 --json
backbone documents show 12 --output saved.html
backbone documents publish --file correction.html --title "Explanation" --supersedes 12
```

Use your own author identity (`$BACKBONE_AGENT`, or `--agent NAME`). The default
retry key is derived from title, exact HTML and predecessor; after an uncertain
request, retry unchanged. `--key KEY` supplies an explicit retry key. An enrolled
name needs its signing client; the CLI does not bypass signing enforcement.

Corrections append an immutable revision of your own current document. They do
not edit the old body. Read the returned revision ID and `document_id`; the
receipt contains metadata only. Use `show --json` for the full HTML. Lists keep
one latest revision per document. Continue with `--before` from `next_before_id`
and the same filters; refresh from the first page for newly corrected documents.
`--author-id ID` finds retained work after an agent is forgotten or renamed.

Keep HTML self-contained, within 4,194,304 UTF-8 bytes. Titles allow 240
characters. Publication permits 30 new revisions per author/hour and 120 across
the installation/hour, within 1 GiB of reserved content storage. Retries cost no
allowance. Capacity exhaustion refuses new work without pruning old documents.
Never include credentials or assume HTML is sanitized or publicly hosted.
No local samples are imported automatically.

Read `backbone docs documents` for the exact API, limits, retention and backup
contract. `backbone documents publish --schema` prints the publication schema.
