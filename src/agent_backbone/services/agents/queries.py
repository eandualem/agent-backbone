"""Transport-independent agent views for API, CLI and integrations."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from agent_backbone.config import AgentSpec, BackboneConfig
from agent_backbone.services.agents._inference import agent_state, get_agent_state
from agent_backbone.services.runtimes import RUNTIME_ENV_KEY
from agent_backbone.services.terminal import list_sessions_rich, query_environment_var


class EnrichedAgent(BaseModel):
    """Agent with merged static config + live state."""

    name: str
    session: str
    configured: bool = True
    runtime: str | None = None
    """Runtime the session was launched with (live), else the configured runtime."""
    model: str | None = None
    """The model observed by the runtime's hook while the session is up, else the saved one."""
    model_source: str = Field(
        default="configured",
        description="``observed`` (the runtime's hook saw it answer), ``configured`` "
        "(saved selection, unverified) or ``runtime_default`` (nothing saved or seen).",
    )
    dir: str = ""
    repo: str = ""
    tags: list[str] = Field(default_factory=list)
    description: str = ""
    watches: list[str] = Field(default_factory=list)
    state: str = "unknown"
    reason: str | None = None
    current_issue: int | None = None
    current_repo: str | None = None
    online: bool = False
    plan_file: str | None = None
    plan_title: str | None = None
    tmux_created: str | None = None
    tmux_attached: bool = False
    tmux_windows: int = 0
    last_activity: float | None = None
    state_since: float | None = None
    last_message: str | None = None
    detail: str | None = None
    state_source: str = "default"
    evidence: list[str] = Field(default_factory=list)


class SubscriptionView(BaseModel):
    """One subscription: a source, its filter and the priority of matches."""

    id: int
    source: str
    filter: str
    priority: str = "normal"


def subscription_views(spec: AgentSpec | None) -> list[SubscriptionView]:
    return [
        SubscriptionView(id=sub.id, source=sub.source, filter=sub.filter, priority=sub.priority)
        for sub in (spec.subscriptions if spec else ())
    ]


class AgentConfigView(BaseModel):
    """Non-secret view of a configured agent."""

    name: str
    dir: str
    runtime: str
    model: str | None = None
    repo: str = ""
    watches: list[str] = Field(default_factory=list)
    subscriptions: list[SubscriptionView] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    description: str = ""
    always_on: bool = False
    unattended: bool = False
    inbox_only: bool = False

    @classmethod
    def from_spec(cls, spec: AgentSpec) -> AgentConfigView:
        return cls(
            name=spec.name,
            dir=str(spec.path),
            runtime=spec.runtime,
            model=spec.model,
            repo=spec.repo,
            watches=list(spec.watches),
            subscriptions=subscription_views(spec),
            tags=list(spec.tags),
            description=spec.description,
            always_on=spec.always_on,
            unattended=spec.unattended,
            inbox_only=spec.inbox_only,
        )


async def build_enriched_agent(
    session: str,
    config: BackboneConfig,
    active_sessions: set[str],
    tmux_info: dict | None = None,
) -> EnrichedAgent:
    """Build an EnrichedAgent for a session (configured agent or ad-hoc session)."""
    spec = config.agents.get(session)
    if spec is not None and spec.inbox_only:
        # It has no session: a tmux session with its name is not its own.
        active_sessions, tmux_info = set(), None
    online = session in active_sessions
    if online:
        snapshot = await agent_state(config, session)
    else:
        # The shared tmux listing already proved the session absent. Reconcile
        # saved metadata without trying to capture a terminal that cannot exist.
        snapshot = await get_agent_state(
            config.state_dir,
            session,
            config.timing.stale_threshold_seconds,
            runtime_hint=spec.runtime if spec else None,
            pane_content="",
        )

    tmux_created = None
    tmux_attached = False
    tmux_windows = 0
    last_activity: float | None = None
    if tmux_info:
        created_ts = tmux_info.get("created", 0)
        if created_ts:
            tmux_created = datetime.fromtimestamp(created_ts, tz=UTC).isoformat()
        tmux_attached = tmux_info.get("attached", False)
        tmux_windows = tmux_info.get("windows", 0)
        activity_ts = tmux_info.get("activity", 0)
        if activity_ts:
            last_activity = float(activity_ts)

    state_value = snapshot.state.value if online else "offline"

    runtime: str | None = spec.runtime if spec else None
    if online:
        with contextlib.suppress(Exception):
            runtime = await query_environment_var(session, RUNTIME_ENV_KEY) or runtime

    observed = snapshot.model if online else None
    configured_model = spec.model if spec else None
    return EnrichedAgent(
        name=session,
        session=session,
        configured=spec is not None,
        runtime=runtime,
        model=observed or configured_model,
        model_source=(
            "observed" if observed else "configured" if configured_model else "runtime_default"
        ),
        dir=str(spec.path) if spec else "",
        repo=spec.repo if spec else "",
        tags=list(spec.tags) if spec else [],
        description=spec.description if spec else "",
        watches=list(spec.watches) if spec else [],
        state=state_value,
        reason=snapshot.reason if online else None,
        current_issue=snapshot.current_issue,
        current_repo=snapshot.current_repo,
        online=online,
        plan_file=snapshot.plan_file,
        plan_title=snapshot.plan_title,
        tmux_created=tmux_created,
        tmux_attached=tmux_attached,
        tmux_windows=tmux_windows,
        last_activity=last_activity,
        state_since=snapshot.timestamp if snapshot.timestamp else None,
        last_message=snapshot.last_message,
        detail=snapshot.detail,
        state_source=snapshot.source,
        evidence=list(snapshot.evidence),
    )


def listable_sessions(config: BackboneConfig, active_sessions: set[str]) -> list[str]:
    """Configured agents first, then any other active tmux session (minus the backbone's own)."""
    names = list(config.agents.names)
    hidden = {config.backbone.session_name}
    names.extend(sorted(s for s in active_sessions if s not in config.agents and s not in hidden))
    return names


async def build_session_snapshot(config: BackboneConfig) -> list[EnrichedAgent]:
    """The full enriched snapshot of every listable session, uncached."""
    rich_sessions = await list_sessions_rich()
    # Sessions in one tmux group (``new-session -t leo``, as terminal viewers
    # create) share their windows: one agent, not several. Keep the configured
    # members, otherwise the session named after the group, otherwise the oldest.
    groups: dict[str, list[dict]] = {}
    for session in rich_sessions:
        groups.setdefault(session.get("group") or session["name"], []).append(session)
    tmux_lookup: dict[str, dict] = {}
    for group, members in groups.items():
        kept = [m for m in members if m["name"] in config.agents] or [
            next((m for m in members if m["name"] == group), None)
            or min(members, key=lambda m: m.get("created", 0))
        ]
        tmux_lookup.update((m["name"], m) for m in kept)
    active_sessions = set(tmux_lookup.keys())
    coros = [
        build_enriched_agent(session, config, active_sessions, tmux_lookup.get(session))
        for session in listable_sessions(config, active_sessions)
    ]
    return list(await asyncio.gather(*coros))
