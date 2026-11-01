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
        # Uvicorn sends WebSocket access lines through its error logger.
        if (
            record.name == "uvicorn.error"
            and isinstance(record.args, tuple)
            and str(record.msg).startswith('%s - "WebSocket %s"')
            and len(record.args) >= 2
        ):
            args = list(record.args)
            args[1] = str(args[1]).partition("?")[0]
            record.args = tuple(args)
        # Uvicorn shares its error logger with the WebSocket protocol library:
        # DEBUG contains raw headers and authentication frames. Keep server
        # lifecycle/access lines and errors, but never enable its wire traces.
        level = logging.WARNING
        if debug:
            if record.name == "agent_backbone" or record.name.startswith("agent_backbone."):
                level = logging.DEBUG
            elif record.name == "uvicorn" or record.name.startswith("uvicorn."):
                level = logging.INFO
        return record.levelno >= level


log_policy = DiagnosticLogFilter()


class DiagnosticFormatter(logging.Formatter):
    """Tracebacks are developer detail, not part of ordinary warning output."""

    def format(self, record: logging.LogRecord) -> str:
        if log_policy.mode != "debug" and not log_policy.verbose:
            record = copy.copy(record)
            if record.exc_info and record.exc_info[0] is not None:
                record.msg = f"{record.getMessage()} ({record.exc_info[0].__name__})"
                record.args = ()
            record.exc_info = record.exc_text = record.stack_info = None
        return super().format(record)


def set_log_mode(mode: str) -> None:
    # An invalid/unavailable policy must not accidentally enable diagnostic output.
    log_policy.mode = (
        mode if isinstance(mode, str) and mode in {"off", "standard", "debug"} else "off"
    )
