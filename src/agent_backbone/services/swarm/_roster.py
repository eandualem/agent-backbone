"""Roster parsing — member specs like ``scout*3@claude/sonnet``.

The model half may carry a reasoning effort (``coordinator@codex/gpt-6-astra:high``);
the runtime splits it off at launch, so nothing here has to know the levels.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SPEC_RE = re.compile(
    r"^(?P<role>[a-z][a-z0-9-]*)"
    r"(?:\*(?P<count>\d{1,2}))?"
    # The model may itself contain "/" (OpenCode names models provider/model):
    # the runtime is the letters right after "@", the model everything after
    # the first "/".
    r"(?:@(?P<runtime>[a-z]+)(?:/(?P<model>[A-Za-z0-9._:/-]+))?)?$"
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
    """Parse ``role[*N][@runtime[/model[:effort]]]`` (e.g. ``scout*3@claude/sonnet``,
    ``coordinator@codex/gpt-6-astra:high``, ``scout@opencode/google/gemini-3.8-flash``)."""
    match = _SPEC_RE.match(raw.strip())
    if not match:
        raise ValueError(
            f"invalid member spec {raw!r} — expected role[*N][@runtime[/model[:effort]]], "
            "e.g. scout*3@claude/sonnet or coordinator@codex/gpt-6-astra:high"
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
    """Agent names for the roster: ``<swarm>-<role>``, numbered when the role
    appears more than once — across specs too, so ``scout@codex`` plus
    ``scout@claude`` cannot collide on one name.

    Raises ValueError when two roles still map to one name (``scout*2`` plus
    a role literally called ``scout-1``): two members under one agent name
    would silently merge into one corrupted agent.
    """
    role_total: dict[str, int] = {}
    for spec in specs:
        role_total[spec.role] = role_total.get(spec.role, 0) + spec.count
    counter: dict[str, int] = {}
    names: list[tuple[str, MemberSpec]] = []
    for spec in specs:
        for _ in range(spec.count):
            counter[spec.role] = counter.get(spec.role, 0) + 1
            if role_total[spec.role] == 1:
                names.append((f"{swarm}-{spec.role}", spec))
            else:
                names.append((f"{swarm}-{spec.role}-{counter[spec.role]}", spec))
    seen: set[str] = set()
    for label, _ in names:
        if label in seen:
            raise ValueError(
                f"member specs map two members to the same agent name {label!r} — "
                "rename one of the roles"
            )
        seen.add(label)
    return names
