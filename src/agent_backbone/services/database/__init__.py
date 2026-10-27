"""Database service — engine lifecycle, ORM models and the persistence API."""

from agent_backbone.services.database._conversations_repo import (
    KINDS as CONVERSATION_KINDS,
)
from agent_backbone.services.database._conversations_repo import (
    TEXT_CHARACTERS as CONVERSATION_TEXT_CHARACTERS,
)
from agent_backbone.services.database._conversations_repo import (
    ConversationRefused,
    Limits,
    UnknownArtifact,
)
from agent_backbone.services.database._diagnostics_repo import diagnostic_details
from agent_backbone.services.database._documents_repo import (
    DocumentCapacity,
    DocumentConflict,
    DocumentRateLimit,
)
from agent_backbone.services.database._reports_repo import (
    ReportConflict,
    ReportForbidden,
    ReportRateLimit,
)
from agent_backbone.services.database._signing_repo import RECEIPT_RETENTION_DAYS
from agent_backbone.services.database._time import format_iso, now_iso, parse_iso
from agent_backbone.services.database.backbone_db import BackboneDB
from agent_backbone.services.database.base import Base
from agent_backbone.services.database.engine import build_engine

__all__ = [
    "CONVERSATION_KINDS",
    "CONVERSATION_TEXT_CHARACTERS",
    "RECEIPT_RETENTION_DAYS",
    "BackboneDB",
    "Base",
    "ConversationRefused",
    "DocumentCapacity",
    "DocumentConflict",
    "DocumentRateLimit",
    "Limits",
    "ReportConflict",
    "ReportForbidden",
    "ReportRateLimit",
    "UnknownArtifact",
    "build_engine",
    "diagnostic_details",
    "format_iso",
    "now_iso",
    "parse_iso",
]
