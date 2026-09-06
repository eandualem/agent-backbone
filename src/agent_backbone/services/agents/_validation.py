"""Validation shared by registration, updates and launch, independent of transport."""

from __future__ import annotations

import re

from agent_backbone.config import AgentSpec
from agent_backbone.services.runtimes import RUNTIMES


def validate_repo(repo: str, *, allow_empty: bool = False) -> None:
    if allow_empty and repo == "":
        return
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("repo must be OWNER/REPO")
    if any(part in {".", ".."} for part in repo.split("/")):
        raise ValueError("repo must be OWNER/REPO")


def validate_agent_spec(spec: AgentSpec) -> None:
    if not isinstance(spec.name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", spec.name):
        raise ValueError("invalid agent name")
    if not isinstance(spec.dir, str) or not spec.dir.strip() or "\0" in spec.dir:
        raise ValueError("dir must be a nonempty path")
    if not isinstance(spec.runtime, str) or spec.runtime not in RUNTIMES:
        raise ValueError(f"Unknown runtime: {spec.runtime}")
    if spec.model is not None and (
        not isinstance(spec.model, str) or any(c.isspace() or ord(c) < 32 for c in spec.model)
    ):
        raise ValueError("model must be a model ID, optionally followed by :effort")
    rt = RUNTIMES[spec.runtime]
    model, effort = rt.split_model(spec.model)
    if effort and not model:
        raise ValueError("model effort requires a model ID")
    try:
        rt.check_effort(effort)
        rt.check_unattended(spec.unattended)
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
    validate_repo(spec.repo, allow_empty=True)
    if not isinstance(spec.watches, (tuple, list)):
        raise ValueError("watches must be a list of repositories")
    for repo in spec.watches:
        validate_repo(repo)
    if not isinstance(spec.tags, (list, tuple)) or not all(isinstance(t, str) for t in spec.tags):
        raise ValueError("tags must be a list of strings")
    if not isinstance(spec.env, dict) or not all(
        isinstance(k, str)
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)
        and isinstance(v, str)
        and "\0" not in v
        for k, v in spec.env.items()
    ):
        raise ValueError("env must map environment variable names to strings")
    if not isinstance(spec.description, str):
        raise ValueError("description must be a string")
    if not isinstance(spec.always_on, bool) or not isinstance(spec.unattended, bool):
        raise ValueError("always_on and unattended must be booleans")
