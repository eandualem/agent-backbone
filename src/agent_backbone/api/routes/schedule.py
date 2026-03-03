"""Today's schedule endpoints — cron evaluation + ROUTINE.md parsing + personal items."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from croniter import croniter
from fastapi import APIRouter, Depends, HTTPException

from agent_backbone.api.deps import get_config, get_monitoring_service
from agent_backbone.api.models import ListEnvelope, PersonalScheduleCreate, ScheduleEntry
from agent_backbone.config import BackboneConfig
from agent_backbone.services.agents import MonitoringService

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["schedule"])

# Entity -> ROUTINE.md path mapping
_ROUTINE_PATHS: dict[str, str] = {
    "leo": "~/ws/leo/ROUTINE.md",
    "ike": "~/ws/core/ike/ROUTINE.md",
    "feynman": "~/orchestration/ROUTINE.md",
    "brunel": "~/infra/ROUTINE.md",
}

# Pattern: ## SectionName (HH:MM)
_ROUTINE_HEADER_RE = re.compile(r"^## (.+?)\s*\((\d{2}:\d{2})\)\s*$")


def _parse_routine_labels(path: Path) -> dict[str, str]:
    """Parse ROUTINE.md and return {HH:MM -> label} mapping."""
    labels: dict[str, str] = {}
    if not path.exists():
        return labels
    try:
        for line in path.read_text().splitlines():
            m = _ROUTINE_HEADER_RE.match(line)
            if m:
                label, time_str = m.group(1), m.group(2)
                labels[time_str] = label
    except OSError:
        pass
    return labels


def _done_file_path(state_path: Path) -> Path:
    """Path to today's done-state file."""
    today = datetime.now().strftime("%Y-%m-%d")
    return state_path / f"schedule-done-{today}.json"


def _load_done_state(state_path: Path) -> dict[str, bool]:
    """Load done state for today."""
    path = _done_file_path(state_path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_done_state(state_path: Path, done: dict[str, bool]) -> None:
    """Persist done state for today."""
    path = _done_file_path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(done))


def _get_cron_fire_times_today(cron_expr: str, tz: ZoneInfo) -> list[str]:
    """Get all fire times for a cron expression on today's date."""
    now = datetime.now(tz)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0)

    times: list[str] = []
    cron = croniter(cron_expr, start_of_day)
    while True:
        next_fire = cron.get_next(datetime)
        if not next_fire.tzinfo:
            next_fire = next_fire.replace(tzinfo=tz)
        if next_fire > end_of_day:
            break
        times.append(next_fire.strftime("%H:%M"))
    return times


# --- Personal schedule helpers ---


def _slugify(label: str) -> str:
    """Convert label to a safe ID slug."""
    slug = re.sub(r"[^\w\s-]", "", label.lower())
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")
    return slug or "untitled"


def _personal_schedule_path(state_path: Path) -> Path:
    """Path to the personal schedule JSON file."""
    return state_path / "personal-schedule.json"


def _load_personal_schedule(state_path: Path) -> dict:
    """Load personal schedule data. Returns dict with 'items' list."""
    path = _personal_schedule_path(state_path)
    if not path.exists():
        return {"items": []}
    try:
        data = json.loads(path.read_text())
        if "items" not in data:
            data["items"] = []
        return data
    except (json.JSONDecodeError, OSError):
        return {"items": []}


def _save_personal_schedule(state_path: Path, data: dict) -> None:
    """Persist personal schedule data."""
    path = _personal_schedule_path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _personal_items_for_today(items: list[dict]) -> list[dict]:
    """Filter personal schedule items that should show today."""
    now = datetime.now()
    today_dow = (now.weekday() + 1) % 7  # Python weekday: Mon=0 -> convert to Sun=0
    today_str = now.strftime("%Y-%m-%d")

    result = []
    for item in items:
        if item.get("recurring", False):
            days = item.get("days")
            if days is None or today_dow in days:
                result.append(item)
        else:
            item_date = item.get("date")
            if item_date is None or item_date == today_str:
                result.append(item)
    return result


# --- Endpoints ---


@router.get("/schedule/today", response_model=ListEnvelope[ScheduleEntry])
async def get_today_schedule(
    config: BackboneConfig = Depends(get_config),
    monitoring_svc: MonitoringService = Depends(get_monitoring_service),
):
    """Get today's schedule — cron fire times merged with ROUTINE.md labels + personal items."""
    schedules = monitoring_svc.load_schedules()
    done_state = _load_done_state(config.agent_state.state_path)
    entries: list[ScheduleEntry] = []

    for entity, schedule in schedules.items():
        if not schedule.get("enabled", True):
            continue

        cron_expr = schedule.get("cron")
        if not cron_expr:
            continue

        tz_name = schedule.get("timezone", config.heartbeat.default_timezone)
        tz = ZoneInfo(tz_name)
        fire_times = _get_cron_fire_times_today(cron_expr, tz)

        # Load ROUTINE.md labels for this entity
        routine_path_str = _ROUTINE_PATHS.get(entity)
        routine_labels: dict[str, str] = {}
        if routine_path_str:
            routine_labels = _parse_routine_labels(Path(routine_path_str).expanduser())

        for time_str in fire_times:
            item_id = f"{entity}-{time_str}"
            label = routine_labels.get(time_str, schedule.get("description", "Heartbeat"))
            entries.append(
                ScheduleEntry(
                    id=item_id,
                    entity=entity,
                    time=time_str,
                    label=label,
                    type="heartbeat",
                    done=done_state.get(item_id, False),
                )
            )

    # Personal schedule items
    personal_data = _load_personal_schedule(config.agent_state.state_path)
    today_items = _personal_items_for_today(personal_data["items"])
    today_str = datetime.now().strftime("%Y-%m-%d")

    for item in today_items:
        item_id = item["id"]
        done_dates = item.get("done_dates", [])
        entries.append(
            ScheduleEntry(
                id=item_id,
                entity="elias",
                time=item["time"],
                label=item["label"],
                type="personal",
                done=today_str in done_dates,
            )
        )

    entries.sort(key=lambda e: (e.time, e.entity))
    return ListEnvelope(items=entries, total=len(entries))


@router.patch("/schedule/today/{item_id}")
async def toggle_schedule_done(
    item_id: str,
    config: BackboneConfig = Depends(get_config),
):
    """Toggle the done state of a schedule item (heartbeat or personal)."""
    # Check if it's a personal schedule item first
    personal_data = _load_personal_schedule(config.agent_state.state_path)
    for item in personal_data["items"]:
        if item["id"] == item_id:
            today_str = datetime.now().strftime("%Y-%m-%d")
            done_dates = item.get("done_dates", [])
            if today_str in done_dates:
                done_dates.remove(today_str)
                new_done = False
            else:
                done_dates.append(today_str)
                new_done = True
            item["done_dates"] = done_dates
            _save_personal_schedule(config.agent_state.state_path, personal_data)
            return {"id": item_id, "done": new_done}

    # Fall through to heartbeat done-state logic
    done_state = _load_done_state(config.agent_state.state_path)
    current = done_state.get(item_id, False)
    done_state[item_id] = not current
    _save_done_state(config.agent_state.state_path, done_state)
    return {"id": item_id, "done": not current}


@router.post("/schedule/personal", response_model=ScheduleEntry, status_code=201)
async def create_personal_item(
    body: PersonalScheduleCreate,
    config: BackboneConfig = Depends(get_config),
):
    """Create a personal schedule item."""
    personal_data = _load_personal_schedule(config.agent_state.state_path)
    existing_ids = {item["id"] for item in personal_data["items"]}

    # Generate unique slug ID
    base_slug = _slugify(body.label)
    slug = base_slug
    counter = 1
    while slug in existing_ids:
        slug = f"{base_slug}-{counter}"
        counter += 1

    new_item: dict = {
        "id": slug,
        "time": body.time,
        "label": body.label,
        "recurring": body.recurring,
        "done_dates": [],
    }
    if body.days is not None:
        new_item["days"] = body.days

    personal_data["items"].append(new_item)
    _save_personal_schedule(config.agent_state.state_path, personal_data)

    return ScheduleEntry(
        id=slug,
        entity="elias",
        time=body.time,
        label=body.label,
        type="personal",
        done=False,
    )


@router.delete("/schedule/personal/{item_id}")
async def delete_personal_item(
    item_id: str,
    config: BackboneConfig = Depends(get_config),
):
    """Delete a personal schedule item."""
    personal_data = _load_personal_schedule(config.agent_state.state_path)
    original_len = len(personal_data["items"])
    personal_data["items"] = [i for i in personal_data["items"] if i["id"] != item_id]

    if len(personal_data["items"]) == original_len:
        raise HTTPException(status_code=404, detail="Personal schedule item not found")

    _save_personal_schedule(config.agent_state.state_path, personal_data)
    return {"deleted": True, "id": item_id}
