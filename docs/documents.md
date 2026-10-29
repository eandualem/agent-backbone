# Durable Documents

Publish a self-contained HTML document when someone needs to return to an
explanation, analysis or interactive report. Backbone retains the exact UTF-8
content and immutable corrections in SQL. Publication sends no notification and
creates no public web page. A native reader can list metadata and fetch one
revision through the authenticated API.

```bash
backbone documents publish --file explanation.html --title "How the system works" --validate
backbone documents publish --file explanation.html --title "How the system works" --json
backbone documents --agent app
backbone documents show 12 --json
backbone documents show 12 --output saved.html
backbone documents publish --file corrected.html --title "How the system works" --supersedes 12
```

Run inside the author's agent session or pass `--agent NAME` when publishing.
`--file -` reads HTML from stdin. The default retry key hashes the title, HTML
and predecessor ID; `--key KEY` supplies a caller-managed key. After an uncertain
request, retry the same content and key. Each attempt under an enrolled sender
needs a fresh signature nonce. The CLI uses the ordinary authenticated agent
path; an enrolled sender must publish through its existing signing client.
`backbone documents publish --schema` exposes the publication JSON schema.

HTML, including line endings, is preserved exactly. Titles are preserved too;
blank titles, control characters and more than 240 characters are rejected.
The CLI's default detail view prints metadata; `--json` includes the complete
HTML, and `--output FILE` writes its exact bytes without executing it. Only
open authored HTML in a reader whose security policy you trust. Backbone does
not sanitize HTML or provide it with browser credentials, a public URL or CORS.

## Revisions, identities and discovery

Every revision has an integer `id`; every document has a stable `document_id`.
A correction must name the same author's current revision in `supersedes_id`.
Competing stale corrections and changed reuse of a retry key return conflict.
An identical retry returns the stored revision even after a later correction.
A revision's direct successor is returned as `superseded_by`; neither the old
title nor its body changes.

Authors have stable IDs shared with authored reports and Responses. A rename
keeps that ID, while each revision retains the name used when it was published.
Forgetting an agent leaves all documents and retry receipts intact; registering
that name again creates a different author identity. The current-name filter
never selects a forgotten author's content by matching its old name. Use
`--author-id ID` or the all-authors list to find retained work.

The list selects the latest revision **per document before pagination**, sorted
by revision ID descending. Use `--before ID` from `next_before_id`, preserving
your filters. This is a changing latest view, not a snapshot: a correction can
move a document above your cursor. Refresh from the first page to discover it.
List and publication receipts contain metadata only. Bodies are fetched only
by an exact revision read, which also reports its direct successor.

## API contract

All operations use Backbone's existing read-authentication boundary. Publication
also uses [signed-sender enforcement](owner-confirmed-messages.md) with `author`
as its sender field. Enrolled names require signatures over the **original
request bytes**, including HTML; unenrolled agents retain the existing shared
API-key trust model. Names alone are never proof of authorship.

`POST /api/documents` takes:

```json
{"author":"app","request_id":"overview-v1","title":"Overview","html":"<h1>Overview</h1>","supersedes_id":null}
```

`author` is a registered agent name. `request_id` is 1–128 characters from
`A–Z`, `a–z`, `0–9`, `_`, `.`, `:`, `-`. `supersedes_id` is optional/null or a
positive signed 64-bit integer. Unknown fields and type coercions are rejected.
Success is HTTP 201 for a new revision or 200 for an identical retry:
`{"record": DocumentMetadata, "created": boolean}`.

`GET /api/documents` takes optional `agent`, `author_id`, `before_id` and `limit`
(default 20, range 1–100). Filters intersect. `author_id` is 32 lowercase hex
characters; `before_id` is a positive signed 64-bit integer. It returns
`{"items": [DocumentMetadata], "has_more": boolean, "next_before_id": integer|null}`.
The next cursor is the last returned revision ID only when there are more items.

`GET /api/documents/{revision_id}` returns DocumentMetadata plus `html: string`,
without a wrapper. The integer path ID must be positive and fit signed 64-bit.

| Metadata field | JSON type | Meaning |
| --- | --- | --- |
| `id` | integer | Immutable revision ID |
| `document_id` | string | Stable root identity, 32 lowercase hex characters |
| `author_id` | string | Stable author identity, 32 lowercase hex characters |
| `author_name` | string | Author name at publication |
| `agent_name` | string or null | Current name, null after forget |
| `request_id` | string | Publisher retry key |
| `title` | string | Exact title |
| `created_at` | string | Server UTC ISO 8601 timestamp |
| `content_bytes` | integer | UTF-8 HTML byte count |
| `content_sha256` | string | SHA-256 of those exact bytes, 64 lowercase hex characters |
| `supersedes_id` | integer or null | Direct predecessor |
| `superseded_by` | integer or null | Direct successor |

Errors use `{"detail": string}` for missing author/revision (404), revision or
retry conflicts (409), wire limit (413), rate limit (429, `Retry-After: 3600`),
and exhausted storage admission (507). Validation returns 422 with
`{"detail":[{"loc":[string],"msg":string,"type":string}]}`, without rejected
input. Existing authentication and signature errors retain their existing
shapes: authentication 401; signing refusals use
`{"detail":{"reason":string,"message":string}}` with 403, 409, 413, 422 or
503 as appropriate. There is no document deletion, public HTML endpoint,
notification subscription or browser authentication endpoint.

## Limits and backups

Decoded HTML must be nonempty, valid UTF-8 and at most **4,194,304 bytes**,
including inline assets. Metadata, serialized with HTML replaced by an empty
string, ASCII escaping and JSON's default separators, is at most **8,192 bytes**.
The publication wire cap is **25,174,016 bytes** (`6 × HTML limit + metadata
limit`) and is enforced before parsing. The exact-read response fits that same
bound even for maximally escaped HTML. Other signed routes keep their existing
1 MiB request cap.

New revisions are limited to **30 per author per rolling hour**, **120 across
the installation per rolling hour**, and **1 GiB** total admitted storage.
Admission reserves the decoded HTML bytes plus 8 KiB per revision for metadata
and retry data. This bounds application content, not SQL index/WAL/backup disk
usage. The limits are fixed operational bounds, like the existing report rate
limit. Identical retries are checked first and consume no new allowance.
Concurrent publishers share a transactional SQL admission counter. Exhaustion
rejects new content without deleting old work.

Documents, author IDs, revision links, permanent retry keys and the admission
counter live in the SQL database, separate from report pruning. Back up and
restore the **whole database**, including `documents`, `document_storage` and
`agents`; a directory of exported HTML is not a database backup. Use SQLite's
online backup API (or stop all writers before a file copy), and use your
PostgreSQL installation's consistent database backup/restore procedure.
Preserve the existing installation's authentication/signing configuration when
restoring it. SQLite backup restoration and retries are tested; no provider-hosted
PostgreSQL restoration is claimed by those tests.
