"""Start and stop one registered provider process from the local control plane."""

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time

from agent_backbone.api.local_transport import peer_pid
from agent_backbone.cli._profile_client import Binding, Refused, resolve_dotenv_reference
from agent_backbone.services.database import ProfileRegistry

START_SECONDS = 30
STOP_SECONDS = 60
_POLL_SECONDS = 0.1


def _binding(registry: ProfileRegistry, reservation) -> Binding:
    identity = reservation.identity
    return Binding.capture(
        registry.show(identity.profile_id),
        (identity.profile_id, identity.installation_id, identity.lifecycle_id),
        resolve_dotenv_reference,
    )


def _answers(binding: Binding) -> bool:
    """Whether this exact identity is serving. Another identity is an error, not absence."""
    try:
        asyncio.run(binding.handshake())
    except Refused as exc:
        if exc.code != "service_unavailable":
            raise
        return False
    return True


def _connect(endpoint: str) -> socket.socket | None:
    connection = socket.socket(socket.AF_UNIX)
    try:
        connection.connect(endpoint)
    except (FileNotFoundError, ConnectionRefusedError):
        connection.close()
        return None
    return connection


class ProviderServices:
    """Effects behind lifecycle receipts; the operation journal serializes callers."""

    def __init__(self, registry: ProfileRegistry):
        self.registry = registry

    def start(self, reservation) -> None:
        binding = _binding(self.registry, reservation)
        if _answers(binding):
            return
        root = reservation.data_dir
        adopted = reservation.kind == "legacy_default"
        (root / "state").mkdir(mode=0o700, exist_ok=True)
        log = os.open(
            root / "state" / "provider.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
        )
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    *(["-I"] if sys.flags.isolated else []),
                    "-B",
                    "-m",
                    "agent_backbone.cli",
                    *(
                        ["up"]
                        if adopted
                        else [
                            "profiles",
                            "serve",
                            "--registry",
                            str(self.registry.path),
                            reservation.identity.profile_id,
                        ]
                    ),
                ],
                # The adopted installation keeps the environment it has always had.
                env={**os.environ, "BACKBONE_DATA_DIR": str(root)}
                if adopted
                else {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL") if k in os.environ},
                cwd=root if adopted else root / "home",
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        finally:
            os.close(log)
        deadline = time.monotonic() + START_SECONDS
        while time.monotonic() < deadline and process.poll() is None:
            if _answers(binding):
                return
            time.sleep(_POLL_SECONDS)
        if process.poll() is None:
            process.terminate()
        raise ValueError("Provider did not start")

    def stop(self, reservation) -> None:
        """Signal only the process the kernel reports behind this verified endpoint."""
        binding = _binding(self.registry, reservation)
        connection = _connect(binding.endpoint)
        if connection is None:
            return
        with connection:
            asyncio.run(binding.handshake())
            pid = peer_pid(connection)
        os.kill(pid, signal.SIGTERM)
        # The socket file can outlive the process: uvicorn replays SIGTERM after
        # shutdown. Refused connections, not a missing file, mean it has stopped.
        deadline = time.monotonic() + STOP_SECONDS
        while time.monotonic() < deadline:
            remaining = _connect(binding.endpoint)
            if remaining is None:
                return
            remaining.close()
            time.sleep(_POLL_SECONDS)
        raise ValueError("Provider did not stop")
