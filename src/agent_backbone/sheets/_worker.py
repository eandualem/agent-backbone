"""Bound resources before importing the native engine; communicate only bounded JSON."""

import json
import resource
import sys

from agent_backbone.sheets._calculation import WORKER_BYTES
from agent_backbone.sheets._errors import SheetError
from agent_backbone.sheets._models import canonical_json, checked_warnings


def main():
    memory = 512 * 1024 * 1024
    if sys.platform != "darwin":
        resource.setrlimit(resource.RLIMIT_DATA, (memory, memory))
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (WORKER_BYTES, WORKER_BYTES))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    try:
        raw = sys.stdin.buffer.read(2 * WORKER_BYTES + 1)
        if len(raw) > 2 * WORKER_BYTES:
            raise SheetError("calculation_limit", "calculation input exceeds byte limit")
        from agent_backbone.sheets._engine import execute

        result = execute(json.loads(raw))
        if "warnings" in result:
            checked_warnings(result["warnings"])
        output = canonical_json(result)
        if len(output) > WORKER_BYTES:
            raise SheetError("calculation_limit", "calculation result exceeds byte limit")
    except MemoryError:
        output = canonical_json(
            {"error": {"code": "calculation_limit", "message": "spreadsheet memory limit exceeded"}}
        )
    except SheetError as exc:
        output = canonical_json({"error": exc.detail})
    except Exception:
        # Native errors can contain arbitrary imported text and private paths.
        output = canonical_json(
            {
                "error": {
                    "code": "calculation_failed",
                    "message": "spreadsheet engine could not complete this operation",
                }
            }
        )
    sys.stdout.buffer.write(output)


if __name__ == "__main__":
    main()
