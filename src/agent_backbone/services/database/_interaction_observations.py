"""Finite capture coverage and registration-bound sampled state; no causal claims."""

import json
from datetime import timedelta
from itertools import pairwise

from sqlalchemy import select, text

from agent_backbone.services.database._repo import diagnostic_mode
from agent_backbone.services.database._state_history_repo import stored_evidence
from agent_backbone.services.database._time import format_iso, now_iso, parse_iso
from agent_backbone.services.database.models import InteractionCaptureCheckpointORM

_CHECK = InteractionCaptureCheckpointORM.__table__
MAX_GAP_SECONDS = 120


async def checkpoint(conn, recorder_id):
    if await diagnostic_mode(conn) == "off":
        return
    at = now_iso()
    previous = (
        (
            await conn.execute(
                select(_CHECK)
                .where(_CHECK.c.recorder_id == recorder_id)
                .order_by(_CHECK.c.id.desc())
                .limit(1)
            )
        )
        .mappings()
        .first()
    )
    legacy = await conn.scalar(
        text("""
        SELECT COUNT(*) FROM message_queue q
        WHERE q.status IN ('pending','in_progress','checkpoint','uncertain')
        AND NOT EXISTS (SELECT 1 FROM interactions i WHERE i.operation_id=q.operation_id)
        AND NOT EXISTS (SELECT 1 FROM interaction_events e WHERE e.operation_id=q.operation_id)
    """)
    )
    missing = await conn.scalar(
        text("""
        SELECT COUNT(*) FROM deliveries d WHERE d.created_at >= :since AND d.created_at <= :until
        AND NOT EXISTS (SELECT 1 FROM interactions i WHERE i.operation_id=d.operation_id)
        AND NOT EXISTS (SELECT 1 FROM interaction_events e WHERE e.operation_id=d.operation_id)
    """),
        {"since": previous["at"] if previous else at, "until": at},
    )
    reason = (
        "legacy_pending_without_original" if legacy else "uncaptured_transport" if missing else None
    )
    if previous and at < previous["at"]:
        reason = "clock_regressed"
    if (
        previous
        and previous["reason"] == reason
        and 0 <= (parse_iso(at) - parse_iso(previous["at"])).total_seconds() < 60
    ):
        return
    await conn.execute(
        text("""INSERT INTO interaction_coverage (id,capture_started_at,retained_from)
        VALUES (1,:at,:at) ON CONFLICT (id) DO NOTHING"""),
        {"at": at},
    )
    await conn.execute(_CHECK.insert().values(recorder_id=recorder_id, at=at, reason=reason))


async def message_intervals(conn, since, until, snapshot):
    # One bounded predecessor can support a span beginning before the window.
    low = format_iso(parse_iso(since) - timedelta(seconds=MAX_GAP_SECONDS))
    high = min(snapshot["at"], format_iso(parse_iso(until) + timedelta(seconds=MAX_GAP_SECONDS)))
    rows = (
        (
            await conn.execute(
                select(_CHECK)
                .where(
                    _CHECK.c.id <= snapshot.get("checkpoint_high", 0),
                    _CHECK.c.at >= low,
                    _CHECK.c.at <= high,
                )
                .order_by(_CHECK.c.at, _CHECK.c.id)
                .limit(10001)
            )
        )
        .mappings()
        .all()
    )
    truncated = len(rows) > 10000
    intervals = []
    reasons = set()
    for row in rows[:10000]:
        if row["reason"] and since <= row["at"] < until:
            reasons.add(row["reason"])
    for first, last in pairwise(rows[:10000]):
        seconds = (parse_iso(last["at"]) - parse_iso(first["at"])).total_seconds()
        if (
            first["reason"]
            or last["reason"]
            or first["recorder_id"] != last["recorder_id"]
            or not 0 < seconds <= MAX_GAP_SECONDS
        ):
            continue
        start, end = max(first["at"], since), min(last["at"], until)
        if start >= end:
            continue
        if intervals and intervals[-1]["until"] == start:
            intervals[-1]["until"] = end
        else:
            intervals.append(
                {"since": start, "until": end, "completeness": "complete_within_declared_scope"}
            )
    complete = (
        not truncated
        and len(intervals) == 1
        and intervals[0]["since"] == since
        and intervals[0]["until"] == until
    )
    if not complete:
        reasons.add("capture_interval_gaps")
    if truncated:
        reasons.add("capture_interval_limit")
    return intervals, complete, truncated, sorted(reasons)


async def state_spans(conn, agent_id, since, until, high, *, after=None, limit=200):
    # The window function sees adjacent real samples, including the bounded
    # predecessor/successor. A changed state, recorder or large gap stays unknown.
    fields = ["at", "id", "state", "reason", "recorder_id"]
    following = ",".join(
        f"LEAD({f}) OVER (PARTITION BY agent_id ORDER BY at,id) AS next_{f}" for f in fields
    )
    where_after = ""
    params = {
        "agent": agent_id,
        "since": since,
        "until": until,
        "high": high,
        "limit": limit + 1,
        "low": format_iso(parse_iso(since) - timedelta(seconds=MAX_GAP_SECONDS)),
        "upper": format_iso(parse_iso(until) + timedelta(seconds=MAX_GAP_SECONDS)),
    }
    if after:
        where_after = " AND (at > :after_at OR (at=:after_at AND id>:after_id))"
        params.update(after_at=after[0], after_id=after[1])
    rows = (
        (
            await conn.execute(
                text(f"""
        WITH samples AS (SELECT *, {following} FROM registered_state_observations
          WHERE (CAST(:agent AS TEXT) IS NULL OR agent_id=:agent)
          AND id<=:high AND at>=:low AND at<=:upper)
        SELECT * FROM samples WHERE at < :until
          AND (at>=:since OR next_at>:since) {where_after}
        ORDER BY at,id LIMIT :limit
    """),
                params,
            )
        )
        .mappings()
        .all()
    )
    more = len(rows) > limit
    selected = rows[:limit]
    result = []
    for row in selected:
        paired = (
            row["next_at"] is not None
            and row["recorder_id"] == row["next_recorder_id"]
            and row["state"] == row["next_state"]
            and row["reason"] == row["next_reason"]
            and 0
            < (parse_iso(row["next_at"]) - parse_iso(row["at"])).total_seconds()
            <= MAX_GAP_SECONDS
        )
        if row["at"] < since and not paired:
            continue
        end = min(row["next_at"], until) if paired else row["at"]
        result.append(
            {
                "id": str(row["id"]),
                "agent_id": row["agent_id"],
                "name_at_observation": row["name"],
                "since": max(row["at"], since),
                "until": end,
                "state": row["state"],
                "reason": row["reason"],
                "first_observed_at": row["at"],
                "last_observed_at": row["next_at"] if paired else row["at"],
                "observation_ids": [str(row["id"]), str(row["next_id"])]
                if paired
                else [str(row["id"])],
                "basis": "matching_samples" if paired else "point_observation",
                "time_basis": "observed",
                "evidence": stored_evidence(json.loads(row["evidence"])),
            }
        )
    retained = await conn.scalar(
        text(
            "SELECT MIN(at) FROM registered_state_observations "
            "WHERE (CAST(:agent AS TEXT) IS NULL OR agent_id=:agent) AND id<=:high"
        ),
        params,
    )
    coverage = {
        "since": since,
        "until": until,
        "retained_from": retained,
        "completeness": "sampled_partial" if retained else "unavailable",
        "reasons": ["sampling_does_not_measure_continuous_state", "legacy_name_only_excluded"],
        "max_sample_gap_seconds": MAX_GAP_SECONDS,
        "legacy_name_only_excluded": True,
        "unknown_outside_returned_spans": True,
    }
    return result, coverage, [selected[-1]["at"], selected[-1]["id"]] if more else None


def exclude_intervals(intervals, gaps):
    """Subtract uncertain attribution without discarding known adjacent coverage."""
    merged = []
    for start, end in sorted(gaps):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    result = []
    index = 0
    for interval in intervals:
        start, end = interval["since"], interval["until"]
        while index < len(merged) and merged[index][1] <= start:
            index += 1
        cursor = index
        while cursor < len(merged) and merged[cursor][0] < end:
            low, high = merged[cursor]
            if start < low:
                result.append(interval | {"since": start, "until": min(low, end)})
            start = max(start, high)
            cursor += 1
        if start < end:
            result.append(interval | {"since": start, "until": end})
    return result
