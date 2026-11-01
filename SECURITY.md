# Security policy

## Reporting a vulnerability

Please report security problems privately, not in a public issue or pull
request. Use GitHub's private reporting form:
[Report a vulnerability](https://github.com/eandualem/agent-backbone/security/advisories/new).

A useful report includes:

- the Backbone version (`backbone --version`), operating system and supported
  agent CLI versions involved;
- relevant configuration, including the runtime's permission and sandbox mode;
- the access and actions needed first, such as a scoped agent credential,
  operator access, a repository comment or a concurrent registration change;
- a minimal reproduction, expected and observed behavior, and the concrete
  impact on a Backbone capability.

Use synthetic values for credentials in reproductions. Do not attach your
`.env`, database or full logs; include only the relevant excerpt after checking
it for credentials and private content. There is no diagnostic-upload service.

You will get a reply in the same advisory thread. Fixes are released on PyPI
and described in the advisory once users can upgrade.

## Supported versions

Only the latest release on [PyPI](https://pypi.org/project/agent-backbone/)
receives security fixes. Upgrade with `backbone upgrade` or
`uv tool upgrade agent-backbone`.

## Reporting scope

Backbone coordinates user-owned agents on one machine. Their CLI harnesses
enforce execution permissions and sandboxing; Backbone is responsible for the
identity, access and delivery rules of the capabilities it adds. See the
[security model](docs/security.md) for those boundaries and known limitations.

Examples include accepting an unauthenticated API request, granting an agent
operator-only access, attributing a conversation to the wrong registration,
forwarding a service credential into a session, or accepting a forged owner
confirmation. State the required access and timing without assuming they make
the defect common or harmless. Rare identity and concurrency defects still
matter when they violate a product guarantee.

An unrestricted process running as the same OS user can access local files;
Backbone does not add an OS isolation boundary. Reports are still welcome when
a supported configuration exposes more access or data than documented, defeats
a runtime restriction through a Backbone capability, or makes a known limit worse.

## Verifying a download

Releases after 0.4.4 are built by this repository's release workflow and carry
a signed build provenance record. Earlier releases have none. See
[Verify a download](docs/security.md#verify-a-download).
