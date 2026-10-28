"""No native spreadsheet code runs in the server process."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from asyncio.subprocess import create_subprocess_exec

from agent_backbone.sheets._errors import SheetError
from agent_backbone.sheets._models import canonical_json

WORKER_BYTES = 16 * 1024 * 1024
WORKER_SECONDS = 10
WORKER_MEMORY_BYTES = 512 * 1024 * 1024


class SheetCalculator:
    """One executing worker per daemon; contention is explicit, never an unseen queue."""

    def __init__(self):
        self._busy = False

    async def run(self, request: dict) -> dict:
        if self._busy:
            raise SheetError("calculation_busy", "a sheet calculation is already running", 503)
        body = canonical_json(request)
        if len(body) > 2 * WORKER_BYTES:
            raise SheetError("calculation_limit", "calculation input exceeds byte limit")
        self._busy = True
        process = None
        scratch = None
        try:
            scratch = tempfile.TemporaryDirectory(prefix="backbone-sheet-worker-")
            process = await create_subprocess_exec(
                sys.executable,
                "-I" if sys.flags.isolated else "-P",
                "-B",
                "-m",
                "agent_backbone.sheets._worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                # Do not inherit agent credentials or live installation settings.
                env={"LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8", "TMPDIR": scratch.name},
            )

            async def monitor_memory():
                if sys.platform != "darwin":
                    await asyncio.Future()
                from agent_backbone.sheets._memory import resident_bytes

                while process.returncode is None:
                    try:
                        size = resident_bytes(process.pid)
                    except ProcessLookupError:
                        return
                    except OSError as exc:
                        raise SheetError(
                            "calculation_failed", "cannot monitor spreadsheet memory"
                        ) from exc
                    if size > WORKER_MEMORY_BYTES:
                        raise SheetError(
                            "calculation_limit", "spreadsheet memory threshold exceeded"
                        )
                    await asyncio.sleep(0.025)

            async def exchange():
                async def send():
                    process.stdin.write(body)
                    await process.stdin.drain()
                    process.stdin.close()

                async def receive():
                    output = bytearray()
                    while chunk := await process.stdout.read(65536):
                        if len(output) + len(chunk) > WORKER_BYTES:
                            raise SheetError("calculation_limit", "calculation output is too large")
                        output.extend(chunk)
                    return output

                sender = asyncio.create_task(send())
                try:
                    output = await receive()
                    await sender
                    await process.wait()
                    return output
                finally:
                    if not sender.done():
                        sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)

            worker = asyncio.create_task(exchange())
            monitor = asyncio.create_task(monitor_memory())
            try:
                async with asyncio.timeout(WORKER_SECONDS):
                    done, _ = await asyncio.wait(
                        {worker, monitor}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if monitor in done:
                        monitor.result()
                    output = await worker
            finally:
                for task in (worker, monitor):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(worker, monitor, return_exceptions=True)
            if process.returncode:
                raise SheetError(
                    "calculation_failed", "spreadsheet engine stopped without a result"
                )
            try:
                result = json.loads(output)
            except (ValueError, UnicodeError) as exc:
                raise SheetError("calculation_failed", "invalid spreadsheet engine result") from exc
            if "error" in result:
                error = result["error"]
                raise SheetError(error["code"], error["message"])
            return result
        except TimeoutError as exc:
            raise SheetError(
                "calculation_limit", "spreadsheet calculation exceeded 10 seconds"
            ) from exc
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise SheetError(
                "calculation_failed", "spreadsheet engine stopped without a result"
            ) from exc
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            self._busy = False
            if scratch is not None:
                scratch.cleanup()
