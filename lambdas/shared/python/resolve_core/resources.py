"""Resource tracking for AWS Health campaign resources.

Writes individual resource records to the ResourcesTable with campaign
association, Health status tracking, and inline tags. Supports batch
writes for first ingestion and conditional upserts for re-ingestion
that preserve ticket fields owned by the JIRA/Sync Lambdas.

Consumers: Processor Lambda, Reconciliation Lambda.
Dependencies: boto3 (DynamoDB resource), resolve_core.event_parser,
resolve_core.tags.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any, List, NamedTuple, Optional

from botocore.exceptions import ClientError

from resolve_core.event_parser import extract_resource_tags, parse_health_date
from resolve_core.tags import normalize_tags

logger = logging.getLogger("resolve_core")

# --- Constants ---

_MAX_BATCH_SIZE = 25
_MAX_BATCH_RETRIES = 5
_BATCH_RETRY_BASE_MS = 100

_TTL_DAYS = 180
_TTL_SECONDS = _TTL_DAYS * 86400

_MAX_ENTITY_VALUE_LEN = 2048
_MAX_TRACKING_KEY_BYTES = 1024
_MAX_LOG_VALUE_LEN = 256
_MAX_TAG_PAYLOAD_BYTES = 10240  # 10 KB

_ACCOUNT_ID_PATTERN = re.compile(r"\d{12}")

_VALID_HEALTH_STATUSES = frozenset({"PENDING", "RESOLVED", "IMPAIRED", "UNKNOWN"})

# SECURITY (IMPL-014-06 / SR-12): Hardcoded UpdateExpression for upserts.
# MUST NOT be constructed at runtime. Only touches health-owned fields.
_UPSERT_UPDATE_EXPR = (
    "SET #hs = :hs, #lut = :lut, #ua = :ua"
)

_UPSERT_ATTR_NAMES = {
    "#hs": "healthStatus",
    "#lut": "lastUpdatedTime",
    "#ua": "updatedAt",
}

# SECURITY (SR-12): Runtime assertion — ticket-owned fields must never
# appear in the upsert expression.
_TICKET_FIELDS = frozenset({
    "ticketId", "ticketStatus", "ticketRawStatus", "ticketUrl", "ticketUpdatedAt",
    "tickets",
})

# SECURITY (SR-12): Explicit check — not assert — so it survives
# Python -O optimization (FINDING-IMPL-015-03).
for _f in _TICKET_FIELDS:
    if _f in _UPSERT_UPDATE_EXPR:
        raise RuntimeError(
            f"SECURITY VIOLATION: ticket field '{_f}' in _UPSERT_UPDATE_EXPR"
        )


# --- Return types ---

class WriteResult(NamedTuple):
    """Return type for :func:`write_resources`."""
    written: int
    updated: int
    skipped: int
    failed: int
    counts: dict


# --- Public API ---

__all__ = [
    "write_resources",
    "update_resource_status",
    "update_routed_via",
    "WriteResult",
]


def write_resources(
    resources_table: Any,
    campaigns_table: Any,
    campaign_id: str,
    campaign_type: str,
    entities: List[dict],
    account_tags: dict,
    affected_account: str,
    event_region: str,
    is_new_campaign: bool,
    now: str,
) -> WriteResult:
    """Write resource records and update campaign counters.

    For new campaigns, uses ``BatchWriteItem`` for speed. For
    re-ingestion, uses conditional ``PutItem`` + ``UpdateItem``
    fallback to preserve ticket fields.

    Args:
        resources_table: DynamoDB Table resource for ResourcesTable.
        campaigns_table: DynamoDB Table resource for CampaignsTable.
        campaign_id: Campaign partition key.
        campaign_type: ``"resource-level"`` or ``"account-level"``.
        entities: Extracted entities from the Health event.
        account_tags: Normalized account-level tags.
        affected_account: 12-digit AWS account ID.
        event_region: Event-level region fallback.
        is_new_campaign: True if campaign was just created.
        now: ISO 8601 timestamp.

    Returns:
        A :class:`WriteResult` with write outcomes and absolute counts.
    """
    written = 0
    updated = 0
    skipped = 0
    failed = 0

    if campaign_type == "account-level" and not entities:
        ok = _write_account_record(
            resources_table, campaign_id, affected_account,
            account_tags, now,
        )
        written = 1 if ok else 0
        failed = 0 if ok else 1
    else:
        items = _build_resource_items(
            campaign_id, entities, account_tags, event_region, now,
        )
        if not items:
            logger.info(
                "No valid resources to write — campaign_id=%s entity_count=%d",
                _sanitize_log(campaign_id), len(entities),
            )
        elif is_new_campaign:
            w, f = _batch_write(resources_table, items)
            written, failed = w, f
        else:
            for item in items:
                result = _upsert_resource(resources_table, item)
                if result == "created":
                    written += 1
                elif result == "updated":
                    updated += 1
                elif result == "failed":
                    failed += 1
                else:
                    skipped += 1

    counts = _compute_and_update_counts(
        resources_table, campaigns_table, campaign_id, now,
    )

    logger.info(
        "Resource write complete — campaign_id=%s written=%d updated=%d "
        "skipped=%d failed=%d total=%d pending=%d resolved=%d",
        _sanitize_log(campaign_id), written, updated, skipped, failed,
        counts.get("totalResourceCount", 0),
        counts.get("pendingCount", 0),
        counts.get("resolvedCount", 0),
    )

    return WriteResult(
        written=written, updated=updated, skipped=skipped,
        failed=failed, counts=counts,
    )


def update_resource_status(
    resources_table: Any,
    campaign_id: str,
    tracking_key: str,
    new_status: str,
    now: str,
) -> bool:
    """Update a single resource's health status.

    Args:
        resources_table: DynamoDB Table resource for ResourcesTable.
        campaign_id: Campaign partition key.
        tracking_key: Resource sort key.
        new_status: New health status value.
        now: ISO 8601 timestamp.

    Returns:
        True if updated, False on failure.
    """
    status = _validate_health_status(new_status)
    try:
        resources_table.update_item(
            Key={"campaignId": campaign_id, "trackingKey": tracking_key},
            UpdateExpression=_UPSERT_UPDATE_EXPR,
            ExpressionAttributeNames=_UPSERT_ATTR_NAMES,
            ExpressionAttributeValues={
                ":hs": status,
                ":lut": now,
                ":ua": now,
            },
        )
        return True
    except ClientError as exc:
        logger.error(
            "Resource status update failed — "
            "error_code=PROC_RESOURCE_WRITE_FAILED "
            "campaign_id=%s tracking_key=%s exception_type=%s "
            "containsPII=false",
            _sanitize_log(campaign_id),
            _sanitize_log(tracking_key),
            type(exc).__name__,
        )
        return False


def update_routed_via(
    resources_table: Any,
    campaign_id: str,
    routed_via: str,
    routing_error: Optional[str],
    now: str,
) -> None:
    """Persist routing attribution (``routedVia``) on all resources of a campaign.

    Queries every resource row for ``campaign_id`` and ``UpdateItem``-SETs
    ``routedVia`` and ``updatedAt`` (plus ``routingError`` when supplied) on
    each ``(campaignId, trackingKey)``. This is the single shared writer used by
    both the Processor Lambda (real-time, step k.1) and the Reconciliation
    Lambda (daily Health-API catch-up) so the persisted attribution vocabulary
    cannot drift between the two paths (STORY-126 / RT-07).

    Idempotent: the write is a SET on the composite key — re-running writes the
    same value and never creates rows, so the coverage scan-aggregation counts
    each resource once regardless of how many times reconciliation runs.

    Best-effort / non-fatal: a DynamoDB failure is caught, logged with a
    structured error code, and swallowed. Attribution is strictly secondary to
    ingestion — a transient write failure must never abort the caller. The
    resource stays unattributed (buckets as ``failed``) until the next
    processor/reconcile pass re-derives it.

    Args:
        resources_table: DynamoDB Table resource for ResourcesTable.
        campaign_id: Campaign partition key.
        routed_via: Attribution value from
            :func:`resolve_core.routing.derive_routed_via`
            (∈ ``{resourceTag, accountTag, account, service, default, error}``).
        routing_error: Human-readable failure reason to persist when the
            outcome is a genuine no-target ``error``, else ``None``.
        now: ISO 8601 timestamp for ``updatedAt``.
    """
    query_kwargs = {
        "KeyConditionExpression": "campaignId = :cid",
        "ExpressionAttributeValues": {":cid": campaign_id},
        "ProjectionExpression": "campaignId, trackingKey",
    }

    try:
        while True:
            resp = resources_table.query(**query_kwargs)
            for item in resp.get("Items", []):
                # SR-12/SEC-126-2: parameterized expression — never interpolate
                # campaign_id, routed_via, or routing_error into the string.
                update_expr = "SET #rv = :rv, #ua = :ua"
                attr_names = {"#rv": "routedVia", "#ua": "updatedAt"}
                attr_values = {":rv": routed_via, ":ua": now}
                if routing_error:
                    update_expr += ", #re = :re"
                    attr_names["#re"] = "routingError"
                    attr_values[":re"] = routing_error

                resources_table.update_item(
                    Key={
                        "campaignId": item["campaignId"],
                        "trackingKey": item["trackingKey"],
                    },
                    UpdateExpression=update_expr,
                    ExpressionAttributeNames=attr_names,
                    ExpressionAttributeValues=attr_values,
                )

            if "LastEvaluatedKey" not in resp:
                break
            query_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    except ClientError as exc:
        # SEC-126-3/SEC-126-6: log only sanitized identifiers; non-fatal.
        logger.error(
            "Failed to update routedVia — "
            "error_code=PROC_RESOURCE_WRITE_FAILED "
            "campaign_id=%s exception_type=%s containsPII=false",
            _sanitize_log(campaign_id),
            type(exc).__name__,
        )


# --- Private helpers ---


def _sanitize_log(val: Any) -> str:
    """Truncate and strip control chars for safe log output."""
    text = str(val) if val is not None else ""
    text = text.replace("\n", "").replace("\r", "").replace("\x00", "")
    return text[:_MAX_LOG_VALUE_LEN]


def _validate_entity_value(entity_value: Any) -> Optional[str]:
    """Validate and sanitize an entity value (IMPL-014-03 / SR-3, SR-4).

    Returns sanitized string or None if invalid.
    """
    if not isinstance(entity_value, str) or not entity_value.strip():
        return None
    # Strip control characters U+0000–U+001F except U+000A (newline)
    cleaned = re.sub(r"[\x00-\x09\x0b-\x1f]", "", entity_value)
    cleaned = cleaned.strip()
    if not cleaned:
        return None
    if len(cleaned) > _MAX_ENTITY_VALUE_LEN:
        cleaned = cleaned[:_MAX_ENTITY_VALUE_LEN - 1] + "\u2026"
    return cleaned


def _derive_tracking_key(entity_value: str) -> str:
    """Derive tracking key from entity value (SR-6).

    Falls back to SHA-256 hash if UTF-8 byte length exceeds 1024.
    """
    if len(entity_value.encode("utf-8")) <= _MAX_TRACKING_KEY_BYTES:
        return entity_value
    digest = hashlib.sha256(entity_value.encode("utf-8")).hexdigest()
    logger.info(
        "Tracking key exceeds 1024 bytes, using SHA-256 — "
        "original_length=%d hash=%s",
        len(entity_value.encode("utf-8")), digest[:16],
    )
    return f"SHA256:{digest}"


def _validate_account_id(account_id: Any) -> Optional[str]:
    """Validate 12-digit numeric account ID (IMPL-014-04 / SR-5)."""
    if not isinstance(account_id, str):
        return None
    if _ACCOUNT_ID_PATTERN.fullmatch(account_id):
        return account_id
    return None


def _validate_health_status(status: Any) -> str:
    """Validate health status enum (IMPL-014-05 / SR-10)."""
    if isinstance(status, str) and status in _VALID_HEALTH_STATUSES:
        return status
    logger.info(
        "Unknown healthStatus mapped to UNKNOWN — original=%s",
        _sanitize_log(status),
    )
    return "UNKNOWN"


def _extract_region(entity_value: str, fallback_region: str) -> str:
    """Extract region from ARN or use fallback."""
    if entity_value.startswith("arn:"):
        parts = entity_value.split(":")
        if len(parts) >= 4 and parts[3]:
            return parts[3]
    return fallback_region or "unknown"


def _check_tag_payload_size(tags: dict) -> dict:
    """Enforce max 10KB total serialized tag payload (IMPL-014-01 / SR-1)."""
    serialized = json.dumps(tags, separators=(",", ":"))
    if len(serialized.encode("utf-8")) <= _MAX_TAG_PAYLOAD_BYTES:
        return tags
    # Truncate by removing keys until under limit
    result: dict = {}
    for key, value in tags.items():
        result[key] = value
        if len(json.dumps(result, separators=(",", ":")).encode("utf-8")) > _MAX_TAG_PAYLOAD_BYTES:
            del result[key]
            logger.warning(
                "Tag payload exceeds 10KB limit — "
                "error_code=TAG_PAYLOAD_EXCEEDED truncated_count=%d",
                len(tags) - len(result),
            )
            break
    return result


def _build_resource_items(
    campaign_id: str,
    entities: List[dict],
    account_tags: dict,
    event_region: str,
    now: str,
) -> List[dict]:
    """Build validated ResourcesTable items from entity list."""
    safe_account_tags = _check_tag_payload_size(normalize_tags(account_tags))
    ttl = int(time.time()) + _TTL_SECONDS
    items = []

    for entity in entities:
        if not isinstance(entity, dict):
            continue

        entity_value = _validate_entity_value(entity.get("entityValue"))
        if entity_value is None:
            logger.warning(
                "Skipping resource with invalid entityValue — "
                "error_code=PROC_RESOURCE_WRITE_FAILED "
                "campaign_id=%s containsPII=false",
                _sanitize_log(campaign_id),
            )
            continue

        account_id = _validate_account_id(entity.get("awsAccountId"))
        if account_id is None:
            logger.warning(
                "Skipping resource with invalid accountId — "
                "error_code=PROC_RESOURCE_WRITE_FAILED "
                "campaign_id=%s entity_value=%s containsPII=false",
                _sanitize_log(campaign_id),
                _sanitize_log(entity_value),
            )
            continue

        tracking_key = _derive_tracking_key(entity_value)
        raw_status = entity.get("status", "")
        health_status = _validate_health_status(raw_status)
        region = _extract_region(entity_value, event_region)
        resource_tags = _check_tag_payload_size(
            normalize_tags(extract_resource_tags(entity))
        )
        last_updated = parse_health_date(entity.get("lastUpdatedTime")) or now

        items.append({
            "campaignId": campaign_id,
            "trackingKey": tracking_key,
            "correlationId": campaign_id,
            "entityValue": entity_value,
            "accountId": account_id,
            "region": region,
            "healthStatus": health_status,
            "ticketId": "",
            "ticketStatus": "none",
            "ticketRawStatus": None,
            "ticketUrl": None,
            "ticketUpdatedAt": None,
            "tickets": {},
            "resourceTags": resource_tags,
            "accountTags": safe_account_tags,
            "lastUpdatedTime": last_updated,
            "createdAt": now,
            "updatedAt": now,
            "expiresAt": ttl,
        })

    return items


def _batch_write(table: Any, items: List[dict]) -> tuple[int, int]:
    """Batch write items in groups of 25 with retry (design §4.4.1).

    Uses table.batch_writer() which handles Python→DynamoDB type
    serialization automatically and retries unprocessed items internally.
    """
    written = 0
    failed = 0

    for i in range(0, len(items), _MAX_BATCH_SIZE):
        batch = items[i : i + _MAX_BATCH_SIZE]
        try:
            with table.batch_writer() as writer:
                for item in batch:
                    writer.put_item(Item=item)
            written += len(batch)
        except ClientError as exc:
            logger.error(
                "BatchWriteItem failed — "
                "error_code=PROC_RESOURCE_WRITE_FAILED "
                "batch_offset=%d attempt=0 exception_type=%s "
                "containsPII=false",
                i, type(exc).__name__,
            )
            failed += len(batch)

    return written, failed


def _upsert_resource(table: Any, item: dict) -> str:
    """Conditional PutItem with UpdateItem fallback (design §3.1).

    Returns: "created", "updated", "skipped", or "failed".
    """
    campaign_id = item["campaignId"]
    tracking_key = item["trackingKey"]

    # Phase 1: Try conditional PutItem for new resource
    try:
        table.put_item(
            Item=item,
            ConditionExpression=(
                "attribute_not_exists(campaignId) AND "
                "attribute_not_exists(trackingKey)"
            ),
        )
        return "created"
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            logger.error(
                "Resource PutItem failed — "
                "error_code=PROC_RESOURCE_WRITE_FAILED "
                "campaign_id=%s tracking_key=%s exception_type=%s "
                "containsPII=false",
                _sanitize_log(campaign_id),
                _sanitize_log(tracking_key),
                type(exc).__name__,
            )
            return "failed"

    # Phase 2: Resource exists — update health status only
    try:
        table.update_item(
            Key={"campaignId": campaign_id, "trackingKey": tracking_key},
            UpdateExpression=_UPSERT_UPDATE_EXPR,
            ExpressionAttributeNames=_UPSERT_ATTR_NAMES,
            ExpressionAttributeValues={
                ":hs": item["healthStatus"],
                ":lut": item["lastUpdatedTime"],
                ":ua": item["updatedAt"],
            },
        )
        return "updated"
    except ClientError as exc:
        logger.error(
            "Resource UpdateItem failed — "
            "error_code=PROC_RESOURCE_WRITE_FAILED "
            "campaign_id=%s tracking_key=%s exception_type=%s "
            "containsPII=false",
            _sanitize_log(campaign_id),
            _sanitize_log(tracking_key),
            type(exc).__name__,
        )
        return "failed"


def _write_account_record(
    table: Any,
    campaign_id: str,
    affected_account: str,
    account_tags: dict,
    now: str,
) -> bool:
    """Write a single ACCOUNT# tracking record for account-level campaigns."""
    valid_account = _validate_account_id(affected_account)
    if valid_account is None:
        logger.warning(
            "Skipping account-level record with invalid accountId — "
            "error_code=PROC_RESOURCE_WRITE_FAILED "
            "campaign_id=%s containsPII=false",
            _sanitize_log(campaign_id),
        )
        return False

    tracking_key = f"ACCOUNT#{valid_account}"
    safe_tags = _check_tag_payload_size(normalize_tags(account_tags))
    ttl = int(time.time()) + _TTL_SECONDS

    item = {
        "campaignId": campaign_id,
        "trackingKey": tracking_key,
        "correlationId": campaign_id,
        "entityValue": None,
        "accountId": valid_account,
        "region": None,
        "healthStatus": None,
        "ticketId": "",
        "ticketStatus": "none",
        "ticketRawStatus": None,
        "ticketUrl": None,
        "ticketUpdatedAt": None,
        "tickets": {},
        "resourceTags": {},
        "accountTags": safe_tags,
        "lastUpdatedTime": None,
        "createdAt": now,
        "updatedAt": now,
        "expiresAt": ttl,
    }

    try:
        table.put_item(
            Item=item,
            ConditionExpression=(
                "attribute_not_exists(campaignId) AND "
                "attribute_not_exists(trackingKey)"
            ),
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return True  # Already exists — idempotent success
        logger.error(
            "Account record write failed — "
            "error_code=PROC_RESOURCE_WRITE_FAILED "
            "campaign_id=%s tracking_key=%s exception_type=%s "
            "containsPII=false",
            _sanitize_log(campaign_id),
            _sanitize_log(tracking_key),
            type(exc).__name__,
        )
        return False


def _compute_and_update_counts(
    resources_table: Any,
    campaigns_table: Any,
    campaign_id: str,
    now: str,
) -> dict:
    """Query ResourcesTable for absolute counts and update CampaignsTable.

    Uses SET (not ADD) for idempotent counter updates (design §3.3).
    """
    pending = 0
    resolved = 0
    total = 0

    # Paginated query to handle large campaigns
    query_kwargs = {
        "KeyConditionExpression": "campaignId = :cid",
        "ExpressionAttributeValues": {":cid": campaign_id},
        "ProjectionExpression": "healthStatus",
    }

    while True:
        resp = resources_table.query(**query_kwargs)
        for item in resp.get("Items", []):
            total += 1
            status = item.get("healthStatus")
            if status == "RESOLVED":
                resolved += 1
            elif status is not None:
                pending += 1
            # healthStatus=None → account-level record, not counted

        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
        query_kwargs["ExclusiveStartKey"] = last_key

    counts = {
        "totalResourceCount": total,
        "pendingCount": pending,
        "resolvedCount": resolved,
    }

    try:
        campaigns_table.update_item(
            Key={"campaignId": campaign_id},
            UpdateExpression=(
                "SET #trc = :trc, #pc = :pc, #rc = :rc, #ua = :ua"
            ),
            ExpressionAttributeNames={
                "#trc": "totalResourceCount",
                "#pc": "pendingCount",
                "#rc": "resolvedCount",
                "#ua": "updatedAt",
            },
            ExpressionAttributeValues={
                ":trc": total,
                ":pc": pending,
                ":rc": resolved,
                ":ua": now,
            },
            ConditionExpression="attribute_exists(campaignId)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.error(
                "Campaign not found for counter update — "
                "error_code=PROC_CAMPAIGN_NOT_FOUND "
                "campaign_id=%s containsPII=false",
                _sanitize_log(campaign_id),
            )
        else:
            logger.error(
                "Campaign counter update failed — "
                "error_code=PROC_RESOURCE_WRITE_FAILED "
                "campaign_id=%s exception_type=%s containsPII=false",
                _sanitize_log(campaign_id),
                type(exc).__name__,
            )

    return counts
