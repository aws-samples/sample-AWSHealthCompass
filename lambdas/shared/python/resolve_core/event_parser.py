"""Event parsing and date normalization utilities for AWS Health events.

This module provides pure-Python functions for parsing, normalizing, and
extracting fields from AWS Health EventBridge events and Health API responses.
All functions are stateless, side-effect-free, and never raise exceptions to
the caller — malformed input returns a documented safe default.

Consumers: Processor Lambda, Reconciliation Lambda, API Lambda.
Dependencies: Python stdlib only (no boto3, no third-party packages).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, List, NamedTuple, Optional

logger = logging.getLogger("resolve_core")

# --- Module-level constants (immutable) ---

_REGION_PATTERN = re.compile(r"[a-z]{2}(-[a-z]+-\d+)?")
_SECURITY_SERVICES: frozenset[str] = frozenset(
    {"RISK", "ABUSE", "IAM", "GUARDDUTY"}
)

_KNOWN_ACTIONABILITY_VALUES: frozenset[str] = frozenset(  # SEC-R-5
    {"ACTION_REQUIRED", "ACTION_MAY_BE_REQUIRED", "INFORMATIONAL"}
)

_SERVICE_ARN_MAP: dict[str, str] = {
    "EC2": "arn:aws:ec2:{region}:{account_id}:instance/{resource_id}",
    "EBS": "arn:aws:ec2:{region}:{account_id}:volume/{resource_id}",
    "S3": "arn:aws:s3:::{resource_id}",
    "RDS": "arn:aws:rds:{region}:{account_id}:db:{resource_id}",
}

_MAX_DATE_LEN = 256
_MAX_DESC_LEN = 32768
_MAX_PAGE_STR_LEN = 10
_PAGE_UPPER_BOUND = 100
_MAX_LOG_EVENT_ARN = 512
# --- NamedTuple return types (ISSUE-2, ISSUE-4) ---

class ActionabilityResult(NamedTuple):
    """Return type for :func:`infer_actionability`."""
    value: str
    was_inferred: bool


class PageInfo(NamedTuple):
    """Return type for :func:`coerce_page_fields`."""
    page: int
    total_pages: int


# --- Public API ---

__all__ = [
    "parse_health_date",
    "extract_entities",
    "generate_resource_arn",
    "extract_description",
    "coerce_page_fields",
    "infer_actionability",
    "extract_account_tags",
    "extract_resource_tags",
    "should_filter_backup_event",
    "ActionabilityResult",
    "PageInfo",
]


def parse_health_date(value: Any) -> Optional[str]:
    """Normalize a Health event date to ISO 8601 string.

    Handles RFC 2822 (``"Mon, 22 Jan 2024 14:00:00 GMT"``), ISO 8601
    (``"2024-01-22T14:00:00Z"``), and Python ``datetime`` objects.

    Args:
        value: Date value from a Health event. Any type accepted.

    Returns:
        ISO 8601 string with ``Z`` suffix, or ``None`` if *value* is
        ``None``, empty, or unparseable.
    """
    if value is None:
        return None

    # Handle datetime objects (from boto3 Health API responses)
    if isinstance(value, datetime):
        return _datetime_to_iso(value)  # isoformat only

    if not isinstance(value, str):
        return None

    if not value.strip():
        return None

    value = value[:_MAX_DATE_LEN]
    # ISO 8601 detection: look for the date-time separator pattern
    # (digit-T-digit) to avoid false positives from "GMT" in RFC 2822.
    if re.search(r"\dT\d", value):
        return _normalize_iso_string(value)

    # RFC 2822 via stdlib (no custom regex)
    try:
        dt = parsedate_to_datetime(value)
        return _datetime_to_iso(dt)
    except (ValueError, TypeError):
        logger.warning(
            "Unparseable date value — error_code=PROC_DATE_PARSE_FAILED "
            "input_type=%s",
            type(value).__name__,
        )
        return None


def extract_entities(detail: Any) -> List[dict]:
    """Extract affected entities from a Health event detail object.

    Checks ``affectedEntities`` first, then falls back to
    ``affectedResources``. Non-dict elements are filtered out (ISSUE-3).

    Args:
        detail: The ``detail`` dict from a Health EventBridge event.

    Returns:
        List of entity dicts, or ``[]`` if neither field is present,
        the input is not a dict, or the field values are not lists.
    """
    if not isinstance(detail, dict):
        return []

    entities = detail.get("affectedEntities")
    used_variant = False

    if not isinstance(entities, list) or not entities:
        resources = detail.get("affectedResources")
        if isinstance(resources, list) and resources:
            entities = resources
            used_variant = True
        else:
            return []

    # Filter non-dict elements (ISSUE-3)
    result = [e for e in entities if isinstance(e, dict)]

    if used_variant:
        event_arn = _sanitize_log_value(
            detail.get("eventArn", "unknown"), _MAX_LOG_EVENT_ARN
        )
        logger.warning(
            "Used 'affectedResources' variant field — "
            "event_arn=%s entity_count=%d",
            event_arn,
            len(result),
        )  # count only, no entity values

    # log when both variants are present
    if (
        isinstance(detail.get("affectedEntities"), list)
        and detail["affectedEntities"]
        and isinstance(detail.get("affectedResources"), list)
        and detail["affectedResources"]
    ):
        logger.info(
            "Both affectedEntities (%d) and affectedResources (%d) present "
            "— using affectedEntities",
            len(detail["affectedEntities"]),
            len(detail["affectedResources"]),
        )

    return result


def generate_resource_arn(
    service: str,
    region: str,
    resource_id: str,
    account_id: str,
) -> str:
    """Generate a proper ARN from a bare resource identifier.

    Supported services: EC2 (instance), EBS (volume), S3 (bucket), RDS (db).
    Unknown services return *resource_id* unchanged.
    Values already starting with ``arn:`` are returned unchanged.

    Args:
        service: AWS service name (e.g. ``"EC2"``). Case-insensitive.
        region: AWS region (e.g. ``"us-east-1"``).
        resource_id: The ``entityValue`` from the Health event.
        account_id: 12-digit AWS account ID.

    Returns:
        A well-formed ARN string, or the original *resource_id* if the
        service is unsupported, the value is already an ARN, or input
        validation fails.
    """
    if not isinstance(resource_id, str) or not resource_id:
        return resource_id if isinstance(resource_id, str) else ""

    # Strip control characters
    resource_id = _strip_control_chars(resource_id)

    # Already an ARN — pass through
    if resource_id.startswith("arn:"):
        return resource_id

    # Validate inputs — return resource_id on failure
    if not isinstance(service, str) or not service.replace("_", "").isalnum():
        logger.debug("ARN generation skipped: invalid service format")
        return resource_id

    if not isinstance(region, str) or not _REGION_PATTERN.fullmatch(region):
        logger.debug("ARN generation skipped: invalid region format")
        return resource_id

    if (
        not isinstance(account_id, str)
        or len(account_id) != 12
        or not account_id.isdigit()
    ):
        logger.debug("ARN generation skipped: invalid account_id format")
        return resource_id

    template = _SERVICE_ARN_MAP.get(service.upper())
    if template is None:
        return resource_id

    return template.format(
        region=region, account_id=account_id, resource_id=resource_id
    )


def extract_description(event_description: Any) -> str:
    """Extract ``latestDescription`` from a Health event description array.

    Prefers the ``en_US`` language entry; falls back to the first element.
    Callers must escape the returned text for their output context
    (ADF, HTML, etc.) — this function returns raw text.

    Args:
        event_description: The ``eventDescription`` array from the event
            detail, or ``None``.

    Returns:
        The description string, or ``""`` if the input is missing, empty,
        or malformed.
    """
    if isinstance(event_description, str):
        return _sanitize_description(event_description)

    if not isinstance(event_description, list) or not event_description:
        return ""

    # Prefer en_US entry
    for entry in event_description:
        if isinstance(entry, dict) and entry.get("language") == "en_US":
            desc = entry.get("latestDescription", "")
            if isinstance(desc, str):
                return _sanitize_description(desc)

    # Fall back to first element
    first = event_description[0]
    if isinstance(first, dict):
        desc = first.get("latestDescription", "")
        if isinstance(desc, str):
            return _sanitize_description(desc)

    if isinstance(first, str):
        return _sanitize_description(first)

    return ""


def coerce_page_fields(detail: Any) -> PageInfo:
    """Convert ``page`` and ``totalPages`` from string to int.

    EventBridge delivers these as strings (``"1"``); the Health API
    delivers them as integers. This function normalises both to ints
    clamped to ``[1, 100]``.

    Args:
        detail: The ``detail`` dict from a Health EventBridge event.

    Returns:
        A :class:`PageInfo` named tuple ``(page, total_pages)`` defaulting
        to ``PageInfo(1, 1)`` if the input is missing or malformed.
    """
    if not isinstance(detail, dict):
        return PageInfo(1, 1)

    page = _coerce_int(detail.get("page"), "page")
    total_pages = _coerce_int(detail.get("totalPages"), "totalPages")
    return PageInfo(page, total_pages)


def infer_actionability(detail: Any) -> ActionabilityResult:
    """Return the actionability value, inferring it when absent.

    When the ``actionability`` field is present and non-empty, it is
    returned as-is with ``was_inferred=False``. When missing, the value
    is inferred from ``service`` — security services default to
    ``ACTION_MAY_BE_REQUIRED``; all others also default to
    ``ACTION_MAY_BE_REQUIRED``.

    Args:
        detail: The ``detail`` dict from a Health EventBridge event.

    Returns:
        An :class:`ActionabilityResult` named tuple
        ``(value, was_inferred)`` defaulting to
        ``ActionabilityResult("ACTION_MAY_BE_REQUIRED", True)`` if the
        input is missing or malformed.
    """
    default = ActionabilityResult("ACTION_MAY_BE_REQUIRED", True)

    if not isinstance(detail, dict):
        return default

    event_arn = _sanitize_log_value(
        detail.get("eventArn", "unknown"), _MAX_LOG_EVENT_ARN
    )

    raw = detail.get("actionability")
    if isinstance(raw, str) and raw.strip():
        stripped = raw.strip()
        if stripped not in _KNOWN_ACTIONABILITY_VALUES:  # SEC-R-4
            logger.warning(
                "Unknown actionability value — "
                "source=infer_actionability value=%s event_arn=%s "
                "inferred=true",
                _sanitize_log_value(stripped, 64),
                event_arn,
            )
            return default
        return ActionabilityResult(stripped, False)

    # Infer — currently all paths produce the same value.
    # The security-service branch exists for future differentiation.
    service = detail.get("service", "")
    is_security = isinstance(service, str) and service.upper() in _SECURITY_SERVICES

    logger.debug(
        "Actionability inferred — "
        "source=infer_actionability result=ACTION_MAY_BE_REQUIRED "
        "service=%s is_security_service=%s event_arn=%s",
        _sanitize_log_value(service, 64),
        is_security,
        event_arn,
    )

    return default


def extract_account_tags(detail: Any) -> dict:
    """Extract ``accountTags`` from a Health event detail object.

    Args:
        detail: The ``detail`` dict from a Health EventBridge event.

    Returns:
        A dict of account-level tags, or ``{}`` if the field is missing,
        not a dict, or the input is malformed.
    """
    if not isinstance(detail, dict):
        return {}
    tags = detail.get("accountTags")
    if isinstance(tags, dict):
        return tags
    return {}


def extract_resource_tags(entity: Any) -> dict:
    """Extract ``resourceTags`` from a single affected entity.

    Args:
        entity: One element from the ``affectedEntities`` /
            ``affectedResources`` array.

    Returns:
        A dict of resource-level tags, or ``{}`` if the field is missing,
        not a dict, or the input is malformed.
    """
    if not isinstance(entity, dict):
        return {}
    tags = entity.get("resourceTags")
    if isinstance(tags, dict):
        return tags
    return {}


def should_filter_backup_event(detail: Any, config: Any) -> bool:
    """Determine whether a backup event should be filtered (skipped).

    Checks ``detail.backupEvent`` with a strict ``is True`` identity test
    (fail-open on unexpected types). When the event is a backup
    and filtering is enabled, returns ``True`` to signal the caller to skip
    further processing.

    Args:
        detail: The ``detail`` dict from a Health EventBridge event.
        config: Config dict for the ``FILTER_BACKUP_EVENTS`` item, e.g.
            ``{"enabled": True}``. Pass ``{}`` or ``None`` when the item
            is absent — defaults to filtering enabled.

    Returns:
        ``True`` if the event should be **skipped** (is a backup event
        and filtering is enabled). ``False`` otherwise.
    """
    if not isinstance(detail, dict):
        return False

    # D-1: strict identity check — only Python bool True triggers filter
    if detail.get("backupEvent") is not True:
        return False

    # D-2/D-5: default enabled=True when config absent (fail-safe)
    if not isinstance(config, dict):
        return True

    return config.get("enabled", True) is True


# --- Private helpers (underscore prefix, not exported) ---


def _sanitize_log_value(val: Any, max_len: int) -> str:
    """Strip control characters and truncate for safe log output."""
    text = str(val) if val is not None else ""
    text = text.replace("\n", "").replace("\r", "").replace("\x00", "")
    return text[:max_len]


def _strip_control_chars(val: str) -> str:
    """Remove \\n, \\r, \\x00 from a string."""
    return val.replace("\n", "").replace("\r", "").replace("\x00", "")


def _datetime_to_iso(dt: datetime) -> str:
    """Format a datetime to ISO 8601 with Z suffix."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_iso_string(value: str) -> str:
    """Normalise an ISO 8601 string to end with Z."""
    value = value.strip()
    if value.endswith("+00:00"):
        value = value[:-6] + "Z"
    if value.endswith("+0000"):
        value = value[:-5] + "Z"
    return value


def _sanitize_description(text: str) -> str:
    """Strip null bytes and truncate description."""
    return text.replace("\x00", "")[:_MAX_DESC_LEN]


def _coerce_int(val: Any, field_name: str) -> int:
    """Coerce a value to int clamped to [1, _PAGE_UPPER_BOUND]."""
    if val is None:
        return 1
    # bool is a subclass of int — check before isinstance(val, int)
    if isinstance(val, bool):
        logger.warning(
            "Non-numeric page value — "
            "error_code=PROC_EVENT_PARSE_FAILED field=%s",
            field_name,
        )
        return 1
    if isinstance(val, int):
        return max(1, min(val, _PAGE_UPPER_BOUND))
    if isinstance(val, str):
        if len(val) > _MAX_PAGE_STR_LEN:
            logger.warning(
                "Non-numeric page value — "
                "error_code=PROC_EVENT_PARSE_FAILED field=%s",
                field_name,
            )
            return 1
        try:
            return max(1, min(int(val), _PAGE_UPPER_BOUND))
        except (ValueError, TypeError):
            logger.warning(
                "Non-numeric page value — "
                "error_code=PROC_EVENT_PARSE_FAILED field=%s",
                field_name,
            )
            return 1
    # float and other unexpected types
    logger.warning(
        "Non-numeric page value — "
        "error_code=PROC_EVENT_PARSE_FAILED field=%s",
        field_name,
    )
    return 1
