"""Bound catalog transport; the requesting CLI never inspects native runtimes."""

import asyncio
import json
import sys
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from agent_backbone.cli._profile_client import (
    MAX_RESPONSE,
    Binding,
    Refused,
    _object,
    resolve_binding,
)


class _Compatibility(BaseModel):
    model_config = ConfigDict(strict=True)
    compatible: bool
    reason: str
    message: str
    path: str | None
    version: str | None
    version_error: str | None


class _Unattended(BaseModel):
    model_config = ConfigDict(strict=True)
    supported: bool
    args: list[str]
    sandboxed: bool | None


class _Item(BaseModel):
    model_config = ConfigDict(strict=True)
    id: str
    display_name: str
    available: bool
    setup_supported: bool
    reports_state: str
    models: list[str]
    efforts: list[str]
    unattended: _Unattended
    authentication: Literal[
        "unknown",
        "not_applicable",
        "not_authenticated",
        "authenticated",
        "unavailable",
        "unsupported",
    ]
    setup_compatibility: _Compatibility


class _Catalog(BaseModel):
    model_config = ConfigDict(strict=True)
    schema_version: Literal[2]
    profile_id: str
    installation_id: str
    lifecycle_id: str
    items: list[_Item] = Field(max_length=64)


async def catalog(binding: Binding):
    await binding.handshake()
    try:
        async with asyncio.timeout(120):
            transport = httpx.AsyncHTTPTransport(uds=binding.endpoint, retries=0)
            async with (
                httpx.AsyncClient(
                    transport=transport, trust_env=False, follow_redirects=False, timeout=120
                ) as client,
                client.stream(
                    "GET",
                    "http://bound-provider/api/runtimes?schema_version=2&check_setup=true",
                    headers={**binding.headers(), "Accept-Encoding": "identity"},
                ) as response,
            ):
                if response.status_code in (401, 403):
                    raise Refused("operator_required")
                if response.status_code == 409:
                    raise Refused("binding_mismatch")
                if response.status_code != 200:
                    raise Refused("service_unavailable")
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise Refused("binding_mismatch")
                raw = bytearray()
                async for chunk in response.aiter_raw(chunk_size=4096):
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE:
                        raise Refused("binding_mismatch")
            body = json.loads(raw, object_pairs_hook=_object)
            if not isinstance(body, dict) or type(body.get("schema_version")) is not int:
                raise Refused("binding_mismatch")
            result = _Catalog.model_validate(body)
            if (result.profile_id, result.installation_id, result.lifecycle_id) != (
                binding.profile_id,
                binding.installation_id,
                binding.lifecycle_id,
            ) or len({item.id for item in result.items}) != len(result.items):
                raise Refused("binding_mismatch")
            return result.model_dump()
    except Refused:
        raise
    except (httpx.HTTPError, OSError, TimeoutError):
        raise Refused("service_unavailable") from None
    except (ValueError, RecursionError):
        raise Refused("binding_mismatch") from None


def cmd_bound_runtimes(args):
    expected = (args.profile, args.expected_installation_id, args.expected_lifecycle_id)
    if not all(expected) or not args.json or not args.check_setup:
        print(
            "Bound runtime discovery requires --json --check-setup and the complete identity.",
            file=sys.stderr,
        )
        return 2
    try:
        result = asyncio.run(catalog(resolve_binding(expected)))
    except Refused as exc:
        print("Bound runtime discovery is unavailable: " + exc.code + ".", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0
