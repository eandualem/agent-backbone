# Profile connection contract

Use this contract to build a client that keeps work attached to one profile.
A profile owns one provider, fleet, database, runtime account environment and
integration configuration. Multiple workspaces can share that profile. Selecting
another profile affects new work; it cannot retarget an existing request.

**Implementation status:** every installation runs as a profile. `backbone init` and
`backbone setup` create Default, and `backbone up` refuses to run without one. Isolated
profiles can be created, started and stopped, and agents run inside them on Claude Code,
Codex and OpenCode. Bound discovery, identity, sessions, CLI sign-in and setup schema 2
are implemented. Not done yet: recovery of interrupted operations, replacement and
restore fencing, integration account proof and permission-context inspection.
Provider implementation is tracked in
[#548](https://github.com/eandualem/agent-backbone/issues/548), with the consumer in
[agent-interface#182](https://github.com/eandualem/agent-interface/issues/182).
The agreed design revision is **2**; JSON `schema_version: 1` is the first wire version.
Extension revision **1** adds setup schema 2 and registration-aware permission inspection
without changing revision 2 identity or lifecycle rules. There is no earlier released
profile protocol.

Each provider's database stores its profile and lifecycle IDs against the signing
audience. A replay with identical values is accepted and a conflicting one refused. Before
creating directories, migrating or starting anything, startup checks that identity and the
directory against the external registry. A database holding an identity that the registry
does not explain never starts. A copied database is not evidence of a valid lifecycle.

The registry is a separate SQLite file outside every provider's data tree. It reserves
each profile's identity and canonical data directory. Conflicting identities, overlapping
directories and changed replay input are refused. Directory ownership also refuses case
and Unicode-normalization aliases, including on case-sensitive filesystems. Invalidated
reservations remain as failed records across a database restore and cannot be revived by
replay. These checks run at startup and on each bound request; they do not stop a
same-user process from changing files.

A provider pins its tmux server before any terminal use, and later changes are refused.
Commands, interactive attachment and browser PTY attachment use that socket, regardless of
the inherited tmux selection. Attaching from a client on another server requires a separate
terminal.

On a running provider, authenticated
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
metadata refuses subsequent operations. Internally bound HTTP requests retain a
per-profile, cross-process shared lock from admission through response completion,
including streamed responses and cancellation. Invalidation takes an exclusive
lock and refuses as busy while such a request is active; it can be retried after
the request finishes. Concurrent invalidation also returns fence-busy and can be
retried once the current invalidation finishes. Other profiles remain independent. Private lock files live
beside the external registry and are never removed during provider restore or
cleanup. Discovery and reconciliation do not create them; guarded operations do.
Older reservations overlapping the lock directory remain readable for recovery,
but fencing refuses until that storage conflict is reconciled. New reservations
cannot overlap the lock directory.
Each reconciled provider limits outstanding registry workers to four. Requests
waiting for a slot do not enter the executor queue; cancellation retains an
occupied slot until its worker finishes. This includes admission and subsequent
registry checks, so cancelled requests cannot accumulate unbounded registry work.
The lock covers the ASGI call lifetime. Work that outlives that call, including
detached tasks or threads, needs its own captured operation guard. Periodic callbacks and detached integration work retain their own guards. Connected Socket.IO event handlers
retain the captured provider fence through handler completion or cancellation
cleanup. Emissions acquire a separate fence before recipient validation and keep
it in a tracked task until Socket.IO's packet-send tasks finish. Caller cancellation
can return immediately, allowing reentrant disconnect cleanup to finish, while
the emission task retains its own fence. Connection admission captures the
provider before reading registrations and holds its fence through the initial session snapshot. Failed, cancelled or disconnected
admissions discard their captured state; a changed application context is refused.
Attached PTYs acquire their own captured provider fence before creation and retain
it until process termination and reader-executor shutdown succeed. Per-attachment
resource tasks serialize creation, resize and cleanup independently of handler
cancellation. Disconnect retires pending attaches; EOF or forwarder cancellation
triggers resource cleanup rather than releasing the fence. A failed cleanup keeps
its session and fence for retry after the cause is corrected. Shutdown closes socket
admission before stopping producers, then drains admitted handlers, pending terminal
operations, retained attachments and emissions before disposing services or the
database. Cancelled shutdown callers leave the drain owned; failed cleanup retains
its resources and dependencies for retry. An unsuccessful lifespan shutdown prevents
an automatic in-process upgrade restart. Delayed validation notices and runtime
setup checks capture their originating configuration, database and provider. Before
starting, they acquire a separate operation guard and recheck the captured context;
work whose reservation retires before that check is refused without side effects.
The guard is retained through execution. Bound first-agent setup captures the
request body and full identity, namespaces receipts by that tuple and request ID,
and validates provider and native ownership before effects and after deferred
probes. Cancellation shares the original normalized receipt and prevents a delayed
original request, including when no native environment is available. Schema-1 setup
cannot execute against an isolated provider; Default accepts it. Client cancellation does not cancel setup
workers, and cancellation of the owned batch waits for its executor functions to
exit before releasing the reservation. Shutdown closes admission and drains these API tasks, including every
setup worker, before stopping integrations or storage; cancelling the shutdown
caller leaves the work owned for a later drain. Periodic job callbacks and diagnostic
writes now acquire the captured provider guard in application wiring; the scheduler
remains independent of profile services. Job callbacks retain their entry configuration
and database; integration, source, session-feed and lifecycle-hook suppliers remain live so persistent
polling tasks see authorization and configuration changes. External Telegram handlers and independently retained discovery workers now acquire
the captured provider guard. The live configuration supplier refuses provider or
storage replacement while continuing to observe authorization revocations. Shutdown
stops fetching new Telegram updates, drains the already fetched queue with live
authorization, then closes callback admission and drains retained discovery work.
Each callback owns a finite task separate from PTB's persistent dispatch loop;
cleanup does not require a still-valid identity. Native integration account and
destination acceptance is still required before activation. Provider worker threads
retain their callers through actual worker exit,
including repeated cancellation. Native command creation and cleanup retain the
child handle through reaping; Git cleanup also covers its process group. Telegram
shutdown closes topic-discovery admission and drains its retained topic tasks before
closing the client. Integration-registry shutdown retains both background drainage
and component stops through repeated caller cancellation. Native account acceptance remains a prerequisite before activation. Session-feed
history writes retain their own captured provider guard through transaction
cleanup, including after a snapshot times out or is cancelled. They recheck the
feed's database, configuration and terminal context before writing; a retired or
mismatched context cannot record history.
Replacement and restore fencing and integration isolation are not done yet
(#574, #576).
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
discovery and operation lookup report the registry as unavailable. Explicit
initialization/apply, or replay of the original reservation or invalidation, can
recover the journal before validating the recovered metadata. Apply preserves the
original committed receipt and never repeats unknown effects; reads never perform
that write or
substitute an empty registry. Reconciliation and mutation callers must preserve
the original identity and directory through recovery.

An application started without a profile context, such as a custom ASGI runner, rejects
profile-bound clients: any profile identity header or Socket.IO identity field returns
`binding_mismatch`, including partial or empty bindings. Existing ingress limits and
credential/registration failures run first. The guard then refuses HTTP requests before
route-specific permission or signature checks, and sockets before subscriptions. It keeps
a bound operation from silently reaching a provider that cannot attest its identity.

[Shared fixtures](contracts/profiles-v1.json) contain discovery records, identity,
HTTP headers, Socket.IO identity/error fields, lifecycle requests/receipts,
unknown-operation lookup, binding cases and extension examples described below. All identifiers and paths are synthetic.
`baseline-example-v1` is a fixture value, not an available baseline; `core-v1` is the shipped one. The file's top-level groups and binding cases
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
Listing is read-only: it does not start, migrate or repair providers. An
unavailable registry is an error, not an empty successful list. With `--json`, list,
show and operation report it on stdout with exit status 1: `registry_absent` before
any profile exists, `registry_unavailable` for anything else.

Run these commands from a local operator terminal. Recognized managed-agent
environment markers and agent credentials are refused before registry access.
The local OS user and filesystem permissions are the authority boundary here;
these environment checks are defensive routing checks, not proof against an
unrestricted same-user process that removes its markers. The CLI does not load a
selected provider's operator key to authorize this independent control plane.
Runtime sandbox restrictions still apply; see the [security model](security.md).
The commands read
`~/.local/share/agent-backbone-profiles/registry.db`, outside provider data trees;
`BACKBONE_DATA_DIR` does not select the control-plane registry. No provider secret,
configuration, API or database is loaded. Human output labels provisioning state;
`--json` returns the exact list/show shape. Lifecycle
`apply` explicitly initializes or upgrades the external registry; discovery and
`operation` lookup never initialize or recover it.

The CLI opens a database directly only for the Default profile at that directory, and only
when no provider answers. With no profile it refuses and creates nothing. `init` and
`setup` (`initialize`) are the exception, because they create Default: they first refuse a
directory inside any reservation, including a failed one, and any directory other than an
existing Default's. An unreadable or invalid registry blocks all of these, because
ownership cannot be checked, and they never repair it. An isolated profile's database is
opened only by its own provider.

Registry discovery and reservation checks never recover journals implicitly.

List returns `schema_version` and `items`. Each item has `profile_id`,
`display_name`, `kind` and `state`. `kind` is `legacy_default` or `isolated`.
Provisioning `state` is `creating`, `ready` or `failed`; it does not say whether
the daemon is running.

Show returns an atomic record with those fields, `schema_version`, `binding` and
`git_identity`, which is `copied` when creation copied the person's Git identity and
otherwise `none` (always `none` for Default, which uses the person's own Git config).
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

## Open terminal sessions

`POST /api/profile/shells` ensures a shell on the profile's private tmux server for a
bound operator: `{"schema_version":1,"shell_id":"<12 lowercase hex>","cwd":<absolute
path or null>}`. A null `cwd` starts in the profile's own HOME. The response names the
session. The same `shell_id` always means the same session: a live one is returned
unchanged with `created: false` and its `cwd` ignored, and it is never killed or
respawned. A bad directory returns `invalid_cwd`, a terminal failure
`shell_unavailable`. The session gets only the locale and search path from the host,
with its roots inside the profile. It also gets the profile's data directory, port and
identity (`BACKBONE_DATA_DIR`, `BACKBONE_PORT`, `BACKBONE_PROFILE_ID`,
`BACKBONE_INSTALLATION_ID`, `BACKBONE_LIFECYCLE_ID`), so the `backbone` CLI in that shell
reaches this provider. None of these is a secret, and no credential is passed.

`POST /api/profile/accounts/sign-in` with `{"schema_version":1,"runtime":"codex"}` runs
that CLI's own sign-in in a session on the same server, using that runtime's private
environment in this profile. All CLIs in a profile share one HOME, and each keeps its
own store inside it. Attach to the returned session to complete it. Claude Code,
Codex and OpenCode are supported. Backbone never sees the credentials; the CLI stores
them in its own files inside the profile. On macOS, a CLI that saves its sign-in in the
keychain (Claude Code, and also git and gh) uses the profile's own keychain,
`keychain/profile.keychain-db` in the profile directory. The profile creates it on its
first start, makes it the default keychain of the profile's home, and unlocks it
at each start with a password kept in the profile's `.env`. The person's own keychain is
not used. A repeat request while that session is live
returns it unchanged with `created: false`. The session ends when the CLI succeeds. When the CLI
fails it stays open until someone presses Enter, so the failure can be read. After the
session ends, a repeat starts a new
one. `runtime_unavailable` refuses another runtime or one that is not installed. The
`sessions` group in the shared fixtures has these bodies.

These sessions are named `shell-<shell_id>` and `sign-in-<runtime>`. Agent names in
those forms are reserved: starting, setting up, registering or renaming an agent refuses a
name that starts with `sign-in-` or is `shell-` followed by 12 hex digits (any case).

`GET /api/profile/accounts/status?runtime=codex` runs that one CLI's own status command in
this profile's environment, with a 5-second limit, and returns
`{"schema_version":1,"runtime":"codex","authentication":"authenticated"}`. It makes no
model call and reads no credential itself. `authenticated` means the CLI reports a stored
sign-in, which does not prove the account still works; `not_authenticated` means it
reports signed out; `unavailable` means no usable result, such as a timeout. A sign-in
session ending does not show that it worked, so read this after it ends. Read it when a
setup screen opens and when a sign-in ends, and repeat it only for the CLI being signed in
while that session is open: OpenCode starts a private local server for each read, and
each CLI may update its own bookkeeping files. `runtime_unavailable` refuses as for
sign-in, including on Default.
`backbone profiles sign-in PROFILE_ID --runtime codex` opens and attaches the sign-in
session from a terminal; it does not read the status, so use the status route after it ends.

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

The commands are available as a durable request journal. Apply validates the request
before initializing the external registry, reserves its exact normalized input and
returns the original receipt on replay. Changed input returns `request_conflict`
without replacing the original record. A pending operation prevents another
start/stop request for that profile; the conflicting request retains a failed
`operation_busy` receipt. Pending lifecycle requests also serialize by
installation identity. An interrupted pending operation stays pending across
reopen and exact replay, without repeating effects or automatically releasing its
reservation. Its outcome needs explicit reconciliation.

Create with the `core-v1` baseline, the one shipped version, has effects. It
allocates the profile, installation and lifecycle IDs and a loopback port once,
together with the reservation; creates a private directory beside the registry; writes
a generated `BACKBONE_API_KEY`; installs the starter policies and skills a fresh
install gets; initializes a database holding the identity and those assignments;
reconciles them; and publishes `ready` in the transaction that completes the receipt.
It copies nothing from Default. The optional `git_identity` field is `none` (the
default) or `copy`: copy reads only `user.name` and `user.email` from the person's own
global Git config and writes them to `.gitconfig` in the profile's home. Credentials,
helpers, SSH keys and GitHub CLI sign-ins are never copied. When either value is unset,
`git_identity_unavailable` refuses before reserving anything. An interruption stays
pending and non-ready. A failure that
Create can observe leaves a `failed` tombstone with `profile_create_failed` and
retains the directory. `profile_path_too_long` refuses, before reserving anything,
a location whose socket paths would not fit the Unix limit.

Start launches the provider detached, unless that exact identity already answers, and
succeeds once the authenticated identity check passes. Stop verifies the same identity,
then signals only the process the kernel reports behind that endpoint, and succeeds
once connections are refused. Nothing listening is already stopped. It never forces a
process to exit. The profile's private tmux server and its sessions outlive the
provider. `service_start_failed` and `service_stop_failed` report a provider that did
not get there in time; another identity behind the endpoint is never signalled.

Any other baseline returns `baseline_unavailable`, and
start/stop return
`profile_not_ready` for a reservation still being created or `binding_mismatch` for a
stale, invalidated or unknown tuple. A `failed` reservation is a permanent invalidation tombstone, not
a valid binding awaiting readiness.
These are durable failed-operation receipts with nonzero exit status. Refusal
classification and receipt storage commit atomically, so a failure before that commit
does not leave a pending reservation. Refusals do not create provider directories or
start/stop services.
An unchanged failed request continues to return that failure;
new work after prerequisites change requires a new request ID. Receipts have no
automatic expiry or pruning: the original outcome and changed-input conflict are
retained without a replay window. Any future compaction or storage quota needs an
explicit policy that preserves those guarantees; provider event retention does
not apply to this external lifecycle journal.

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

Unbound compatibility belongs only to the exact Default profile, preserved
installation identity and lifecycle. Display name, kind, path and creation order
never grant it. Renaming that instance preserves eligibility; another profile
named Default and a replacement instance do not inherit it.

A stored bound workspace never downgrades after a failed handshake. Existing setup
schema 1 stays unbound. The following extension defines bound setup; it does not silently
convert saved schema-1 requests.

## The Default profile

`backbone init` and `backbone setup` (`initialize`) create the first profile, named
Default. It is the installation at its data directory, in the person's own environment:
their HOME, CLI sign-ins and default tmux server. Other profiles are isolated from it and
from each other. Creating it again is safe and changes nothing, and only one data directory
can be Default. `backbone up` serves it, and refuses when no profile exists.

Default serves its identity on both its port and its socket. It installs no captured
runtime environments, so its agents use the person's CLI sign-ins; schema-2 setup and the
bound runtime catalog report those runtimes unavailable there, and the app uses schema 1
for Default. Start launches `backbone up` if nothing answers. Stop signals that process,
and a service manager may start it again.

A shell on Default is a login shell in the host environment, without the installation's
secrets, and sign-in there refuses, because Default uses the person's own CLI sign-ins.

Only the exact Default tuple receives revision 2 legacy eligibility.

## Set up an agent within a captured profile

The local CLI admits an operator before reading the external registry or the
selected credential. It captures one atomic ready record, reads its exact private
dotenv reference without interpolation, and verifies the authenticated identity
and terminal socket over that record's Unix endpoint. There is no ambient
credential, proxy, redirect or legacy-provider fallback. Each invocation retains
the original request bytes and resolved credential; it never refreshes or retries
the transport automatically. A lost mutation result remains uncertain.

After a successful handshake and before the first setup or cancellation POST, a
private local receipt captures the normalized intent fingerprint, endpoint,
credential reference and digest, and
terminal socket. It lives beside the registry in its protected operations directory,
outside provider data. It stores neither credential bytes nor the request's brief.
Both actions share one tuple-and-request-ID receipt. Concurrent capture is serialized;
changed intent returns `request_conflict`, and changed connection metadata returns
`binding_mismatch`. Existing or corrupt receipts are never replaced to recapture
an operation. An existing capture is checked before the handshake and again before
the POST; a failed initial handshake does not pin a never-dispatched connection.
Each profile has its own capture lock and capacity. Receipts do not expire; at the
existing 1,024-receipt capacity per profile, new IDs are refused while recorded IDs
remain usable. Capacity refusal explains that limit with `retry: "none"`; repeating
or replacing a new ID cannot free space. The provider's execution and
cancellation journal remains authoritative.

A new CLI process is not permission to refresh a pending operation's connection.
Digest-only continuity deliberately refuses a rotated credential, even if the
original credential might still work. This is a conservative availability limit,
not seamless recovery across rotation. A refusal proves neither cancellation nor
absence of previous effects; retain the original request and inspect its provider.

`backbone setup --file request.json --jsonl` accepts the agreed schema-2
`setup.first_agent` shape. It replaces schema-1 `data_dir` authority with
`profile_id`, `expected_installation_id` and `expected_lifecycle_id`. The remaining
agent, hooks, untagged brief and greeting fields retain their existing meanings.
Optional terminal presentation fields, where supported, retain their own contract;
this extension adds none. The project directory is a work location, never native
HOME or account authority. No client environment, CLI config root or credential
input is accepted. The provider resolves native context from its own registry.

Validate the full tuple before each effect, including project-directory creation,
and after deferred probes. A replacement refuses the captured work; it cannot
rewrite its identity. Retain executable, exact request ID/body and binding across
selection changes, interruption and retry. Persist pending setup under the full
tuple and request ID. Do not convert saved schema-1 requests or fall back to them.

Every schema-2 event echoes the requested profile, installation and lifecycle IDs,
including failures before an instance can be resolved. Events retain increasing
`seq`, the original request ID and `step`/`result` types. Schema-2 step events also
identify the original action. Step events carry step/status; results retain the
existing status/code/message/retry/completed_steps/data shape. `setup.progress`
and `setup.result` illustrate one invocation; other results are independent cases
with their own sequence. Ready means the CLI stayed running after launch, not that it
reached its prompt or is signed in.

Cancellation changes only the original action to `cancel_first_agent`, preserving
ID, body and binding. Compare the saved body after normalizing that action back to
`first_agent`. Action-specific reply replay is scoped by action and request ID, but both
actions share the original normalized operation and its cancellation marker. A
separate cancellation reply must never leave the original action executable.
Even cancellation before receipt creation blocks a delayed original submission;
`setup.cancelled_original_result` shows its refusal. Linearize cancellation with
execution and refuse another lifecycle. Safe cancellation returns
`request_cancelled`, `retry: new_request`, no completed steps and the original
cancelled ID; unsafe cancellation preserves work with `cancellation_unsafe`.
After a lost cancellation response, retry that cancellation, not first-agent setup.

The `runtime_catalog.command` fixture gives the bound `runtimes --json --check-setup`
flags. Schema 2 echoes the full tuple and retains the existing item fields. Its
nonempty catalog is an illustrative item, not a complete installed-runtime inventory.
All probes use provider-owned native environments; host PATH may locate a binary
but cannot supply account/config roots. Reject stale bindings before subprocess
spawn. Discovery never logs in or calls a model. In the existing `authentication`
field, `not_authenticated` means an observed signed-out state; `unavailable` means
no usable probe result and `unsupported` means no supported probe. Unknown discovery
remains `unknown`; binary/version availability proves none of these authentication
outcomes. The catalog runs no account status; use the account status route. `authentication_cases` shows field values, not full responses. Unsupported
schema/flags leave discovery unavailable; never retry unbound.

The bound CLI uses `GET /api/runtimes?schema_version=2&check_setup=true` after
the identity handshake. This operator-only variant returns the schema-2 envelope;
omitting `schema_version`, or passing `1`, retains the existing API list shape.
The provider owns and drains the observation task through executor completion,
rechecks its captured context before each probe and before publication, and
retains the operation fence after client cancellation. A runtime without an owned
native environment has unavailable setup observation, without a host probe.
Adapters without isolated native support report unsupported authentication.
Available binaries and compatible versions still leave authentication unknown.

Bound setup also rechecks after registration/profile locks and database reads,
before snapshot publication and credential provisioning, and after terminal
capacity and process-creation waits before sending launch input. A committed
registration is recorded before later checks can refuse further work. Cleanup of
an admitted launch still removes temporary secrets and completes session scrubbing.
Greeting delivery carries the same check through readiness, runtime detection,
queue admission, paste and submit waits. If authority changes after text was
pasted, retain an uncertain receipt for inspection instead of submitting or
automatically repeating it; cleanup still removes the operation's paste buffer.
A proven guard refusal before paste or queue admission clears only that greeting
attempt, so the same request can retry once its original authority is valid.

## Inspect permissions for one agent registration

List and inspect responses now include the durable `registration_id`; bound reads
recheck it before returning, and inspect accepts `expected_registration_id`. A
cached row or deferred inspect for a replaced registration returns the fixed
`registration_changed` error. The runtime-context permission endpoint below is
still unimplemented.

Authenticated bound `/api/agents` items and `/api/agents/{name}/inspect` expose
`registration_id` from the durable registration (`AgentSpec.registration_id`, stored
as `AgentORM.report_identity`). It is unchanged lowercase UUID hex matching
`^[a-f0-9]{32}$`. Capture it from the current agent registration, never from a
process/session ID or a retained content record's author ID. Unregistered
live-session rows expose null; reads never allocate registrations. The
`bound_list_member_fields` and `bound_inspect_fields` fixtures are additive field
fragments, not replacements for existing list or inspect response shapes.

Capture tuple, name, registration and selection generation when selecting a target.
A fresh name navigation may acquire its initial ID through one bound inspect without
`expected_registration_id`. Once captured, every inspect refresh supplies that ID.
`GET /api/agents/{name}/runtime-context?expected_registration_id=ID` always requires
it and the existing three binding headers. Omitted required input and present
empty/malformed input are invalid, never permission to perform a name-only read.
A well-formed changed ID returns the exact `registration_changed` HTTP 409 fixture
without replacement inspection data, including when the retained registration
has become null on an unregistered row. Capture and recheck registration immediately
before returning list, inspect or context results. A change during inspection must
also refuse the response. Binding/authentication refusals keep their existing order.

The context response echoes tuple, name, runtime and registration. Consumers validate
all of them and the retained selection generation before applying deferred results.
Replacement requires fresh explicit selection, never an automatic target refresh.
An unregistered row cannot form a permission target. An unknown runtime reports
unsupported; no attestable profile leaves inspection unavailable/404 with no host
reads. No error falls back to Default, ambient HOME or client filesystem parsing.

The provider parses effective launch configuration and project rules with their
precedence and bounded reads/results. `permissions` has status
available/unavailable/unsupported, string arrays allow/deny/ask, `{name,value}`
string modes and null or fixed `{code,message}` error. File scopes are
runtime/project/project_local; states are read/missing/invalid/unreadable.
`display_path` is relative and display-only, never filesystem-read authority.
A missing optional file can accompany available metadata. Missing required context
or an invalid/unreadable required source is unavailable, never apparently
unrestricted empty permissions. Fixtures include both distinctions.

Omit or redact credential-bearing permission values. Never return complete config,
auth data, process environment or raw parser errors. The synthetic redaction case
requires omission from allow; the same rule applies to deny, ask, modes and paths.
These are inspection observations, not proof of every runtime authorization.

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


## Internal native environment preparation

Captured contexts also recheck adapter-declared account and configuration files
before use. An external symlink, hard-linked file or non-regular file refuses
the operation without reading its contents or invoking the native command.
Missing files remain valid for an empty profile. These checks do not inspect
credential-helper settings, machine policy or project configuration, and do not
protect against concurrent writes by another process with the same OS identity.

A provider captures a separate, immutable native environment and executable for each runtime before use. Trust
preparation, hooks, user-file readers, transcript and usage readers, version probes,
and launch/resume commands use that capture. Native account invocations carry the
same environment, including CLI sign-in. Per-agent overrides
cannot redirect native stores or select an inherited account/server. Complete tmux
launch environments clear server-only variables and disable environment import on
attach. Activation must own the pinned server and its global environment; these
launch helpers do not fence arbitrary external tmux writes. Default retains its
existing environment and launch behavior. Native path checks reject observed
redirects outside captured roots; they are not a sandbox against a same-user
process concurrently replacing files or path components.

Claude Code, Codex and OpenCode have internal preparation tests; other model adapters
refuse this preparation. Codex requests file credential storage, subject to native
managed policy. OpenCode uses a private native server. Claude Code and Codex
sign-ins were checked on the working installation, and a signed-in OpenCode result is
not yet seen (#579); the adversarial acceptance list and lifecycle
fencing are not done yet (#575, #574).

Internal skill materialization can now claim the old and new checkout and their
common Git directories in the external profile registry before removing links,
creating links or updating Git excludes. A competing profile is refused and must
use a separate checkout; linked worktrees still share the common Git directory.
Claims are atomic and remain after skill cleanup or profile invalidation. Releasing
or transferring them requires explicit reconciliation, not automatic expiry.
No ownership marker is added to the project. Explicit registry initialization
upgrades the internal store; discovery remains read-only.
Guarded materialization also refuses symlinked or hard-linked Git excludes and
nonregular Git `commondir` files before cleanup or writes.

The internal ownership context binds these claims to a reconciled provider identity
and cannot switch profiles in a running process. When installed, launch checks
ownership before trust preparation even without shared skills; rematerialization,
agent cleanup and swarm worktree creation/removal use the same identity. A conflicting
cleanup retains the agent registration and its links. Subdirectory launches claim
the containing checkout, and linked worktrees also claim the canonical main checkout
whose native configuration they may consume. Retained callbacks recheck the registry
and refuse an invalidated lifecycle.
Standalone separate Git directories remain independent. Linked worktrees whose
shared Git directory does not use the conventional `.git` layout are refused
before claims are saved, because the main checkout cannot be identified safely.
Swarm creation checks that future linked-worktree layout before creating its
branch, worktree or member registrations. For separate storage named `.git`, Git
reports the storage parent as its canonical main worktree; swarm creation reserves
that parent before creating resources, just as linked-worktree launch does.
Disabling the skill store does not block cleanup: guarded cleanup uses the store
location retained in the agent's manifest.
A manifest with a missing or non-list `links` field is retained for repair instead
of discarding it and leaving its links behind.

The same launch and materialization behavior tests run for Claude Code, Codex and
OpenCode. Isolated providers take these claims; Default does not yet, so keep one checkout
out of both Default and an isolated profile. Native configuration contents, credential
helpers, managed policy, database restore and in-flight operation fencing are not covered.
Claims do not protect against a same-user process replacing files concurrently.
