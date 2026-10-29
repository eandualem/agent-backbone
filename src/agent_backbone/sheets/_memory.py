"""macOS resident-memory sampling; this is a watchdog, not an allocation sandbox."""

import ctypes
import os


class _RusageV0(ctypes.Structure):
    # Apple's public rusage_info_v0 ABI (sys/resource.h).
    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [
        (name, ctypes.c_uint64)
        for name in (
            "user_time",
            "system_time",
            "idle_wakeups",
            "interrupt_wakeups",
            "pageins",
            "wired_size",
            "resident_size",
            "phys_footprint",
            "start",
            "exit",
        )
    ]


def resident_bytes(pid: int) -> int:
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    read = library.proc_pid_rusage
    read.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(_RusageV0)]
    read.restype = ctypes.c_int
    usage = _RusageV0()
    if read(pid, 0, ctypes.byref(usage)):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return usage.resident_size
