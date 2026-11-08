"""An interaction is its own immutable conversation artifact."""

import json

from sqlalchemy import select

from agent_backbone.services.database.models import InteractionORM

_I = InteractionORM.__table__


async def artifact(conn, artifact_id):
    row = (await conn.execute(select(_I).where(_I.c.id == artifact_id))).mappings().first()
    if row is None:
        return None
    people = {}
    for name in ("sender", "recipient"):
        actor = json.loads(row[name])
        if actor["kind"] == "agent" and actor["id"] and actor["attribution"] != "unverified":
            people[actor["id"]] = actor["name_at_capture"]
    responsible = next(iter(people), None)
    version = {
        "revision_id": artifact_id,
        "label": "original message",
        "created_at": row["accepted_at"],
        "author_id": responsible,
        "author_name": people.get(responsible, ""),
    }
    return {
        "kind": "interaction",
        "artifact_id": artifact_id,
        "title": " ".join(row["preview"].split())[:120],
        "versions": [version],
        "latest": version,
        "responsible_id": responsible,
        "responsible_name": people.get(responsible),
        "interaction_participants": people,
        "operation_id": row["operation_id"],
        "original_context": {
            "interaction_id": artifact_id,
            "accepted_at": row["accepted_at"],
            "sender": json.loads(row["sender"]),
            "recipient": json.loads(row["recipient"]),
            "body": {
                "level": "full" if row["body"] is not None else "excerpt",
                "text": row["body"] if row["body"] is not None else row["preview"],
                "reason": None if row["body"] is not None else "retention",
                "format": row["body_format"],
            },
        },
    }
