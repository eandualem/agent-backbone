"""Gateway entry point — starts the backbone API via uvicorn.

All webhook handling, dedup, and routing logic has moved to the FastAPI app
(api/app.py, api/routes/webhook.py, api/webhook_utils.py) and the persistence
service (BackboneDB.is_duplicate, BackboneDB.load_dedup_cache).
"""

from __future__ import annotations

import logging


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    import uvicorn

    from agent_backbone.api.app import create_app
    from agent_backbone.settings import AppSettings

    settings = AppSettings()
    config = settings.build_config()
    app = create_app()
    uvicorn.run(app, host="127.0.0.1", port=config.gateway.port, log_level="info")


if __name__ == "__main__":
    main()
