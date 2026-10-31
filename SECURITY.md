# Security policy

## Reporting a vulnerability

Please report security problems privately, not in a public issue or pull
request. Use GitHub's private reporting form:
[Report a vulnerability](https://github.com/eandualem/agent-backbone/security/advisories/new).

A useful report includes:

- the Backbone version (`backbone --version`) and operating system;
- the agent CLIs involved, if any;
- what an attacker needs first (another OS account, a GitHub comment, a
  Telegram message, the API key, a process running as your user);
- the steps to reproduce and what they lead to.

You will get a reply in the same advisory thread. Fixes are released on PyPI
and described in the advisory once users can upgrade.

## Supported versions

Only the latest release on [PyPI](https://pypi.org/project/agent-backbone/)
receives security fixes. Upgrade with `backbone upgrade` or
`uv tool upgrade agent-backbone`.

## What counts as a vulnerability

Backbone runs as one trusted OS user on one machine. Read the
[security model](docs/security.md) first: it states what Backbone protects and
what it does not. A way around a protection it claims is a vulnerability, for
example reaching the API without a valid key, an agent credential doing what
only the operator key may do, a secret reaching an agent session, or a sender
forging the owner-confirmed marker. Behaviour the model lists as unprotected,
such as a process running as the same user reading Backbone's files, is a known
limitation. Reports that make one of those limits worse are still welcome.

## Verifying a download

Releases after 0.4.4 are built by this repository's release workflow and carry
a signed build provenance record. Earlier releases have none. See
[Verify a download](docs/security.md#verify-a-download).
