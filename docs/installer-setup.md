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
submitting that task. The brief alone is not proof of a greeting; a delivery receipt
means submitted or queued, never that the model answered. Readiness checks make no
model calls. Permission/login prompts remain visible; retry the same request after
handling them in the terminal. Ambiguous startup is not reported as authentication
failure. A custom brief applies to this first launch; ordinary later starts use
normal agent instruction selection.

## Results and recovery

Requests are limited to 32,768 UTF-8 bytes. JSONL events carry `schema_version: 1`,
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
`request_conflict` and `filesystem_unsupported`. `retry` is `same_request`, `new_request` or `none`.

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

The spreadsheet subprocess uses isolated Python imports and disables bytecode
writes, so native calculations cannot add cache files to a verified engine payload.
