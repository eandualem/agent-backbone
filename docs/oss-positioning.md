# Open-source positioning

Research for [issue #83](https://github.com/eandualem/agent-backbone/issues/83):
where agent-backbone sits in the landscape of tools for running and
coordinating terminal coding agents, how it actually differs, what a
skeptical senior engineer would flag, and what it would take to make this a
plug-and-play project strangers adopt. Star/activity counts were pulled via
`gh api` on 2026-09-01 and are cited per project; treat them as a snapshot.

## 1. Landscape

Two families cover nearly everything found. Within each, two coordination
shapes recur: **isolated parallel sessions with no cross-talk** (claude-squad,
uzi) versus **explicit supervisor/message-passing** (Tmux-Orchestrator, CAO,
amux, agent-backbone). agent-backbone sits in the second shape, with a
database — not files or a live server process — as the source of truth.

**A. Tmux/terminal-session managers** — run multiple coding-CLI processes in
tmux; they differ mainly in whether sessions can talk to each other.

- **claude-squad** ([smtg-ai/claude-squad](https://github.com/smtg-ai/claude-squad),
  8,405★, active, AGPL-3.0) — one tmux session + one git worktree per agent
  (Claude Code, Codex, OpenCode, Amp, Aider); zero inter-agent messaging, a
  human drives everything from a TUI. The closest architectural analog to
  agent-backbone's own session mechanism.
- **cli-agent-orchestrator / CAO** ([awslabs/cli-agent-orchestrator](https://github.com/awslabs/cli-agent-orchestrator),
  1,167★, active, AWS Labs, Apache-2.0) — a supervisor agent delegates to
  tmux-isolated worker CLIs (12 supported providers) via a local
  `cao-server` (HTTP API + PTY WebSocket). No documented busy/idle check
  before delivery.
- **amux** ([mixpeek/amux](https://github.com/mixpeek/amux), 378★, young,
  very active, MIT+Commons Clause) — the closest architectural peer overall:
  a kanban Board, durable Workers, cron-like Schedulers, and Messages
  "delivered at turn boundaries," SQLite-backed. Not pure OSS (Commons
  Clause blocks commercial resale).
- **Tmux-Orchestrator** ([Jedward23/Tmux-Orchestrator](https://github.com/Jedward23/Tmux-Orchestrator),
  1,810★, stale since 2025-07-14) — the original Orchestrator→PM→Engineer
  hierarchy; its paste-reliability script is direct prior art for
  `safe_deliver`.
- One line each: **Claude Code Agent Farm** ([Dicklesworthstone](https://github.com/Dicklesworthstone/claude_code_agent_farm),
  914★) coordinates via shared lock files, no live messaging at all; **Uzi**
  ([devflowinc/uzi](https://github.com/devflowinc/uzi), 582★, stale) offers
  only a one-way `broadcast` to all sessions.

**B. GitHub-issue-driven agent runners** — trigger on issues/PRs, execute in
ephemeral CI.

- **claude-code-action** ([anthropics/claude-code-action](https://github.com/anthropics/claude-code-action),
  8,768★, active, Anthropic-official) — `@claude` mentions or issue
  assignment trigger a stateless, one-shot run on the user's own Actions
  runner.
- **GitHub Agentic Workflows / gh-aw** ([github/gh-aw](https://github.com/github/gh-aw),
  5,071★, very active, GitHub-official) — compiles Markdown workflow files
  into vendor-agnostic Actions runs (Copilot, Claude Code, Codex, Gemini);
  the biggest commoditization risk to this whole category given first-party
  GitHub backing and a materially more mature security architecture (see
  §3).
- **OpenHands resolver** (folded into [OpenHands/OpenHands](https://github.com/OpenHands/OpenHands),
  85,765★) — a label (`fix-me`) triggers the full OpenHands agent in a
  sandbox, opens a PR "within minutes"; the standalone predecessor repo is
  archived.
- One line each: **claude-hub** ([claude-did-this/claude-hub](https://github.com/claude-did-this/claude-hub),
  487★, stale ~10mo) is a small self-hosted webhook bridge; **Sweep**
  ([sweepai/sweep](https://github.com/sweepai/sweep), 7,707★, stale ~1yr)
  was the original "issue→PR bot" poster child and pivoted to a JetBrains
  product in 2026 — useful evidence that a standalone issue-to-PR bot has
  struggled commercially as its own category.

**C. Adjacent frameworks** (substrate, not turnkey coding-CLI managers, one
line each): **OpenHands** is itself a full autonomous agent, not a manager
of other agents' CLIs; **SWE-agent / mini-swe-agent**
([SWE-agent/SWE-agent](https://github.com/SWE-agent/SWE-agent), 20,177★) is
the canonical single-shot issue→PR agent, one run per issue, no session
layer; **Claude Agent SDK** ([anthropics/claude-agent-sdk-python](https://github.com/anthropics/claude-agent-sdk-python),
8,010★) is the harness other tools embed — agent-backbone deliberately
drives CLIs over tmux/PTY instead, which is the source of its
runtime-agnosticism; **CrewAI** (57,888★) and **LangGraph** (40,798★) are
domain-general multi-agent frameworks people build coding orchestration on
top of, not coding-agent managers out of the box.

**claude-flow / Ruflo** ([ruvnet/claude-flow](https://github.com/ruvnet/claude-flow),
renamed "Ruflo," 69,997★) deserves a caution rather than a feature
comparison: it's an MCP-server swarm tool (shared vector memory, ~210 MCP
tool calls), not a tmux/terminal-session manager — a different mechanism
entirely. Cite the star count only with the caveat that several linked
plugin docs are skeletal and 881 open issues suggest maturity lags the
marketing.

## 2. Differentiation

Per this repo's own docs, agent-backbone is "a local control plane for
terminal AI agents": it runs coding CLIs in tmux, decides when each is
provably safe to paste text into, and routes work to them from GitHub
Issues, Telegram, or its own API — while explicitly *not* calling models
itself (not a framework), not running DAGs (not a workflow engine), and
shipping no UI (`README.md`, `docs/concepts.md`).

| Axis | agent-backbone | amux | CAO | claude-squad | claude-code-action / gh-aw |
|---|---|---|---|---|---|
| Delivery safety | `safe_deliver` + `get_agent_state`: fresh hook state authoritative, terminal fallback, every snapshot carries `evidence`; busy agents never interrupted. Documented mechanism. | Claims delivery "at turn boundaries" via an `AgentProtocol`; detection mechanism undocumented, README admits terminal-scraping is today's fallback. | Not documented at all. | Not applicable — human attaches directly, no background paste. | Stateless per-run; no mid-session delivery concept. |
| GitHub issue intake | First-class: `(repo, issue_number)`-scoped, provenance-enveloped, into a persistent session. | None found. | None found. | None. | Yes — but into an ephemeral CI run, not a session. |
| Durability | SQLite/Postgres, DB is sole config source. | SQLite-backed, comparable. | Undisclosed; state appears bound to the running server process. | Not documented beyond a settings file. | N/A — Actions manages the run. |
| Runtime scope | Architectural layering (`terminal` leaf service), not a fixed CLI list. | Named list of 3 CLIs. | Named list of 12 CLIs (maintained allowlist, not an abstraction). | Named list of 4 CLIs. | Claude only / vendor-agnostic engine list (gh-aw). |
| License | MIT | MIT + Commons Clause (blocks commercial resale) | Apache-2.0 | AGPL-3.0 | N/A (Actions/CI tooling) |

**The two claims worth stating plainly, precisely worded:**

1. **GitHub-issue intake into persistent local sessions is, on the evidence
   gathered by both landscape scouts independently, unoccupied.** Every
   project with GitHub-issue/PR intake (claude-code-action, gh-aw, OpenHands
   resolver, claude-hub) executes in a stateless, ephemeral CI run with no
   durable session; every project with persistent local sessions
   (claude-squad, CAO, amux, Tmux-Orchestrator) has no GitHub-issue intake.
   agent-backbone is the only one surveyed that combines both — this is the
   lead differentiator.
2. **No competitor documents an enforced busy/idle delivery-safety
   mechanism equivalent to `safe_deliver` + `get_agent_state`.** amux comes
   closest in spirit — it claims turn-boundary delivery — but publishes no
   detection mechanism and names terminal scraping as its present-day
   fallback; CAO documents nothing on this axis. The honest framing is
   "nobody has published an equivalent," not "nobody has this."

**Secondary, real but smaller differentiators:** hook-pushed state with an
inspectable `evidence` trail (six of the seven session managers surveyed
scrape terminal text only; amux is the sole other project claiming
otherwise, with an unstated mechanism); runtime-agnostic adapters spanning
six CLIs behind one interface, versus every peer's fixed named list; swarms
implemented as ordinary agents sharing the exact same state/delivery/audit
machinery as a solo agent, rather than a separate orchestration engine — no
peer in this landscape documents that pattern.

**Honest overlaps, stated plainly, not glossed over:** claude-squad and Uzi
already own "one tmux session + worktree per agent" as a UX pattern —
agent-backbone's session mechanism is not itself novel there. amux and AWS
CAO are visibly converging on the same control-plane shape (durable
primitives, supervisor, message delivery); amux in particular is aiming at
the same delivery-safety problem `safe_deliver` solves, just not there yet
publicly. gh-aw, being GitHub's own tool with vendor-agnostic engine
support, could commoditize the "GitHub issue → agent" intake pattern
entirely if it grows session persistence — worth watching, not dismissing.

**License and naming, briefly:** agent-backbone's plain MIT is a cleaner
adoption signal than amux's Commons-Clause carve-out or claude-squad's
AGPL-3.0. On naming: `agent-backbone` is unclaimed on PyPI and npm, with
only a handful of dead, 0–1★ same-named repos elsewhere on GitHub — no
rename is warranted, but the CLI's short names (`backbone`, `ab`) will
collide in search with the far larger, unrelated Backbone.js ecosystem; a
one-line README disambiguation is worth adding.

## 3. Qualities and limitations

*(Weighted heaviest per this document's brief — a skeptical senior
engineer's read, not a sales pitch.)*

**What's genuinely strong:**

- **Layering is tested, not just asserted.** `tests/unit/test_imports.py`
  spawns a fresh subprocess interpreter per entry module specifically to
  catch circular imports that only manifest at cold start — the kind of
  test a team writes only after being bitten by exactly that bug.
- **Docs match code.** A line-by-line cross-read of `docs/concepts.md` and
  `docs/security.md` against `_delivery.py` and `_inference.py` found no
  drift between the described decision order/evidence behavior and what the
  code actually does.
- **A real, fast CI gate.** 730 unit tests pass in ~8s with SQLite
  in-memory and tmux mocked, run identically in `make check` and GitHub
  Actions across Python 3.11–3.13.
- **Unusually honest self-reported status.** `docs/status-and-roadmap.md`
  lists "missing on purpose" gaps with reasons and named rough edges
  (a queue-expiry footgun, a `shell` runtime caveat) in-repo, not extracted
  by a reviewer.
- **Live, precise self-filed bug tracking.** Issue #81 — "Backbone secrets
  leak into every agent session's environment," filed by the author against
  their own project — gives a concrete root cause, concrete impact, three
  ranked remediation options, and a testable acceptance criterion. Filing
  a bug that precise against your own security model is itself an
  engineering-capability signal, not just a black mark.
- **A mostly deliberate security posture on paper:** localhost binding and
  bearer auth by default, required webhook HMAC, a Telegram allowlist,
  read-only terminal streaming, and issue/comment bodies never relayed
  verbatim (title + author + short preview + link, plus provenance
  envelopes) — the same convention this swarm operates under.

**What's missing or weak — concrete, not hand-wavy:**

- **Issue #81 is open and unfixed today.** Every backbone-started agent's
  tmux environment currently leaks `BACKBONE_API_KEY`,
  `GITHUB_WEBHOOK_SECRET`, and GitHub App credentials to any code that
  agent runs — directly undercutting `docs/security.md`'s stated trust
  model (agents as untrusted). This is the single fact a skeptical stranger
  is most likely to hit first if they read issues before docs.
- **No PyPI package.** Install is a git-URL `uv tool install`, not
  `pip install agent-backbone` / `uvx agent-backbone` — real friction for
  casual adoption and for anyone behind a package-allowlist policy.
- **Runtime hook coverage is one of six.** Only Claude Code gets a real
  hook; Codex, Gemini CLI, OpenCode, and Aider are terminal-scraped, which
  the project's own docs call weaker. The "runtime-agnostic" pitch is true
  of the plumbing, not yet of detection quality, across runtimes.
- **Single-machine, single-user by design**, and issue #81 shows the
  isolation between agents on that one machine is itself currently
  imperfect — a team wanting shared-service use has real work ahead.
- **All agents share one GitHub token by default** — every agent shows up
  as the same author; per-agent identity requires manual per-agent token
  setup, a real gap for teams wanting audit-grade attribution.
- **No SECURITY.md, no CHANGELOG.md, no CODE_OF_CONDUCT.md** at repo root.
  For a project whose core pitch is a security-relevant trust boundary, the
  absence of a disclosed vulnerability-reporting process is a gap that
  matters more here than it would for a typical CLI tool.
- **Windows is explicitly "not planned"** (tmux dependency) and the version
  is `2.0.0a0` — both honestly labeled, but both real ceilings a stranger
  should expect, not surprises.
- **Head-on against the strongest CI-hosted alternatives, gh-aw and
  claude-code-action:** both need zero local install and no standing
  process, gh-aw runs the agent in its own Docker container behind an
  egress firewall with a separate credential gateway and a
  prompt-injection/secret-leak detection pass before any write lands, and
  both scope credentials to the single run rather than sharing one
  long-lived token. agent-backbone trades all of that away for exactly what
  it gains instead — durable multi-turn sessions and a delivery queue
  neither of them has — but that trade should be stated plainly, not
  glossed over, especially next to the open #81.

**Verdict on engineering capability:** strong. The invariant-as-tested-
function pattern, the fresh-interpreter import test, doc-to-code fidelity,
and especially the precisely-scoped self-filed bug reports show someone who
finds their own bugs through real usage and writes them up like a reviewer
would. The gap between what the docs promise and what the code delivers is
smaller here than in most alpha-stage OSS.

**Verdict on stranger-adoptability today:** adoptable for a technical
single user willing to install from a git URL, accept alpha status, and
read the documented gaps first. Not yet adoptable for anyone with a hard
security bar (the open secret-leak bug), a team/multi-tenant need, or a
no-git-installs policy.

## 4. Path to adoption

Concrete, prioritized — not exhaustive.

1. **Fix #81 before any public push.** A control plane whose own pitch is
   "you can trust what gets pasted into your agents" cannot launch while it
   leaks its own control-plane credentials into every agent's environment.
   This is a blocker, not a backlog item, for anything below.
2. **Ship a PyPI package.** `uv tool install`/`pipx install` from a git URL
   is real friction; comparable tools converge on listing `uv tool install`,
   `pipx install`, and (later) Homebrew side by side as parallel one-liners
   (e.g. `llm`'s and `aider`'s install docs) — `uv tool install
   agent-backbone` / `uvx agent-backbone` should be the first line a
   stranger sees. A brew formula is a credibility signal worth adding once
   adoption justifies the maintenance, not a v1 requirement.
3. **Put a recorded demo at the top of the README, before any architecture
   explanation.** The comparable-tool pattern (starship, claude-squad,
   ripgrep) is: one-line positioning → install one-liner → an animated
   terminal recording → *then* prose. `vhs` (charmbracelet/vhs) is the
   standard way to script a reproducible terminal-GIF from a `.tape` file,
   with a companion GitHub Action to re-render it in CI so the README demo
   can't silently drift from real behavior. The existing README's own
   "Quick start" content (init → hooks install → up, then `agent start` →
   `tell` → `inspect`) is already the right five-minute flow — it just needs
   to be *shown*, not only typed out.
4. **Publish GitHub Releases with real changelog entries**, not just
   conventional-commit history — this repo already enforces
   `feat:`/`fix:`/`docs:` prefixes in `CLAUDE.md`, so the remaining gap is
   surfacing that history as versioned Releases, since a release cadence
   badge and "recent activity" are what a stranger checks before adopting
   infrastructure they'll run unattended.
5. **Add a SECURITY.md.** For a project whose differentiator is a trust
   boundary around what gets pasted into an agent's terminal, a disclosed
   vulnerability-reporting process is table stakes for OSS due diligence,
   not optional polish — and it directly addresses the credibility gap
   #81 otherwise leaves open.
6. **One-line naming disambiguation in the README** ("not related to
   Backbone.js") — cheap, and preempts the one real search-confusion risk
   the naming check surfaced; no rename is warranted.
7. **License/CI/version badges at the top of the README** once the above
   land — MIT, the passing `make check` CI gate, and a real PyPI version
   are all genuine, verifiable signals this project already has; they're
   just not visible at a glance today.

**Minimum credible first public release:** a PyPI package
(`pip install`/`uvx agent-backbone`), a five-minute recorded demo at the top
of the README, a SECURITY.md, and #81 fixed — in that order of blocking
priority. Everything else in this list (Releases/changelog, badges, naming
disambiguation) improves the pitch; these four are what stand between
"a strong alpha a technical stranger can evaluate" and "a project a
skeptical senior engineer would actually recommend running."
