"""Database service — engine lifecycle, ORM models, session management, persistence."""

from agent_backbone.services.database.backbone_db import BackboneDB
from agent_backbone.services.database.base import Base
from agent_backbone.services.database.config import DatabaseConfig
from agent_backbone.services.database.exceptions import DatabaseError, PersistenceError
from agent_backbone.services.database.interface import DatabaseService, build_engine
from agent_backbone.services.database.models import (
    AcknowledgmentORM,
    AgentStateORM,
    DedupLogORM,
    DeliveryORM,
    IssueDependencyORM,
    MessageQueueORM,
)

__all__ = [
    "AcknowledgmentORM",
    "AgentStateORM",
    "BackboneDB",
    "Base",
    "DatabaseConfig",
    "DatabaseError",
    "DatabaseService",
    "DedupLogORM",
    "DeliveryORM",
    "IssueDependencyORM",
    "MessageQueueORM",
    "PersistenceError",
    "build_engine",
]
