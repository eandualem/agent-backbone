"""Database service — engine lifecycle, ORM models, session management, persistence."""

from agent_backbone.services.database.backbone_db import BackboneDB
from agent_backbone.services.database.base import Base
from agent_backbone.services.database.config import DatabaseConfig
from agent_backbone.services.database.exceptions import DatabaseError, PersistenceError
from agent_backbone.services.database.interface import DatabaseService
from agent_backbone.services.database.models import (
    AcknowledgmentORM,
    AgentActivityORM,
    AgentStateORM,
    DedupLogORM,
    DeliveryORM,
    HeartbeatORM,
    IssueDependencyORM,
    MessageQueueORM,
    TelemetryCheckpointORM,
)

__all__ = [
    "AcknowledgmentORM",
    "AgentActivityORM",
    "AgentStateORM",
    "BackboneDB",
    "Base",
    "DatabaseConfig",
    "DatabaseError",
    "DatabaseService",
    "DedupLogORM",
    "DeliveryORM",
    "HeartbeatORM",
    "IssueDependencyORM",
    "MessageQueueORM",
    "PersistenceError",
    "TelemetryCheckpointORM",
]
