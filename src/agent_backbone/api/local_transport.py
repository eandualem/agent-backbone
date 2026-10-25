"""Optional private Unix listener with OS peer identity checks."""

from __future__ import annotations

import ctypes
import os
import socket
import stat
import struct
import sys
from contextlib import contextmanager
from pathlib import Path

from uvicorn.protocols.http.h11_impl import H11Protocol


def peer_uid(connection: socket.socket) -> int:
    """The connected Unix peer's kernel-reported effective UID."""
    if sys.platform == "darwin":
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        libc = ctypes.CDLL(None, use_errno=True)
        getpeereid = libc.getpeereid
        getpeereid.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
        ]
        getpeereid.restype = ctypes.c_int
        if getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return uid.value
    if hasattr(socket, "SO_PEERCRED"):
        credentials = connection.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
        return struct.unpack("3i", credentials)[1]
    raise OSError("Unix peer identity is unavailable on this platform")


class LocalPeerProtocol(H11Protocol):
    """Refuse a Unix peer before parsing HTTP; TCP keeps its existing auth rules."""

    def connection_made(self, transport):
        self._peer_allowed = False
        super().connection_made(transport)
        connection = transport.get_extra_info("socket")
        try:
            self._peer_allowed = connection is not None and (
                connection.family != socket.AF_UNIX or peer_uid(connection) == os.geteuid()
            )
        except OSError:
            self._peer_allowed = False
        if not self._peer_allowed:
            transport.close()

    def data_received(self, data):
        if self._peer_allowed:
            super().data_received(data)


@contextmanager
def private_listener(data_dir: Path, location: str):
    """Bind only an owned 0600 socket inside a private directory under data_dir."""
    path = Path(location).expanduser()
    root = data_dir.resolve()
    if not path.is_absolute() or not path.parent.resolve().is_relative_to(root):
        raise ValueError("backbone.socket_path must be absolute and inside the data directory")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.stat()
    if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) != 0o700:
        raise ValueError("the socket parent must be owned by this account with mode 0700")
    if path.exists() or path.is_symlink():
        previous = path.lstat()
        if (
            not stat.S_ISSOCK(previous.st_mode)
            or previous.st_uid != os.geteuid()
            or stat.S_IMODE(previous.st_mode) != 0o600
        ):
            raise ValueError("refusing to replace an unsafe socket path")
        with socket.socket(socket.AF_UNIX) as probe:
            try:
                probe.connect(str(path))
            except ConnectionRefusedError:
                path.unlink()  # private parent; this is an owned, inactive socket
            else:
                raise ValueError("the local socket is already in use")
    connection = socket.socket(socket.AF_UNIX)
    identity = None
    try:
        connection.bind(str(path))
        path.chmod(0o600)
        identity = path.lstat().st_ino
        connection.set_inheritable(True)
        yield connection
    finally:
        connection.close()
        if identity is not None:
            try:
                if path.lstat().st_ino == identity:
                    path.unlink()
            except FileNotFoundError:
                pass
