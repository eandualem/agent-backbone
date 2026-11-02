# Profile connection contract

Use this contract to build a client that keeps work attached to one profile.
A profile owns one provider, fleet, database, runtime account environment and
integration configuration. Multiple workspaces can share that profile. Selecting
another profile affects new work; it cannot retarget an existing request.

**Implementation status:** this page and its fixtures define the agreed contract.
Operator list/show discovery and refusal of unreconciled startup are implemented.
Ready profile activation and lifecycle commands remain unimplemented. The identity
endpoint and bound-request checks have an internal reconciliation context, but no
production startup installs it yet; the contract below is not a procedure for
activating profiles.
Provider implementation is
tracked in [#548](https://github.com/eandualem/agent-backbone/issues/548), with the
consumer in [agent-interface#182](https://github.com/eandualem/agent-interface/issues/182).
The agreed design revision is **2**; JSON `schema_version: 1` is the first wire
version. There is no earlier released profile protocol.

Provider-local identity persistence is implemented as an internal database
reservation. It stores the profile and lifecycle IDs against the existing signing
audience, accepts an identical replay and refuses conflicting assignments. Reading
an unreserved database creates no identity. Startup checks for this identity before
creating directories, initializing or migrating the database, or starting agents,
hooks, integrations or jobs. Direct CLI database fallback, `init` and setup
initialization apply the same guard before writing credentials or receipts.
Any identity record is refused because activation is not implemented. Directory
ownership is checked separately against the external registry. This does not adopt
Default, enable bound clients or grant legacy compatibility. Ready activation and
replacement fencing remain unimplemented; a copied database is not evidence of a
valid active lifecycle.

An internal, separately initialized SQLite registry now reserves isolated-profile
identity and canonical data-directory ownership outside the provider data tree.
Its read-only discovery snapshots match the non-ready list/show shapes below;
they do not initialize or repair a missing registry. Conflicting identities,
overlapping directories and changed replay input are refused. Directory ownership
also refuses case and Unicode-normalization aliases, including on case-sensitive
filesystems. Invalidated
reservations remain as failed records across provider database restoration and
cannot be revived by replay. This is a startup reconciliation guard, not
running-service/job fencing. It cannot publish `ready`, adopt Default or activate
a replacement. The full lifecycle and runtime account boundary remain unfinished.

The terminal service has an internal process-level socket pin for the future
profile startup path. It must be selected before terminal use; subsequent changes
are refused. Commands, interactive attachment and browser PTY attachment use that
explicit socket, regardless of inherited tmux selection. Attaching from a client
on another server requires a separate terminal. Legacy processes retain their
existing environment behavior. No profile startup or selection command enables
this pin yet; it does not establish runtime account isolation or provider readiness.

Internal read-only reconciliation captures the external reservation, local
SQLite identity, baseline and explicit terminal socket. It refuses changed
ownership, identity, signing audience, baseline or noncanonical paths without
creating or repairing storage. It leaves discovery non-ready and does not lift
the startup refusal. External databases and Default adoption are not supported
by this isolated-provider prerequisite.

With that context installed by a future activation path, authenticated
`GET /api/profile` returns the agreed identity response. HTTP content operations
compare the complete tuple, the actual serving database and data directory,
persisted metadata and process terminal pin before route handling. This internal
path accepts a canonical file-backed SQLite URL without query overrides.
Socket connections capture the context and recheck it before
events and emissions, including operator connections. Each emission reconciles
provider metadata once, then checks each recipient's captured context and
registration and targets only those validated recipients. The result is not
reused by later emissions. Unbound `GET /health` retains its keyless probe and
unbound `POST /webhooks/github` retains webhook signature authentication; both
still reject an invalidated provider context. Any profile header, including an
empty or partial tuple, keeps the API-key and binding checks on those routes.
Invalidation or changed
metadata refuses subsequent operations; this does not drain in-flight requests
or fence background jobs. Complete replacement/restore fencing, ready publication
and runtime/integration isolation remain required before activation is exposed.
The legacy identity endpoint authenticates and then returns 404.

Registry initialization publishes a complete private database without replacing
an existing registry. Its containing directory must belong to the current user
and cannot be group- or other-writable, even with the sticky bit: SQLite's sibling
journal files also need protection. Discovery reads stored
directory metadata; it does not
inspect provider directories. Reservation and reconciliation check the selected
directory against the filesystem. A changed provider path cannot prevent reading
other records or invalidating the exact stored reservation.
If SQLite needs writable journal recovery after an interrupted mutation,
discovery reports the registry as unavailable. Replaying the original reservation
or invalidation can recover the journal; reads never perform that write or
substitute an empty registry. Reconciliation and mutation callers must preserve
the original identity and directory through recovery.

The unadopted provider currently rejects profile-bound clients: any profile
identity header or Socket.IO identity field returns `binding_mismatch`, including
partial or empty bindings. Existing ingress limits and credential/registration
failures run first. This preliminary guard then refuses HTTP requests before
route-specific permission or signature checks, and sockets before subscriptions.
Unbound clients retain all existing checks and behavior. The guard prevents a bound
operation from silently reaching a legacy provider; it cannot attest an identity
and does not implement the bound-provider contract below, adoption, discovery or
isolation.

[Shared fixtures](contracts/profiles-v1.json) contain discovery records, identity,
HTTP headers, Socket.IO identity/error fields, lifecycle requests/receipts,
unknown-operation lookup and binding cases. All identifiers and paths are synthetic.
`baseline-example-v1` is a fixture value, not an available baseline. The file's top-level groups and binding cases
are a test container, not a new API response. Consumers can pin this file to a
commit and run its cases against their own implementation.

## Verify the provider

The immutable identity is `(profile_id, installation_id, lifecycle_id)`:

- `profile_id` identifies the profile across replacements and display-name changes.
- `installation_id` is the provider's existing persistent signing audience.
- `lifecycle_id` is an opaque instance generation. Ordinary stop/start, binary or
  baseline upgrade, credential rotation and display rename preserve it.
  Replacement or restored activation changes it before the instance becomes ready.
  Changing the signing audience is not a substitute for this generation.

The authenticated `GET /api/profile` response contains `schema_version`, all three
identity fields, `baseline_version` and `tmux.socket_path`. The baseline version
records provenance; it is not part of connection identity.

Every bound HTTP request, including the identity handshake, sends:

```text
X-Backbone-Profile-ID: profile-a
X-Backbone-Installation-ID: installation-a
X-Backbone-Lifecycle-ID: lifecycle-a1
```

Socket.IO authentication carries the same identity as `profile_id`,
`installation_id` and `lifecycle_id`, alongside its existing authentication fields.
The `socket_auth` fixture supplies only these three additional identity fields;
it does not replace a credential. No new Socket.IO client is required.

The provider reads and validates its own durable identity at startup. It enforces
the full expected tuple before each content operation, including reads, writes,
approvals and streams. A successful handshake is not enough: replacing a socket
between handshake and mutation must not redirect the mutation.

Keep existing authentication, signature, role and registration checks. After
successful authentication, a missing tuple on a provider requiring binding returns
HTTP 409 `binding_required`; a partial or mismatching tuple returns 409
`binding_mismatch`. Errors contain no actual mismatching identity or sibling data.
Existing 401/403 authentication and permission failures remain. The fixture's
`http_errors` records specify the response bodies; the outer `status`/`body` pair
is fixture metadata. Socket.IO namespace rejection uses `connect_error` with
`{"message":"Profile binding does not match.","data":{"code":"binding_mismatch"}}`
(or the corresponding `binding_required` message/code). The `socket_errors`
fixtures specify these payloads, without inventing an HTTP status for sockets.
Existing socket authentication/permission failures remain unchanged. A binding
rejection must not trigger automatic retry under a new tuple or without a tuple;
a new connection requires explicit identity verification. Never retry a rejected
bound request without its identity.
An unsupported schema or older provider's 404 is an explicit compatibility failure.

## Discover a connection

`backbone profiles list --json` and `backbone profiles show PROFILE_ID --json` are
local operator control-plane operations, separate from any provider's content API.
Agent-facing tools cannot use them as a gateway to discover sibling fleets.
Listing is read-only: it does not adopt, start, migrate or repair providers. An
unavailable registry is an error, not an empty successful list.

Run these commands from a local operator terminal. Managed agent contexts and
agent credentials are refused before accessing the registry. They read
`~/.local/share/agent-backbone-profiles/registry.db`, outside provider data trees;
`BACKBONE_DATA_DIR` does not select the control-plane registry. No provider secret,
configuration, API or database is loaded. Human output labels provisioning state;
`--json` returns the exact list/show shape. The current registry supports only
`creating` and `failed` records, with null bindings. There is no creation command
yet; a missing registry returns a nonzero exit and an error on stderr.

Before opening mutable storage, both daemon startup and direct CLI fallback check
that the canonical provider directory and configured SQLite database path
do not overlap any reservation, including failed reservations. This prevents a
pre-identity database restore from starting as legacy in a reserved tree. Missing
registries preserve ordinary unadopted startup; existing but unreadable or invalid
registries block it because directory ownership cannot be checked. Startup never
creates or repairs the registry. A reserved database cannot start as legacy from
another directory. This check does not fence an already running provider or
establish account isolation.

Ordinary legacy startup retains SQLite crash recovery: after confirming that
neither location overlaps a reservation, a hot rollback journal may be recovered
by reopening the existing database read-write and inspecting identity again.
This does not create a database or migrate its schema. Other read errors never
trigger writable fallback. Explicit read-only database URLs and existing-installation
setup inspection stay read-only; a recovery requirement is an error. An unfinished
fresh setup may recover its own local database only after validating the exact
recorded request and unchanged credentials under the existing receipt lock.
Registry discovery and reservation checks never recover journals implicitly.

List returns `schema_version` and `items`. Each item has `profile_id`,
`display_name`, `kind` and `state`. `kind` is `legacy_default` or `isolated`.
Provisioning `state` is `creating`, `ready` or `failed`; it does not say whether
the daemon is running.

Show returns an atomic record with those fields, `schema_version` and `binding`.
A ready binding contains installation and lifecycle identity, a Unix endpoint,
a private dotenv credential reference and an explicit tmux socket. A non-ready
record has `binding: null` and cannot be selected for content. The record remains
available when a ready provider is stopped.

The registry supplies an expected binding, not proof of the endpoint's identity.
The client resolves only that exact supported credential reference, performs the
authenticated handshake and compares the identity and tmux socket. It must not
fall back to inherited credentials, another file, another provider or Default.
Unsupported reference kinds fail explicitly. No executable commands, runtime
environment overlays or credential bytes appear in discovery JSON.

The provider owns runtime environments, path permissions and service identities.
The client retains existing tmux server/pane/window checks: a socket path alone
does not establish that a terminal survived server replacement.

## Keep work pinned

Capture the identity, endpoint, credential reference and resolved credential when
an operation begins. Retain them through pagination, retry, upload/download,
stream reconnect, delayed callbacks, dialogs and approvals. Complete against that
binding or cancel explicitly. Credential rotation creates a separately verified
connection; it does not rewrite pending work. Cache keys and notification navigation
include provider identity. A selection change cannot populate another profile with
an old response or authorize work on an identically named resource.

## Apply local lifecycle operations

The local control plane starts stopped providers; an unavailable content endpoint
cannot start itself. The agreed command forms are:

```text
backbone profiles apply --file request.json --json
backbone profiles operation REQUEST_ID --json
```

The fixture includes exact create/start/stop request shapes. Start and stop name
`profile_id`, `expected_installation_id` and `expected_lifecycle_id`. Create takes
only a request ID, display name and explicit baseline version; it cannot import
credentials or arbitrary paths/environment from Default.

Create reserves identities and private paths once. Identical request-ID/body replay
returns the same operation; different input under that ID is a conflict. Serialize
opposing lifecycle actions per profile or refuse as busy. Compare the expected
tuple before effects. A timeout leaves the result unknown until the original
operation receipt is read; do not create a new request to guess at its outcome.
An unknown-operation lookup exits nonzero with the `operation_lookup_unknown`
fixture body. It is a lookup error, not a fabricated failed-operation receipt,
and does not prove the timed-out action had no effect. Distinguish lookup errors
from operation receipts by their JSON body shape, not by exit status alone.
Retain the original ID,
body and binding; only an identical resubmission can use the idempotency guarantee.
Reserve the durable operation record before any side effect.

Receipts carry `schema_version`, `request_id`, `action`, all three identity fields,
`status` and `error`. Status is `pending`, `succeeded` or `failed`. Errors have bounded
`code` and `message` fields; create identity fields may be null only before
reservation. Successful start means service launch completed, not that the client
has verified content access. Stop targets only the registered instance and retains
its data and other providers.

Registry and provider metadata must agree before publishing `ready`. Interrupted
reconciliation stays non-ready; conflicting startup metadata fails. Replacement
or restore fences the old accepting services, jobs and streams before publishing
a new lifecycle. A guard outside the restored profile database prevents old data
from resurrecting an accepting generation. Old receipts, retries and retained
pending actions cannot be rebound; recovered work needs explicit reconciliation.
Hostile rollback of every same-user snapshot is outside this product boundary.

## Preserve legacy connections

Unbound compatibility belongs only to the exact adopted profile, preserved
installation identity and lifecycle. Display name, kind, path and creation order
never grant it. Renaming that instance preserves eligibility; another profile
named Default and a replacement instance do not inherit it.

Before adoption, explicit legacy single-provider startup keeps its current
behavior. It remains separate from standalone use without Backbone. A stored
bound workspace never downgrades after a failed handshake. Existing setup schema 1
stays legacy-only. The detailed Default-adoption action/receipt and profile-bound
setup schema are not defined by these fixtures; they must be specified before
those operations are implemented, including cancel, recovery and runtime probes.

## Validate isolation

The binding cases assume valid credentials and permission for the requested action;
they do not authorize an unauthenticated caller. Run them against server enforcement,
not only a client-side comparison. Each case supplies independent identity and
legacy-eligibility inputs; the discovery examples are not shared registry state
for the cases. `ordinary_restart` specifies the expected post-restart binding,
not evidence that a restart has been executed. Pair them with A/B tests using
identical resource and pane names, switching during in-flight work and replacing the endpoint between
handshake and mutation. Check restart stability, restore invalidation, partial
bindings, renamed eligible Default and an unrelated profile named Default.

Passing these fixture cases alone does not establish complete profiles support.
Runtime login/refresh/logout, integration account and destination isolation, retained
content, queues, exports and diagnostics require their own behavioral evidence.
Claude Code, Codex and OpenCode require equivalent outcomes; other adapters must
record support or a gap. No runtime coverage is changed by retaining this contract.
