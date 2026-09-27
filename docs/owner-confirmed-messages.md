# Signed senders and owner-confirmed messages

Status: draft specification for review. The wire format below is what the
implementation follows; changes are made here first.

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
| Unsigned request under an enrolled name | refused, never delivered; a metadata-only audit row is kept |
| Any sender before its name is enrolled | the ordinary envelope, unchanged |

Backbone writes the marker and the label from the verified record only. In
every message body, a line that starts with `[via:` (after optional spaces) is
shown as `[quoted] [via:…`, including the body's first line, which follows the
envelope on the same line. That is presentation only: the signed text is kept
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

**Reset.** A replace or clear that is not signed by the current key removes the
authority of everything admitted under the old epoch: open grants, unclaimed
confirmations, queued deliveries and pending transitions. Their receipts are
kept. After an ordinary rotation, work already admitted keeps its bounded
lifetime; the old epoch admits no new requests.

## Signed requests

A request is *made as* a name when any identity field in it names that
sender: the JSON body fields `from_entity`, `session`, `agent` and
`initiator`, a `{session}` path parameter, or an `agent` or `session` query
parameter. Names are compared after Unicode NFKC normalization, case folding
and trimming, so a case or width variant counts as the same name. Once a name
is enrolled, every request made as it must be signed, including inbox reads
and acknowledgements. Requests that carry no identity (status, inspect,
reports listing, usage) need no signature.

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
  name and then value, joined as `name=value` with `&`. Empty when there is no
  query.
- `body sha256`: the lowercase hex SHA-256 of the exact body bytes sent, or of
  the empty string when there is no body.

Backbone rejects a signed request whose timestamp differs from its own clock by
more than 300 seconds, or whose nonce it has already seen for that sender and
epoch. An identical retry (same nonce, same signed bytes) returns the original
result instead of acting twice.

### Strict parsing

A signed JSON body is parsed strictly: a duplicate key anywhere, an unknown
field, or a field of the wrong type is refused. `owner_confirmation` must
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
  "new_fingerprint": "<hex SHA-256 of the new key, or \"none\">",
  "request_id": "<UUID4>",
  "expires_at": 1790500000
}
```

`expires_at` is Unix time in whole seconds, 15 minutes after creation. A newer
transition for the same name replaces a pending one.

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
Telegram user id (`telegram.owner_user_id`) in an allowed chat.

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

### Observations

`GET /api/signing/observations?sender=<name>&after=<seq>` returns, oldest
first, the requests made as the name while a set or replace is pending:
sequence number, time, method, path and outcome (`verified`, `unsigned`,
`signature_invalid`, …). Bodies are never recorded.

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
no more than 300 seconds after it.

When the request is admitted, Backbone commits the nonce, the confirmation id,
the receipt and the queued delivery in one transaction, then delivers. A steer
that is refused (the agent is not working) commits nothing, and the same
confirmation may be sent again.

The same `confirmation_id` sent again with a fresh nonce returns the existing
result only when every confirmed field (recipient, kind, text, hash, source,
`confirmed_at`, epoch) is identical; anything else is refused and never
becomes a second delivery.

The response adds `confirmation_id` and `receipt` to the usual message or steer
response.

### Receipts

Each confirmation keeps an immutable receipt: sequence number, confirmation id,
sender, recipient, kind, the exact signed text, its hash, source,
`confirmed_at`, `delivered_at` and key epoch. Receipts are kept for 90 days.

`GET /api/signing/receipts?after=<seq>&limit=<n>` (a signed request made as the
sender) returns the sender's receipts after a sequence number, oldest first,
with `next_after`, `oldest_seq` and `retention_days`. When `after` is below
`oldest_seq - 1`, the response sets `"gap": true`: receipts were removed by
retention, which is a coverage gap, not "nothing new".

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
| 409 | `nonce_reused` | the nonce was used for a different request |
| 409 | `confirmation_conflict` | the confirmation id was used for a different confirmation |
| 422 | `malformed_request` | a missing, unknown, duplicate or wrongly typed field |
| 503 | `verifier_unavailable` | Backbone cannot verify signatures; only signed senders are affected |

## Validating a confirmation (agents)

`backbone message validate <confirmation_id>` claims the confirmation for the
calling agent and prints the exact confirmed text, which the agent acts on.
`backbone message validate <confirmation_id> --done` closes the claim.

Backbone identifies the caller from the process that makes the call (the agent
session it runs in), not from a name, session id or environment variable the
caller supplies. A caller that is not the confirmation's recipient gets no
text. Details of the claim and its recovery after a restart are in [open
questions](#open-questions).

## Known limitations

- The app's private key is, for now, a file readable by any process running as
  the owner's user, agents included. Such a process could sign a request or a
  rotation; the app's reconciliation of its own log against the receipts finds
  this only afterwards. Protected storage is tracked in #323.
- The shared API key still authenticates every caller. Sender names other than
  enrolled ones remain claims.
- A process that edits Backbone's database or replaces its CLI is out of scope.

## Open questions

- The task binding of a validate claim, and exactly what recovers it after a
  restart.
- How the caller's session is determined for validate.

## Test vectors

`tests/fixtures/signing_vectors.json` holds disposable keys, inputs, the exact
signed bytes and signatures, and negative cases. An app that signs requests
should run them in its own test suite and pin the file by its SHA-256.
