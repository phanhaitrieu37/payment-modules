"""Enumerations shared by the domain, storage and adapters.

Values are the exact strings stored in the database, so storage code reads them through
``.value`` instead of writing its own literals.
"""

from __future__ import annotations

from enum import StrEnum


def coerce_enum_fields(instance: object, **fields: type[StrEnum]) -> None:
    """Replace plain-string values of a frozen dataclass with their StrEnum members.

    Storage rows carry enum columns as ``str``; coercing at the boundary keeps comparisons
    exact and makes an unknown value raise ``ValueError`` instead of being mis-routed.
    ``None`` is left as is for optional fields.
    """
    for name, enum_type in fields.items():
        value = getattr(instance, name)
        if value is not None:
            object.__setattr__(instance, name, enum_type(value))


class Direction(StrEnum):
    IN = "in"
    OUT = "out"
    UNKNOWN = "unknown"


class Environment(StrEnum):
    TEST = "test"
    LIVE = "live"


class IntentStatus(StrEnum):
    AWAITING_PAYMENT = "awaiting_payment"
    PAID = "paid"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class MatchState(StrEnum):
    RECORDED = "recorded"
    SETTLED = "settled"
    IN_REVIEW = "in_review"
    NOT_APPLICABLE = "not_applicable"
    CLOSED_EXTERNAL = "closed_external"
    DUPLICATE_OF = "duplicate_of"


class InboxStatus(StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    PROCESSED = "processed"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"
    QUARANTINED = "quarantined"


class ReviewReason(StrEnum):
    RECEIVER_UNBOUND = "RECEIVER_UNBOUND"
    NO_REFERENCE = "NO_REFERENCE"
    AMBIGUOUS_REFERENCE = "AMBIGUOUS_REFERENCE"
    TENANT_MISMATCH = "TENANT_MISMATCH"
    ALREADY_PAID = "ALREADY_PAID"
    INTENT_CANCELLED = "INTENT_CANCELLED"
    INTENT_SUPERSEDED = "INTENT_SUPERSEDED"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    LATE = "LATE"
    UNVERIFIED_IDENTITY = "UNVERIFIED_IDENTITY"


class ReviewResolution(StrEnum):
    ATTACH_TO_INTENT = "attach_to_intent"
    MARK_EXTERNAL = "mark_external"
    MARK_DUPLICATE_OF = "mark_duplicate_of"
    BIND_RECEIVER = "bind_receiver"
    ACCEPT_LATE = "accept_late"


class ReconcileMode(StrEnum):
    DETECT_ONLY = "detect_only"
    AUTO_SETTLE = "auto_settle"


class ConnectionStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    NOT_READY = "not_ready"
    DISABLED = "disabled"


class SettlementOrigin(StrEnum):
    AUTO = "auto"
    OPERATOR_REVIEW = "operator_review"


class ProfileKind(StrEnum):
    GENERATED = "generated"
    LEGACY_IMPORT = "legacy_import"


class ProfileStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    ACCEPTED_LEGACY = "accepted_legacy"
    RETIRED = "retired"


class ReadinessStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RETIRED = "retired"


class ObservationSource(StrEnum):
    WEBHOOK = "webhook"
    API = "api"


class LinkStatus(StrEnum):
    LINKED = "linked"
    UNLINKED = "unlinked"
    AMBIGUOUS = "ambiguous"


class LinkMethod(StrEnum):
    SAME_SOURCE_ID = "same_source_id"
    BANK_REFERENCE = "bank_reference"
    OPERATOR = "operator"


class IdentityKind(StrEnum):
    WEBHOOK_ID = "webhook_id"
    API_ID = "api_id"


class FirstSource(StrEnum):
    WEBHOOK = "webhook"
    RECONCILE = "reconcile"


class EventKeyKind(StrEnum):
    PROVIDER_ID = "provider_id"
    BODY_HASH = "body_hash"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    PUBLISHED = "published"
    FAILED = "failed"


class DecisionOutcome(StrEnum):
    SETTLE = "SETTLE"
    REVIEW = "REVIEW"


class ReviewCaseStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class MerchantStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class ReceivingAccountStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    RETIRED = "retired"


class ReconciliationRunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AuthMode(StrEnum):
    HMAC = "hmac"


class ReceiptTimeSource(StrEnum):
    """Where the effective receipt time used for the late check came from."""

    WEBHOOK_RECEIVED = "webhook_received"
    PROVIDER_VERIFIED = "provider_verified"
    OBSERVED = "observed"


class ProcessingErrorCode(StrEnum):
    """The only values stored in inbox ``last_error_code`` and outbox ``last_error``.

    Exception class names go to structured logs, never to these columns, so dashboards and
    alerts can rely on a closed set.
    """

    NORMALIZE_FAILED = "normalize_failed"
    NO_EVENT_KEY = "no_event_key"
    POLICY_VIOLATION = "policy_violation"
    OBSERVATION_CONFLICT = "observation_conflict"
    MAX_ATTEMPTS_EXCEEDED = "max_attempts_exceeded"
    HANDLER_ERROR = "handler_error"
    TRANSIENT_ERROR = "transient_error"
