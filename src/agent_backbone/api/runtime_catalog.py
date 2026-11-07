"""Provider-owned runtime observations under an immutable operation fence."""

import asyncio

from fastapi import HTTPException

from agent_backbone.api.profile_binding import BINDING_MISMATCH, provider_context_current
from agent_backbone.base import run_thread
from agent_backbone.services.runtimes import RUNTIMES, NativeEnvironment


async def bound_catalog(request):
    provider = getattr(request.state, "profile_provider", None)
    if provider is None:
        raise HTTPException(status_code=409, detail=BINDING_MISMATCH)
    state = request.app.state
    config, db = state.config, state.db

    async def check():
        if (
            state.profile_provider is not provider
            or state.db is not db
            or not await provider_context_current(provider, state.config, db)
            or state.profile_provider is not provider
            or state.db is not db
        ):
            raise HTTPException(status_code=409, detail=BINDING_MISMATCH)

    async def observe():
        loop = asyncio.get_running_loop()
        items = []
        for runtime in RUNTIMES.values():
            await check()
            try:
                environment = runtime.environment()
                if not isinstance(
                    environment, NativeEnvironment
                ) or not environment.root.is_relative_to(provider.reservation.data_dir):
                    environment = None
            except ValueError:
                environment = None

            def probe(runtime=runtime, environment=environment):
                # The executor may start later than admission. Recheck at the
                # actual read/probe boundary, while retaining the owning task.
                asyncio.run_coroutine_threadsafe(check(), loop).result()
                return runtime.bound_discovery(environment)

            items.append(await run_thread(probe))
            await check()
        identity = provider.reservation.identity
        return {
            "schema_version": 2,
            "profile_id": identity.profile_id,
            "installation_id": identity.installation_id,
            "lifecycle_id": identity.lifecycle_id,
            "items": items,
        }

    await check()
    captured = getattr(state, "bound_runtime_catalog", None)
    if captured is None or captured[2].done():
        task = state.api_tasks.create(observe, config=config, db=db, provider=provider)
        state.bound_runtime_catalog = (provider, db, task)
    else:
        if captured[0] is not provider or captured[1] is not db:
            raise HTTPException(status_code=409, detail=BINDING_MISMATCH)
        task = captured[2]
    result = await asyncio.shield(task)
    await check()
    return result
