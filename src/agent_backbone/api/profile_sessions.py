"""Operator terminal sessions on a bound provider's private tmux server."""

import asyncio
import os
import pwd
from pathlib import Path

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_backbone.config import session_secret_keys
from agent_backbone.services.runtimes import RUNTIMES
from agent_backbone.services.terminal import session_exists, start_session

_SIGN_IN_RUNTIMES = ("claude", "codex", "opencode")
_lock = asyncio.Lock()
# Keep the CLI's last words on screen: a sign-in can end before anyone attaches.
_HOLD_OPEN = 'if "$@"; then exit 0; fi; echo; echo "Press Enter to close."; read _'


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


def _is_default(request: Request) -> bool:
    return request.state.profile_provider.reservation.kind == "legacy_default"


async def _ensure(request: Request, session: str, cwd: str, command: list[str], env) -> dict:
    """Return a live session unchanged; never kill or respawn one."""
    identity = _identity(request)
    # The Default profile uses the person's environment, without the installation's secrets.
    options = (
        {"environment": dict(env), "inherit_environment": False}
        if env is not None
        else {"scrub": session_secret_keys(request.app.state.config.data_dir)}
    )
    async with _lock:
        created = not await session_exists(session)
        if created and not await start_session(
            session,
            working_dir=cwd,
            command=command,
            **options,
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


def _isolated_shell_environment(request: Request) -> dict[str, str]:
    """Point the CLI in this shell at this profile. None of these values is a secret."""
    identity = _identity(request)
    config = request.app.state.config
    return {
        **RUNTIMES["shell"].environment(),
        "BACKBONE_DATA_DIR": str(config.data_dir),
        "BACKBONE_PORT": str(config.backbone.port),
        "BACKBONE_PROFILE_ID": identity.profile_id,
        "BACKBONE_INSTALLATION_ID": identity.installation_id,
        "BACKBONE_LIFECYCLE_ID": identity.lifecycle_id,
    }


async def ensure_shell(request: Request) -> dict:
    body = await _parsed(request, _Shell)
    session = f"shell-{body.shell_id}"
    _identity(request)
    environment = None if _is_default(request) else _isolated_shell_environment(request)
    home = environment["HOME"] if environment is not None else str(Path.home())
    cwd = body.cwd if body.cwd is not None else home
    # A live session keeps its directory, so its cwd is ignored rather than judged.
    if not await session_exists(session) and not (Path(cwd).is_absolute() and Path(cwd).is_dir()):
        raise _error(422, "invalid_cwd", "The directory must be an absolute existing path.")
    login_shell = pwd.getpwuid(os.getuid()).pw_shell or "/bin/sh"
    # Default's shell reads the person's profile, like their own terminal.
    command = [login_shell, "-l"] if environment is None else [login_shell]
    result = await _ensure(request, session, cwd, command, environment)
    return {**result, "shell_id": body.shell_id}


async def account_status(request: Request, runtime: str = "") -> dict:
    """One CLI's own sign-in status: a stored sign-in, not proof the account works."""
    _identity(request)
    if _is_default(request):
        raise _error(422, "runtime_unavailable", "Default keeps its existing CLI sign-ins.")
    if runtime not in _SIGN_IN_RUNTIMES:
        raise _error(422, "runtime_unavailable", "This runtime has no sign-in here.")
    try:
        RUNTIMES[runtime].account_invocation("status")
    except ValueError:
        raise _error(422, "runtime_unavailable", "This runtime is not installed.") from None
    authentication = await asyncio.to_thread(RUNTIMES[runtime].observed_authentication)
    return {"schema_version": 1, "runtime": runtime, "authentication": authentication}


async def sign_in(request: Request) -> dict:
    """Run a CLI's own sign-in in a session the operator attaches to."""
    body = await _parsed(request, _SignIn)
    _identity(request)
    if _is_default(request):
        raise _error(
            422,
            "runtime_unavailable",
            "Default keeps its existing CLI sign-ins; manage them with the CLI in a terminal.",
        )
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
