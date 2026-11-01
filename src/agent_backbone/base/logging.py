"""Process-local emission policy, selected from the installation settings."""

from __future__ import annotations

import copy
import logging


class DiagnosticLogFilter(logging.Filter):
    """Keep opt-in verbosity separate from optional capture being disabled."""

    mode = "off"  # No output before the installation setting is read.
    verbose = False

    def filter(self, record: logging.LogRecord) -> bool:
        if self.mode == "off":
            return False
        debug = self.mode == "debug" or self.verbose
        if record.name == "uvicorn.access":
            if not debug:
                return False
            # Request query values are not needed to identify the API route.
            if isinstance(record.args, tuple) and len(record.args) == 5:
                args = list(record.args)
                args[2] = str(args[2]).partition("?")[0]
                record.args = tuple(args)
        # Dependency wire/SQL traces can include headers, credentials and bodies.
        owned = record.name.startswith(("agent_backbone", "uvicorn"))
        return record.levelno >= (logging.DEBUG if debug and owned else logging.WARNING)


log_policy = DiagnosticLogFilter()


class DiagnosticFormatter(logging.Formatter):
    """Tracebacks are developer detail, not part of ordinary warning output."""

    def format(self, record: logging.LogRecord) -> str:
        if log_policy.mode != "debug" and not log_policy.verbose:
            record = copy.copy(record)
            record.exc_info = record.exc_text = record.stack_info = None
        return super().format(record)


def set_log_mode(mode: str) -> None:
    # An invalid/unavailable policy must not accidentally enable diagnostic output.
    log_policy.mode = (
        mode if isinstance(mode, str) and mode in {"off", "standard", "debug"} else "off"
    )
