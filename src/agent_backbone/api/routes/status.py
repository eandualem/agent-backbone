"""System status and health endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from agent_backbone.api.deps import (
    get_config,
    get_db,
    get_optional_github,
    get_scheduler,
    get_state_service,
    get_telegram_service,
    get_tmux_service,
)
from agent_backbone.api.models import (
    AgentConfigResponse,
    EnrichedAgent,
    JobStatusResponse,
    RepoStatus,
    ServiceHealth,
    SystemDigest,
)
from agent_backbone.api.session_updates import build_enriched_agent, listable_sessions
from agent_backbone.config import BackboneConfig
from agent_backbone.services.agents import StateService
from agent_backbone.services.database import BackboneDB
from agent_backbone.services.github import GitHubClient
from agent_backbone.services.scheduler import PeriodicScheduler
from agent_backbone.services.telegram import TelegramService
from agent_backbone.services.terminal import TmuxService

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["status"])


@router.get("/status", response_model=SystemDigest)
async def get_system_status(
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
    gh: GitHubClient | None = Depends(get_optional_github),
    state_svc: StateService = Depends(get_state_service),
    tmux_svc: TmuxService = Depends(get_tmux_service),
):
    """System-wide status digest: sessions, agents, deliveries."""
    active = await tmux_svc.list_sessions()
    active_set = set(active)

    agents: list[EnrichedAgent] = [
        await build_enriched_agent(session, config, active_set, state_svc)
        for session in listable_sessions(config, active_set)
    ]

    failed_rows = await db.get_failed_deliveries(limit=1000)

    try:
        last_events = await db.last_event_time_by_repo()
    except Exception:
        last_events = {}
    repos = [
        RepoStatus(
            repo=repo,
            owners=[s.name for s in config.agents.owners(repo)],
            watchers=[s.name for s in config.agents.watchers(repo)],
            last_event_at=last_events.get(repo),
        )
        for repo in config.agents.repos
    ]

    pending_issues: int | None = None
    if gh is not None and repos:
        try:
            pending_issues = 0
            for repo in config.agents.repos:
                pending_issues += len(await gh.list_issues(state="open", repo_full_name=repo))
        except Exception:
            log.warning("Failed to fetch pending issues from GitHub")
            pending_issues = None

    return SystemDigest(
        active_sessions=active,
        agent_count=len(agents),
        pending_issues=pending_issues,
        failed_deliveries=len(failed_rows),
        github_intake=config.github_intake,
        agents=agents,
        repos=repos,
    )


@router.get("/status/services", response_model=ServiceHealth)
async def get_service_health(
    config: BackboneConfig = Depends(get_config),
    db: BackboneDB = Depends(get_db),
    scheduler: PeriodicScheduler | None = Depends(get_scheduler),
    telegram: TelegramService | None = Depends(get_telegram_service),
    gh: GitHubClient | None = Depends(get_optional_github),
):
    """Health of the backbone's own components."""
    health = ServiceHealth()

    try:
        health.database = "up" if await db.check_connection() else "down"
    except Exception:
        health.database = "down"

    if scheduler is not None:
        sched = await scheduler.health_check()
        health.scheduler = "up" if sched.get("healthy") else "degraded"
        health.jobs = [
            JobStatusResponse(
                name=job.name,
                interval_seconds=job.interval_seconds,
                runs=job.runs,
                failures=job.failures,
                running=job.running,
                last_started=job.last_started,
                last_finished=job.last_finished,
                last_error=job.last_error,
            )
            for job in scheduler.jobs
        ]

    if telegram is not None and telegram.enabled:
        health.telegram = "up" if telegram.running else "down"

    if gh is not None:
        health.github = config.github_intake
    elif config.github.intake != "off":
        health.github = "unconfigured"

    return health


@router.get("/config/agents", response_model=list[AgentConfigResponse])
async def get_agent_config(config: BackboneConfig = Depends(get_config)):
    """Return the configured agents (non-secret)."""
    return [
        AgentConfigResponse(
            name=spec.name,
            dir=str(spec.path),
            runtime=spec.runtime,
            model=spec.model,
            repo=spec.repo,
            watches=list(spec.watches),
            tags=list(spec.tags),
            description=spec.description,
        )
        for spec in config.agents
    ]
