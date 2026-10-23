"""Select an OpenCode v2 session through its native API before opening the TUI.

This launcher helper is standard-library-only. It never edits native storage or
submits a prompt. Selection uses one private native stdio server and waits for
native migration completion before reading or changing a session.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from functools import partial
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from urllib.parse import urlsplit


def api(
    binary: str, operation: str, *, server: str, env: dict, body=None, session_id: str | None = None
):
    command = [binary, "api", "--server", server, operation]
    if operation == "model.list":
        command.extend(["--param", f"location[directory]={os.getcwd()}"])
    if operation == "session.list":
        command.extend(["--param", f"directory={os.getcwd()}"])
    if operation == "session.list":
        for parameter in ("limit=1", "order=desc", "parentID=null"):
            command.extend(["--param", parameter])
    if session_id is not None:
        command.extend(["--param", f"sessionID={session_id}"])
    if body is not None:
        command.extend(["--data", json.dumps(body)])
    try:
        # Native startup may migrate its database. Readiness can time out while
        # this continues; never kill a migration to meet that UI wait deadline.
        result = subprocess.run(command, capture_output=True, text=True, check=False, env=env)
    except OSError as exc:
        raise RuntimeError(f"OpenCode {operation} failed: {type(exc).__name__}") from exc
    if result.returncode:
        raise RuntimeError(f"OpenCode {operation} failed (exit {result.returncode})")
    if not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError(f"OpenCode {operation} returned invalid JSON") from exc


def migration_status(endpoint: str, password: str):
    # The CLI's --server health check has a five-second timeout. Migration can
    # keep that server busy longer, so use its status endpoint without a deadline.
    connection = HTTPConnection("127.0.0.1", urlsplit(endpoint).port)
    authorization = base64.b64encode(f"opencode:{password}".encode()).decode()
    try:
        connection.request(
            "GET",
            "/api/experimental/migration/v1",
            headers={"Authorization": f"Basic {authorization}"},
        )
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError("OpenCode could not report history migration status")
        return json.loads(response.read())
    except (HTTPException, OSError, ValueError) as exc:
        raise RuntimeError("OpenCode could not report history migration status") from exc
    finally:
        connection.close()


@contextmanager
def standalone(binary: str):
    """Use the native stdin lease, without registering or touching a shared service."""
    # This server selects metadata; only the later TUI may report agent state.
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"BACKBONE_AGENT", "BACKBONE_STATE_DIR"}
    }
    environment["OPENCODE_PASSWORD"] = secrets.token_urlsafe(32)
    process = subprocess.Popen(
        [binary, "serve", "--stdio", "--hostname", "127.0.0.1", "--port", "0"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=environment,
    )
    try:
        try:
            ready = json.loads(process.stdout.readline())
            endpoint = ready["url"]
            address = urlsplit(endpoint)
            if address.scheme != "http" or address.hostname != "127.0.0.1" or not address.port:
                raise ValueError("not the requested loopback address")
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(
                "OpenCode private server did not report its loopback address"
            ) from exc

        # Native stdout remains a pipe after readiness; keep it drained.
        def drain_output():
            for _ in process.stdout:
                pass

        drain = threading.Thread(target=drain_output, daemon=True)
        drain.start()
        request = partial(api, binary, server=endpoint, env=environment)
        while True:
            status = migration_status(endpoint, environment["OPENCODE_PASSWORD"])
            state = status.get("status") if isinstance(status, dict) else None
            if state == "completed":
                break
            if state not in ("required", "running"):
                raise RuntimeError(
                    "OpenCode history migration failed; inspect it in OpenCode before retrying"
                )
            time.sleep(0.5)
        yield request
    finally:
        # EOF releases only this process. Never stop a managed native service.
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        # A descendant may still hold the pipe open. Do not wait for the drain
        # thread or close its locked reader; exec/exit closes our descriptors.


def prepare_session(
    request, model: str, effort: str | None, *, session_id: str | None = None
) -> str:
    """Validate the native catalog, select once, and verify the durable selection."""
    provider, separator, model_id = model.partition("/")
    if not separator or not provider or not model_id:
        raise RuntimeError("OpenCode requires a model in provider/model format")
    catalog = request("model.list")
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
    fresh = session_id is None
    if fresh:
        created = request(
            "session.create",
            body={"model": target, "location": {"directory": os.getcwd()}},
        )
        session = created.get("data") if isinstance(created, dict) else None
        session_id = session.get("id") if isinstance(session, dict) else None
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError("OpenCode did not return the new session ID")
    else:
        current = request("session.get", session_id=session_id)
        existing = current.get("data") if isinstance(current, dict) else None
        if not isinstance(existing, dict) or existing.get("id") != session_id:
            raise RuntimeError("OpenCode did not identify the requested session")
        request("session.switchModel", body={"model": target}, session_id=session_id)
    try:
        result = request("session.get", session_id=session_id)
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
    except RuntimeError as exc:
        if fresh:
            # This launch created it and has not handed it to a TUI or prompt.
            try:
                request("session.remove", session_id=session_id)
            except RuntimeError:
                raise RuntimeError(f"{exc}; could not remove the unused new session") from exc
        raise
    return session_id


def launch(selection: dict, command: list[str]) -> None:
    """Prepare the exact session before the native TUI reads its first snapshot."""
    model, effort, resume = (selection.get(key) for key in ("model", "effort", "resume"))
    session_id = resume if isinstance(resume, str) else None
    if model or session_id:
        with standalone(command[0]) as request:
            if resume is True and model:
                result = request("session.list")
                sessions = result.get("data") if isinstance(result, dict) else None
                if not isinstance(sessions, list):
                    raise RuntimeError("OpenCode returned an invalid session list")
                if sessions:
                    session_id = sessions[0].get("id") if isinstance(sessions[0], dict) else None
                    if not isinstance(session_id, str):
                        raise RuntimeError("OpenCode did not identify the latest session")
            if model:
                session_id = prepare_session(request, model, effort, session_id=session_id)
            elif session_id:
                result = request("session.get", session_id=session_id)
                session = result.get("data") if isinstance(result, dict) else None
                if not isinstance(session, dict) or session.get("id") != session_id:
                    raise RuntimeError("OpenCode did not identify the requested session")
    if session_id:
        command.extend(["--session", session_id])
    elif resume is True:
        command.append("--continue")
    os.execv(command[0], command)


def failure_path(state_dir: Path, agent: str, launch_id: str) -> Path:
    identity = hashlib.sha256(launch_id.encode()).hexdigest()
    return state_dir / "launches" / agent / f"{identity}.opencode-error"


def record_failure(error: str) -> None:
    """Retain a launch-bound diagnostic when the terminal closes immediately."""
    state, agent, launch_id = (
        os.environ.get(key)
        for key in ("BACKBONE_STATE_DIR", "BACKBONE_AGENT", "BACKBONE_LAUNCH_ID")
    )
    if not (state and agent and launch_id) or Path(agent).name != agent:
        return
    try:
        target = failure_path(Path(state), agent, launch_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(error[:1000])
        os.replace(temporary, target)
    except OSError:
        pass  # stderr remains available if recording itself is unavailable


if __name__ == "__main__":
    try:
        launch(json.loads(sys.argv[1]), sys.argv[2:])
    except (RuntimeError, OSError) as exc:
        record_failure(str(exc))
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
