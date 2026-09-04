"""Campaign creation and deduplication for AWS Health events.

Implements the conditional-write-with-fallback pattern: ``PutItem`` with
``attribute_not_exists(campaignId)`` for new campaigns, ``UpdateItem``
merge for re-ingested events. PLEs merge via ``service:eventTypeCode``;
all other events use ``eventArn`` as the campaign ID.

Consumers: Processor Lambda, Reconciliation Lambda.
Dependencies: boto3 (DynamoDB resource), resolve_core.event_parser,
resolve_core.tags, resolve_core.pagination.
"""

from __future__ import annotations

import logging
from typing import Any, List, NamedTuple, Optional

from botocore.exceptions import ClientError

from resolve_core.event_parser import (
    coerce_page_fields,
    extract_account_tags,
    extract_description,
    infer_actionability,
    parse_health_date,
)
from resolve_core.pagination import count_resources
from resolve_core.tags import normalize_tags

logger = logging.getLogger("resolve_core")

# --- Constants ---

_PLE_SUFFIX = "_PLANNED_LIFECYCLE_EVENT"
_MAX_EVENT_ARN_LEN = 1024
_MAX_LOG_VALUE_LEN = 256

# SECURITY: Fields that MUST use if_not_exists on merge to prevent
# data corruption and duplicate JIRA ticket creation (SR-09a).
_IMMUTABLE_FIELDS = frozenset({
    "status", "campaignType", "service", "eventTypeCode",
    "eventTypeCategory", "eventArn", "affectedAccount",
    "actionability", "actionabilityInferred", "dispatched", "createdAt",
})

# SECURITY: Fields that MUST NOT appear in merge expressions.
# These are owned by JIRA Lambda and Sync Lambda (SR-09b).
_TICKET_FIELDS = frozenset({
    "ticketsCreated", "ticketsClosed", "ticketsInProgress",
    "completionPct", "routing",
})


# Campaign statuses.
#
# NOTE (STORY-118 / King Yip Finding 3, ACCEPT AS DEBT): this module owns the
# `status` attribute on the CampaignsTable item — the CAMPAIGN STATE MACHINE
# below (ACTIVE/COMPLETED/PARTIAL/FILTERED). A DIFFERENT attribute,
# `campaignStatus`, lives on the SAME item and is owned exclusively by the
# STORY-114 ticketing lock in lambdas/api/dashboard_handlers.py
# (handle_create_tickets). The two names look related but track unrelated
# concerns — this module never reads or writes `campaignStatus`. See
# resolve_core.constants.CAMPAIGN_STATE_FIELD / TICKETING_LOCK_FIELD for the
# full writeup. Do not rename `status` to reduce the naming collision; that
# is deferred to a future CampaignsTable migration.
VALID_CAMPAIGN_STATUSES = frozenset({"ACTIVE", "COMPLETED", "PARTIAL", "FILTERED"})

# Statuses managed by dispatch/pagination logic — recalculate_completion
# must never overwrite these (design §7.3).
_EXTERNALLY_MANAGED_STATUSES = frozenset({"FILTERED", "PARTIAL"})

# Valid status transitions for update_campaign_status.
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "ACTIVE": frozenset({"COMPLETED", "FILTERED"}),
    "COMPLETED": frozenset({"ACTIVE"}),
    "PARTIAL": frozenset({"ACTIVE"}),
    "FILTERED": frozenset({"ACTIVE"}),
}


# --- Return types ---

class CampaignResult(NamedTuple):
    """Return type for :func:`create_or_update_campaign`."""

    campaign_id: str
    action: str           # "CREATED" | "MERGED"
    campaign_type: str    # "resource-level" | "account-level"
    is_page_one: bool
    resource_count: int
    description_changed: bool
    deadline_changed: bool


class CompletionResult(NamedTuple):
    """Return type for :func:`recalculate_completion`."""

    completion_pct: Optional[float]  # None when no denominator, else 0.0–100.0
    status: Optional[str]            # "ACTIVE", "COMPLETED", or None (unchanged)


# --- Public API ---

__all__ = [
    "derive_campaign_id",
    "determine_campaign_type",
    "create_or_update_campaign",
    "recalculate_completion",
    "update_campaign_status",
    "CampaignResult",
    "CompletionResult",
    "VALID_CAMPAIGN_STATUSES",
]


def derive_campaign_id(detail: Any) -> str:
    """Derive a campaign ID from a Health event detail dict.

    PLEs (event type codes ending with ``_PLANNED_LIFECYCLE_EVENT``)
    use ``service:eventTypeCode`` so that the same deprecation across
    regions merges into one campaign (BRD Q-2, E-10). All other events
    use ``eventArn`` for strict per-event deduplication.

    Args:
        detail: The ``detail`` dict from a Health EventBridge event.

    Returns:
        Campaign ID string.

    Raises:
        ValueError: If ``eventArn`` is missing or empty.
    """
    if not isinstance(detail, dict):
        raise ValueError("Health event detail is not a dict")

    event_arn = detail.get("eventArn")
    if not isinstance(event_arn, str) or not event_arn.strip():
        raise ValueError("Health event missing eventArn — malformed event")

    event_arn = event_arn.strip()
    if len(event_arn) > _MAX_EVENT_ARN_LEN:  # SR-02a
        event_arn = event_arn[:_MAX_EVENT_ARN_LEN]

    if not event_arn.startswith("arn:aws:health:"):  # SR-02b
        logger.warning(
            "Non-standard eventArn prefix — event_arn=%s",
            _sanitize_log(event_arn),
        )

    event_type_code = detail.get("eventTypeCode")
    if isinstance(event_type_code, str) and event_type_code.endswith(_PLE_SUFFIX):
        service = detail.get("service")
        if not isinstance(service, str) or not service.strip():
            service = "UNKNOWN"
        return f"{service}:{event_type_code}"

    return event_arn


def determine_campaign_type(entities: List[dict]) -> str:
    """Determine whether a campaign is resource-level or account-level.

    Args:
        entities: Extracted entities list from :func:`extract_entities`.

    Returns:
        ``"resource-level"`` if entities are present, ``"account-level"``
        otherwise.
    """
    return "resource-level" if entities else "account-level"


def create_or_update_campaign(
    table: Any,
    detail: Any,
    entities: List[dict],
    now: str,
    mode: str = "eventbridge",
) -> CampaignResult:
    """Create a new campaign or merge into an existing one.

    Uses DynamoDB conditional ``PutItem`` for new campaigns and
    ``UpdateItem`` for merges. Idempotent — safe to call multiple times
    for the same event.

    Args:
        table: DynamoDB Table resource (``boto3.resource("dynamodb").Table``).
        detail: The ``detail`` dict from a Health EventBridge event.
        entities: Extracted entities from :func:`extract_entities`.
        now: ISO 8601 timestamp for ``createdAt`` / ``updatedAt``.
        mode: ``"eventbridge"`` (incremental counts) or
            ``"reconciliation"`` (absolute counts).

    Returns:
        A :class:`CampaignResult` with the outcome.

    Raises:
        ValueError: If ``eventArn`` is missing (propagated from
            :func:`derive_campaign_id`).
        ClientError: On non-retryable DynamoDB errors (``ValidationException``,
            ``ResourceNotFoundException``).
    """
    campaign_id = derive_campaign_id(detail)
    campaign_type = determine_campaign_type(entities)
    page_info = coerce_page_fields(detail)
    is_page_one = page_info.page == 1

    # Page 1: attempt conditional create
    if is_page_one:
        try:
            item = _build_new_item(
                detail, entities, campaign_id, campaign_type,
                page_info, now,
            )
            table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(campaignId)",
            )
            logger.info(
                "Campaign created — campaign_id=%s campaign_type=%s "
                "service=%s event_type_code=%s resource_count=%d source=%s",
                _sanitize_log(campaign_id),
                campaign_type,
                _sanitize_log(item.get("service", "")),
                _sanitize_log(item.get("eventTypeCode", "")),
                len(entities),
                mode,
            )
            return CampaignResult(
                campaign_id=campaign_id,
                action="CREATED",
                campaign_type=campaign_type,
                is_page_one=True,
                resource_count=len(entities),
                description_changed=False,
                deadline_changed=False,
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                # SR-08a/b: log campaignId and operation only, never Item content
                logger.error(
                    "Campaign write failed — error_code=DYNAMO_WRITE_FAILED "
                    "campaign_id=%s operation=put_item exception_type=%s",
                    _sanitize_log(campaign_id),
                    type(exc).__name__,
                )
                raise
            # Campaign exists — fall through to merge

    # Merge path: existing campaign or page > 1
    # SECURITY: Two-phase description/deadline change detection.
    # Race window between GetItem and UpdateItem is acceptable for
    # Alpha single-level history (SR-07a, FINDING-IMPL-03).
    desc_changed, deadline_changed = _detect_changes(
        table, campaign_id, detail,
    )

    expr, names, values = _build_merge_expression(
        detail, entities, campaign_id, campaign_type,
        page_info, now, mode, desc_changed, deadline_changed,
    )

    try:
        table.update_item(
            Key={"campaignId": campaign_id},
            UpdateExpression=expr,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        # SR-08a/b: log campaignId and operation only
        logger.error(
            "Campaign write failed — error_code=DYNAMO_WRITE_FAILED "
            "campaign_id=%s operation=update_item exception_type=%s",
            _sanitize_log(campaign_id),
            type(exc).__name__,
        )
        raise

    logger.info(
        "Campaign merged — campaign_id=%s action=MERGED "
        "new_resource_count=%d is_page_one=%s source=%s",
        _sanitize_log(campaign_id),
        len(entities),
        is_page_one,
        mode,
    )

    return CampaignResult(
        campaign_id=campaign_id,
        action="MERGED",
        campaign_type=campaign_type,
        is_page_one=is_page_one,
        resource_count=len(entities),
        description_changed=desc_changed,
        deadline_changed=deadline_changed,
    )


def recalculate_completion(campaign: dict) -> CompletionResult:
    """Compute completion percentage and status from campaign counters.

    Pure function — no side effects, no DynamoDB calls. Callers must
    NOT invoke this when ``campaign["status"]`` is ``FILTERED`` or
    ``PARTIAL``; those statuses are managed by dispatch and pagination
    logic respectively (design §7.3).

    Args:
        campaign: Dict with keys ``campaignType``, ``statusCode``,
            ``status``, ``totalResourceCount``, ``resolvedCount``,
            ``ticketsCreated``, ``ticketsClosed``.

    Returns:
        A :class:`CompletionResult` with ``(completion_pct, status)``.
        ``completion_pct`` is ``None`` when there is no denominator.
        ``status`` is ``None`` when the current status should not change.

    Raises:
        ValueError: If required keys are missing or have invalid types.
    """
    # --- Input validation (SEC-01 / SR-01a) ---
    campaign_type = campaign.get("campaignType", "")
    if campaign_type not in ("resource-level", "account-level"):
        raise ValueError(
            f"Invalid campaignType: {_sanitize_log(campaign_type)}"
        )

    status_code = campaign.get("statusCode", "")
    current_status = campaign.get("status", "")

    total = _safe_non_negative_int(campaign.get("totalResourceCount", 0))
    resolved = _safe_non_negative_int(campaign.get("resolvedCount", 0))
    tickets_created = _safe_non_negative_int(campaign.get("ticketsCreated", 0))
    tickets_closed = _safe_non_negative_int(campaign.get("ticketsClosed", 0))

    # --- Guard: never overwrite externally managed statuses ---
    if current_status in _EXTERNALLY_MANAGED_STATUSES:
        return CompletionResult(completion_pct=None, status=None)

    # --- Rule 1: Health event closure overrides everything ---
    if status_code == "closed":
        return CompletionResult(completion_pct=100.0, status="COMPLETED")

    # --- Resource-level campaigns ---
    if campaign_type == "resource-level":
        if total == 0:
            return CompletionResult(completion_pct=None, status=None)

        # Clamp resolved to total (SR-01b)
        resolved = min(resolved, total)
        pct = round((resolved / total) * 100, 1)

        if resolved == total:
            return CompletionResult(completion_pct=100.0, status="COMPLETED")

        # Secondary signal: all tickets closed
        if tickets_created > 0 and tickets_closed >= tickets_created:
            return CompletionResult(completion_pct=pct, status="COMPLETED")

        return CompletionResult(completion_pct=pct, status="ACTIVE")

    # --- Account-level campaigns ---
    if tickets_created == 0:
        return CompletionResult(completion_pct=None, status=None)

    # Clamp closed to created (SR-01b)
    tickets_closed = min(tickets_closed, tickets_created)
    pct = round((tickets_closed / tickets_created) * 100, 1)

    if tickets_closed == tickets_created:
        return CompletionResult(completion_pct=100.0, status="COMPLETED")

    return CompletionResult(completion_pct=pct, status="ACTIVE")


def update_campaign_status(
    table: Any,
    campaign_id: str,
    new_status: str,
    now: str,
) -> bool:
    """Transition a campaign to a new status with guard rails.

    Only allows transitions defined in ``_ALLOWED_TRANSITIONS``.
    Uses a DynamoDB conditional write to prevent race conditions.

    Args:
        table: DynamoDB Table resource for CampaignsTable.
        campaign_id: Campaign partition key.
        new_status: Target status (``ACTIVE``, ``COMPLETED``, etc.).
        now: ISO 8601 timestamp.

    Returns:
        ``True`` if the transition succeeded, ``False`` if the
        transition was invalid or the campaign does not exist.
    """
    if new_status not in VALID_CAMPAIGN_STATUSES:
        logger.warning(
            "Invalid target status — campaign_id=%s new_status=%s",
            _sanitize_log(campaign_id), _sanitize_log(new_status),
        )
        return False

    # Build condition: campaign must exist AND current status must allow
    # the transition. We accept any status that lists new_status as a
    # valid target.
    allowed_from = [
        s for s, targets in _ALLOWED_TRANSITIONS.items()
        if new_status in targets
    ]
    if not allowed_from:
        logger.warning(
            "No valid source statuses for transition — "
            "campaign_id=%s new_status=%s",
            _sanitize_log(campaign_id), _sanitize_log(new_status),
        )
        return False

    condition_parts = [f"#s = :from{i}" for i in range(len(allowed_from))]
    condition_expr = (
        "attribute_exists(campaignId) AND ("
        + " OR ".join(condition_parts)
        + ")"
    )
    attr_values: dict[str, Any] = {
        ":newStatus": new_status,
        ":now": now,
    }
    for i, from_status in enumerate(allowed_from):
        attr_values[f":from{i}"] = from_status

    try:
        table.update_item(
            Key={"campaignId": campaign_id},
            UpdateExpression="SET #s = :newStatus, #ua = :now",
            ExpressionAttributeNames={
                "#s": "status",
                "#ua": "updatedAt",
            },
            ExpressionAttributeValues=attr_values,
            ConditionExpression=condition_expr,
        )
        logger.info(
            "Campaign status updated — campaign_id=%s new_status=%s",
            _sanitize_log(campaign_id), new_status,
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.info(
                "Campaign status transition rejected — "
                "campaign_id=%s new_status=%s "
                "(campaign missing or invalid source status)",
                _sanitize_log(campaign_id), new_status,
            )
            return False
        logger.error(
            "Campaign status update failed — "
            "error_code=DYNAMO_WRITE_FAILED "
            "campaign_id=%s operation=update_status exception_type=%s",
            _sanitize_log(campaign_id), type(exc).__name__,
        )
        raise


# --- Private helpers ---


def _safe_non_negative_int(val: Any) -> int:
    """Coerce a value to a non-negative integer (SEC-01 / SR-01a).

    Returns 0 for None, negative, or non-numeric values.
    """
    if val is None:
        return 0
    try:
        n = int(val)
        return max(n, 0)
    except (TypeError, ValueError):
        return 0


def _sanitize_log(val: Any) -> str:
    """Truncate and strip control chars for safe log output."""
    text = str(val) if val is not None else ""
    text = text.replace("\n", "").replace("\r", "").replace("\x00", "")
    return text[:_MAX_LOG_VALUE_LEN]


def _build_new_item(
    detail: dict,
    entities: List[dict],
    campaign_id: str,
    campaign_type: str,
    page_info: Any,
    now: str,
) -> dict:
    """Build a complete DynamoDB item for a new campaign."""
    actionability = infer_actionability(detail)
    counts = count_resources(entities)
    description = extract_description(detail.get("eventDescription"))
    account_tags = normalize_tags(extract_account_tags(detail))

    return {
        "campaignId": campaign_id,
        "eventArn": detail.get("eventArn", ""),
        "service": detail.get("service", "UNKNOWN"),
        "eventTypeCode": detail.get("eventTypeCode", ""),
        "eventTypeCategory": detail.get("eventTypeCategory", ""),
        "affectedAccount": detail.get("affectedAccount", ""),
        "description": description,
        "previousDescription": "",
        "startTime": parse_health_date(detail.get("startTime")) or "",
        "endTime": parse_health_date(detail.get("endTime")) or "",
        "statusCode": detail.get("statusCode", ""),
        "actionability": actionability.value,
        "actionabilityInferred": actionability.was_inferred,
        "accountTags": account_tags,
        "campaignType": campaign_type,
        "status": "ACTIVE",
        "dispatched": False,
        "totalResourceCount": counts.total,
        "pendingCount": counts.pending,
        "resolvedCount": counts.resolved,
        "pagesReceived": 1,
        "totalPages": page_info.total_pages,
        "createdAt": now,
        "updatedAt": now,
    }


def _detect_changes(
    table: Any,
    campaign_id: str,
    detail: dict,
) -> tuple[bool, bool]:
    """Read current campaign to detect description and deadline changes.

    Returns:
        Tuple of ``(description_changed, deadline_changed)``.
    """
    desc_changed = False
    deadline_changed = False

    try:
        resp = table.get_item(
            Key={"campaignId": campaign_id},
            ProjectionExpression="description, startTime",
            ConsistentRead=False,
        )
    except ClientError as exc:
        # SR-08a/b: log campaignId and operation only
        logger.error(
            "Campaign read failed — error_code=DYNAMO_WRITE_FAILED "
            "campaign_id=%s operation=get_item exception_type=%s",
            _sanitize_log(campaign_id),
            type(exc).__name__,
        )
        # On read failure, skip change detection — merge proceeds safely
        return False, False

    existing = resp.get("Item")
    if not existing:
        return False, False

    new_desc = extract_description(detail.get("eventDescription"))
    old_desc = existing.get("description", "")
    if new_desc and old_desc and new_desc != old_desc:
        desc_changed = True
        logger.info(
            "Description changed — campaign_id=%s "
            "old_description_length=%d new_description_length=%d",
            _sanitize_log(campaign_id),
            len(old_desc),
            len(new_desc),
        )

    new_start = parse_health_date(detail.get("startTime")) or ""
    old_start = existing.get("startTime", "")
    if new_start and old_start and new_start != old_start:
        deadline_changed = True
        logger.warning(
            "Deadline changed — campaign_id=%s "
            "old_start_time=%s new_start_time=%s",
            _sanitize_log(campaign_id),
            _sanitize_log(old_start),
            _sanitize_log(new_start),
        )

    return desc_changed, deadline_changed


def _build_merge_expression(
    detail: dict,
    entities: List[dict],
    campaign_id: str,
    campaign_type: str,
    page_info: Any,
    now: str,
    mode: str,
    desc_changed: bool,
    deadline_changed: bool,
) -> tuple[str, dict, dict]:
    """Build the UpdateItem expression for merging into an existing campaign.

    Returns:
        Tuple of ``(UpdateExpression, ExpressionAttributeNames,
        ExpressionAttributeValues)``.
    """
    actionability = infer_actionability(detail)
    counts = count_resources(entities)
    description = extract_description(detail.get("eventDescription"))
    account_tags = normalize_tags(extract_account_tags(detail))

    names: dict[str, str] = {}
    values: dict[str, Any] = {}
    set_clauses: list[str] = []
    add_clauses: list[str] = []

    # --- Always-SET fields (mutable, overwritten on every merge) ---
    _add_set(set_clauses, names, values, "updatedAt", now)
    _add_set(set_clauses, names, values, "statusCode", detail.get("statusCode", ""))
    _add_set(set_clauses, names, values, "totalPages", page_info.total_pages)
    _add_set(set_clauses, names, values, "endTime", parse_health_date(detail.get("endTime")) or "")
    _add_set(set_clauses, names, values, "accountTags", account_tags)

    # --- Description change: archive old, write new ---
    if desc_changed:
        # Read the old description from the existing item to archive it.
        # The GetItem in _detect_changes already confirmed the change.
        names["#previousDescription"] = "previousDescription"
        names["#description"] = "description"
        values[":newDesc"] = description
        # Use if_not_exists for previousDescription to avoid overwriting
        # an already-archived value in a race (best-effort).
        set_clauses.append("#description = :newDesc")
        # Archive: set previousDescription to whatever description currently is.
        # We use a DynamoDB expression to read the current value atomically.
        set_clauses.append("#previousDescription = #description")
    elif description:
        # No change detected, but still set description with if_not_exists
        # for the page-2-before-page-1 race.
        names["#description"] = "description"
        values[":desc"] = description
        set_clauses.append("#description = if_not_exists(#description, :desc)")

    # --- Deadline change ---
    if deadline_changed:
        new_start = parse_health_date(detail.get("startTime")) or ""
        _add_set(set_clauses, names, values, "startTime", new_start)
    else:
        new_start = parse_health_date(detail.get("startTime")) or ""
        if new_start:
            names["#startTime"] = "startTime"
            values[":startTime"] = new_start
            set_clauses.append("#startTime = if_not_exists(#startTime, :startTime)")

    # --- SECURITY: if_not_exists guards on immutable fields (SR-09a) ---
    # These fields are set only on first write. Subsequent merges preserve
    # the original values. This prevents re-dispatch and counter corruption.
    _guard_fields = {
        "status": "ACTIVE",
        "campaignType": campaign_type,
        "service": detail.get("service", "UNKNOWN"),
        "eventTypeCode": detail.get("eventTypeCode", ""),
        "eventTypeCategory": detail.get("eventTypeCategory", ""),
        "eventArn": detail.get("eventArn", ""),
        "affectedAccount": detail.get("affectedAccount", ""),
        "actionability": actionability.value,
        "actionabilityInferred": actionability.was_inferred,
        "dispatched": False,
        "createdAt": now,
    }

    if mode == "reconciliation":
        # SR-10a/b: Reconciliation still guards a subset of immutable fields.
        # It MAY overwrite affectedAccount, actionability, actionabilityInferred.
        _reconcile_guarded = {
            "createdAt", "dispatched", "eventArn", "service",
            "eventTypeCode", "eventTypeCategory",
        }
        for field, default_val in _guard_fields.items():
            alias = f"#{field}"
            val_key = f":{field}"
            names[alias] = field
            values[val_key] = default_val
            if field in _reconcile_guarded:
                set_clauses.append(f"{alias} = if_not_exists({alias}, {val_key})")
            else:
                set_clauses.append(f"{alias} = {val_key}")
    else:
        # EventBridge mode: all immutable fields guarded with if_not_exists
        for field, default_val in _guard_fields.items():
            alias = f"#{field}"
            val_key = f":{field}"
            names[alias] = field
            values[val_key] = default_val
            set_clauses.append(f"{alias} = if_not_exists({alias}, {val_key})")

    # --- Count fields: mode-dependent semantics ---
    if mode == "reconciliation":
        # Absolute counts — reconciliation is source of truth
        _add_set(set_clauses, names, values, "totalResourceCount", counts.total)
        _add_set(set_clauses, names, values, "pendingCount", counts.pending)
        _add_set(set_clauses, names, values, "resolvedCount", counts.resolved)
        _add_set(set_clauses, names, values, "pagesReceived", page_info.total_pages)
    else:
        # Incremental counts — EventBridge pages are additive
        _add_add(add_clauses, names, values, "totalResourceCount", counts.total)
        _add_add(add_clauses, names, values, "pendingCount", counts.pending)
        _add_add(add_clauses, names, values, "resolvedCount", counts.resolved)
        names["#pagesReceived"] = "pagesReceived"
        values[":one"] = 1
        add_clauses.append("#pagesReceived :one")

    # --- Build final expression ---
    parts = []
    if set_clauses:
        parts.append("SET " + ", ".join(set_clauses))
    if add_clauses:
        parts.append("ADD " + ", ".join(add_clauses))

    expr = " ".join(parts)

    # SECURITY: Verify no ticket-owned fields leaked into expression (SR-09b).
    # Explicit check — not assert — so it survives Python -O (FINDING-IMPL-015-03).
    for forbidden in _TICKET_FIELDS:
        if forbidden in expr:
            raise RuntimeError(
                f"SECURITY VIOLATION: ticket field '{forbidden}' in merge expression"
            )

    return expr, names, values


def _add_set(
    clauses: list[str],
    names: dict[str, str],
    values: dict[str, Any],
    field: str,
    val: Any,
) -> None:
    """Append a simple SET clause: ``#field = :field``."""
    alias = f"#{field}"
    val_key = f":{field}"
    names[alias] = field
    values[val_key] = val
    clauses.append(f"{alias} = {val_key}")


def _add_add(
    clauses: list[str],
    names: dict[str, str],
    values: dict[str, Any],
    field: str,
    val: Any,
) -> None:
    """Append an ADD clause: ``#field :field``."""
    alias = f"#{field}"
    val_key = f":{field}"
    names[alias] = field
    values[val_key] = val
    clauses.append(f"{alias} {val_key}")
