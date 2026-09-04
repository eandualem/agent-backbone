[from:osreview2-coordinator]

# Independent review before wider use

Review target: `eandualem/agent-backbone` at `793faaf` (`main`, 0.1.0), with small
certain corrections applied on `swarm/osreview2`. Two independent reviewers covered
the runtime/import and duplication/defect charges; the coordinator reproduced and
synthesized the results and performed the reader/claims pass.

Severity means: **P1** can send the wrong terminal control or acknowledge data that
was not retained; **P2** is a material reliability or maintainability defect with a
bounded workaround; **P3** is a low-risk cleanup or documentation problem. No P0 was
found.

## Ranked overview

| Rank | Severity | Finding | Status |
|---:|:---:|---|---|
| 1 | P1 | Plan control is Claude-specific, is callable for every runtime, and bypasses `safe_deliver` | Fixed (#108) |
| 2 | P1 | `queued: true` does not prove that a message was retained; enqueue errors and content dedup can lose it | Fixed (#109) |
| 3 | P2 | Hook installation and event translation are not `Runtime` capabilities | Fixed (#123) |
| 4 | P2 | A missing swarm directory leaves a registered Git worktree that blocks reuse | Fixed (#121 follow-ups) |
| 5 | P2 | A transient GitHub error widens the acknowledgment gate to closed issues | Fixed (#121 follow-ups) |
| 6 | P2 | Queue expiry has no per-message terminal record | Fixed (#121 follow-ups) |
| 7 | P2 | Import enforcement misses lazy edges and leaves `agents -> help` outside the documented graph | Fixed (#121 follow-ups) |
| 8 | P2 | CLI/API/Telegram mutations are duplicated and have already drifted | Fixed: stop and forget behind shared operations (#121 follow-ups) |
| 9 | P2 | Runtime IDs and hook behavior have owners outside the runtime registry | Fixed (#123) |
| 10 | P3 | Duplicate DTOs and one unreachable queue branch add avoidable maintenance risk | Open |

## 1. Runtime boundary

### R1 — P1 / rank 1: plan control can send Claude controls to another runtime

The invariant says all runtime-specific behavior belongs in the adapter and every
paste from API code goes through `safe_deliver` (`CLAUDE.md:20-30`). The plan surface
breaks both rules:

- `services/agents/launch.py:328-334` implements approval as the Claude Code
  Shift+Tab sequence, `Escape` then `[Z`, without a runtime argument.
- `api/routes/plans.py:80-90` exposes that operation for any registered session.
- rejection sends raw `Escape` and then calls `runtimes.send_message` directly
  (`api/routes/plans.py:93-115`); response input also calls `send_message` directly
  (`api/routes/plans.py:118-138`). These paths bypass the queue, readiness gate,
  provenance rules, paste result handling, and delivery record.
- This is reachable for a non-Claude adapter now: the external state endpoint accepts
  a `waiting_for_human`/`plan` state for every registered agent
  (`api/routes/agents.py:356-379`).
- The adapter contract only models permission prompts through `approve_keys` and
  `approve_prompt` (`services/runtimes/base.py:90-104,320-331`); it has no plan
  approve/reject/respond capability and no explicit unsupported result.

Concrete reproduction: register a Codex or OpenCode agent, POST its state as
`{"state":"waiting_for_human","reason":"plan"}`, then POST
`/api/plans/<agent>/approve`. The route reaches `approve_plan(<agent>)` and sends the
Claude key sequence. Existing route tests mock `approve_plan` and therefore cannot
detect the cross-runtime control (`tests/unit/api/routes/test_api_plans.py:136-155`).

Recommendation: make plan actions explicit adapter capabilities with an unsupported
result; resolve the registered runtime in the route; and route plan feedback/input
through `safe_deliver`. Add a non-Claude test that asserts zero terminal keys.

### R2 — P2 / rank 3: hooks are a parallel runtime implementation

`Runtime` supplies only `hook_launch_args` (`services/runtimes/base.py:128-132`), and
Claude's adapter uses it only to lazy-import a separate installer
(`services/runtimes/claude.py:82-100`). The settings location, event vocabulary,
script copying, merge format, install and uninstall lifecycle are Claude-specific but
live in `hooks/install.py:19-45,72-79,114-154`; event and tool translation live in
`hooks/claude_hook.py:97-184`. The CLI then dispatches explicitly on `claude`
(`cli/__init__.py:174-187`, `cli/agents.py:426-449`).

A Codex or OpenCode hook adapter needs more than launch arguments:

- hook capability metadata for CLI/API discovery;
- scoped install/uninstall and the runtime's settings/config target;
- launch-time wiring that need not be a CLI argument;
- payload/event translation into shared state and action records;
- the plan-control capabilities from R1 before emitting `reason=plan`.

Recommendation: define a hook capability/lifecycle contract on `Runtime` (or a hook
adapter owned by it), then parameterize lifecycle and state-round-trip tests over the
registry. Keep hook executables standard-library-only as required by
`CLAUDE.md:75-77`.

### R3 — P2 / rank 9: adding a runtime has more than one source of truth

The extension-point docstring says a runtime is one new module plus registration
(`services/runtimes/base.py:1-8`), and the registry says no other module names a CLI
(`services/runtimes/__init__.py:1-8`). In practice:

- the full supported-ID tuple is also owned by `config.py:44-46`, and the registry
  asserts that the two copies agree (`services/runtimes/__init__.py:12,30-49`);
- defaults/fallbacks occur at `config.py:70,258,360,689`, `api/models.py:77`,
  `services/database/models.py:35`, and the initial migration at
  `services/database/migrations/versions/2026_09_02_3fb2fe03898c_initial_schema.py:57`;
- hook availability and dispatch are separately hard-coded at
  `cli/__init__.py:181` and `cli/agents.py:426-449`;
- runtime-specific hook implementation and plan controls are the external sites in
  R1 and R2.

Persistence defaults may remain explicit compatibility exceptions, but accepted IDs
and capabilities should derive from one registry. Add an AST/literal boundary test
with narrowly documented exceptions. Today the test at
`tests/unit/services/runtimes/test_runtimes.py:17-20` catches drift only after two
sources have been edited; it does not prove the advertised one-module extension.

## 2. Layering

### L1 — P2 / rank 7: the executable import check is incomplete

The documented graph says `services.agents` may import runtimes, terminal and database
(`CLAUDE.md:31-43`), but `services/agents/launch.py:13-20` eagerly imports
`agent_backbone.help`. `help` has no position in the graph, while the agents forbidden
set omits it (`tests/unit/test_imports.py:75-83`). The check merely imports each named
package and inspects `sys.modules` (`tests/unit/test_imports.py:106-124`), so it cannot
observe function-local imports. Its matrix also omits several production leaves and
entry modules (`tests/unit/test_imports.py:20-103`).

No confirmed forbidden service-to-service upward edge was found after inspecting
function-local imports; `agents -> help` is the one graph ambiguity. The current 14
fresh-interpreter tests pass, but that result is weaker than the claim that the graph
is asserted.

Recommendation: assign `help` a layer, retain fresh-process tests for circular import
behavior, and add a static AST import-edge test across every production module,
including imports nested inside functions.

## 3. Duplication and dead code

### U1 — P2 / rank 8: command mutations have three policy surfaces

`services/agents/operations.py:1-5` says API and direct CLI paths share functions so
they never drift, but it shares only start resolution/execution
(`services/agents/operations.py:40-100`). Stop, set, watch, unwatch and forget remain
implemented separately in the API (`api/routes/agents.py:224-333`) and CLI direct mode
(`cli/agents.py:141-181,279-359`); Telegram is a third direct caller
(`services/integrations/telegram/_commands.py:136-181,294-330`).

The concrete drift was safety-relevant: only the API refused to stop the backbone's
own session, while API-down CLI and Telegram called the unguarded tmux stop path;
Telegram delivery/control also accepted an arbitrary unregistered tmux name. The
branch adds consistent own-session and registration guards at
`cli/agents.py:162-180`, `integrations/telegram/_commands.py:136-181,294-330`, and
`integrations/telegram/_routing.py:96-124`, with regressions at
`tests/unit/test_cli.py:198-203` and
`tests/unit/services/integrations/telegram/test_telegram.py:98-105,147-154,188-222`.

Recommendation: put every mutation behind shared operations and keep API/CLI/Telegram
as parsing/authorization/presentation layers. The local guards are a certain fix, not
a substitute for consolidation.

### U2 — P3 / rank 10: duplicate shapes and unreachable code

- `StateSnapshot` (`services/agents/models.py:39-52`) and `AgentStateDetail`
  (`api/models.py:170-183`) duplicate the same fields.
- `SwarmResult` (`services/swarm/interface.py:50-58`) and `SwarmCreateResponse`
  (`api/routes/swarms.py:33-41`) duplicate their fields; the route constructs the API
  object from `result.__dict__`, so drift is detected only at runtime.
- Delivery-reference rendering and Telegram status/queue presentation repeat CLI/API
  query and formatting logic.
- `QueueRepo.enqueue` has an `issue`/no-number branch
  (`services/database/_queue_repo.py:45-56`) that production cannot reach because
  `_enqueue` returns on exactly that input
  (`services/routing/_delivery.py:147-161`).

No genuinely unreferenced production functions, classes, or repository methods were
found by definition/reference scanning. Remove the unreachable branch when queue
semantics are next changed; use explicit response constructors or shared serializers
for the DTO pairs.

## 4. Defects

### D1 — P1 / rank 2: queue acknowledgment is not tied to persistence

`_enqueue` catches every exception and returns no success value
(`services/routing/_delivery.py:147-175`). `finish` ignores whether a row was inserted
(`services/routing/_delivery.py:239-263`), while the messages endpoint derives
`queued: true` solely from the blocked outcome
(`api/routes/messages.py:42-57`, `services/routing/_delivery.py:42-55`). Thus an
enqueue exception still produces a normal response claiming queueing.

There is a second loss mode without an exception. Every pending non-issue message is
unique only on `(session_name, content_hash)`
(`services/database/models.py:238-250`), and `QueueRepo.enqueue` returns `-1` on a
conflict (`services/database/_queue_repo.py:19-61`). `_enqueue` silently treats that
as success. Two intentional identical direct messages to the same agent therefore
produce two `queued: true` responses but only one row. The existing test proves the
mechanism by asserting the second direct message returns `-1` and only two of three
messages remain (`tests/unit/services/database/test_persistence.py:456-482`). The same
key also conflates equal comment/watch/escalation text without an event identity.

Concrete exception reproduction: make an agent busy, patch `db.queue.enqueue` to
raise, POST `/api/messages`, and inspect both the response and queue. The route returns
the blocked outcome with `queued: true`; `message_queue` has no row. No failing test is
committed because it intentionally describes unresolved API semantics.

Recommendation: make `_enqueue` return an inserted/deduplicated/failed result and make
`safe_deliver` carry it to callers. `queued` must mean a durable row exists. Dedup
should use a stable source event/delivery identity, not message text; an intentional
repeat must remain distinct.

### D2 — P2 / rank 4: missing worktree directories leave Git metadata behind

Teardown calls `remove_worktree` only if the filesystem path exists
(`services/swarm/interface.py:311-320`), while creation retries the existing branch
without cleaning a missing registered worktree
(`services/swarm/_worktree.py:59-69`). A real-Git reproduction is:

```text
git worktree add .backbone/swarms/sw -b swarm/sw
rm -rf .backbone/swarms/sw
# teardown skips `git worktree remove`
git worktree add .backbone/swarms/sw -b swarm/sw
# fatal: branch already exists
git worktree add .backbone/swarms/sw swarm/sw
# fatal: missing but already registered worktree
```

Recommendation: attempt administrative removal even when the directory is missing,
then use a targeted `git worktree prune`/repair path for Git's “missing but registered”
result. Add a real temporary-repository regression; the mocked teardown test currently
does not assert removal (`tests/unit/services/swarm/test_swarm.py:361-398`).

### D3 — P2 / rank 5: a scope lookup failure expands the acknowledgment gate

During queue drain, failure to list the target's open GitHub queue leaves `scope=None`
(`services/jobs/retry.py:65-84`). `_get_unacknowledged_gate_issue` converts None to an
empty set, and an empty set disables filtering, so it scans the last 100 historical
issue deliveries, including closed issues (`services/routing/_delivery.py:86-110`). An
old delivered-but-unacknowledged closed issue can then return `awaiting_ack` and stop
the whole queue (`services/jobs/retry.py:86-97`).

Concrete reproduction: seed a successful delivery for closed `repo#1` without an
acknowledgment. Calling the gate with `{("repo", 2)}` returns `None`; calling it with
`None`, as the exception path does, returns `("repo", 1)`. Recommendation: distinguish
“scope unavailable” from “no open items” and defer/skip the gate rather than widening
it.

### D4 — P2 / rank 6: expiry is not an observable terminal outcome

`expire_pending` bulk-updates queue rows to `expired`
(`services/database/_queue_repo.py:122-136`); the retry job emits only an aggregate
count (`services/jobs/retry.py:48-58`). There is no per-message delivery record or log,
so `agent inspect`/deliveries cannot tell the sender which queued message was finally
dropped. Non-issue messages have no other retry history.

Concrete reproduction: enqueue a direct message, move its `enqueued_at` 31 minutes
back, and run the drain with the default 30-minute expiry. The queue row becomes
`expired`, but no delivery row records an expiry outcome for that message. The branch
corrects the CLI promise to name the configured expiry
(`cli/agents.py:384-392`); persistence/audit semantics remain open.

Recommendation: expire rows individually (or return their identities) and record an
`expired` terminal outcome with session, kind, source, and preview.

### D5–D9 — small certain defects fixed on this branch

- **All sessions gone:** `main` returned before offline detection, queue hygiene, and
  notifications. `monitor_agents` now continues with the empty active set
  (`services/jobs/monitor.py:60-99`), locked by
  `tests/unit/services/jobs/test_monitor.py:379-396`.
- **Configured runtime discarded during fallback:** `agent_state` now passes the
  stored runtime to terminal inference (`services/agents/_inference.py:161-169`); a
  Gemini busy-marker regression is at
  `tests/unit/services/agents/test_agent_state.py:254-264`. API-down CLI inspection
  now uses that configured helper too (`cli/agents.py:262-274`), covered by
  `tests/unit/test_cli.py:204-221`.
- **Historical failures multiplied retries:** `DeliveryRepo.failed` now returns only
  the latest attempt per repo/issue/target (`services/database/_delivery_repo.py:161-186`),
  covered by `tests/unit/services/database/test_persistence.py:130-142`.
- **Failed session stop followed by destructive teardown:** teardown now collects
  failed stops and leaves the worktree, registrations and swarm active
  (`services/swarm/interface.py:291-320`), covered by
  `tests/unit/services/swarm/test_swarm.py:400-431`.
- **Stop/delivery/control guard drift:** API-down CLI refuses the backbone session;
  Telegram refuses that session and all unregistered targets. Evidence and tests are
  listed under U1.

Suspected lease races, `queued.index(record)`, monitor-lock TOCTOU, detector
sanitization, and issue-claim reclaim races were investigated and dropped because no
reachable failing interleaving was found.

## 5. Reader's pass and claims

### Reader tasks

| Question | Result |
|---|---|
| Where is state decided? | Clear and correct: `docs/how-it-works.md:47-93` and `docs/concepts.md:60-81` lead from hook-first state to terminal fallback and evidence. The configured-runtime fallback bug found during that trace is fixed in D5–D9. |
| Where is a message pasted? | Clear for ordinary delivery: `docs/how-it-works.md:95-133` leads to `safe_deliver`, the runtime adapter, then tmux paste. The plan endpoints are the undocumented bypass in R1. |
| How do I add a runtime? | Material wrong turn: public docs describe selecting runtimes, but no contributor path explains the extension contract; the only source claim says one module plus registration (`services/runtimes/base.py:1-8`), which omits R1–R3's external owners. |
| What happens when the backbone is down? | Mostly clear: `docs/how-it-works.md:263-270` correctly says agents keep running, API messages fail, and intake catches up. “Webhook intake runs its startup backfill” needs the `github.backfill_on_start` qualification documented at `docs/configuration.md:54-57`; it can be disabled. |

### Claims that are not true as written

- README says a message is “stored until” the recipient is ready and later says it is
  delivered when free (`README.md:22`, `README.md:70,78`). Queue entries expire, and
  D1 shows `queued: true` does not currently prove storage.
- API and CLI docs promise durable queueing from the response
  (`docs/api.md:122-128`, `docs/cli.md:184-195`). That is false on enqueue error and
  content-dedup conflict (D1). Do not soften this only in prose; fix the response
  contract, then document its exact states.
- Status says a busy message is delivered exactly once and every delivery is recorded
  (`docs/status-and-roadmap.md:14-17`). D1 permits zero retained copies, while D4 has
  no terminal expiry record.
- README said every started agent was briefed at launch. The actual policy has a
  disable switch, plain shell gets none, and several resumed runtimes are not
  re-briefed (`docs/configuration.md:46-48`). The branch narrows the README statement
  to the supported/default/new-session behavior (`README.md:27-31`).

## Branch contents and verification

Only high-confidence local corrections are included: empty-session monitoring,
configured runtime hints, latest-attempt retry selection, stop-before-delete swarm
teardown, CLI/Telegram target guards, truthful CLI expiry wording, the README brief
qualification, regression tests, and this report. The architectural/API findings are
intentionally report-only.

Added by the repository's own agent while reviewing the branch: the `SwarmError`
that stop-before-delete teardown raises is now handled at both callers (the
issue-closed hook logs it and leaves the swarm active; `DELETE /swarms/{name}`
answers 409 with the reason), with a route test; `API_VERSION` reads the package
version instead of a hard-coded string; the README brief sentence was reworded
without changing its qualification.

Validation on the branch:

- Focused regression set: **227 passed**.
- Runtime/hook/import reviewer set: **134 passed**.
- Import-only reviewer set: **14 passed**.
- `make check`: **passed** — Ruff lint and format checks passed; **889 tests passed**
  with one pre-existing `PtySession._read_loop` unawaited-coroutine warning.
