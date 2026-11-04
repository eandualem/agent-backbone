"""Operator terminal sessions on a bound provider's private tmux server."""

import asyncio
import os
import pwd
from pathlib import Path

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_backbone.services.runtimes import RUNTIMES
from agent_backbone.services.terminal import session_exists, start_session

_SIGN_IN_RUNTIMES = ("claude", "codex", "opencode")
_lock = asyncio.Lock()
# Keep the CLI's last words on screen: a sign-in can end before anyone attaches.
_HOLD_OPEN = '"$@"; echo; echo "Press Enter to close."; read _'


class _Shell(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: int = Field(ge=1, le=1)
    shell_id: str = Field(pattern=r"^[0-9a-f]{12}$")
    cwd: str | None


class _SignIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: int = Field(ge=1, le=1)
    runtime: str


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


async def _parsed(request: Request, model):
    try:
        return model.model_validate_json(await request.body())
    except ValidationError:
        raise _error(422, "invalid_request", "The request is not valid.") from None


def _identity(request: Request):
    provider = getattr(request.state, "profile_provider", None)
    if provider is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return provider.reservation.identity


async def _ensure(request: Request, session: str, cwd: str, command: list[str], env) -> dict:
    """Return a live session unchanged; never kill or respawn one."""
    identity = _identity(request)
    async with _lock:
        created = not await session_exists(session)
        if created and not await start_session(
            session,
            working_dir=cwd,
            command=command,
            environment=dict(env),
            inherit_environment=False,
        ):
            raise _error(503, "shell_unavailable", "The terminal session could not be started.")
    return {
        "schema_version": 1,
        "profile_id": identity.profile_id,
        "installation_id": identity.installation_id,
        "lifecycle_id": identity.lifecycle_id,
        "session": session,
        "created": created,
    }


async def ensure_shell(request: Request) -> dict:
    body = await _parsed(request, _Shell)
    session = f"shell-{body.shell_id}"
    _identity(request)
    environment = RUNTIMES["shell"].environment()
    cwd = body.cwd if body.cwd is not None else environment["HOME"]
    # A live session keeps its directory, so its cwd is ignored rather than judged.
    if not await session_exists(session) and not (Path(cwd).is_absolute() and Path(cwd).is_dir()):
        raise _error(422, "invalid_cwd", "The directory must be an absolute existing path.")
    login_shell = pwd.getpwuid(os.getuid()).pw_shell or "/bin/sh"
    result = await _ensure(request, session, cwd, [login_shell], environment)
    return {**result, "shell_id": body.shell_id}


async def sign_in(request: Request) -> dict:
    """Run a CLI's own sign-in in a session the operator attaches to."""
    body = await _parsed(request, _SignIn)
    _identity(request)
    if body.runtime not in _SIGN_IN_RUNTIMES:
        raise _error(422, "runtime_unavailable", "This runtime cannot be signed in here.")
    try:
        command, environment = RUNTIMES[body.runtime].account_invocation("login")
    except ValueError:
        raise _error(422, "runtime_unavailable", "This runtime is not installed.") from None
    held = ["/bin/sh", "-c", _HOLD_OPEN, "sign-in", *command]
    result = await _ensure(
        request, f"sign-in-{body.runtime}", environment["HOME"], held, environment
    )
    return {**result, "runtime": body.runtime}
