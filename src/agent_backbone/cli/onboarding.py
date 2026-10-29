"""Finite operator setup actions for installers; no agent workflow or scheduler."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import socket
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from time import monotonic

from dotenv import dotenv_values
from pydantic import TypeAdapter, ValidationError

from agent_backbone.cli import _common, service
from agent_backbone.config import agent_credential, bootstrap_config, sqlite_url
from agent_backbone.fs import AtomicCreateUnsupported
from agent_backbone.fs import atomic_create_text as _exclusive_file
from agent_backbone.models import (
    SETUP_BODY_BYTES,
    SetupFirstAgent,
    SetupInitialize,
    SetupRequest,
    SetupResult,
)
from agent_backbone.models import (
    setup_result as result,
)
from agent_backbone.services.database import SetupReceipt, SetupReceiptError
from agent_backbone.templates import core_starter_content

_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
ADAPTER = TypeAdapter(SetupRequest)
_READINESS_SECONDS = 30.0


def add_setup_parser(sub) -> None:
    parser = sub.add_parser("setup", help="initialize an installation or create its first agent")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="request JSON file, or - for stdin")
    source.add_argument("--schema", action="store_true", help="print the accepted request schema")
    parser.add_argument("--validate", action="store_true", help="validate input without changes")
    parser.add_argument(
        "--jsonl", action="store_true", help="emit versioned progress and one result"
    )
    parser.set_defaults(func=cmd_setup)


class Output:
    def __init__(self, request_id):
        self.request_id, self.seq = request_id, 0

    def emit(self, kind, **data):
        self.seq += 1
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "request_id": self.request_id,
                    "seq": self.seq,
                    "type": kind,
                    **data,
                }
            ),
            flush=True,
        )

    def step(self, name, status):
        self.emit("step", step=name, status=status)

    def finish(self, record):
        self.emit("result", **record)
        return (
            2
            if record["code"] == "invalid_request"
            else {"completed": 0, "failed": 1, "action_required": 3}[record["status"]]
        )


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate field")
        value[key] = item
    return value


def cmd_setup(args: argparse.Namespace) -> int:
    if args.schema:
        if args.validate:
            print("--validate requires --file", file=sys.stderr)
            return 2
        print(json.dumps(ADAPTER.json_schema()))
        return 0
    if not args.jsonl:
        print("--file requires --jsonl", file=sys.stderr)
        return 2
    output = Output("unknown")
    action = "validate"
    try:
        if args.file == "-":
            raw = sys.stdin.buffer.read(SETUP_BODY_BYTES + 1)
        else:
            with open(args.file, "rb") as stream:
                raw = stream.read(SETUP_BODY_BYTES + 1)
        if len(raw) > SETUP_BODY_BYTES:
            raise ValueError("request too large")
        data = json.loads(raw, object_pairs_hook=_unique_object)
        if isinstance(data, dict):
            if isinstance(data.get("request_id"), str) and _REQUEST_ID.fullmatch(
                data["request_id"]
            ):
                output.request_id = data["request_id"]
            if data.get("action") in ("initialize", "first_agent") and not args.validate:
                action = data["action"]
        request = ADAPTER.validate_python(data)
    except (OSError, ValueError, ValidationError, RecursionError):
        return output.finish(
            result(
                action,
                "invalid_request",
                "Invalid setup request; use --schema.",
                status="failed",
                retry="new_request",
            )
        )
    if args.validate:
        return output.finish(
            result(
                "validate",
                "validated",
                "Request shape is valid.",
                status="completed",
                retry="none",
                data={"valid": True, "requested_action": request.action},
            )
        )
    output.step("preflight", "running")
    try:
        _operator_environment()
        _safe_directory(Path(request.data_dir))
        record = asyncio.run(_execute(request, output))
    except SetupReceiptError as exc:
        record = result(request.action, exc.code, str(exc))
    except AtomicCreateUnsupported:
        record = result(
            request.action,
            "filesystem_unsupported",
            "Setup requires a filesystem with hard-link support; choose another data directory.",
            retry="new_request",
        )
    except _common.OperatorFileUnavailable:
        record = result(
            request.action,
            "operator_required",
            "Setup requires the existing installation operator key in its .env file; "
            "environment-only authentication uses the ordinary connection flow.",
            retry="none",
        )
    except (OSError, ValueError, sqlite3.Error):
        record = result(
            request.action,
            "installation_conflict",
            "Setup could not inspect this installation safely.",
        )
    except Exception:
        record = result(
            request.action,
            "outcome_unknown",
            "Setup stopped without confirming the result; retry the same request.",
        )
    return output.finish(record)


def _operator_environment():
    # Check before reading a file. Never turn a managed session into an operator
    # by discarding an empty, invalid or filtered collaboration credential.
    if (
        "BACKBONE_AGENT_CREDENTIAL" in os.environ
        or os.environ.get("BACKBONE_AGENT")
        or os.environ.get("BACKBONE_CLIENT_AGENT")
        or os.environ.get("BACKBONE_CLIENT_REGISTRATION")
        or agent_credential(os.environ.get("BACKBONE_API_KEY", ""))
    ):
        raise SetupReceiptError("operator_required", "Run setup from an operator process.")


def _safe_directory(path: Path):
    if path.resolve() != path or (path.exists() and not path.is_dir()):
        raise SetupReceiptError(
            "installation_conflict", "Use a directory without symlink redirects."
        )
    for name in (".env", "backbone.db"):
        item = path / name
        if item.is_symlink() or (item.exists() and not item.is_file()):
            raise SetupReceiptError(
                "installation_conflict", "Installation files must be regular files."
            )


async def _execute(request, output):
    if isinstance(request, SetupFirstAgent):
        config = await _common.read_client_config(request.data_dir, strict=True, operator_file=True)
        response = await _common.api(
            config, "POST", "/api/setup/first-agent", json_body=request.model_dump(), timeout=120
        )
        if response is None and _common.request_not_sent("/api/setup/first-agent"):
            return result(
                request.action,
                "service_unavailable",
                "The service is unavailable; no request was sent.",
            )
        if response is None:
            return result(
                request.action,
                "outcome_unknown",
                "No result was received; retry the same request to reconcile.",
            )
        status, body = response
        if status in (401, 403):
            return result(
                request.action, "operator_required", "The service requires an operator credential."
            )
        if status != 200 or not isinstance(body, dict) or "code" not in body:
            return result(
                request.action, "service_unavailable", "This service cannot execute setup."
            )
        try:
            body = SetupResult.model_validate(body).model_dump()
            if body["action"] != request.action:
                raise ValueError("mismatched action")
        except ValueError:
            return result(
                request.action, "outcome_unknown", "The service returned an invalid setup result."
            )
        for step in body["completed_steps"]:
            output.step(step, "completed")
        return body
    return await _initialize(request, output)


async def _endpoint_occupied(config):
    def connect():
        try:
            with socket.create_connection((config.backbone.host, config.backbone.port), timeout=2):
                return True
        except ConnectionRefusedError:
            return False
        except OSError as exc:
            raise SetupReceiptError(
                "installation_conflict", "Cannot establish whether the service address is free."
            ) from exc

    return await asyncio.to_thread(connect)


def _observation_data(config, installation):
    return {
        "data_dir": str(config.data_dir),
        "installation": installation,
        "service_state": "unknown",
        "api_url": None,
        "health": "unknown",
    }


async def _observe(config, installation, *, deadline=None):
    data = _observation_data(config, installation)
    path = service.setup_service_path()
    if path is None:
        data["service_state"] = "unsupported"
    elif not path.exists():
        data["service_state"] = "not_installed"
    elif service.setup_service_owner() == config.data_dir.resolve():
        remaining = 3.0 if deadline is None else min(3.0, deadline - monotonic())
        if remaining <= 0:
            return data, "Service readiness observation did not finish."
        # Cancelling a to_thread await does not stop its worker. Bound the
        # underlying manager command too, so asyncio.run's executor shutdown
        # cannot add a fresh three-second probe after the readiness deadline.
        data["service_state"] = await asyncio.to_thread(
            service.setup_service_state, timeout=remaining
        )
    identity = await _common.api(config, "GET", "/api/setup/identity", timeout=2)
    if identity is None:
        return (
            data,
            "The service identity endpoint did not respond; check local address/proxy settings.",
        )
    if identity[0] in (401, 403):
        return (
            data,
            f"Service identity authentication failed (HTTP {identity[0]}). Preserve credentials.",
        )
    if identity[0] != 200:
        return data, f"The service identity endpoint returned HTTP {identity[0]}."
    if not isinstance(identity[1], dict) or identity[1].get("data_dir") != str(
        config.data_dir.resolve()
    ):
        return data, "The responding service did not confirm the selected data directory."
    data["api_url"] = _common.api_url(config, "")
    health = await _common.api(config, "GET", "/health", timeout=2)
    if health is None:
        return data, "The service health endpoint did not respond."
    if health[0] != 200:
        return data, f"The service health endpoint returned HTTP {health[0]}."
    if not isinstance(health[1], dict) or type(health[1].get("healthy")) is not bool:
        return data, "The service health endpoint returned an invalid health result."
    data["health"] = "healthy" if health[1]["healthy"] else "unhealthy"
    if data["health"] == "healthy":
        return data, ""
    # Only fixed component names are safe to include; never echo exception text
    # or arbitrary response bodies from an unverified/misconfigured endpoint.
    components = health[1].get("components", {})
    failed = [
        name
        for name in ("database", "agents", "scheduler", "github", "integrations", "sources")
        if isinstance(components, dict)
        and isinstance(components.get(name), dict)
        and components[name].get("healthy") is False
    ]
    detail = (
        "Unhealthy components: " + ", ".join(failed) + "."
        if failed
        else "Service health is unhealthy."
    )
    return data, detail


async def _wait_for_service(config):
    """Allow cold startup, bounding observation time without restarting a service."""
    deadline = monotonic() + _READINESS_SECONDS
    observed = _observation_data(config, "created")
    detail = "Service readiness observation did not finish."
    while (remaining := deadline - monotonic()) > 0:
        try:
            observed, detail = await asyncio.wait_for(
                _observe(config, "created", deadline=deadline), timeout=remaining
            )
        except TimeoutError:
            break
        if observed["health"] == "healthy":
            return observed, ""
        await asyncio.sleep(min(0.5, max(0, deadline - monotonic())))
    return observed, (
        f"Service health was not confirmed within {_READINESS_SECONDS:g} seconds. "
        f"{detail} {service.setup_service_guidance()}"
    )


def _owned_file(receipt, path, text):
    digest = hashlib.sha256(text.encode()).hexdigest()
    key = str(path)
    owned = receipt.record.setdefault("files", {})
    if path.exists() or path.is_symlink():
        if (
            path.is_symlink()
            or not path.is_file()
            or owned.get(key) != digest
            or hashlib.sha256(path.read_bytes()).hexdigest() != digest
        ):
            raise SetupReceiptError(
                "starter_conflict", "An installation file changed; it was preserved."
            )
        return
    owned[key] = digest
    receipt.save()
    _exclusive_file(path, text)


async def _inspect_existing(boot):
    # Validate before creating even a receipt in a directory that may be unrelated.
    # Neither path initializes, repairs or migrates the installation database.
    if boot.database_url != sqlite_url(boot.data_dir):
        declared = dotenv_values(boot.env_path, interpolate=False).get("BACKBONE_DATABASE_URL")
        if declared != boot.database_url:
            raise SetupReceiptError(
                "installation_conflict",
                "The external database must be declared in this installation's .env file; "
                "environment-only configuration uses the ordinary connection flow.",
            )
    config = await _common.read_client_config(boot.data_dir, strict=True, operator_file=True)
    if boot.database_url == sqlite_url(boot.data_dir):
        with closing(
            sqlite3.connect((boot.data_dir / "backbone.db").as_uri() + "?mode=ro", uri=True)
        ) as db:
            db.execute("SELECT key FROM settings LIMIT 1").fetchall()
            db.execute("SELECT name FROM agents LIMIT 1").fetchall()
    return config


async def _initialize(request: SetupInitialize, output):
    data_dir = Path(request.data_dir)
    boot = bootstrap_config(data_dir)
    existing = boot.env_path.is_file() or (data_dir / "backbone.db").exists()
    if not existing and boot.database_url != sqlite_url(data_dir):
        raise SetupReceiptError(
            "installation_conflict", "Fresh setup requires its own local database."
        )
    receipt = SetupReceipt(data_dir, request.model_dump())
    recorded_request = receipt.path.is_file() and not receipt.path.is_symlink()
    if existing and not recorded_request:
        await _inspect_existing(boot)
    if (
        not existing
        and data_dir.exists()
        and any(p.name != "setup-receipts" for p in data_dir.iterdir())
        and not recorded_request
    ):
        raise SetupReceiptError(
            "installation_conflict", "The selected directory contains unrelated files."
        )
    if not data_dir.exists():
        data_dir.mkdir(mode=0o700, parents=True)
    with receipt:
        fresh = receipt.record.get("fresh")
        if fresh is None:
            if not existing and any(p.name != "setup-receipts" for p in data_dir.iterdir()):
                raise SetupReceiptError(
                    "installation_conflict", "The selected directory contains unrelated files."
                )
            fresh = not existing
            receipt.save(fresh=fresh)
        if fresh and boot.database_url != sqlite_url(data_dir):
            raise SetupReceiptError(
                "installation_conflict", "Fresh setup retries require their own local database."
            )
        output.step("preflight", "completed")
        if "preflight" not in receipt.record["completed_steps"]:
            receipt.completed("preflight")
        if not fresh:
            config = await _inspect_existing(boot)
            observed, _ = await _observe(config, "existing_preserved")
            final = result(
                "initialize",
                "existing_preserved",
                "Existing installation preserved.",
                status="completed",
                retry="none",
                data=observed,
                steps=("preflight",),
            )
            receipt.save(result=final)
            return final
        if receipt.record.get("result", {}).get("status") == "completed":
            # Re-observe health; never recreate or repair files on replay.
            if not boot.env_path.is_file() or not (data_dir / "backbone.db").is_file():
                raise SetupReceiptError(
                    "installation_conflict",
                    "An initialized installation file is missing; it was not recreated.",
                )
            config = await _common.read_client_config(data_dir, strict=True, operator_file=True)
            observed, detail = (
                await _wait_for_service(config)
                if request.service == "user"
                else await _observe(config, "created")
            )
            final = dict(receipt.record["result"], data=observed)
            if request.service == "user" and final["data"]["health"] != "healthy":
                return result(
                    "initialize",
                    "service_unavailable",
                    detail + " The installation was preserved.",
                    data=final["data"],
                    steps=receipt.record["completed_steps"],
                )
            return final
        if request.service == "user" and "service_definition" not in receipt.record:
            try:
                path, content = service.setup_service_definition(service.setup_binary(), data_dir)
            except ValueError as exc:
                raise SetupReceiptError(
                    "service_unavailable",
                    "A pinned launcher and valid service PATH are required before initialization.",
                ) from exc
            if path.exists() or path.is_symlink():
                raise SetupReceiptError(
                    "installation_conflict", "An existing user service was preserved."
                )
            receipt.save(service_definition={"path": str(path), "content": content})
        if "data" not in receipt.record["completed_steps"]:
            if await _endpoint_occupied(boot):
                raise SetupReceiptError(
                    "installation_conflict", "The service address is already occupied."
                )
            if request.service == "user":
                path = service.setup_service_path()
                if path is None:
                    raise SetupReceiptError(
                        "service_unavailable", "No supported user service manager is available."
                    )
                if path.exists() or path.is_symlink():
                    raise SetupReceiptError(
                        "installation_conflict", "An existing user service was preserved."
                    )
            output.step("data", "running")
            if not boot.env_path.exists():
                if receipt.record.get("database_started"):
                    raise SetupReceiptError(
                        "installation_conflict",
                        "Initialized credentials are missing; they were not recreated.",
                    )
                text = "BACKBONE_API_KEY=" + secrets.token_urlsafe(32) + "\n"
                receipt.save(credential_digest=hashlib.sha256(text.encode()).hexdigest())
                _exclusive_file(boot.env_path, text)
            if hashlib.sha256(boot.env_path.read_bytes()).hexdigest() != receipt.record.get(
                "credential_digest"
            ):
                raise SetupReceiptError(
                    "installation_conflict", "Unrelated credentials appeared during setup."
                )
            if (data_dir / "backbone.db").exists() and not receipt.record.get("database_started"):
                raise SetupReceiptError(
                    "installation_conflict", "An unrelated database appeared during setup."
                )
            receipt.save(database_started=True)
            config = bootstrap_config(data_dir)
            async with _common.Direct(config) as direct:
                if "backbone.port" not in await direct.db.settings.all():
                    await direct.store.set_setting("backbone.port", config.backbone.port)
            receipt.completed("data")
            output.step("data", "completed")
        if "starters" not in receipt.record["completed_steps"]:
            output.step("starters", "running")
            profile, files = core_starter_content()
            for relative, text in files.items():
                target = data_dir / relative
                if not target.resolve().is_relative_to(data_dir):
                    raise SetupReceiptError(
                        "starter_conflict", "A starter path redirects outside the installation."
                    )
                _owned_file(receipt, target, text)
            async with _common.Direct(bootstrap_config(data_dir)) as direct:
                for key, value in {
                    "agents.shared_policy": profile["policies"],
                    "skills.store": str(data_dir / "skills"),
                }.items():
                    stored = await direct.db.settings.all()
                    if key in stored and stored[key] != value:
                        raise SetupReceiptError(
                            "starter_conflict", "Existing starter assignments were preserved."
                        )
                    if key not in stored:
                        await direct.store.set_setting(key, value)
            receipt.completed("starters")
            output.step("starters", "completed")
        if request.service == "user" and "service" not in receipt.record["completed_steps"]:
            output.step("service", "running")
            definition = receipt.record["service_definition"]
            path, content = Path(definition["path"]), definition["content"]
            _owned_file(receipt, path, content)
            receipt.save(pending="service")
            failure = "The loaded service does not confirm this installation's ownership."
            try:
                started = service.start_setup_service(data_dir)
            except service.SetupServiceError as exc:
                started, failure = False, str(exc)
            if not started:
                observed, _ = await _observe(
                    await _common.read_client_config(data_dir, strict=True, operator_file=True),
                    "created",
                )
                return result(
                    "initialize",
                    "service_unavailable",
                    f"{failure} Its files were retained. {service.setup_service_guidance()}",
                    data=observed,
                    steps=receipt.record["completed_steps"],
                )
            receipt.completed("service")
            output.step("service", "completed")
        config = await _common.read_client_config(data_dir, strict=True, operator_file=True)
        observed, detail = (
            await _wait_for_service(config)
            if request.service == "user"
            else await _observe(config, "created")
        )
        if request.service == "user" and observed["health"] != "healthy":
            return result(
                "initialize",
                "service_unavailable",
                detail,
                data=observed,
                steps=receipt.record["completed_steps"],
            )
        final = result(
            "initialize",
            "initialized",
            "Installation initialized.",
            status="completed",
            retry="none",
            data=observed,
            steps=receipt.record["completed_steps"],
        )
        receipt.save(result=final)
        return final
