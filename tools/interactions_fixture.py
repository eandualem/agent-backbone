"""Serve the real candidate reads against labelled disposable data, without agents.

Run with an EMPTY --data-dir. No service manager, runtime or production config is
used. Only interaction/conversation reads and conversation writes are exposed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import socket
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import uvicorn
from fastapi.responses import JSONResponse

from agent_backbone.api.app import create_app
from agent_backbone.config import BackboneConfig, BackboneSection, agents_from_rows
from agent_backbone.services.agents import EnrichedAgent
from agent_backbone.services.database import BackboneDB


async def build(directory):
    key = secrets.token_urlsafe(32)
    config = BackboneConfig(api_key=key, backbone=BackboneSection(data_dir=str(directory)))
    db = BackboneDB("sqlite+aiosqlite:///" + str(directory / "fixture.db"))
    await db.start()
    for name, runtime in (
        ("coordinator", "codex"),
        ("researcher", "opencode"),
        ("writer", "claude"),
    ):
        await db.agents.upsert(
            name,
            dir=str(directory / name),
            runtime=runtime,
            model=None,
            repo="",
            tags=[],
            env={},
            description="Illustrative fixture only",
            unattended=False,
            inbox_only=True,
        )
    rows = await db.agents.list()
    config = replace(config, agents=agents_from_rows(rows))
    app = create_app(config).other_asgi_app
    app.state.db = db
    app.state.github = None
    app.state.feed = SimpleNamespace(
        snapshot=AsyncMock(
            return_value=[
                EnrichedAgent(
                    name=r["name"],
                    session=r["name"],
                    runtime=r["runtime"],
                    state="offline",
                    description="Illustrative fixture only",
                    evidence=["fixture; no real runtime"],
                )
                for r in rows
            ]
        )
    )
    ids = {r["name"]: r["report_identity"] for r in rows}
    sender = await db.interactions.attribution("coordinator", ids["coordinator"])
    interaction = await db.interactions.capture(
        operation_id="1" * 32,
        message="ILLUSTRATIVE FIXTURE: inspect the sample change. No model was called.",
        sender="coordinator",
        recipient="researcher",
        source="fixture",
        metadata={"sender": sender},
    )
    await db.deliveries.record(
        issue_number=None,
        target_entity="researcher",
        session_name="researcher",
        outcome="delivered",
        source="fixture",
        operation_id="1" * 32,
    )
    owner = await db.interactions.attribution("", owner_origin=True)
    await db.interactions.capture(
        operation_id="2" * 32,
        message="ILLUSTRATIVE FIXTURE: explain this example.",
        sender="",
        recipient="coordinator",
        source="fixture",
        metadata={"sender": owner},
    )
    await db.interactions.observe(
        "2" * 32, "queued", outcome="pending", source="fixture", time_basis="recorded"
    )

    @app.middleware("http")
    async def only_fixture_surface(request, call_next):
        path = request.url.path
        allowed_read = request.method == "GET" and (
            path.startswith("/api/interactions")
            or path.startswith("/api/conversations")
            or path in ("/api/agents", "/api/agents/states")
        )
        allowed_write = (
            request.method == "POST"
            and path.startswith("/api/conversations/interaction/")
            and (path.endswith("/messages") or path.endswith("/read"))
        )
        if not (allowed_read or allowed_write):
            return JSONResponse(
                status_code=403,
                content={"detail": "Disposable interaction fixture: action disabled"},
            )
        return await call_next(request)

    (directory / "api-key").write_text(key)
    (directory / "api-key").chmod(0o600)
    receipt = {
        "fixture": True,
        "socket": str(directory / "api.sock"),
        "api_key_file": str(directory / "api-key"),
        "data_dir": str(directory),
        "agents": ids,
        "interaction": interaction,
        "real_agents": False,
        "model_calls": False,
        "service_manager": False,
    }
    (directory / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return app, db, receipt


async def run(directory, check):
    app, db, receipt = await build(directory)
    try:
        with patch("agent_backbone.api.routes.conversations.deliver_now", AsyncMock()):
            if check:
                import httpx

                from agent_backbone.api.interaction_models import Interaction, Summary

                headers = {"Authorization": "Bearer " + (directory / "api-key").read_text()}
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://fixture",
                    headers=headers,
                ) as client:
                    summary = await client.get(
                        "/api/interactions/summary",
                        params={"since": "2020-01-01T00:00:00Z", "until": "2040-01-01T00:00:00Z"},
                    )
                    summary.raise_for_status()
                    Summary.model_validate(summary.json())
                    detail = await client.get("/api/interactions/" + receipt["interaction"])
                    detail.raise_for_status()
                    Interaction.model_validate(detail.json())
                    blocked = await client.post("/api/agents/writer/start", json={})
                    assert blocked.status_code == 403
                print(json.dumps({"check": "passed", "receipt": str(directory / "receipt.json")}))
            else:
                print(
                    json.dumps({"receipt": str(directory / "receipt.json"), "fixture": True}),
                    flush=True,
                )
                socket_path = directory / "api.sock"
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                    listener.bind(str(socket_path))
                    socket_path.chmod(0o600)
                    listener.setblocking(False)
                    await uvicorn.Server(
                        uvicorn.Config(app, lifespan="off", log_level="warning")
                    ).serve(sockets=[listener])
    finally:
        await db.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    directory = args.data_dir.resolve()
    if directory.exists() and any(directory.iterdir()):
        parser.error("--data-dir must be empty; existing installations are never reused")
    os.umask(0o077)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    (directory / "home").mkdir(mode=0o700)
    for key in list(os.environ):
        if key.startswith("BACKBONE_") or key in ("TMUX", "TMUX_PANE"):
            del os.environ[key]
    os.environ["HOME"] = str(directory / "home")
    os.environ["TMUX_TMPDIR"] = str(directory / "tmux")
    asyncio.run(run(directory, args.check))


if __name__ == "__main__":
    main()
