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
from agent_backbone.services.database._current_plans_repo import CurrentPlanError
from agent_backbone.services.database._diagnostics_repo import diagnostic_details
from agent_backbone.services.database._documents_repo import (
    OPERATOR_AUTHOR,
    DocumentCapacity,
    DocumentConflict,
    DocumentRateLimit,
)
from agent_backbone.services.database._profile_default import (
    create_default_profile,
    default_tmux_socket,
)
from agent_backbone.services.database._profile_identity_repo import ProfileIdentity
from agent_backbone.services.database._profile_operations import (
    ProfileOperations,
    validate_profile_operation,
)
from agent_backbone.services.database._profile_provider import (
    ProfileProvider,
    reconcile_profile_provider,
)
from agent_backbone.services.database._profile_registry import (
    ProfileBinding,
    ProfileRegistry,
    ProfileReservation,
)
from agent_backbone.services.database._profile_startup import check_profile_startup
from agent_backbone.services.database._reports_repo import (
    ReportConflict,
    ReportForbidden,
    ReportRateLimit,
)
from agent_backbone.services.database._setup_receipts import (
    BoundSetupReceipt,
    SetupReceipt,
    SetupReceiptError,
)
from agent_backbone.services.database._signing_repo import RECEIPT_RETENTION_DAYS
from agent_backbone.services.database._time import format_iso, now_iso, parse_iso
from agent_backbone.services.database.backbone_db import BackboneDB
from agent_backbone.services.database.base import Base
from agent_backbone.services.database.engine import build_engine

__all__ = [
    "CONVERSATION_KINDS",
    "CONVERSATION_TEXT_CHARACTERS",
    "OPERATOR_AUTHOR",
    "RECEIPT_RETENTION_DAYS",
    "BackboneDB",
    "Base",
    "BoundSetupReceipt",
    "ConversationRefused",
    "CurrentPlanError",
    "DocumentCapacity",
    "DocumentConflict",
    "DocumentRateLimit",
    "Limits",
    "ProfileBinding",
    "ProfileIdentity",
    "ProfileOperations",
    "ProfileProvider",
    "ProfileRegistry",
    "ProfileReservation",
    "ReportConflict",
    "ReportForbidden",
    "ReportRateLimit",
    "SetupReceipt",
    "SetupReceiptError",
    "UnknownArtifact",
    "build_engine",
    "check_profile_startup",
    "create_default_profile",
    "default_tmux_socket",
    "diagnostic_details",
    "format_iso",
    "now_iso",
    "parse_iso",
    "reconcile_profile_provider",
    "validate_profile_operation",
]
