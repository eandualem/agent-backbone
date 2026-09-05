# Swarm audit — 2026-09-05 (GPT-6-Astra, run cut short)

Issue #133. Coordinator `codex/gpt-6-astra:high`, two scouts `gpt-6-astra:low`.
The run ended after ~25 minutes when the Codex plan's usage window emptied;
scout-1 had been switched to `gpt-5.6-luna` by a rate-limit dialog answered
as a permission prompt (#136). Nothing was implemented by the swarm. Both
scouts' reports are preserved here verbatim (scout-1 from its owned report
file, scout-2 from the text of its `backbone tell` messages), followed by the
coordinator's two source validations and the initiator's disposition of each
candidate.

---

# Scout 1 work-pipeline audit — 2026-09-05

Scope: read-only review for issue #133. Required first reads: `CLAUDE.md`,
`docs/concepts.md`, and `docs/how-it-works.md`. Reviewed every Python module
under `services/routing`, `services/jobs`, `services/integrations`,
`services/swarm`, and `services/github`, plus `services/scheduler.py`.
No source or test files were changed.

## Candidates for coordinator validation

Each item below is a source-backed candidate. Line numbers refer to the
2026-09-05 checkout and should be rechecked after edits.

1. **Polling can permanently lose comments.** `services/jobs/github_poll.py`
   lines 157–164 skip a comment when `get_issue_raw` fails, but `had_errors`
   is initialized only at line 170 and therefore does not record this failure.
   If comment C1 at 10:00 cannot hydrate its issue and C2 at 10:01 succeeds,
   lines 190–191 advance `_since` to 10:01:01; C1 is never fetched again.
   Reproduced with mocked GitHub calls: one hydration exception, one successful
   dispatch, and the cursor advanced past the skipped comment.

2. **GitHub REST listings are single-page.** `services/github/interface.py`
   lines 285–313 (`list_issues_since`), 329 (`list_comments_since`), 357
   (`list_issues`), and 366 (`list_comments`) request `per_page=100` but never
   follow pagination. More than 100 records silently disappear. A page filled
   with pull requests can also hide later issues. This affects polling, queue
   selection, API listing, acknowledgement checks, and dependency sync.

3. **Restart/backfill watermark uses receipt time, not source progress.**
   `services/jobs/github_poll.py` lines 111–117 use
   `events.last_time_by_repo()`, while `services/database/_events_repo.py`
   lines 91–105 return `MAX(received_at)`. A day-old GitHub event that failed
   during backfill can be skipped after restart because the next `since` is
   based on when newer events were received, not the oldest unprocessed source
   timestamp. Persisting source cursors or retaining failed event payloads is
   needed for a durable fix.

4. **Concurrent sends to one session are not serialized.**
   `services/routing/_delivery.py` lines 308–399 perform gate, readiness,
   paste, and submit without a per-session lock; `services/runtimes/__init__.py`
   line 115 adds no lock. Two distinct issue events can both observe an empty
   prompt and interleave paste/Enter. A barrier test around
   `get_session_intelligence` reproduced two `DELIVERED` results with two
   simultaneous sends, violating the one-issue-at-a-time invariant.

5. **Ignored/unknown explicit targets fall back to the owner.**
   `services/routing/_targets.py` lines 56–67 build `explicit` only from
   configured, non-ignored targets. For a sole owner and `for:human` where
   `human` is ignored or unknown, `explicit` becomes empty and an opened issue
   is routed to the owner. Explicit targeting should suppress owner fallback;
   routing and `list_open_queue_for_target` should agree on the result.

6. **Configured settling grace is effectively dead.**
   `services/routing/_intelligence.py` lines 119–137 apply the grace period
   only when `idle_since` is passed. Repository call-site search found no
   production caller supplying it; `_delivery.py` only forwards the optional
   value. Sessions can therefore be delivered to immediately after becoming
   idle despite `timing.grace_period_seconds`.

7. **Escalation notifications can be marked sent after storage failure.**
   `services/jobs/escalation.py` around line 390 records a plan notification
   as sent when `outcome_queues(...)` says the outcome is queueable. That helper
   reports intent, not whether `_enqueue` actually stored a row. If the DB
   rejects the queue write, the alert is suppressed for the dedup window and
   lost.

8. **Telegram users with the same first name collide as senders.**
   `services/integrations/telegram/_routing.py` lines 140–151 and
   `_commands.py` lines 181–190 use `_sender_tag`, which returns first name
   (or username) and lowercases it. Two users named Alice sending identical
   text receive the same queue dedup identity. The envelope may retain a
   display name, but queue identity should use the stable Telegram user id.

9. **Manual topic route override is inconsistent for outbound replies.**
   `services/integrations/telegram/_topic_discovery.py` lines 109–117 make
   `agent_topic` scan discovery after config, while `effective_routes` at
   lines 94–101 merges config over discovery. With discovery `{7: A}` and
   config `{7: B}`, inbound messages route to B but A's replies still select
   thread 7. Outbound lookup must use the merged, config-wins mapping.

10. **Telegram topic routes are not scoped to a group.**
    `_topic_discovery.py` lines 140–147 discover a global group id and
    `_routing.py` lines 103–116 use global thread ids. Allow-listed groups can
    reuse thread ids; a message in one group can overwrite discovery or route
    into another group's agent topic. Bind discovery/routes to the originating
    group and reject mismatched groups.

11. **Active-issue comment bypass treats unknown repo as a match.**
    `services/routing/_delivery.py` lines 112–117 return true when either repo
    is empty (`not repo or not current_repo`). If the hook reports only issue
    number 42, a comment for `other/repo#42` can bypass busy/waiting protection
    while the agent works on `own/repo#42`. Require a known, matching repo or
    decline the bypass.

12. **Retry failures can starve newer issue deliveries.**
    `services/jobs/retry.py` lines 123–136 return `acknowledged`, `no_repo`, or
    `issue_closed` without retiring the failed delivery. `delivery_retry` then
    repeatedly selects the oldest retryable rows (limit 20, lines 178–181), so
    closed/acknowledged failures can permanently occupy the retry window.
    Queue-scope failure at line 138 also aborts the whole retry pass before its
    queue drain.

13. **Swarm creation rollback has leak paths.**
    `services/swarm/interface.py` lines 207–208 create the briefs directory
    before the rollback `try`; a mkdir failure leaves the registered worktree
    and active DB row. In the rollback at lines 248–258, failed stops and a
    false `remove_worktree` result are ignored before members are forgotten and
    the swarm is marked disbanded, preventing safe recovery.

14. **Telegram plan commands use raw hook state instead of reconciled state.**
    `_commands.py` lines 280 and 426 (`cmd_viewplan`, `cmd_approve`) call
    `read_state_file` directly. A stale/missing hook file can reject a valid
    terminal-inferred plan or show an obsolete plan, contrary to the shared
    `agent_state` authority rule used by the monitor.

15. **Integration reply status conflates failure with no surface.**
    `services/integrations/telegram/interface.py` `_send` (around lines 75–88)
    returns false for HTTP/API failure, while `_registry.py` lines 65–73 maps
    every false result to `no_surface`. A real Telegram outage is reported as
    no topic, hiding a retryable failure from callers and operators.

16. **Dependency edges remain stale after a successful empty result.**
    `services/routing/_dependencies.py` lines 94–98 call
    `db.dependencies.sync` only when `subs` is truthy. If a previously known
    parent now has zero sub-issues, the successful empty response is skipped
    and old dependency rows remain, causing false future “blocked” checks.
    Fetch errors should be distinguished from a successful empty list.

## Coverage and ranked larger recommendations

Coverage includes all assigned routing, jobs, integrations, swarm, GitHub,
and scheduler Python modules, plus the required architecture documents. The
working tree remained clean. Behavioral checks used mocked/in-memory calls and
reproduced candidates 1, 4, 5, 8, and 9.

1. Share a per-monitor-tick GitHub queue snapshot among dependency sync,
   pending delivery, offline reporting, and lifecycle paths. This removes
   repeated list calls and makes one tick internally consistent.
2. Add a durable source cursor (or persisted raw event payload) for GitHub
   polling so restart and partial-dispatch semantics cannot lose old events.
3. Put a per-session delivery lock around the complete readiness/paste/submit
   transaction; coordinate it with queue claims and retry drains.
4. Give integrations one registration source for both lifecycle instances and
   static human notifications; adding a vendor should require one entry.
5. Make documented priority settings match queue behavior: dependent counts
   are never passed to `compute_priority_score` in `_targets.py` line 125, and
   age is derived from issue number rather than creation time across repos.
6. Clarify whether swarm members count as repository owners. Swarm creation
   registers every member with the same repo, which changes a formerly sole
   owner into a multi-owner repository and changes unlabelled issue routing.

---

# Scout 2 machinery-and-edges audit — 2026-09-05

Scope: read-only review of `services/agents`, `services/runtimes`,
`services/terminal`, `services/database`, `hooks/`, `config.py`, `models.py`,
`api/`, `cli/`. Reconstructed from the scout's messages to the coordinator,
in the order sent.

- Candidate S2-1 (high): terminal/_core.py paste_message lines 94-104 uses unnamed global tmux load-buffer then paste-buffer -d in separate awaits. Concurrent deliveries to DIFFERENT agents can interleave load(A), load(B), paste(A), paste(B), swapping message bodies; per-session delivery locks cannot prevent this. Use unique named buffer per paste and cleanup on failure; regression test controlled interleaving. Candidate S2-2: _core.py session_exists line 62 and terminal commands use raw tmux -t names, which allow prefix/glob targeting. If app is offline but app-2 exists, operations for app can target app-2 (including stop); own-session string equality guard can be bypassed by a prefix. Validate exact session targets (=name) consistently.

- More concrete candidates: S2-3 config.py:219 accepts routing.ignore_targets=[{}], persists it in AgentStore.set_setting before refresh; build_config:664 then raises TypeError constructing frozenset, breaking refresh and subsequent startup. Reject non-string members before DB write. S2-4 hooks/backbone_state.py:46,85-94: issue_from_prompt("Review 2 changes for issue #42", {}) records issue 2 (regex matches any earlier number); unqualified #N also retains previous repo, misattributing cross-repo work. S2-5 agents/_inference.py:169 overrides fresh idle with dialog but copies current_issue without current_repo; loses issue identity specifically while dialog shows. S2-6 database/_queue_repo.py retains every delivered/expired row and full message forever; no DELETE FROM message_queue anywhere, api/app.py:66-67 prune only deliveries/events. Add terminal-status queue retention or rank as recommendation.

- S2-7 invariant defect: runtimes/codex.py interrupt_queued_delivery=True; runtimes/base.py _submit sends Escape when submission reports queued. Sequence: ready gate passes, runtime starts work before paste/submit (or active-issue busy comment delivery), Codex queues message, backbone explicitly interrupts busy work. Contradicts CLAUDE busy-never-interrupted; queued should count success without Escape. S2-8 api/routes/plans.py _require_plan_waiting uses read_state_file directly, bypasses agent_state; stale plan file plus terminal now busy still allows approve/reject keys through plan_control (which checks existence/runtime but no current dialog). Replace state gate with reconciled state and regression stale-hook/busy-pane. S2-9 _file_reader.py: read_state_file catches JSON decode only, then data.get/float outside guard; valid JSON [] or {"ts":"bad"} crashes state consumers instead of terminal fallback. Regression malformed schema fixtures.

- Follow-up S2-4 reproduced with stdlib import: issue_from_prompt("Review 2 changes for issue #42", {}) -> (2,None); envelope-only "[via:github issue:42] New comment: no numeric text" leaves previous issue 17 unchanged (colon syntax unsupported). S2-10 hook action detector backbone_state.py:99+ regex treats echo of quoted "gh issue comment 42 --repo owner/repo" as real ack (reproduced), and selects first --repo anywhere in compound command. OpenCode hook logs at tool.execute.before, so denied/failed commands acknowledge too. Recommend command-aware parsing plus success gating; regression echo/failed command/compound repos. S2-11 terminal/_pty.py orphan cleanup kills saved bare PIDs without checking process identity: after daemon crash + PID reuse, restart SIGTERMs unrelated process. Persist/check birth identity and executable before signaling; no live destructive test needed.

- S2-12 security: cli/setup.py _write_env_value_locked writes all secrets with tmp.write_text before chmod(0600); normal umask 022 creates readable 0644 secret temp until chmod. Create/open temp with mode 0600 before writing, as cmd_init already does. S2-13 cli/setup.py cmd_init and doctor print config.database_url/boot.database_url raw (password-bearing PostgreSQL URLs); use services/database/engine.redact_url, including failure output. S2-14 models.py:10 _FROM_TAG_PATTERN only accepts letter-leading alnum/hyphen names and lowercases; store.sanitize_name allows digits, underscore, dot and uppercase. Registered agent app_test or 123-agent cannot acknowledge via [from:...] (and uppercase canonical names are changed). Align identity vocabulary. S2-15 hook install.py:44 copyfile rewrites shared live hook scripts non-atomically at every agent launch; another running agent can execute an empty/partial script. Atomic replacement avoids lost hook states during concurrent starts.

- Final scout-2 references: S2-7 codex.py:126/base.py:573; S2-8 api/routes/plans.py:49; S2-9 _file_reader.py:82; S2-10 OpenCode before hook :183; S2-11 _pty.py:245-257; S2-12 cli/setup.py:121; S2-13 setup.py:65,216,218; S2-15 hooks/install.py:46. Additional recommendations ranked: (1) serialize discover/register/update lifecycle: store.py:100,145,163 read stale snapshots before awaits; concurrent discover same basename/different dirs both choose same name, last upsert wins; start lock only acquired later operations.py:121, and forget can delete between resolve and start. (2) shared validation for AgentSpec/API/direct CLI: offline agent set tags=123 crashes, runtime nonsense stored; online Pydantic differs. (3) runtime registration metadata single source: config.RUNTIMES + registry + detection order + HOOK_FILES all need edits for new runtime, contrary one-module promise; CLI agents.py also names codex. (4) reuse SessionFeed in status instead of sequential duplicate state queries per agent. No code changed; candidates need your source validation before implementation.

- Assignment complete: read #133, CLAUDE.md, docs/concepts.md and docs/how-it-works.md first. Prior detailed S2-1..15 reports were stored in your queue while offline/busy. Highest priority recap: S2-1 terminal/_core.py:94-104 global unnamed load-buffer/paste-buffer race across different agents; load A/load B/paste A swaps bodies. S2-2 _core.py:62 and terminal targets raw -t names prefix-match app-2 when app offline, including kill-session; use exact targets. S2-7 runtimes/codex.py:126/base.py:573 explicitly Escape-interrupts when a delivery queues, violating never interrupt busy. S2-8 api/routes/plans.py:49 gates approve/reject on raw stale hook, so stale plan + currently busy pane sends keys. S2-12 cli/setup.py:121 writes secret temp under umask before chmod0600. S2-13 setup.py:65,216,218 prints PostgreSQL URL passwords. No edits or commits. Sending remaining candidates/coverage next.

- Remaining recap: S2-3 config.py:219,664 accepts/persists routing.ignore_targets=[{}], then frozenset crashes refresh/startup. S2-4 hooks/backbone_state.py:46,85: reproduced prompt "Review 2 changes for issue #42" -> issue 2; envelope issue:42 alone not parsed; unqualified refs can retain old repo. S2-5 agents/_inference.py:169 dialog override copies issue but drops repo. S2-6 queue_repo retains all delivered/expired full messages forever; api/app.py:66-67 prunes only events/deliveries. S2-9 agents/_file_reader.py:82 valid JSON [] or ts="bad" throws instead of terminal fallback. S2-10 backbone_state.py:99 regex falsely acknowledges echo of quoted gh issue comment; OpenCode hook:183 logs before execution/permission success. S2-11 terminal/_pty.py:257 blindly SIGTERMs saved PIDs after crash/reuse. S2-14 models.py:10 rejects [from:app_test]/[from:123-agent] though valid registered names. S2-15 hooks/install.py:46 truncates shared live hook scripts while copying at each launch. Details/repro sequences already queued in previous individual reports.

- Coverage complete across assigned machinery/edges: agents store/lifecycle/launch/state/files/acks/audit; all seven runtimes plus shared registry/pane/base; terminal core/sessions/copy-mode/PTY; database repositories/models/engine/schema repair and migration environment; Python/JS hooks/install; config/models; API auth/lifespan/feed/Socket.IO and functional routes; CLI parsing/agents/config/server/secrets/service/upgrade/swarm. Source inspection plus stdlib hook repros; no live destructive tests, no tests against real DB, no implementation. Ranked larger recommendations: 1 serialize discover-register-start-forget and partial updates (store.py:100,145,163 + operations.py:121) to avoid concurrent same-basename overwrite/lost updates; 2 shared AgentSpec validation across API/direct CLI (offline tags=123 crashes while API rejects); 3 single runtime metadata source (config ids, registry/detection, hook list and CLI choices duplicate additions); 4 reuse SessionFeed in status instead of sequential duplicate per-agent tmux reads. Queue retention S2-6 can be a bounded fix or recommendation. idle — task done; no edits or commits; awaiting validation or exact follow-up assignment.

---

# Coordinator validations (before the run ended)

- **S1-5** confirmed in source: `route_issue` filters explicit targets before deciding owner fallback (`_targets.py:64-77`).
- **S1-1** confirmed in source: `GitHubPoller.run` drops a failed `get_issue_raw` before `had_errors` exists (`github_poll.py:162-179`).
- Baseline on `swarm/audit`: `make check` green, 1106 tests.

---

# Disposition

Every candidate was checked against `develop` at `4df78b0` by the initiator
(agent-backbone). **Fixed** means implemented in this change with a regression
test; **recommendation** means real but larger than this change or a design
call for the owner; **rejected** means not reproducible in the source.

## Scout 1

| # | Disposition | Note |
|---|---|---|
| 1 | **Fixed** | A comment whose issue cannot be hydrated now sets `had_errors`; the cursor stays put and the comment is refetched. |
| 2 | **Fixed** | `list_issues_since`, `list_comments_since`, `list_comments` follow `Link: rel="next"` to the end (`_request_all`). `list_issues` keeps its explicit `per_page`. |
| 3 | Recommendation | Durable source cursor for polling; needs a schema decision (persisted cursor or raw payloads). |
| 4 | Recommendation | Per-session delivery lock around gate/paste/submit; interacts with queue claims and the retry drain — design first. |
| 5 | **Fixed** | An issue with any `for:` label is *addressed*; the sole owner no longer receives it when every target is a person or unknown. |
| 6 | Recommendation | `idle_since` is only ever passed as `None`; either wire the idle timestamp from the hook state into `safe_deliver` or drop `timing.grace_period_seconds`. Owner's call — the setting is documented. |
| 7 | Recommendation | `outcome_queues` reports intent; the escalation job should use `deliver()`'s `DeliveryReport.queue`. Small, but touches the escalation dedup semantics. |
| 8 | Recommendation | Telegram sender identity should be the user id, not the lowercased first name; changes the envelope people see. |
| 9 | Recommendation | Outbound topic lookup must use the merged config-wins mapping. |
| 10 | Recommendation | Bind topic discovery to the originating group. 8–10 are one Telegram identity/topic change. |
| 11 | **Fixed** | The active-issue bypass requires a known, matching repository on both sides. |
| 12 | Recommendation | Retire `acknowledged` / `issue_closed` / `no_repo` rows so they stop occupying the retry window; needs a `deliveries` repository method. |
| 13 | **Fixed** | Briefs directory is created inside the rollback `try`; failed stops and a surviving worktree are logged instead of ignored. |
| 14 | **Fixed** | Telegram `viewplan` / `approve` decide on `agent_state`, the reconciled state. |
| 15 | **Fixed** | A Telegram send failure raises, so the registry reports `failed`, not `no_surface`. |
| 16 | **Fixed** | `get_sub_issues` returns `None` on a failed fetch; an empty list now syncs and clears stale edges. |

Recommendations 1–6 (shared queue snapshot, durable cursor, delivery lock,
single integration registration, priority inputs, swarm members as owners)
are recorded; the last is tracked as #137.

## Scout 2

| # | Disposition | Note |
|---|---|---|
| S2-1 | **Fixed** | Every paste uses its own named tmux buffer, deleted on failure. |
| S2-2 | **Fixed** | Every tmux target is `=name` — exact, never a prefix. |
| S2-3 | **Fixed** | A list setting whose default holds strings rejects non-string members before the write. |
| S2-4 | **Fixed** | An issue number is taken only after `#` or the word `issue` (the `issue:N` envelope form included); the first bare number in a prompt is not an issue. Repo retention on an unqualified `#N` is unchanged and intended. |
| S2-5 | **Fixed** | The dialog override carries `current_repo` with `current_issue`. |
| S2-6 | Recommendation | Queue retention for delivered/expired rows; a prune alongside the existing ones. |
| S2-7 | Owner's call | `interrupt_queued_delivery` on Codex is deliberate (a queued paste is submitted only after Codex's next tool call); whether the invariant or the runtime rule wins is a design decision, not a defect fix. |
| S2-8 | **Fixed** | Plan routes (`_require_plan_waiting`, `get_plan_detail`) decide on `agent_state`. |
| S2-9 | **Fixed** | Valid JSON of the wrong shape (`[]`, `ts="bad"`) degrades to the terminal like unreadable JSON. |
| S2-10 | Recommendation | Command-aware acknowledgement detection (quoted `gh issue comment`, first `--repo` in a compound command, OpenCode's before-hook). |
| S2-11 | Recommendation | PTY orphan cleanup should check process identity before signalling. |
| S2-12 | **Fixed** | The secrets temp file is created `0600`. |
| S2-13 | **Fixed** | `init` and `doctor` print the database URL through `redact_url`. |
| S2-14 | **Fixed** | `[from:…]` accepts the registration vocabulary (digits first, `_`, `.`); names are still lowercased on parse — case-sensitive names remain a recommendation. |
| S2-15 | **Fixed** | Hook scripts are replaced atomically at every launch. |

Recommendations 1–4 (serialised agent lifecycle, shared `AgentSpec`
validation, single runtime metadata source, `SessionFeed` reuse in
`status`) are recorded.

## Found by the initiator while running the swarm

- Codex's rate-limit model-switch dialog reported as a permission prompt; a
  Telegram Allow switched a scout's model — #136.
- Swarm members registered as repository owners receive every repository
  event — #137.
- The Telegram permission prompt does not show the command — #135.
- Review deliveries carry no commit or lifecycle — #132.
