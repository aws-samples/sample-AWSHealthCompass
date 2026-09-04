"""Reconciliation Lambda — daily AWS Health API catch-up + on-demand sync.

Calls the AWS Health Organizational View API to catch events missed by
the Amazon EventBridge rule. Normalizes Health API responses into the same dict
shape the Processor extracts from EventBridge, then delegates to the
same resolve_core functions. Zero duplicated business logic.

Triggers:
  - Amazon EventBridge schedule rule: cron(0 2 * * ? *)  (daily at 02:00 UTC)
  - Async AWS Lambda invoke from API Lambda: POST /api/reconcile

SECURITY CONSTRAINTS (IMPL-026-02 / IMPL-026-04 / IMPL-026-05):
- Do NOT log raw Health API responses (contain resource ARNs, account IDs)
- Do NOT store raw exception messages in RECONCILE_STATE — use predefined
  error codes only (IMPL-026-05)
- Validate all Health API response fields before use as Amazon DynamoDB keys
  (IMPL-026-04)
- Use structured logging with field-level selection via resolve_core patterns
- State items (RECONCILE_STATE) must contain only aggregate counts and
  timestamps — no ARNs, ticket keys, or raw error messages
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from resolve_core.campaign import (
    create_or_update_campaign,
    derive_campaign_id,
    determine_campaign_type,
)
from resolve_core.dispatch import evaluate_dispatch, load_dispatch_config
from resolve_core.event_parser import (
    extract_description,
    infer_actionability,
    parse_health_date,
)
from resolve_core.resources import write_resources, update_routed_via
from resolve_core.routing import (
    derive_routed_via,
    extract_affected_account,
    resolve_account_routing,
    resolve_routing,
)
from resolve_core.tags import normalize_tags

logger = logging.getLogger("compass")
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# --- Environment variables ---

_CAMPAIGNS_TABLE = os.environ.get("CAMPAIGNS_TABLE", "compass-campaigns")
_RESOURCES_TABLE = os.environ.get("RESOURCES_TABLE", "compass-resources")
_CONFIG_TABLE = os.environ.get("CONFIG_TABLE", "compass-config")
_INTEGRATION_TOPIC_ARN = os.environ.get("INTEGRATION_TOPIC_ARN", "")
_PAYLOAD_BUCKET = os.environ.get("PAYLOAD_BUCKET", "")
_AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# --- Constants ---

_DEFAULT_LOOKBACK_HOURS = 48
_MAX_LOOKBACK_HOURS = 168  # IMPL-026-07: 7-day upper bound
_STALE_GUARD_MINUTES = 15
_MAX_ERROR_DETAILS = 20
_MAX_LOG_VALUE_LEN = 512
_MAX_EVENT_ARN_LEN = 2048
_MAX_DESCRIPTION_LEN = 32768

# IMPL-026-04: validation patterns for Health API response fields
_EVENT_ARN_PATTERN = re.compile(r"^arn:aws:health:[a-z0-9-]+::event/.+$")
_ACCOUNT_ID_PATTERN = re.compile(r"^\d{12}$")
_SERVICE_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")

# IMPL-026-05: predefined error codes — never use raw str(e)
_ERR_HEALTH_API_FAILED = "CONN_HEALTH_API_FAILED"
_ERR_ENTITY_FETCH_FAILED = "CONN_ENTITY_FETCH_FAILED"
_ERR_DETAIL_FETCH_FAILED = "CONN_DETAIL_FETCH_FAILED"
_ERR_CAMPAIGN_WRITE_FAILED = "DYNAMO_WRITE_FAILED"
_ERR_SNS_PUBLISH_FAILED = "SNS_PUBLISH_FAILED"
_ERR_EVENT_VALIDATION_FAILED = "PROC_EVENT_VALIDATION_FAILED"
_ERR_GUARD_CONFLICT = "SYS_RECONCILIATION_IN_PROGRESS"

# Allowed config prefixes (same as Processor — SEC-S011-14)
_BLOCKED_CONFIG_PREFIXES = ("JIRA_", "SNOW_", "TELEMETRY")

# --- Boto3 resources (module-level for connection reuse) ---

_dynamodb = boto3.resource("dynamodb", region_name=_AWS_REGION)
_campaigns_table = _dynamodb.Table(_CAMPAIGNS_TABLE)
_resources_table = _dynamodb.Table(_RESOURCES_TABLE)
_config_table = _dynamodb.Table(_CONFIG_TABLE)
_sns_client = boto3.client("sns", region_name=_AWS_REGION)
_s3_client = boto3.client("s3", region_name=_AWS_REGION)

# Health API client — us-east-1 only
_health_client = boto3.client(
    "health",
    region_name="us-east-1",
    config=Config(
        retries={"max_attempts": 5, "mode": "adaptive"},
        connect_timeout=10,
        read_timeout=30,
    ),
)


# ===================================================================
# Helpers
# ===================================================================


def _sanitize_log(val: Any) -> str:
    """Truncate and strip control chars for safe log output."""
    text = str(val) if val is not None else ""
    text = text.replace("\n", "").replace("\r", "").replace("\x00", "")
    return text[:_MAX_LOG_VALUE_LEN]


def _now_iso() -> str:
    """Current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_event_arn(arn: Any) -> Optional[str]:
    """Validate eventArn from Health API (IMPL-026-04)."""
    if not isinstance(arn, str) or not arn.strip():
        return None
    arn = arn.strip()[:_MAX_EVENT_ARN_LEN]
    if not _EVENT_ARN_PATTERN.match(arn):
        logger.warning(
            "Invalid eventArn format — error_code=%s arn_prefix=%s",
            _ERR_EVENT_VALIDATION_FAILED, _sanitize_log(arn[:40]),
        )
        return None
    return arn


def _validate_service(service: Any) -> Optional[str]:
    """Validate service name from Health API (IMPL-026-04)."""
    if not isinstance(service, str) or not service.strip():
        return None
    s = service.strip()[:256]
    if not _SERVICE_PATTERN.match(s):
        return None
    return s


# ===================================================================
# Concurrency Guard (Design)
# ===================================================================


def _acquire_guard(now_iso: str) -> bool:
    """Acquire reconciliation guard via DynamoDB conditional write.

    Returns True if acquired, False if another run is active.
    Uses 15-minute stale threshold for crash recovery (Design).
    """
    stale_threshold = (
        datetime.now(timezone.utc) - timedelta(minutes=_STALE_GUARD_MINUTES)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        _config_table.put_item(
            Item={
                "pk": "RECONCILE_STATE",
                "status": "running",
                "started_at": now_iso,
                "updated_at": now_iso,
            },
            ConditionExpression=(
                "attribute_not_exists(pk) "
                "OR #s <> :running "
                "OR started_at < :stale"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":running": "running",
                ":stale": stale_threshold,
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.warning(
                "Reconciliation guard conflict — "
                "error_code=%s", _ERR_GUARD_CONFLICT,
            )
            return False
        raise


def _write_state(
    status: str,
    trigger: str,
    counters: dict,
    error_details: list,
    started_at: str,
) -> None:
    """Write RECONCILE_STATE to ConfigTable (IMPL-026-03: always called)."""
    now = _now_iso()
    try:
        _config_table.put_item(Item={
            "pk": "RECONCILE_STATE",
            "status": status,
            "trigger": trigger,
            "started_at": started_at,
            "completed_at": now,
            "updated_at": now,
            "lookback_hours": _DEFAULT_LOOKBACK_HOURS,
            "events_found": counters.get("events_found", 0),
            "events_ingested": counters.get("events_ingested", 0),
            "events_updated": counters.get("events_updated", 0),
            "events_skipped": counters.get("events_skipped", 0),
            "errors": counters.get("errors", 0),
            "error_details": error_details[:_MAX_ERROR_DETAILS],
        })
    except ClientError:
        logger.error(
            "Failed to write RECONCILE_STATE — "
            "error_code=%s status=%s",
            _ERR_CAMPAIGN_WRITE_FAILED, status,
        )


# ===================================================================
# Health API Client (Design)
# ===================================================================


def _list_health_events(lookback_hours: int = _DEFAULT_LOOKBACK_HOURS) -> list:
    """Paginate DescribeEventsForOrganization with lookback filter.

    IMPL-026-07: defensive upper bound on lookback window.
    """
    if lookback_hours > _MAX_LOOKBACK_HOURS:
        logger.error(
            "Lookback exceeds maximum — defaulting to %d hours",
            _DEFAULT_LOOKBACK_HOURS,
        )
        lookback_hours = _DEFAULT_LOOKBACK_HOURS

    start_time = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    events: list = []
    paginator = _health_client.get_paginator(
        "describe_events_for_organization"
    )
    page_iterator = paginator.paginate(
        filter={
            "eventTypeCategories": ["scheduledChange", "accountNotification"],
            "startTime": {"from": start_time},
        },
    )
    for page in page_iterator:
        events.extend(page.get("events", []))

    logger.info(
        "Health API returned %d events with %dh lookback",
        len(events), lookback_hours,
    )
    return events


def _list_entities_for_event(event_arn: str) -> tuple[list, list]:
    """Paginate DescribeAffectedEntitiesForOrganization for one event.

    Returns (entities, failed_accounts).
    """
    entities: list = []
    failed: list = []
    paginator = _health_client.get_paginator(
        "describe_affected_entities_for_organization"
    )
    page_iterator = paginator.paginate(
        organizationEntityFilters=[{"eventArn": event_arn}],
    )
    for page in page_iterator:
        entities.extend(page.get("entities", []))
        failed.extend(page.get("failedSet", []))

    if failed:
        logger.warning(
            "Entity fetch partial failure — event_arn=%s failed_count=%d",
            _sanitize_log(event_arn), len(failed),
        )
    return entities, failed


def _get_event_description(event_arn: str, account_id: str) -> str:
    """Fetch event description via DescribeEventDetailsForOrganization.

    Returns description string or empty string on failure.
    """
    try:
        resp = _health_client.describe_event_details_for_organization(
            organizationEventDetailFilters=[{
                "eventArn": event_arn,
                "awsAccountId": account_id,
            }],
        )
        details = resp.get("successfulSet", [])
        if details:
            desc_obj = details[0].get("eventDescription", {})
            raw = desc_obj.get("latestDescription", "")
            if isinstance(raw, str):
                return raw[:_MAX_DESCRIPTION_LEN]
        return ""
    except ClientError:
        logger.warning(
            "Description fetch failed — error_code=%s event_arn=%s",
            _ERR_DETAIL_FETCH_FAILED, _sanitize_log(event_arn),
        )
        return ""


# ===================================================================
# Normalization Layer (Design)
# ===================================================================


def _normalize_health_api_event(
    event: dict,
    entities: list,
    description: str,
) -> Optional[dict]:
    """Convert Health API response to internal event format.

    Returns a dict identical in shape to what the Processor extracts
    from an EventBridge Health event detail object, or None if the
    event fails validation (IMPL-026-04).

    Resource tags are read inline from each Health API ``AffectedEntity``
    (the ``tags`` field, defensively falling back to ``resourceTags``).
    That field exists in the API response shape but is "Currently not
    supported" by AWS today, so it is empty for now; the read is
    forward-built and auto-populates when AWS ships inline entity tags.
    Empty resource tags degrade to account-ID / default routing via the
    unchanged routing engine's failover chain.

    Account tags stay empty: the Health Organizational View API exposes
    no account-tags field of any kind (documented AWS Health behavior).
    This is a documented
    limitation; account-ID / default routing covers
    those events.
    """
    event_arn = _validate_event_arn(event.get("arn"))
    if event_arn is None:
        return None

    service = _validate_service(event.get("service"))
    if service is None:
        return None

    event_type_code = event.get("eventTypeCode", "")
    if not isinstance(event_type_code, str) or not event_type_code.strip():
        return None

    # Derive affectedAccount from first entity, or empty string
    affected_account = ""
    for entity in entities:
        if isinstance(entity, dict):
            acct = entity.get("awsAccountId", "")
            if isinstance(acct, str) and _ACCOUNT_ID_PATTERN.match(acct):
                affected_account = acct
                break

    # Normalize entities to match EventBridge shape
    normalized_entities = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        entity_value = entity.get("entityValue", "")
        if not isinstance(entity_value, str) or not entity_value.strip():
            continue
        acct = entity.get("awsAccountId", "")
        if not isinstance(acct, str) or not _ACCOUNT_ID_PATTERN.match(acct):
            continue
        normalized_entities.append({
            "entityValue": entity_value.strip()[:_MAX_EVENT_ARN_LEN],
            "awsAccountId": acct,
            "status": entity.get("statusCode", "PENDING"),
            "lastUpdatedTime": entity.get("lastUpdatedTime"),
            # Resource tags read inline from the Health API AffectedEntity.
            # Single raw-field read point: `tags` (AWS AffectedEntity schema
            # field) OR `resourceTags` (EventBridge-shaped variant), else {}.
            # Empty `{}` is falsy so it falls through; a populated `tags`
            # wins; normalize_tags coerces any non-dict to {} (never raises).
            # Field is "Currently not supported" by AWS today -> empty ->
            # account/default failover; auto-populates when AWS ships it.
            "resourceTags": normalize_tags(
                entity.get("tags") or entity.get("resourceTags") or {}
            ),
        })

    # Build detail dict matching EventBridge shape
    return {
        "eventArn": event_arn,
        "service": service,
        "eventTypeCode": event_type_code.strip(),
        "eventTypeCategory": event.get("eventTypeCategory", ""),
        "region": event.get("region", ""),
        "startTime": parse_health_date(event.get("startTime")) or "",
        "endTime": parse_health_date(event.get("endTime")) or "",
        "lastUpdatedTime": parse_health_date(
            event.get("lastUpdatedTime")
        ) or "",
        "statusCode": event.get("statusCode", ""),
        "affectedAccount": affected_account,
        # Org View API exposes no account-tags field (documented
        # AWS Health behavior); documented
        # limitation, failover covers it.
        "accountTags": {},
        "eventDescription": [{"latestDescription": description}],
        "affectedEntities": normalized_entities,
    }


# ===================================================================
# Config Loading (mirrors Processor pattern)
# ===================================================================


def _load_config() -> dict:
    """Load routing and dispatch config from ConfigTable."""
    config: dict = {}

    for pk in ("FILTER_BACKUP_EVENTS", "DISPATCH_PRESET",
               "ROUTING_DEFAULT", "ROUTING_STRATEGY"):
        try:
            resp = _config_table.get_item(
                Key={"pk": pk}, ConsistentRead=False,
            )
            item = resp.get("Item")
            if item:
                config[pk] = item
        except ClientError:
            logger.error(
                "Config load failed — error_code=CONFIG_LOAD_FAILED key=%s",
                pk,
            )

    # Scan for prefix-based keys
    for prefix in ("DISPATCH_RULE#", "TAG_ROUTING#", "ROUTING#"):
        try:
            scan_kwargs: dict[str, Any] = {
                "FilterExpression": "begins_with(pk, :prefix)",
                "ExpressionAttributeValues": {":prefix": prefix},
                "ConsistentRead": False,
            }
            item_count = 0
            while True:
                resp = _config_table.scan(**scan_kwargs)
                for item in resp.get("Items", []):
                    pk_val = item.get("pk", "")
                    if isinstance(pk_val, str) and pk_val.startswith(
                        _BLOCKED_CONFIG_PREFIXES
                    ):
                        continue
                    config[pk_val] = item
                    item_count += 1
                    if item_count >= 1000:
                        break
                if item_count >= 1000:
                    break
                if "LastEvaluatedKey" not in resp:
                    break
                scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        except ClientError:
            logger.error(
                "Config scan failed — error_code=CONFIG_LOAD_FAILED "
                "prefix=%s", prefix,
            )

    return config


# ===================================================================
# Dispatch & Routing (reuses resolve_core)
# ===================================================================


def _evaluate_dispatch_for_event(detail: dict, config: dict) -> dict:
    """Evaluate dispatch window for a reconciliation event."""
    preset = config.get("DISPATCH_PRESET", {})
    mode = preset.get("mode", "all") if isinstance(preset, dict) else "all"
    # Build rules list from config cache
    rules = [
        v for k, v in config.items()
        if isinstance(k, str) and k.startswith("DISPATCH_RULE#")
        and isinstance(v, dict)
    ]
    rules.sort(key=lambda r: r.get("rule_id", ""))
    dispatch_config = {"mode": mode, "rules": rules}
    return evaluate_dispatch(
        detail.get("eventTypeCode", ""),
        detail.get("eventTypeCategory", ""),
        dispatch_config,
    )


def _resolve_routing_for_event(
    detail: dict, config: dict,
) -> dict:
    """Resolve routing for a reconciliation event.

    Uses empty envelope since reconciliation has no EventBridge envelope.
    """
    account_tags = normalize_tags(detail.get("accountTags", {}))
    entities = detail.get("affectedEntities", [])
    return resolve_routing(
        detail=detail,
        envelope={},  # No EventBridge envelope for reconciliation
        account_tags=account_tags,
        entities=entities,
        config_cache=config,
    )


# ===================================================================
# SNS Publish (mirrors Processor pattern)
# ===================================================================

_MAX_SNS_BYTES = 200 * 1024
_HARD_SNS_LIMIT = 256 * 1024
_CAMPAIGN_ID_SAFE = re.compile(r"[^a-zA-Z0-9:_\-.]")


def _build_standardized_event(
    detail: dict,
    entities: list,
    campaign_id: str,
    campaign_type: str,
    routing: dict,
    dispatch: dict,
    now: str,
) -> dict:
    """Build v2.0 standardized event for SNS publish."""
    actionability_result = infer_actionability(detail)
    resources = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        resources.append({
            "arn": entity.get("entityValue", ""),
            "entityValue": entity.get("entityValue", ""),
            "accountId": entity.get("awsAccountId", ""),
            "status": entity.get("status", ""),
            "lastUpdatedTime": parse_health_date(
                entity.get("lastUpdatedTime")
            ) or "",
            # Derived from the already-normalized entity (single raw read
            # lives in _normalize_health_api_event). Re-normalization is
            # idempotent and mirrors the processor's build for parity.
            "resourceTags": normalize_tags(entity.get("resourceTags", {})),
        })

    return {
        "timestamp": now,
        "source": "compass-reconciliation",
        "version": "2.0",
        "event": {
            "eventArn": detail.get("eventArn", ""),
            "eventTypeCode": detail.get("eventTypeCode", ""),
            "eventTypeCategory": detail.get("eventTypeCategory", ""),
            "service": detail.get("service", ""),
            "region": detail.get("region", ""),
            "affectedAccount": detail.get("affectedAccount", ""),
            "startTime": detail.get("startTime", ""),
            "endTime": detail.get("endTime", ""),
            "description": extract_description(
                detail.get("eventDescription")
            ),
            "statusCode": detail.get("statusCode", ""),
            "actionability": actionability_result.value,
            "actionabilityInferred": actionability_result.was_inferred,
            "campaignId": campaign_id,
            "campaignType": campaign_type,
            "action": "RECONCILE",
        },
        "resources": resources,
        # Org View API exposes no account-tags field (documented
        # AWS Health behavior); documented
        # limitation, failover covers it.
        "accountTags": {},
        "routing": routing,
        "dispatch": dispatch,
        "metadata": {
            "originalEventId": None,
            "originalEventTime": None,
            "processingTime": now,
            "ingestionSource": "reconciliation",
            "schemaVersion": "2.0",
        },
    }


def _publish_to_sns(std_event: dict, campaign_id: str) -> None:
    """Publish standardized event to SNS with S3 offload for large payloads."""
    payload = json.dumps(std_event, separators=(",", ":"), default=str)
    payload_bytes = len(payload.encode("utf-8"))

    event_data = std_event.get("event", {})
    attrs = {
        "service": {
            "DataType": "String",
            "StringValue": event_data.get("service", "UNKNOWN")[:256] or "UNKNOWN",
        },
        "eventTypeCategory": {
            "DataType": "String",
            "StringValue": event_data.get("eventTypeCategory", "UNKNOWN")[:256] or "UNKNOWN",
        },
        "action": {
            "DataType": "String",
            "StringValue": "RECONCILE",
        },
        "hasResources": {
            "DataType": "String",
            "StringValue": str(bool(std_event.get("resources"))).lower(),
        },
    }

    if payload_bytes <= _MAX_SNS_BYTES:
        _sns_client.publish(
            TopicArn=_INTEGRATION_TOPIC_ARN,
            Message=payload,
            MessageAttributes=attrs,
        )
        return

    # S3 offload for large payloads
    safe_id = _CAMPAIGN_ID_SAFE.sub("", campaign_id)[:256]
    now_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    s3_key = f"payloads/{safe_id}/{now_ts}.json"

    try:
        _s3_client.put_object(
            Bucket=_PAYLOAD_BUCKET,
            Key=s3_key,
            Body=payload.encode("utf-8"),
            ContentType="application/json",
        )
    except ClientError:
        if payload_bytes < _HARD_SNS_LIMIT:
            _sns_client.publish(
                TopicArn=_INTEGRATION_TOPIC_ARN,
                Message=payload,
                MessageAttributes=attrs,
            )
            return
        raise

    reference = json.dumps({
        "s3_bucket": _PAYLOAD_BUCKET,
        "s3_key": s3_key,
        "payload_bytes": payload_bytes,
    })
    attrs["payloadLocation"] = {
        "DataType": "String",
        "StringValue": "s3",
    }
    _sns_client.publish(
        TopicArn=_INTEGRATION_TOPIC_ARN,
        Message=reference,
        MessageAttributes=attrs,
    )


# ===================================================================
# Per-Event Processing
# ===================================================================


def _process_single_event(
    health_event: dict,
    config: dict,
    now: str,
) -> str:
    """Process one Health API event through the core pipeline.

    Returns: "ingested" | "updated" | "skipped"
    Raises on unrecoverable errors (caller catches per-event).
    """
    event_arn = health_event.get("arn", "")

    # Fetch entities
    entities, _ = _list_entities_for_event(event_arn)

    # Fetch description — need an account ID for the API call
    account_id = ""
    for entity in entities:
        if isinstance(entity, dict):
            acct = entity.get("awsAccountId", "")
            if isinstance(acct, str) and _ACCOUNT_ID_PATTERN.match(acct):
                account_id = acct
                break
    description = _get_event_description(event_arn, account_id) if account_id else ""

    # Normalize to internal format
    detail = _normalize_health_api_event(health_event, entities, description)
    if detail is None:
        logger.warning(
            "Event failed validation — error_code=%s event_arn=%s",
            _ERR_EVENT_VALIDATION_FAILED, _sanitize_log(event_arn),
        )
        raise ValueError("Event failed validation")

    normalized_entities = detail.get("affectedEntities", [])

    # Filter INFORMATIONAL events
    actionability_result = infer_actionability(detail)
    if actionability_result.value == "INFORMATIONAL":
        return "skipped"

    # Derive campaign ID
    campaign_id = derive_campaign_id(detail)
    campaign_type = determine_campaign_type(normalized_entities)

    # Check if campaign exists (Design)
    existing = _get_existing_campaign(campaign_id)

    # Create or merge campaign
    campaign_result = create_or_update_campaign(
        table=_campaigns_table,
        detail=detail,
        entities=normalized_entities,
        now=now,
        mode="reconciliation",
    )

    # Write resources
    affected_account = detail.get("affectedAccount", "")
    event_region = detail.get("region", "")
    is_new = campaign_result.action == "CREATED"

    write_resources(
        resources_table=_resources_table,
        campaigns_table=_campaigns_table,
        campaign_id=campaign_id,
        campaign_type=campaign_type,
        entities=normalized_entities,
        account_tags={},  # Not available from Health API
        affected_account=affected_account,
        event_region=event_region,
        is_new_campaign=is_new,
        now=now,
    )

    # SNS publish only for NEW campaigns that pass dispatch + routing
    # (Design — existing campaigns already dispatched)
    if is_new:
        dispatch_result = _evaluate_dispatch_for_event(detail, config)
        if dispatch_result.get("dispatched") is True:
            routing_result = _resolve_routing_for_event(detail, config)

            # --- persist routing attribution ---
            # Mirror the processor's step (k.1) write so routing coverage
            # counts reconciliation-ingested resources accurately. Decoupled
            # from the SNS-publish gate (resolvedProject, a JIRA-only signal)
            # so attribution records the TRUE outcome even for
            # ServiceNow-only / default / error cases. Derived from the shared
            # helper so the persisted vocabulary matches the coverage reader's
            # whitelist and cannot drift from the processor (never raw "tag").
            # Best-effort / non-fatal (update_routed_via swallows ClientError):
            # an attribution write failure must not abort ingestion.
            routed_via = derive_routed_via(
                routing_result, config.get("ROUTING_STRATEGY"),
            )
            routing_error = (
                f"No routing rule matched for account {affected_account}"
                if routing_result.get("resolvedBy") == "error" else None
            )
            update_routed_via(
                _resources_table, campaign_id, routed_via, routing_error, now,
            )

            if routing_result.get("resolvedProject") is not None:
                # Update campaign with dispatch/routing state
                _update_campaign_dispatch(
                    campaign_id, True, dispatch_result, now,
                )
                std_event = _build_standardized_event(
                    detail=detail,
                    entities=normalized_entities,
                    campaign_id=campaign_id,
                    campaign_type=campaign_type,
                    routing=routing_result,
                    dispatch=dispatch_result,
                    now=now,
                )
                _publish_to_sns(std_event, campaign_id)
                logger.info(
                    "Reconciliation published — campaign_id=%s",
                    _sanitize_log(campaign_id),
                )
            else:
                _update_campaign_dispatch(
                    campaign_id, True, dispatch_result, now,
                )
        else:
            _update_campaign_dispatch(
                campaign_id, False, dispatch_result, now,
            )
        return "ingested"

    return "updated" if campaign_result.resource_count > 0 else "skipped"


def _get_existing_campaign(campaign_id: str) -> Optional[dict]:
    """Check if campaign exists in CampaignsTable."""
    try:
        resp = _campaigns_table.get_item(
            Key={"campaignId": campaign_id},
            ProjectionExpression="campaignId, #s, dispatched",
            ExpressionAttributeNames={"#s": "status"},
            ConsistentRead=False,
        )
        return resp.get("Item")
    except ClientError:
        return None


def _update_campaign_dispatch(
    campaign_id: str, dispatched: bool, dispatch_result: dict, now: str,
) -> None:
    """Update campaign with dispatch result (mirrors Processor pattern)."""
    try:
        _campaigns_table.update_item(
            Key={"campaignId": campaign_id},
            UpdateExpression=(
                "SET #d = :d, #dm = :dm, #mr = :mr, #s = :s, #ua = :ua"
            ),
            ExpressionAttributeNames={
                "#d": "dispatched",
                "#dm": "dispatchMode",
                "#mr": "matchedRule",
                "#s": "status",
                "#ua": "updatedAt",
            },
            ExpressionAttributeValues={
                ":d": dispatched,
                ":dm": dispatch_result.get("mode", "unknown"),
                ":mr": dispatch_result.get("matchedRule"),
                ":s": "ACTIVE" if dispatched else "FILTERED",
                ":ua": now,
            },
        )
    except ClientError:
        logger.error(
            "Campaign dispatch update failed — "
            "error_code=%s campaign_id=%s",
            _ERR_CAMPAIGN_WRITE_FAILED, _sanitize_log(campaign_id),
        )


# ===================================================================
# Main Handler
# ===================================================================


def lambda_handler(event: dict, context: Any) -> dict:
    """Reconciliation Lambda entry point.

    IMPL-026-02: Validates trigger source from event payload.
    IMPL-026-03: Uses try/finally to always write terminal state.
    """
    # Step 1: Detect trigger type (IMPL-026-02)
    trigger = "scheduled"
    if isinstance(event, dict) and event.get("source") == "dashboard":
        trigger = "on-demand"
    elif isinstance(event, dict) and event.get("detail-type") == "Scheduled Event":
        trigger = "scheduled"

    logger.info(
        "Reconciliation invoked — trigger=%s region=%s",
        trigger, _AWS_REGION,
    )

    started_at = _now_iso()
    counters = {
        "events_found": 0,
        "events_ingested": 0,
        "events_updated": 0,
        "events_skipped": 0,
        "errors": 0,
    }
    error_details: list = []

    # Step 2: Acquire concurrency guard (Design)
    if not _acquire_guard(started_at):
        logger.warning(
            "Reconciliation skipped — another run is active"
        )
        return {"status": "skipped", "reason": "concurrent_execution"}

    # IMPL-026-03: try/finally ensures terminal state is always written
    try:
        # Step 3: Call Health API
        try:
            health_events = _list_health_events(_DEFAULT_LOOKBACK_HOURS)
        except ClientError:
            logger.error(
                "Health API initial call failed — error_code=%s",
                _ERR_HEALTH_API_FAILED,
            )
            _write_state("failed", trigger, counters, [{
                "errorCode": _ERR_HEALTH_API_FAILED,
                "message": "Health API initial call failed",
            }], started_at)
            return {"status": "failed", "reason": "health_api_error"}

        counters["events_found"] = len(health_events)

        if not health_events:
            logger.info("No events found in lookback window")
            _write_state("completed", trigger, counters, [], started_at)
            return {"status": "completed", **counters}

        # Step 4: Load config once for all events
        config = _load_config()
        now = _now_iso()

        # Step 5: Process each event independently (Design)
        for health_event in health_events:
            event_arn = _sanitize_log(health_event.get("arn", "UNKNOWN"))
            try:
                result = _process_single_event(health_event, config, now)
                if result == "ingested":
                    counters["events_ingested"] += 1
                elif result == "updated":
                    counters["events_updated"] += 1
                else:
                    counters["events_skipped"] += 1
            except Exception:
                counters["errors"] += 1
                if len(error_details) < _MAX_ERROR_DETAILS:
                    error_details.append({
                        "errorCode": _ERR_CAMPAIGN_WRITE_FAILED,
                        "message": "Event processing failed",
                    })
                logger.error(
                    "Per-event error — error_code=%s event_arn=%s",
                    _ERR_CAMPAIGN_WRITE_FAILED, event_arn,
                )

        # Step 6: Write final state
        status = (
            "completed_with_errors" if counters["errors"] > 0
            else "completed"
        )
        _write_state(status, trigger, counters, error_details, started_at)

        logger.info(
            "Reconciliation complete — status=%s found=%d ingested=%d "
            "updated=%d skipped=%d errors=%d",
            status,
            counters["events_found"],
            counters["events_ingested"],
            counters["events_updated"],
            counters["events_skipped"],
            counters["errors"],
        )
        return {"status": status, **counters}

    except Exception:
        # IMPL-026-03: always write terminal state on unhandled error
        counters["errors"] += 1
        _write_state("failed", trigger, counters, [{
            "errorCode": _ERR_HEALTH_API_FAILED,
            "message": "Unhandled reconciliation error",
        }], started_at)
        raise
