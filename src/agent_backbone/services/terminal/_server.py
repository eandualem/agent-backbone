"""One explicit tmux server per provider process, selected before terminal use."""

from pathlib import Path

_socket: str | None = None
_used = False


def pin_tmux_server(socket_path: Path) -> None:
    """Pin a lifecycle-owned socket without creating or starting its server.

    The control plane must validate ownership before calling this. This is not
    profile activation or a configurable global active-profile pointer. Legacy
    processes keep their existing environment semantics when no socket is pinned.
    Once terminal use begins, even a legacy process cannot change its selection.
    """
    global _socket
    if (
        not socket_path.is_absolute()
        or ".." in socket_path.parts
        or any(ord(c) < 0x20 or ord(c) == 0x7F for c in str(socket_path))
    ):
        raise ValueError("The tmux socket must be an absolute normalized path")
    resolved = str(socket_path.resolve())
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in resolved):
        raise ValueError("The tmux socket must be an absolute normalized path")
    if _socket == resolved:
        return
    if _socket is not None or _used:
        raise ValueError("The process tmux server selection is already fixed")
    _socket = resolved


def tmux_server_socket() -> str | None:
    """Return the pinned locator; None means legacy environment selection."""
    return _socket


def tmux_command(*args: str) -> list[str]:
    """Freeze selection before an operation can yield or wait for a subprocess."""
    global _used
    _used = True
    return ["tmux", *(["-S", _socket] if _socket is not None else []), *args]
