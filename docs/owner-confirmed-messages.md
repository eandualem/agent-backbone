# Signed senders and owner-confirmed messages

| Part | Status |
|---|---|
| Enrollment, signed requests, the reservation of an enrolled name, rotation, observations | available |
| Owner confirmation, receipts and the reconciliation feed | available |
| `backbone message validate` | in progress |

The wire format below is what the implementation follows; changes are made
here first.

Backbone authenticates callers with one shared API key, so a sender name in a
request is normally a claim, not a proof. This page describes the optional
stronger path for one app: the app enrolls an Ed25519 key for a sender name,
signs every request it makes under that name, and can mark a message as
confirmed by the owner. Backbone verifies the signature, keeps an immutable
receipt of each confirmation, and writes a marker into the envelope that the
receiving agent can check once with `backbone message validate`.

Examples use the sender name `assistant`. Nothing in Backbone is specific to
one app or one name.

## What a recipient sees

| Case | Envelope |
|---|---|
| Owner-confirmed (signed, and confirmed by the owner in the app) | `[via:backbone from:assistant owner-confirmed:<confirmation_id>] <text>` |
| Signed relay (signed, not confirmed) | `[via:backbone from:assistant] (signed relay, not owner-confirmed) <text>` |
| A steer, confirmed or signed | the same, with `(steer for your current task)` after the envelope and before a relay label |
| Unsigned request under an enrolled name | refused, never delivered; a metadata-only audit row is kept |
| Any sender before its name is enrolled | the ordinary envelope, unchanged |

Backbone writes the marker and the label from the verified record only. In
every message, steer and restart continuation, a line that starts with `[via:`
(after optional spaces or invisible characters, in any case, compared after
NFKC normalization, so a fullwidth `［via:` counts) is shown as
`[quoted] [via:…`, including
the body's first line, which follows the envelope on the same line. That is presentation only: the signed text is kept
unchanged in the receipt, and its hash covers the text as sent.

## Enrollment

One sender name has at most one active key, with an epoch (an integer that
starts at 1 and increases with every key change). The name and its key form one
enrollment record. The ordinary configuration API cannot change it; only the
two transitions below can.

**Owner-approved transition** (set, replace or clear, including the first
enrollment):

1. The app sends `POST /api/signing/transitions`. It is not signed by the
   current key.
2. Backbone stores one pending transition and returns its fields.
3. The app computes the transition digest itself and shows it, with the action
   and the target Backbone, in its own interface.
4. The owner copies the digest into Backbone's Telegram bot, from the owner's
   account, in an allowed chat: `/approve_key <digest>`.
5. Backbone applies the transition once, if it is still pending, unexpired and
   the current epoch still equals its expected epoch.

Enforcement for the name starts at that moment. A clear removes the key and
releases the name in the same step. Every applied transition is alerted to the
owner.

**Rotation signed by the current key:** `POST /api/signing/rotation`, signed by
the current key with the `rotate` purpose and carrying a proof from the new
key. It is applied at once and alerted; it needs no approval.

**While a set or replace is pending**, Backbone verifies every request made
under the name against the pending key and records the outcome (verified,
unsigned, bad signature), metadata only, without changing what it admits. The
app reads this at `GET /api/signing/observations` to confirm that every path
signs before the owner approves. The approval reply in Telegram shows the
counts.

**Reset.** A replace or clear that is not signed by the current key starts a new
epoch: the old key admits nothing more, and a pending transition is replaced.
In the same transaction, every confirmation of the old epoch that wasn't
delivered yet is revoked (its receipt says `revoked`), and its queued delivery
is expired, including one the delivery job is holding right now, so it is
never delivered. A message the agent already read from its inbox, or a paste
whose outcome is unknown, stays held until the agent acknowledges it, but its
inbox copy loses the marker and says the confirmation was revoked. A steer already offered to an agent's running turn can still
be taken within its five minutes. With validate, open grants of the old epoch
lose their authority too. After an ordinary rotation, work already admitted
keeps its bounded lifetime; the old epoch admits no new requests.

## Signed requests

A request is *made as* a name when its sender field names that sender. Only
these fields count:

| Route | Sender field |
|---|---|
| `POST /api/messages` | `from_entity` |
| `POST /api/steer` | `from_entity` |
| `POST /api/messages/inbox` (read and acknowledge) | `session` |
| `POST /api/agents/{name}/approve`, `/deny` | `from_entity` |
| `POST /api/agents/{name}/restart` | `from_entity`, when given |
| `POST /api/integrations/reply` | `session` |
| `POST /api/reports` | `agent` |
| `POST /api/swarms` | `initiator` |
| `POST /api/skills`, `PUT /api/skills/{name}/tags` | `actor` |
| `POST /api/agents/{name}/state` | the `{name}` path parameter (a hook writing its own state) |

Names are compared after Unicode NFKC normalization, case folding and trimming,
so a case or width variant counts as the same name. A name that is only the
*subject* of a request doesn't count: reading an agent's output
(`GET /api/sessions/{agent}/output`), filtering reports by `agent`, or acting on
`/api/agents/{name}/…` as a target. Lifecycle requests that carry no sender
(start, stop, a restart without `from_entity`) are not made as anyone.

Once a name is enrolled, every request made as it must be signed. Signature
headers on a request under a name that has no key and no pending transition are
ignored: the request is handled as an ordinary unsigned one.

### Headers

All six are required on a signed request, together with the usual
`Authorization: Bearer <API key>`.

| Header | Value |
|---|---|
| `X-Backbone-Sender` | the enrolled sender name, exactly as enrolled |
| `X-Backbone-Key-Epoch` | the key epoch, decimal (`1`, `2`, …) |
| `X-Backbone-Timestamp` | Unix time in whole seconds, decimal |
| `X-Backbone-Nonce` | 32 lowercase hex characters (16 random bytes), new for every request |
| `X-Backbone-Audience` | this Backbone install's audience id (see below) |
| `X-Backbone-Signature` | base64url without padding of the 64-byte Ed25519 signature |

The sender header must name the same sender as the request's identity fields.

### Signed bytes

The signature covers the UTF-8 bytes of this text, where each field is written
as its byte length in decimal, a colon, the field, and a newline:

```
agent-backbone signed request v1\n
<len>:<purpose>\n
<len>:<METHOD>\n
<len>:<path>\n
<len>:<canonical query>\n
<len>:<body sha256>\n
<len>:<timestamp>\n
<len>:<nonce>\n
<len>:<audience>\n
<len>:<sender>\n
<len>:<epoch>\n
```

- `purpose`: `request` for an ordinary signed request; `rotate` for a key
  rotation (the management signature). Other purposes are used only inside
  proofs (below).
- `METHOD`: the HTTP method in upper case.
- `path`: the request path exactly as sent, without the query string, for
  example `/api/messages`.
- `canonical query`: the query parameters, each name and value percent-encoded
  (RFC 3986 unreserved characters left as they are, upper-case hex), sorted by
  the encoded name and then the encoded value, joined as `name=value` with
  `&`. Empty when there is no query.
- `body sha256`: the lowercase hex SHA-256 of the exact body bytes sent, or of
  the empty string when there is no body.

Backbone rejects a signed request whose timestamp differs from its own clock by
more than 300 seconds, or whose nonce it has already seen for that sender and
epoch (`nonce_reused`), so a request is never acted on twice. To retry after a
lost response, sign again with a fresh nonce and timestamp. For an owner
confirmation, the same `confirmation_id` in that fresh request recovers the
original result (below).

### Strict parsing

A body is read as the API reads it (JSON in UTF-8, UTF-16 or UTF-32, with or
without a byte-order mark). Once any name is enrolled or pending, a body on a
sender-field route that isn't readable JSON is refused, and so is a duplicate
key anywhere. A signed JSON body is parsed strictly: a duplicate key anywhere is refused,
and on the shapes listed under [Owner confirmation](#owner-confirmation) so is
an unknown field or a field of the wrong type. `owner_confirmation` must
agree with the route (`message` for `POST /api/messages`, `steer` for
`POST /api/steer`) and with the recipient in `target_session`.

### Audience

Each Backbone install has an audience id, a UUID created once and never
reused. Read it with `GET /api/signing/audience`. An app reads it before
enrolling and pins it; the enrollment is bound to it, so a request aimed at a
different Backbone fails.

### Keys and fingerprints

A public key is sent as base64url without padding of its 32 raw bytes. Its
fingerprint is the lowercase hex SHA-256 of those 32 bytes, shown in groups of
eight characters.

## Enrollment wire format

### Starting a transition

`POST /api/signing/transitions` (API key; not signed by the current key):

```json
{
  "sender": "assistant",
  "action": "set | replace | clear",
  "expected_epoch": 0,
  "new_public_key": "<base64url raw key, or null for clear>",
  "request_id": "<UUID4>",
  "proof": "<base64url signature by the new key, or null for clear>"
}
```

A name that signs travels in the `X-Backbone-Sender` header, so it must be
printable ASCII (1–64 characters, no whitespace or `[ ]`); other names are
refused, and so are `api`, `unknown` and `backbone`, which Backbone fills in
when a request gives no sender. A request that names it with a case or width variant
in its body is still made as it.

`expected_epoch` is the current epoch the transition replaces (`0` when the
name has no key). `proof` is the new key's signature over the framed text
below, with purpose `enroll-proof`, which shows the app holds the new private
key. A `set` requires that the name has no key, a `replace` or `clear` that it
has one.

The response returns the stored transition, without a digest:

```json
{
  "action": "set",
  "sender": "assistant",
  "audience": "<this install's audience id>",
  "expected_epoch": 0,
  "new_epoch": 1,
  "new_fingerprint": "<hex SHA-256 of the new key, or \"none\">",
  "request_id": "<UUID4>",
  "expires_at": 1790500000
}
```

`new_epoch` is the epoch the new key will take: one above any epoch the name
ever had, so a key enrolled after a clear never reuses an old one (`null` for a
clear). `expires_at` is Unix time in whole seconds, 15 minutes after creation. A
newer transition for the same name replaces a pending one. `new_epoch` is not
part of the digest.

### Framing for proofs and digests

Proofs and digests use the same length-prefixed framing as request signatures,
each with its own first line:

```
agent-backbone enroll proof v1\n    (fields: sender, audience, action, expected_epoch, new_public_key, request_id)
agent-backbone rotate proof v1\n    (fields: sender, audience, old_epoch, new_public_key, nonce, timestamp)
agent-backbone key transition v1\n  (fields: action, sender, audience, expected_epoch, new_fingerprint, request_id, expires_at)
```

Each field is written as `<byte length>:<value>\n`, in the order listed.
Numbers are decimal. `new_public_key` is the base64url text.

The **transition digest** is the lowercase hex SHA-256 of the framed
`key transition` text. The app computes it from the fields it sent and the
`audience` and `expires_at` it received, and shows it in groups of eight
characters. The owner copies it into Telegram:

```
/approve_key 3f2a9c1e 0b7d4e55 …
```

Spaces are ignored. Backbone accepts the command only from the owner's
Telegram user id, in an allowed chat.

The owner's Telegram user id is kept with the enrollment records, not in the
ordinary settings. It can be set once while it is empty, with
`backbone signing owner <user id>`, and the owner is alerted. Running it again
records a change that the current owner approves from their own account with
`/approve_owner <user id>`, and every change is alerted.

### Rotation

`POST /api/signing/rotation`, signed by the current key with purpose `rotate`:

```json
{
  "new_public_key": "<base64url raw key>",
  "proof": "<base64url signature by the new key over the rotate-proof text>"
}
```

The `rotate proof` fields use the request's own `X-Backbone-Nonce` and
`X-Backbone-Timestamp`, and `old_epoch` is the current epoch. The response
returns the new epoch and fingerprint.

### Enrollment state

`GET /api/signing/enrollment?sender=<name>` (API key; no signature needed)
returns:

```json
{
  "sender": "assistant",
  "audience": "<this install's audience id>",
  "epoch": 1,
  "fingerprint": "<hex SHA-256 of the active key, or null>",
  "pending": { "…the transition fields…" }
}
```

`epoch` is `0` and `fingerprint` is `null` when the name has no key; `pending`
is `null` when no transition waits. An app reads it after the owner approves,
to learn its epoch and check that the enrolled fingerprint is its own key.

### Observations

While a set or replace is pending, requests made as the name are also verified
against the pending key, with `X-Backbone-Key-Epoch` set to the transition's
`new_epoch`. The outcome is recorded; admission doesn't change: before the
first enrollment the request is handled as an unsigned one, and a name that is
already enrolled keeps being checked against its active key.

`GET /api/signing/observations?sender=<name>&after=<seq>` (API key; no
signature needed) returns these records, oldest first: sequence number, time,
method, path and outcome (`verified`, `unsigned`, `signature_invalid`, …).
Bodies are never recorded.

## Owner confirmation

On `POST /api/messages` and `POST /api/steer`, a signed request may carry:

```json
"owner_confirmation": {
  "confirmation_id": "<UUID4, lowercase, hyphenated>",
  "text_sha256": "<lowercase hex SHA-256 of the UTF-8 bytes of the message field>",
  "confirmed_at": "<ISO 8601 with offset, e.g. 2026-09-27T08:00:00.123456+00:00>",
  "source": "button | typed | voice"
}
```

`confirmed_at` must be no more than 30 minutes before the request timestamp and
no more than 300 seconds after it. If Backbone could not be reached for longer
than that, the app asks the owner to confirm again rather than sending an old
confirmation.

Backbone never strips or normalizes `message`: the hash is checked against the
exact bytes sent, and the receipt keeps exactly that text, including a trailing
newline. The delivered envelope adds the marker and applies the `[quoted]`
rule to lines that start with `[via:`; a terminal may render whitespace its own
way. The text `backbone message validate` returns is the authoritative one.

When the request is admitted, Backbone commits the nonce, the confirmation id,
the receipt and the queued delivery in one transaction, then delivers. A
message is never pasted twice: Backbone records the start of each paste, and a
paste interrupted before its outcome was recorded is held as uncertain in the
agent's inbox (`backbone inbox`) instead of being retried. A steer
that is refused (the agent is not working) commits nothing, and the same
confirmation may be sent again.

The same `confirmation_id` sent again with a fresh nonce returns the existing
receipt only when every confirmed field (recipient, kind, text, hash, source,
`confirmed_at`, epoch) is identical; anything else is refused with
`confirmation_conflict` and never becomes a second delivery. `confirmed_at` is
compared as the exact string sent; the age check parses it as an instant. The
age check applies to a new confirmation: `confirmation_expired` commits
nothing, and recovering an already-committed identical confirmation returns its
receipt.

A signed body is exactly one of these shapes: messages and steer
`{target_session, from_entity, message, priority?, owner_confirmation?}`
(`priority` on messages only), inbox `{session, acknowledge?}`, approve and
deny `{from_entity}`; other routes keep their documented bodies.

The response adds `confirmation_id` and `receipt` to the usual message or steer
response.

### Receipts

Each confirmation keeps an immutable receipt: sequence number, confirmation id,
sender, recipient, kind, the exact signed text, its hash, source,
`confirmed_at`, `delivered_at`, key epoch and status. Receipts are kept for 90 days.

`GET /api/signing/receipts?after=<seq>&limit=<n>` is a signed request made as
the sender in `X-Backbone-Sender`; `after` (default 0) and `limit` (default 100,
at most 500) are part of the signed query. It returns the sender's receipts
after a sequence number, oldest first, across every key epoch:

```json
{
  "receipts": [
    {"seq": 12, "confirmation_id": "…", "sender": "assistant", "recipient": "ike",
     "kind": "message", "text": "…", "text_sha256": "…", "source": "button",
     "confirmed_at": "2026-09-27T11:00:00.123456+03:00", "delivered_at": null,
     "key_epoch": 1, "status": "admitted", "revoked_at": null}
  ],
  "next_after": 12,
  "oldest_seq": 3,
  "pruned_through": 0,
  "retention_days": 90,
  "gap": false
}
```

- `recipient` is `target_session` exactly as sent. If Backbone redirects the
  message (a swarm's name to its coordinator), `recipient` is unchanged.
- `delivered_at` is `null` until the message is delivered (for a steer, until
  the agent's hook takes it) and set once. It stays `null` for a message that
  expired or was revoked, and for a steer that wasn't taken.
- `status` is `admitted`, or `revoked` once a reset of its key revoked it
  before delivery; `revoked_at` says when. Every other field never changes.
- Sequence numbers are shared by all senders, so one sender's are increasing
  but not consecutive. `pruned_through` is the highest sequence number that
  retention removed for this sender (0 when none). `gap` is true when
  `after` is below it: receipts after the cursor were removed, which is a
  coverage gap, not "nothing new". Retention keeps a receipt whose delivery is
  still waiting, so kept receipts can sit below `pruned_through`: a full page
  advances `next_after` only to its last receipt, and a page with fewer than
  `limit` receipts advances it to at least `pruned_through`. `gap` is true on
  the one page whose cursor crosses `pruned_through` (`after < pruned_through
  <= next_after`), so each gap is reported once and no kept receipt is
  skipped. With nothing new, `next_after` equals `after`.
- `oldest_seq` is the oldest receipt still kept for the sender, or `null`.

## Refusals

Every refusal is `{"detail": {"reason": "<code>", "message": "<text>"}}`.
Nothing is delivered or recorded as a confirmation. Refusals of requests made
as an enrolled name leave a metadata-only audit row.

| Status | `reason` | Meaning |
|---|---|---|
| 400 | `text_hash_mismatch` | `text_sha256` does not match the message |
| 400 | `confirmation_expired` | `confirmed_at` is outside the allowed age |
| 400 | `recipient_mismatch` | the confirmation does not agree with the route or recipient |
| 403 | `signature_required` | a request made as an enrolled name has no signature |
| 403 | `signature_invalid` | the signature does not verify |
| 403 | `sender_mismatch` | the sender header and the request's identity fields disagree |
| 403 | `audience_mismatch` | the audience is not this install's |
| 403 | `timestamp_out_of_window` | the timestamp is more than 300 seconds off |
| 403 | `key_epoch_unknown` | no key with that epoch admits requests |
| 403 | `not_enrolled` | `owner_confirmation` under a name with no enrolled key |
| 409 | `nonce_reused` | the nonce was already used; sign a retry with a fresh one |
| 409 | `confirmation_conflict` | the confirmation id was used for a different confirmation |
| 422 | `malformed_request` | a missing, unknown, duplicate or wrongly typed field |
| 503 | `verifier_unavailable` | Backbone cannot verify signatures; only signed senders are affected |

## Validating a confirmation (agents)

`backbone message validate <confirmation_id>` claims the confirmation for the
calling agent and prints the exact confirmed text, which the agent acts on.
`backbone message validate <confirmation_id> --done` closes the claim.

The confirmation is the task: its text defines the scope. A claim creates a
grant bound to the recipient agent, its registered working directory and the
confirmation id.

- The first claim must come within 24 hours of delivery.
- A grant lasts 24 hours from the claim, or until `--done`.
- Claiming again, for example after a restart of the same agent in the same
  directory, returns the same text flagged `recovered`. That is not fresh
  authority.
- After `--done` or expiry, a claim is refused. A fabricated id, a cancelled
  confirmation or a claim from any other agent or directory is refused.

Backbone identifies the caller from the process that makes the call, not from
a name, session id or environment variable the caller supplies. It follows
the local connection to the calling process, walks the process's ancestry to
a terminal pane Backbone manages, and takes that pane's agent session.
Validation fails closed when this mapping fails: a non-local connection, a
process that is not under a managed pane, or an ambiguous ancestry. A caller
that is not the confirmation's recipient gets no text.

Failed validations are rate-limited and reported once per confirmation, with a
count. An outage blocks only the step that depends on the confirmation.

## Known limitations

- The app's private key is, for now, a file readable by any process running as
  the owner's user, agents included. Such a process could sign a request or a
  rotation; the app's reconciliation of its own log against the receipts finds
  this only afterwards. Protected storage is tracked in #323.
- The shared API key still authenticates every caller. Sender names other than
  enrolled ones remain claims.
- A process that edits Backbone's database or replaces its CLI is out of scope.
- A same-user process can run a command inside another agent's terminal
  session, and would then be identified as that agent by validate.
- Validate identifies local callers only. A caller on another machine needs a
  different mechanism (#218).

## Test vectors

`tests/fixtures/signing_vectors.json` holds disposable keys, inputs, the exact
signed bytes and signatures, and negative cases. It includes a body with
non-ASCII text, a query with reserved characters and a repeated name, and the
digest of a clear transition (`new_fingerprint` `none`). An app that signs requests
should run them in its own test suite and pin the file by its SHA-256.
