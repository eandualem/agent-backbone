"""Database schema — re-exports metadata from the database service module.

The ORM models in services/database/models.py are the source of truth.
This module provides backward-compatible access to metadata for:
1. BackboneDB.start() — metadata.create_all() for test databases
2. Alembic autogenerate — detects schema changes from ORM model metadata
"""

from __future__ import annotations

from agent_backbone.services.database.base import Base

# Import all ORM models so their tables are registered on Base.metadata
from agent_backbone.services.database.models import (  # noqa: F401
    AcknowledgmentORM,
    AgentStateORM,
    DedupLogORM,
    DeliveryORM,
    HeartbeatORM,
    IssueDependencyORM,
    MessageQueueORM,
)

metadata = Base.metadata
