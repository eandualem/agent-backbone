"""One macOS keychain per isolated profile.

On macOS, CLIs keep sign-ins in the user's default keychain, which the Security
framework finds through ``HOME``. A profile's own homes have none, so a sign-in
cannot be saved. Each profile gets its own keychain, made the default of every
home in the profile and unlocked when the profile starts. Its password stays in
the profile's ``.env``. Other platforms keep sign-ins in files and need nothing.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

from dotenv import dotenv_values

PASSWORD_KEY = "BACKBONE_KEYCHAIN_PASSWORD"
# Not "login": macOS ties a keychain of that name to the account password.
KEYCHAIN = Path("keychain") / "profile.keychain-db"


def prepare_profile_keychain(root: Path, homes: Iterable[Path]) -> Path | None:
    """Create once, then on every start unlock and select the profile's keychain."""
    if sys.platform != "darwin":
        return None
    root = Path(root)
    homes = list(homes)
    path = root / KEYCHAIN
    if any(char in str(path) for char in '"\n') or not homes:
        raise ValueError("Profile keychain path is unusable")
    password = _password(root / ".env")
    path.parent.mkdir(mode=0o700, exist_ok=True)
    if not path.exists():
        _security(homes[0], f'create-keychain -p {password} "{path}"')
    # Unlock first: changing a locked keychain's settings asks for authorization.
    _security(homes[0], f'unlock-keychain -p {password} "{path}"')
    # No timeout and no lock on sleep: agents run unattended.
    _security(homes[0], f'set-keychain-settings "{path}"')
    for home in homes:
        # Without this directory the setting is silently not saved.
        (home / "Library" / "Preferences").mkdir(mode=0o700, parents=True, exist_ok=True)
        _security(home, f'default-keychain -d user -s "{path}"')
        _security(home, f'list-keychains -d user -s "{path}"')
        if str(path) not in _security(home, "default-keychain -d user"):
            raise ValueError("Profile keychain did not become the default")
    return path


def _password(env_file: Path) -> str:
    password = dotenv_values(env_file).get(PASSWORD_KEY)
    if password:
        return password
    password = secrets.token_hex(32)
    separator = "" if env_file.read_bytes().endswith(b"\n") else "\n"
    descriptor = os.open(env_file, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "a") as handle:
        handle.write(f"{separator}{PASSWORD_KEY}={password}\n")
        handle.flush()
        os.fsync(descriptor)
    return password


def _security(home: Path, command: str) -> str:
    """One command on stdin, so the password never appears in a process listing."""
    result = subprocess.run(
        ["/usr/bin/security", "-i"],
        input=command + "\n",
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        verb = command.split(" ", 1)[0]
        raise ValueError(f"Profile keychain {verb} failed: {result.stderr.strip()[:200]}")
    return result.stdout
