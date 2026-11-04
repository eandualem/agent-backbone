"""Immutable schema-2 setup invocation over a captured operator connection."""

import asyncio
import json
from dataclasses import dataclass, field

import httpx

from agent_backbone.cli._profile_client import MAX_RESPONSE, Binding, Refused, _object

SETUP_SECONDS = 120


@dataclass(frozen=True)
class PendingSetup:
    binding: Binding
    body: bytes = field(repr=False)
    request_id: str
    action: str

    @classmethod
    def capture(cls, binding, body):
        from pydantic import TypeAdapter

        from agent_backbone.models import (
            SETUP_BODY_BYTES,
            SETUP_CANCEL_BODY_BYTES,
            BoundSetupRequest,
        )

        try:
            if not isinstance(body, bytes) or len(body) > SETUP_CANCEL_BODY_BYTES:
                raise ValueError("Invalid setup bytes")
            data = json.loads(body.decode("utf-8"), object_pairs_hook=_object)
            request = TypeAdapter(BoundSetupRequest).validate_python(data)
            if request.action == "first_agent" and len(body) > SETUP_BODY_BYTES:
                raise ValueError("Setup input exceeds bounds")
        except (ValueError, RecursionError):
            raise Refused("invalid_request") from None
        if (
            request.profile_id,
            request.expected_installation_id,
            request.expected_lifecycle_id,
        ) != (binding.profile_id, binding.installation_id, binding.lifecycle_id):
            raise Refused("binding_mismatch")
        return cls(binding, body, request.request_id, request.action)

    def cancellation(self):
        """Change only the action; retain the original body values and binding."""
        if self.action == "cancel_first_agent":
            return self
        body = json.loads(self.body)
        body["action"] = "cancel_first_agent"
        return PendingSetup.capture(
            self.binding, json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
        )

    async def execute(self):
        """No automatic replay or refreshed connection after an uncertain effect."""
        await self.binding.handshake()
        try:
            async with asyncio.timeout(SETUP_SECONDS):
                return await self._post()
        except (httpx.HTTPError, OSError, TimeoutError, ValueError, RecursionError) as error:
            if isinstance(error, Refused):
                raise
            raise Refused("outcome_unknown", uncertain=True) from None

    async def _post(self):
        from agent_backbone.models import SetupResult

        path = "/api/setup/first-agent"
        if self.action == "cancel_first_agent":
            path += "/cancel"
        transport = httpx.AsyncHTTPTransport(uds=self.binding.endpoint, retries=0)
        async with (
            httpx.AsyncClient(
                transport=transport, trust_env=False, follow_redirects=False, timeout=SETUP_SECONDS
            ) as client,
            client.stream(
                "POST",
                "http://bound-provider" + path,
                content=self.body,
                headers={
                    **self.binding.headers(),
                    "Content-Type": "application/json",
                    "Accept-Encoding": "identity",
                },
            ) as response,
        ):
            if response.status_code in (401, 403):
                raise Refused("operator_required")
            if response.headers.get("content-encoding", "identity") != "identity":
                raise Refused("outcome_unknown", uncertain=True)
            raw = bytearray()
            async for chunk in response.aiter_raw(chunk_size=4096):
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE:
                    raise Refused("outcome_unknown", uncertain=True)
            result = json.loads(raw, object_pairs_hook=_object)
            if (
                response.status_code == 409
                and isinstance(result, dict)
                and isinstance(result.get("detail"), dict)
                and result["detail"].get("code") in {"binding_mismatch", "binding_required"}
            ):
                raise Refused("binding_mismatch")
            if response.status_code != 200:
                raise Refused("outcome_unknown", uncertain=True)
        result = SetupResult.model_validate(result).model_dump()
        if result["action"] != self.action:
            raise Refused("outcome_unknown", uncertain=True)
        return result
