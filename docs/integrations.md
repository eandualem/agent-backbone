# Integrations

An **integration** is a channel where people meet their agents outside the
terminal. Telegram is the one that ships today; the contract is written so
that Slack, Discord, e-mail or a web inbox plug in the same way, and nothing
else in the backbone learns a vendor name.

## What every integration does

| Capability | Meaning | Telegram |
|---|---|---|
| **Inbound** | Text from a person becomes an ordinary delivery through `safe_deliver`, with a `[via:<integration> from:<who>]` envelope. An integration never pastes into a terminal itself | a message in an agent's topic, or `/tell` |
| **Surface per agent** | A place that *is* one agent, so talking there is talking to it | a forum topic mapped to the agent |
| **Reply** | An agent answers into its surface: `backbone reply "…"` / `POST /api/integrations/reply` | posted into the agent's topic |
| **Notify** | Alerts to the humans (plan waiting, session died, copy mode stuck) — into the agent's surface when it has one, else a general destination | the topic, else `telegram.notification_chat_id` |
| **Sync** | Re-provision surfaces whenever the set of registered agents changes | topic routes (see [Telegram](telegram.md)) |
| **Lifecycle** | Started with `backbone up`, reads the live configuration, reports in `GET /api/status/services` under `integrations` | long-polling bot |

Every integration is inert until its credentials exist (`TELEGRAM_TOKEN` in
`.env`); an unconfigured one shows as `disabled` and does nothing.

## How the pieces connect

```
person ──▶ integration ──▶ safe_deliver ──▶ agent terminal
agent  ──▶ backbone reply ──▶ POST /api/integrations/reply ──▶ every enabled integration
jobs   ──▶ notify_humans(config, text, agent=…) ──▶ every configured integration
```

`notify_humans` is config-driven (no running instance needed) so the
scheduler's monitor jobs can alert from a configuration snapshot. It is the
only thing `agents` and `terminal` know about integrations, and they import
it lazily — `integrations` sits *above* `routing` in the layering.

## Adding one

1. Create `services/integrations/<name>/` with a class deriving from
   `services/integrations/base.py:Integration`. Set `name`, implement
   `enabled` (credentials present?), `start`/`stop`, and whichever of
   `reply_to_agent`, `notify`, `sync_agents` the channel supports.
2. Route inbound text through `safe_deliver(...)` with a
   `[via:<name> from:<who>]` envelope — never through tmux directly.
3. Add it to `build_integrations()` in `_registry.py` and, for alerts from
   jobs, a static sender to `_STATIC_NOTIFIERS` in `_notify.py`.
4. Settings go in `SETTINGS_DEFAULTS` under `<name>.*`; secrets in `.env`.
5. Add the entry module to `tests/unit/test_imports.py` and document it in
   `docs/<name>.md`.
