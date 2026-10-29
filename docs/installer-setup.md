# Installer setup

`backbone setup` is an optional operator interface for an installer or desktop
application. It takes transient JSON input, never a required project file. It
runs one finite action and exits; it creates no scheduled workflow. Use
`backbone runtimes --json` for read-only adapter discovery before installation.
`items[].setup_supported` declares whether the adapter supports first-agent setup;
consumers treat a missing field from an older provider as false. Binary availability
does not imply installer support. Authentication in discovery remains unknown; sign in using the runtime's visible
terminal flow. Do not infer authentication from an installed executable.

```sh
backbone setup --schema
backbone setup --file request.json --validate --jsonl
backbone setup --file request.json --jsonl
backbone setup --file - --jsonl
```

Schema and validation are available before credentials or storage. Validation
checks input shape only. Actual execution requires an operator process; inherited
managed-agent identity is refused before reading an operator secret file.
Credentials are never input fields or output events. The app connects with the
existing installation's operator credential from its local secret file, ignoring
an unrelated operator key inherited from the invoking process. Setup issues no new app credential. Environment-only installations keep their
ordinary connection flow; this installer interface requires the local operator
file. An external database URL must also be declared literally in that installation's
`.env`, matching the effective URL; an inherited database URL alone does not prove
that the selected directory is an installation. For `service: "none"`, start a later daemon without an unrelated
`BACKBONE_API_KEY` environment override so it uses that same file.

### Optional setup compatibility checks

Default runtime discovery does not execute installed programs. To inspect the
selected executable before Start, use `backbone runtimes --json --check-setup`
or operator-only `GET /api/runtimes?check_setup=true`. Each runtime gains a
`setup_compatibility` object containing `compatible` (boolean), `reason`,
`message`, `path` (nullable), `version` (nullable), and `version_error` (nullable).
The API observes the running service's executable selection; the CLI observes
its own environment. A shell result does not prove which binary the service uses.

Reasons are `compatible`, `unsupported`, `runtime_missing`,
`version_unavailable`, and `version_unsupported`. Version errors are `missing`,
`timeout`, `unavailable`, `nonzero_exit`, or `invalid_output`. Supported adapters
probe the selected executable with `--version`, with a ten-second timeout per
probe; API probes run off the event loop. Raw output is never returned. Only
OpenCode has a setup minimum-version requirement; a failed version observation
for another supported adapter does not introduce a new setup restriction.
Compatibility does not establish authentication, model availability or launch
success. Missing compatibility metadata from an older provider is unknown.

OpenCode's reported path and version refer to the same selected executable.
An old executable on PATH continues to take precedence over the standalone
installer location. The result explains this rather than silently replacing a
user-selected binary. An unreadable or failed version probe is distinguished
from a known version below 2.1.4. Fix the selected executable or deliberately
choose another tool; cancel its saved setup request before submitting new input.

## Initialize

All fields are required, unknown fields are rejected, and paths must be absolute:

```json
{
  "schema_version": 1,
  "request_id": "install-01",
  "action": "initialize",
  "data_dir": "/absolute/path/to/data",
  "service": "user",
  "starter_profile": "core-v1"
}
```

A fresh installation generates a new operator key in its private `.env` (never
reusing an inherited key), creates local SQLite data, copies the
editable core assets and assigns their policies and skills. `service: "user"`
installs a macOS LaunchAgent or Linux systemd user service and observes its health;
`"none"` initializes without starting a service. There is no background-mode
fallback for an unsupported service manager. An occupied service address or
unrelated service definition is a conflict, not permission to replace it.

After the service manager accepts startup, initialization allows a 30-second
readiness observation window for cold starts. It confirms the selected data
directory through the authenticated identity endpoint before accepting service
health. A running service process alone is not readiness. The finite result keeps
the same schema, codes and retry fields; `service_unavailable` explains whether
the service command, identity authentication, installation identity or health
check failed and points to status/log commands. Raw manager output, credentials
and endpoint error bodies are not copied into the result.

If startup is still incomplete, retain the exact request and installed provider
version. A retry waits for readiness again without restarting a service step
already recorded as completed, rewriting credentials or replacing edited assets.
Do not reset receipts to recover a slow startup. On Linux, inspect
`systemctl --user status agent-backbone.service` and
`journalctl --user -u agent-backbone.service -n 80 --no-pager`; on macOS, inspect
`backbone service status` and the installation's `backbone.log` and
`backbone.out.log`. Keep credentials and unrelated log content private when
sharing a diagnostic excerpt.

An existing installation returns `existing_preserved` with observed connection
and service state. It does not migrate the database, change secrets, replace the
service, assign policies or launch agents. Only a private setup operation receipt
is added. The API URL is returned only when the operator-authenticated identity
endpoint confirms the selected data directory.

The core consists of the editable `starter-collaboration`, `starter-continuity`,
`starter-learning`, `starter-artifacts` and `starter-authority` policies; the
`backbone-documents`, `backbone-reports`, `backbone-responses`, `backbone-sheets`
and `backbone-conversations` skills; and an `orchestrator-v1` brief. Policies live
under `templates/policies`, the brief under `templates/onboarding`, and skills
under `skills` in the installation's data directory. Existing instruction and
skill readers expose the policies and skills. Users may edit these files.
Completed steps are not reapplied while retrying later steps; edits made after
a completed step are preserved.

## First agent

This action is a separate explicit choice. Connecting to an existing installation
must never submit it automatically.

```json
{
  "schema_version": 1,
  "request_id": "first-agent-01",
  "action": "first_agent",
  "data_dir": "/absolute/path/to/data",
  "hooks": ["codex"],
  "agent": {
    "name": "guide",
    "directory": "/absolute/path/to/project",
    "create_directory": true,
    "runtime": "codex",
    "model": null,
    "brief": {"template": "orchestrator-v1"}
  },
  "start_greeting": true
}
```

Use exactly one brief shape: `{"template":"orchestrator-v1"}` or
`{"text":"Your custom instructions"}` (up to 16,000 characters). The model field
is required and nullable. Hooks must contain unique canonical runtime IDs and
include the selected runtime. Claude Code, Codex and OpenCode support this action;
OpenCode must be version 2.1.4 or newer before registration. Other adapters report an unavailable installer path with their normal agent-start
flow as fallback. See the generated [capability contract](runtime-capabilities.md).

Runtime lookup is repeated by the service when first-agent setup runs. An
OpenCode standalone install in `~/.opencode/bin` is discoverable even if it was
installed after the service started. Existing PATH and standard package-manager
locations take precedence; the service does not silently switch away from a
selected older executable. Use the [OpenCode V2 installer](https://opencode.ai/v2/docs/)
(`https://opencode.ai/v2/install` or `npm install -g @opencode/cli`), then verify
the selected `opencode --version` is at least 2.1.4. The V1 installer and V2 share
the command name; discovery of a binary is not proof of version compatibility
or authentication.

The running service executes first-agent setup through operator-only
`POST /api/setup/first-agent`. It verifies core prerequisites, prepares
installation-owned hooks, registers a new agent and starts it with the selected
brief. It preserves global runtime settings and workspace trust prompts; it never
sets unattended mode. Directory creation creates only the chosen directory and
missing parents, without initializing a Git repository or mandatory instructions.
An existing name, case variant, registered directory or unrelated terminal is a
conflict. There is no automatic suffix or takeover.

`start_greeting: true` means the person explicitly requested a greeting on Finish
or Start. After measured readiness, setup sends a bounded greeting task through
the ordinary safe-delivery path. This may incur model usage. False starts without
submitting that task. Setup rechecks the exact registration and launch under the
shared cross-process guard and holds it through submission or queueing. A queued
greeting is a durable direct message visible in `backbone inbox`; ordinary
notification expiry does not discard it. The brief alone is not proof of a greeting;
a delivery receipt means submitted or queued, never that the model answered. Readiness checks make no
model calls. Permission/login prompts remain visible; retry the same request after
handling them in the terminal. Ambiguous startup is not reported as authentication
failure. A custom brief applies to this first launch; ordinary later starts use
normal agent instruction selection.

## Change the first agent before setup makes changes

A user can cancel a saved first-agent request before it makes changes, for
example to switch tools after a runtime preflight refusal. This additive schema-1
action requires provider support in both the installed CLI and running service.
Older providers reject it; rejection is never confirmation of cancellation.

Copy the **entire saved first-agent request**, retaining its request ID and all
other fields, and change only `"action"` to `"cancel_first_agent"`. Submit it with
`backbone setup --file cancel.json --jsonl`, or as an operator to
`POST /api/setup/first-agent/cancel`. The consumer must persist this cancellation
intent before sending it. After an app restart or lost response, retry this
cancellation input, not the original first-agent action.

Cancellation uses the same nonblocking installation lock as first-agent setup.
It checks the original request fingerprint and refuses if any step completed or
any registration, launch, greeting or unfamiliar effect marker exists. It never
probes a runtime, launches, stops, forgets or changes an agent. A missing receipt
is recorded as cancelled too, preventing a delayed original submission from
starting after cancellation wins the lock.

The only confirmation permitting a replacement request is a matching final
result with `action: "cancel_first_agent"`, `status: "completed"`,
`code: "request_cancelled"`, `retry: "new_request"`, `completed_steps: []`, and
`data: {"cancelled_request_id": "<original ID>", "can_start_new_request": true}`.
The CLI exits 0. Save that confirmation and retire the old consumer attempt before
creating the user's replacement input under a **new request ID**. Keep the old
provider receipt. Its permanent marker also makes older provider implementations
reject the original request's fingerprint; it is not a flag they can ignore.

Cancellation is idempotent across process restarts. A concurrent setup holds the
lock and cancellation returns `outcome_unknown`, `action_required`,
`retry: "same_request"`: retry the **cancellation** after the operation finishes.
If work already progressed, the result is `cancellation_unsafe`,
`action_required`, `retry: "none"`; preserve and inspect the original agent or
request. A changed input under the original ID returns `request_conflict`, which
does not confirm cancellation. None of these results unlocks replacement.

A later original first-agent replay returns `request_cancelled`, `failed`,
`retry: "new_request"` without further effects. This is not a rollback: the
operation deliberately refuses once any step completed, even if registration
has not yet occurred. An ordinary runtime permission prompt after launch must be
handled in its terminal. Do not infer cancellation safety from the previous
failure code, empty output steps, a missing terminal, or an interrupted response.
There is no read-only setup status endpoint; replaying first-agent setup is an
execution request, not a status query.

## Results and recovery

Initialize and first-agent requests are limited to 32,768 UTF-8 bytes.
Cancellation allows 32,775 bytes so even a maximum-size original input can
change only its action to the longer cancellation action. JSONL events carry `schema_version: 1`,
`request_id`, a strictly increasing `seq` within that invocation, and `type`
(`step` or `result`). Each invocation emits exactly one final result. Step events
have `step` and `status`. The result has `action`, `status`, `code`, `message`,
`data`, `completed_steps` and `retry`. Exit codes are 0 completed, 1 failed,
2 invalid request and 3 action required. Save the request ID and full input before
invoking the command; interrupted output is not permission to generate a new ID.

Initialize data contains `data_dir`, `installation` (`created` or
`existing_preserved`), `service_state` (`running`, `installed`, `not_installed`,
`unsupported`, `unknown`), `api_url` (nullable) and `health` (`healthy`, `unhealthy`,
`unknown`). First-agent data contains `agent_name`, `launch_id` (nullable), `ready`
(`ready`, `waiting_for_human`, `exited`, `timeout`, `not_started`),
`greeting_delivery_id` (nullable integer) and bounded classification `evidence`.
A preflight failure may have empty `data`. A completed first-agent replay reconciles its registration and launch identities
without requiring its old policy, brief, model or runtime availability to remain
unchanged. A stopped agent is reported as exited with `retry: "none"`; use ordinary
agent start after inspection. Replay never restarts it.

Result codes include `initialized`, `existing_preserved`, `agent_ready`,
`validated`, `invalid_request`, `operator_required`, `installation_conflict`,
`starter_conflict`, `agent_conflict`, `runtime_missing`, `runtime_permission`,
`service_unavailable`, `launch_failed`, `launch_timeout`, `outcome_unknown`,
`request_conflict`, `filesystem_unsupported`, `request_cancelled` and `cancellation_unsafe`. `retry` is `same_request`, `new_request` or `none`.

Private receipts under `setup-receipts` bind each request ID to its full input
fingerprint. A changed request with the same ID conflicts. One nonblocking lock
serializes setup against setup; concurrent invocation returns an action
requirement. Atomic receipts record ownership before effects; complete files are published
without overwriting existing paths. The data directory filesystem must support
hard links; setup reports `filesystem_unsupported` rather than using a partial or
overwriting fallback. Abandoned temporary receipt files are removed under the
exclusive setup lock; accepted request identities are retained. Receipts retain actual
registration, launch-operation, launch and greeting-delivery identities. A partial
setup resumes only its own verified files and local database, even if the invoking
environment changes between retries. If registration or a send was interrupted
without enough evidence, setup reports uncertainty and does not blindly repeat it.

Accepted IDs are retained indefinitely, with a limit of 1,024 receipts and 65,536
bytes per receipt. Capacity refuses new IDs while existing IDs remain usable; no
background cleanup silently makes an old operation new again. Receipts contain
fingerprints and classifications, never credentials. The selected brief is atomically published in a separate private file.
An interrupted registration with insufficient ownership evidence returns
`retry: "none"`; inspect it before using ordinary agent commands. Do not delete receipt identities to retry an uncertain
launch or greeting. Inspect the actual registration/session/delivery first.

## Portable service binding

A fresh service binds to the launcher belonging to its executing installation,
never a different `backbone` found on PATH. A portable payload has a private
`python/` prefix and an `engine-manifest.json` with `schema_version: 1` and relative
`entrypoint: "bin/backbone"`. The launcher must resolve inside that payload.
Service definitions retain absolute PATH entries, omit relative/empty entries,
and put the pinned engine launcher directory first. The invoking process's
runtime-tool PATH follows. A bundled `libexec/tmux` is appended as a fallback
after existing tools; there is no tmux alias beside `bin/backbone`.
Launcher and PATH validation happen before data or credential creation.
An app's discovery PATH does not change an already-running daemon's environment.

The installer owns immutable payload versions and activation; this command does
not write `active.json`. Verify the payload at its final path before initialization,
then atomically activate only after successful fresh initialization. Preserve the
previous payload/activation for rollback and keep existing installations' CLI,
service and configuration. Removing the app must not delete engine or user data.

Backbone-owned Python children disable bytecode writes explicitly, including
terminal environment loading, runtime launch helpers, hooks, spreadsheet workers
and daemon relaunches. Python flags do not propagate to child interpreters, and
isolated Python ignores `PYTHONDONTWRITEBYTECODE`; each invocation passes `-B`.
Daemon relaunches and version probes also preserve the parent's isolated-import
mode. These paths do not add cache files to a verified engine payload.
Saved global/project hook commands and Chrome host wrappers from an older build
keep their original interpreter arguments. After replacing such a payload,
reinstall those commands using the corrected launcher: run
`backbone hooks install <runtime>` for each configured runtime (`claude`, `codex`
or `gemini`), adding `--dir <project>` for each project with saved hooks. If the
Chrome host is configured, run `backbone chrome install` too. Launch-specific
hooks are regenerated on the next agent start.

## Building the bundled engine

The source repository includes an [offline portable engine builder](../tools/engine/README.md).
It builds an exact source commit with pinned interpreter and wheel inputs and
produces the immutable manifest/inventory consumed by the combined installer.
The current lock is for native Apple Silicon; build success does not establish
installer, first-agent, minimum-OS or other-platform acceptance.
