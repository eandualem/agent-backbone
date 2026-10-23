"""Select an OpenCode v2 session through its native API before opening the TUI.

This launcher helper is standard-library-only. It never edits native storage or
submits a prompt; each API command owns a temporary standalone server.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


def api(binary: str, operation: str, *, body=None, session_id: str | None = None):
    command = [binary, "api", "--standalone", operation]
    if operation in ("model.list", "session.list"):
        command.extend(["--param", f"directory={os.getcwd()}"])
    if operation == "session.list":
        for parameter in ("limit=1", "order=desc", "parentID=null"):
            command.extend(["--param", parameter])
    if session_id is not None:
        command.extend(["--param", f"sessionID={session_id}"])
    if body is not None:
        command.extend(["--data", json.dumps(body)])
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"OpenCode {operation} failed: {type(exc).__name__}") from exc
    if result.returncode:
        raise RuntimeError(f"OpenCode {operation} failed (exit {result.returncode})")
    if not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError(f"OpenCode {operation} returned invalid JSON") from exc


def prepare_session(
    binary: str, model: str, effort: str | None, *, session_id: str | None = None
) -> str:
    """Validate the native catalog, select once, and verify the durable selection."""
    provider, separator, model_id = model.partition("/")
    if not separator or not provider or not model_id:
        raise RuntimeError("OpenCode requires a model in provider/model format")
    catalog = api(binary, "model.list")
    entries = catalog.get("data") if isinstance(catalog, dict) else None
    if not isinstance(entries, list):
        raise RuntimeError("OpenCode returned an invalid model catalog")
    selected = next(
        (
            item
            for item in entries
            if isinstance(item, dict)
            and item.get("providerID") == provider
            and item.get("id") == model_id
        ),
        None,
    )
    if selected is None:
        raise RuntimeError(f"OpenCode model is unavailable in this directory: {model}")
    variants = selected.get("variants", [])
    if not isinstance(variants, list):
        raise RuntimeError("OpenCode returned an invalid variant catalog")
    if effort is not None and not any(
        isinstance(item, dict) and item.get("id") == effort for item in variants
    ):
        raise RuntimeError(f"OpenCode model {model} has no variant {effort!r}")
    target = {"providerID": provider, "id": model_id}
    if effort is not None:
        target["variant"] = effort
    if session_id is None:
        created = api(
            binary,
            "session.create",
            body={"model": target, "location": {"directory": os.getcwd()}},
        )
        session = created.get("data") if isinstance(created, dict) else None
        session_id = session.get("id") if isinstance(session, dict) else None
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError("OpenCode did not return the new session ID")
    else:
        current = api(binary, "session.get", session_id=session_id)
        existing = current.get("data") if isinstance(current, dict) else None
        if not isinstance(existing, dict) or existing.get("id") != session_id:
            raise RuntimeError("OpenCode did not identify the requested session")
        api(binary, "session.switchModel", body={"model": target}, session_id=session_id)
    result = api(binary, "session.get", session_id=session_id)
    session = result.get("data") if isinstance(result, dict) else None
    if not isinstance(session, dict) or session.get("id") != session_id:
        raise RuntimeError("OpenCode did not confirm the selected session")
    actual = session.get("model")
    if not isinstance(actual, dict) or any(
        actual.get(key) != value for key, value in target.items()
    ):
        raise RuntimeError("OpenCode did not retain the requested model and variant")
    if effort is None and actual.get("variant") not in (None, "default"):
        raise RuntimeError("OpenCode retained a variant when the model default was requested")
    return session_id


def launch(selection: dict, command: list[str]) -> None:
    """Prepare the exact session before the native TUI reads its first snapshot."""
    model, effort, resume = (selection.get(key) for key in ("model", "effort", "resume"))
    session_id = resume if isinstance(resume, str) else None
    if resume is True and model:
        result = api(command[0], "session.list")
        sessions = result.get("data") if isinstance(result, dict) else None
        if not isinstance(sessions, list) or not sessions:
            raise RuntimeError("OpenCode has no recent session to continue in this directory")
        session_id = sessions[0].get("id") if isinstance(sessions[0], dict) else None
        if not isinstance(session_id, str):
            raise RuntimeError("OpenCode did not identify the latest session")
    if model:
        session_id = prepare_session(command[0], model, effort, session_id=session_id)
    elif session_id:
        result = api(command[0], "session.get", session_id=session_id)
        session = result.get("data") if isinstance(result, dict) else None
        if not isinstance(session, dict) or session.get("id") != session_id:
            raise RuntimeError("OpenCode did not identify the requested session")
    if session_id:
        command.extend(["--session", session_id])
    elif resume is True:
        command.append("--continue")
    os.execv(command[0], command)


if __name__ == "__main__":
    try:
        launch(json.loads(sys.argv[1]), sys.argv[2:])
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
