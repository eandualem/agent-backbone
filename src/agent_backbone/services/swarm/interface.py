"""Swarm lifecycle — a thin layer over agents, worktrees and the issue.

A swarm is one coordinator plus members sharing a single worktree and
branch, created to complete one pre-existing GitHub issue. Members are
ordinary backbone agents; all communication runs through the existing
delivery pipeline. The issue is the channel between the swarm and its
initiator; members talk to their coordinator with ``backbone tell``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_backbone.config import AgentSpec
from agent_backbone.services.agents import start_agent
from agent_backbone.services.routing import safe_deliver
from agent_backbone.services.runtimes import RUNTIMES
from agent_backbone.services.swarm._roster import (
    COORDINATOR_ROLE,
    member_names,
    parse_roster,
)
from agent_backbone.services.swarm._templates import render_brief
from agent_backbone.services.swarm._worktree import (
    create_worktree,
    current_branch,
    is_git_repo,
    remove_worktree,
)
from agent_backbone.services.terminal import session_exists, stop_session

if TYPE_CHECKING:
    from agent_backbone.config import BackboneConfig
    from agent_backbone.services.agents import AgentStore
    from agent_backbone.services.database import BackboneDB

log = logging.getLogger(__name__)

_ISSUE_REF_RE = re.compile(r"^(?P<repo>[\w.-]+/[\w.-]+)#(?P<number>\d{1,7})$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")


class SwarmError(Exception):
    """A swarm operation failed for a reason the caller should show verbatim."""


def _sandboxed(runtime: str) -> bool:
    """Whether ``runtime`` confines its commands to the agent's directory."""
    rt = RUNTIMES.get(runtime)
    return rt is not None and rt.sandboxed


@dataclass
class SwarmResult:
    name: str
    coordinator: str
    members: list[str]
    branch: str
    worktree: str
    repo: str
    issue_number: int


def parse_issue_ref(raw: str) -> tuple[str, int]:
    match = _ISSUE_REF_RE.match(raw.strip())
    if not match:
        raise SwarmError(f"invalid issue reference {raw!r} — expected owner/repo#N")
    return match["repo"], int(match["number"])


def _issue_url(repo: str, number: int) -> str:
    return f"https://github.com/{repo}/issues/{number}"


async def _verify_issue(gh, repo: str, number: int) -> str:
    """The issue's title. Raises SwarmError when it is missing or closed."""
    if gh is None:
        raise SwarmError(
            "GitHub is not configured — a swarm needs its issue verified and is torn "
            "down by the issue-closed event (set GITHUB_TOKEN and try again)"
        )
    try:
        issue = await gh.get_issue(number, repo_full_name=repo)
    except Exception as exc:
        raise SwarmError(f"could not fetch {repo}#{number}: {exc}") from exc
    if issue.state == "closed":
        raise SwarmError(f"{repo}#{number} is closed — a swarm works an open issue")
    return issue.title or f"issue #{number}"


def _facts(
    *,
    swarm: str,
    agent_name: str,
    role: str,
    coordinator: str,
    initiator: str,
    repo: str,
    issue_number: int,
    branch: str,
    worktree: Path,
    members: list[str],
    base_branch: str,
) -> dict[str, str]:
    return {
        "swarm_name": swarm,
        "agent_name": agent_name,
        "role": role,
        "coordinator": coordinator,
        "initiator": initiator or "(human operator)",
        "repo": repo,
        "issue_number": str(issue_number),
        "issue_url": _issue_url(repo, issue_number),
        "branch": branch,
        "base_branch": base_branch,
        "worktree": str(worktree),
        "members": ", ".join(members),
    }


async def create_swarm(
    config: BackboneConfig,
    db: BackboneDB,
    store: AgentStore,
    gh,
    *,
    name: str,
    issue_ref: str,
    member_specs: list[str],
    initiator: str = "",
) -> SwarmResult:
    """Create and start a swarm on an existing issue.

    Steps: validate, verify the issue is open, create the shared worktree
    and branch in the initiator's repository checkout, register + start
    each member with its role brief, then deliver the kickoff to the
    coordinator.
    """
    if not _NAME_RE.match(name):
        raise SwarmError(f"invalid swarm name {name!r} (lowercase, digits, dashes)")
    prior = await db.swarms.get(name)
    if prior is not None and prior.get("status") == "active":
        raise SwarmError(f"swarm '{name}' already exists")
    if config.agents.get(name) is not None or await session_exists(name):
        # `backbone tell <swarm>` resolves agents first, so a swarm sharing an
        # agent's name would be unreachable.
        raise SwarmError(f"swarm name '{name}' is already used by an agent")

    repo, issue_number = parse_issue_ref(issue_ref)
    existing = await db.swarms.active_for_issue(repo, issue_number)
    if existing is not None:
        raise SwarmError(f"swarm '{existing['name']}' is already working {repo}#{issue_number}")
    title = await _verify_issue(gh, repo, issue_number)

    # The worktree is created from the initiating agent's checkout of the repo.
    # An agent swarms on its OWN repository — running in another agent's
    # checkout caused exactly the confusion it sounds like (first live test).
    init_spec = config.agents.get(initiator) if initiator else None
    if init_spec is not None and init_spec.repo.casefold() == repo.casefold():
        repo_dir = init_spec.path
    elif init_spec is not None:
        raise SwarmError(
            f"'{initiator}' does not own a checkout of {repo} — a swarm runs in its "
            f"initiator's repository. Create the issue in your own repository instead, "
            f"or ask the agent that owns {repo} to initiate the swarm"
        )
    else:
        # Human-run CLI (no initiating agent): use the repo owner's checkout.
        owners = [s for s in config.agents if s.repo.casefold() == repo.casefold()]
        if not owners:
            raise SwarmError(
                f"no agent owns a checkout of {repo} — start one there first, or run "
                "swarm create from inside an agent whose directory is that repository"
            )
        repo_dir = owners[0].path
    if not await is_git_repo(repo_dir):
        raise SwarmError(f"{repo_dir} is not a git repository")

    roster = parse_roster(member_specs)
    named = member_names(name, roster)
    coordinator = next(n for n, s in named if s.role == COORDINATOR_ROLE)
    all_names = [n for n, _ in named]
    for agent_name in all_names:
        if config.agents.get(agent_name) is not None or await session_exists(agent_name):
            raise SwarmError(f"agent name '{agent_name}' is already in use")

    try:
        base_branch = await current_branch(repo_dir)
    except RuntimeError as exc:
        raise SwarmError(str(exc)) from exc
    worktree, branch = await create_worktree(repo_dir, name)

    try:
        await db.swarms.create(
            name,
            repo=repo,
            issue_number=issue_number,
            initiator=initiator,
            coordinator=coordinator,
            branch=branch,
            worktree_dir=str(worktree),
        )
    except Exception as exc:
        # Lost a race past the pre-checks (another swarm took the issue or
        # the name meanwhile): don't leave an unregistered worktree behind,
        # it would block the next attempt under this name.
        await remove_worktree(repo_dir, worktree)
        raise SwarmError(f"could not register swarm '{name}': {exc}") from exc

    briefs_dir = config.data_dir / "swarms" / name
    started: list[str] = []
    default_runtime = config.launch.default_runtime
    try:
        briefs_dir.mkdir(parents=True, exist_ok=True)
        for agent_name, spec in named:
            runtime = spec.runtime or default_runtime
            facts = _facts(
                swarm=name,
                agent_name=agent_name,
                role=spec.role,
                coordinator=coordinator,
                initiator=initiator,
                repo=repo,
                issue_number=issue_number,
                branch=branch,
                worktree=worktree,
                members=all_names,
                base_branch=base_branch,
            )
            brief_file = briefs_dir / f"{agent_name}.md"
            brief_file.write_text(render_brief(spec.role, facts, data_dir=config.data_dir))

            agent = AgentSpec(
                name=agent_name,
                dir=str(worktree),
                runtime=runtime,
                model=spec.model,
                repo=repo,
                tags=(f"swarm:{name}", f"role:{spec.role}"),
                # A member parked on a dialog stalls the whole swarm, so a
                # member whose runtime confines it to the worktree never asks.
                # One without a sandbox keeps asking: unattended there would
                # be trust on the machine, the owner's call per agent.
                unattended=config.swarm.unattended_members and _sandboxed(runtime),
            )
            await store.register(agent)
            # The role brief replaces the common backbone brief: at launch
            # where the runtime takes one, else as the first delivered message.
            result = await start_agent(agent, config, brief_file=brief_file, db=db)
            if not result.ok:
                raise SwarmError(f"failed to start member '{agent_name}'")
            started.append(agent_name)
            await store.touch_started(agent_name)
            if result.ready == "exited":
                raise SwarmError(f"member '{agent_name}' exited before reaching its prompt")
    except Exception:
        # Best-effort rollback so a half-started swarm doesn't linger.
        unstopped: list[str] = []
        for agent_name in started:
            try:
                stopped = await stop_session(agent_name)
            except Exception:
                stopped = False
            if not stopped:
                unstopped.append(agent_name)
        # A session that would not die keeps its record — forgetting it would
        # leave a running agent nobody can address — and the worktree it runs in.
        for agent_name, _ in named:
            if agent_name in unstopped:
                continue
            try:
                await store.forget(agent_name)
            except Exception:
                log.debug("rollback: could not forget %s", agent_name)
        if unstopped:
            log.warning(
                "rollback: %s still running; stop them and remove %s by hand",
                ", ".join(unstopped),
                worktree,
            )
        elif not await remove_worktree(repo_dir, worktree):
            log.warning("rollback: worktree %s still exists; remove it by hand", worktree)
        await db.swarms.set_status(name, "disbanded")
        raise

    kickoff = (
        f"[via:backbone swarm:{name}] Your swarm is live. Task: {repo}#{issue_number} "
        f'— "{title}" ({_issue_url(repo, issue_number)}). Read the issue, plan, and '
        f"start assigning work to your members. Your role brief has the details."
    )
    outcome = await safe_deliver(
        coordinator,
        kickoff,
        config,
        db=db,
        source="swarm-kickoff",
        delivery_kind="direct_message",
    )
    log.info("Swarm '%s' started (%d members); kickoff: %s", name, len(all_names), outcome)
    return SwarmResult(
        name=name,
        coordinator=coordinator,
        members=all_names,
        branch=branch,
        worktree=str(worktree),
        repo=repo,
        issue_number=issue_number,
    )


async def _members_of(store: AgentStore, name: str) -> list[AgentSpec]:
    tag = f"swarm:{name}"
    return [s for s in store.agents if tag in s.tags]


async def teardown_swarm(
    config: BackboneConfig,
    db: BackboneDB,
    store: AgentStore,
    swarm: dict,
    *,
    status: str,
) -> list[str]:
    """Stop members, remove the worktree, forget the agents. Returns member names."""
    name = swarm["name"]
    members = await _members_of(store, name)
    failed_stops: list[str] = []
    for member in members:
        if await session_exists(member.name) and not await stop_session(member.name):
            failed_stops.append(member.name)
    if failed_stops:
        raise SwarmError(
            "could not stop swarm member session(s): "
            f"{', '.join(failed_stops)} — the worktree was left in place"
        )
    worktree = Path(swarm["worktree_dir"])
    # The worktree lives at <repo_dir>/.backbone/swarms/<name>.
    repo_dir = worktree.parent.parent.parent
    # A missing directory is still registered with git until removed; a
    # repository that is gone entirely has nothing left to remove.
    if repo_dir.is_dir() and not await remove_worktree(repo_dir, worktree):
        # Stop here so the worktree is not silently orphaned: the members are
        # stopped, nothing is forgotten, and teardown can be retried.
        raise SwarmError(
            f"could not remove worktree {worktree} — resolve the git error and "
            f"disband '{name}' again"
        )
    for member in members:
        try:
            await store.forget(member.name)
        except Exception:
            log.warning("Could not forget swarm member %s", member.name)
    await db.swarms.set_status(name, status)
    log.info("Swarm '%s' torn down (%s): %d members", name, status, len(members))
    return [m.name for m in members]


async def teardown_for_issue(
    config: BackboneConfig, db: BackboneDB, store: AgentStore, repo: str, issue_number: int
) -> str | None:
    """Tear down the active swarm working this issue, if any. Returns its name."""
    swarm = await db.swarms.active_for_issue(repo, issue_number)
    if swarm is None:
        return None
    await teardown_swarm(config, db, store, swarm, status="done")
    return swarm["name"]


async def swarm_overview(db: BackboneDB, store: AgentStore) -> list[dict]:
    """All swarms with their member rosters."""
    swarms = await db.swarms.list()
    for swarm in swarms:
        members = await _members_of(store, swarm["name"])
        swarm["members"] = [
            {
                "name": m.name,
                "role": next((t.split(":", 1)[1] for t in m.tags if t.startswith("role:")), ""),
                "runtime": m.runtime,
                "model": m.model,
            }
            for m in members
        ]
    return swarms
