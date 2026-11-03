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
version. Extension revision **1** adds Default adoption, setup schema 2 and
registration-aware permission inspection without changing revision 2 identity or
lifecycle rules. These extension operations are agreed but not implemented.
There is no earlier released profile protocol.

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
detached tasks or threads, needs its own captured operation guard. This does not
yet fence background jobs or native processes. Connected Socket.IO event handlers
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
an automatic in-process upgrade restart. Detached job threads and other native
lifetimes still require complete drainage before activation. Session-feed
history writes retain their own captured provider guard through transaction
cleanup, including after a snapshot times out or is cancelled. They recheck the
feed's database, configuration and terminal context before writing; a retired or
mismatched context cannot record history.
Complete replacement/restore fencing, ready publication
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
unknown-operation lookup, binding cases and extension examples described below. All identifiers and paths are synthetic.
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
stays legacy-only. The following extension defines adoption and bound setup; it
does not silently convert saved schema-1 requests.

## Adopt Default explicitly

`backbone profiles adoption-preview --json` is the agreed read-only operator
preflight. It resolves the configured existing installation independently of the
selected content profile. It never starts, stops, migrates, repairs or writes.
Unsafe or ambiguous ownership refuses preview rather than choosing an instance.
The `adoption.preview` fixture contains the existing installation ID, canonical
data directory, tmux mapping, baseline and an opaque adoption token.

The token fingerprints canonical data/database locators, installation identity,
credential locator and change evidence, effective service/tmux mapping and recorded
runtime-environment semantics. It contains no credential bytes and grants no
additional authority. The provider owns its computation; clients retain it unchanged.
Submit `adoption.request` through `profiles apply`. The request accepts no arbitrary
data directory, environment, credential source or account-copy input.

Serialize adoption with lifecycle work on that installation. Validate the current
token before adoption, including a new request for an already adopted instance.
Changed metadata returns `adoption_changed`. An identical original request/body
replays its durable receipt; a changed body under that ID conflicts. A new request
for the same original instance returns the existing tuple, never a sibling identity.
An identity-incompatible restore is not repeated adoption. The `adoption.cases`
records describe these separate scenarios. `outcome` is `succeeded` or a refusal
code; optional `expected_receipt` names the corresponding receipt fixture. These
are case metadata, not wire fields. Changed-body reuse is a rejection, not a new
operation or permission to overwrite the original receipt. Completed exact replay returns its recorded result;
it performs no new adoption effects.

Reserve the request before effects and allocate profile/lifecycle IDs once,
preserving the installation ID. Back up touched metadata first and retain that
receipt locally. Preserve credential bytes, native account roots, baseline edits,
data and service/tmux mapping in place. Adoption does not restart a service,
relocate storage or log in/out. Receipt identities may be null before reservation.
Success means metadata reconciliation, not a verified accepting service. Fence old
processes, jobs and streams before any replacement accepts bound traffic, then use
explicit lifecycle operations and an authenticated handshake to establish access.
Only the exact adopted tuple receives revision 2 legacy eligibility.

## Set up an agent within a captured profile

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
with their own sequence. Ready means measured launch readiness, not proven login.

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
outcomes. `authentication_cases` shows field values, not full responses. Unsupported
schema/flags leave discovery unavailable; never retry unbound.

## Inspect permissions for one agent registration

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

Reserved profiles still cannot start. Internal preparation captures a separate,
immutable native environment and executable for each runtime before use. Trust
preparation, hooks, user-file readers, transcript and usage readers, version probes,
and launch/resume commands use that capture. Native account invocations carry the
same environment; no public account operation executes them yet. Per-agent overrides
cannot redirect native stores or select an inherited account/server. Complete tmux
launch environments clear server-only variables and disable environment import on
attach. Activation must own the pinned server and its global environment; these
launch helpers do not fence arbitrary external tmux writes. Default retains its
existing environment and launch behavior. Native path checks reject observed
redirects outside captured roots; they are not a sandbox against a same-user
process concurrently replacing files or path components.

Claude Code, Codex and OpenCode have internal preparation tests; other model adapters
refuse this preparation. Codex requests file credential storage, subject to native
managed policy. OpenCode uses a private native server. These mechanisms are not
account-isolation acceptance: real-account login/logout/refresh, native configuration
and credential-helper constraints, project materialization conflicts, and lifecycle
fencing remain prerequisites before activation. No shipped capability coverage or
profile readiness is promoted by these internal tests.

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
OpenCode. Production startup does not install this context yet. Default adoption
must reconcile existing materializations before installation. Native configuration
contents, credential helpers, managed policy, database restore and in-flight operation
fencing remain prerequisites before activation. Claims do not protect against a
same-user process replacing files concurrently.
