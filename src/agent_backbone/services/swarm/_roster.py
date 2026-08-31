"""Roster parsing — member specs like ``scout*3@claude/sonnet``."""

from __future__ import annotations

import re
from dataclasses import dataclass

_SPEC_RE = re.compile(
    r"^(?P<role>[a-z][a-z0-9-]*)"
    r"(?:\*(?P<count>\d{1,2}))?"
    r"(?:@(?P<runtime>[a-z]+)(?:/(?P<model>[A-Za-z0-9._:-]+))?)?$"
)

COORDINATOR_ROLE = "coordinator"


@dataclass(frozen=True)
class MemberSpec:
    """One roster entry: a role, how many, and what runs it."""

    role: str
    count: int = 1
    runtime: str | None = None  # None -> agents.default_runtime
    model: str | None = None


def parse_member_spec(raw: str) -> MemberSpec:
    """Parse ``role[*N][@runtime[/model]]`` (e.g. ``scout*3@claude/sonnet``)."""
    match = _SPEC_RE.match(raw.strip())
    if not match:
        raise ValueError(
            f"invalid member spec {raw!r} — expected role[*N][@runtime[/model]], "
            "e.g. scout*3@claude/sonnet"
        )
    count = int(match["count"]) if match["count"] else 1
    if count < 1:
        raise ValueError(f"invalid member count in {raw!r}")
    return MemberSpec(
        role=match["role"], count=count, runtime=match["runtime"], model=match["model"]
    )


def parse_roster(raw_specs: list[str]) -> list[MemberSpec]:
    """Parse the roster, defaulting a coordinator and enforcing exactly one.

    A ``coordinator`` spec may appear at most once and always has count 1.
    """
    specs = [parse_member_spec(raw) for raw in raw_specs]
    coordinators = [s for s in specs if s.role == COORDINATOR_ROLE]
    if len(coordinators) > 1 or any(s.count > 1 for s in coordinators):
        raise ValueError("a swarm has exactly one coordinator")
    if not coordinators:
        specs.insert(0, MemberSpec(role=COORDINATOR_ROLE))
    return specs


def member_names(swarm: str, specs: list[MemberSpec]) -> list[tuple[str, MemberSpec]]:
    """Agent names for the roster: ``<swarm>-<role>`` (numbered when count > 1)."""
    names: list[tuple[str, MemberSpec]] = []
    for spec in specs:
        if spec.count == 1:
            names.append((f"{swarm}-{spec.role}", spec))
        else:
            names.extend((f"{swarm}-{spec.role}-{i}", spec) for i in range(1, spec.count + 1))
    return names
