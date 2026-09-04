"""Processor Lambda — Amazon SQS handler and Amazon SNS publish orchestrator.

Receives AWS Health events from the SQS Ingestion Queue (batch_size=1,
ReportBatchItemFailures=true) and executes the processing pipeline:
parse → filter → normalize → dedup → dispatch → route → publish.

Each step calls a shared-core module from resolve_core. This handler
is a thin orchestrator — it sequences calls and manages the SQS contract.

Consumers: SQS ESM (Ingestion Queue).
Dependencies: resolve_core (AWS Lambda Layer), boto3.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import random
from datetime import datetime, timezone
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError

from resolve_core.event_parser import (
    coerce_page_fields,
    extract_account_tags,
    extract_description,
    extract_entities,
    infer_actionability,
    parse_health_date,
    should_filter_backup_event,
)
from resolve_core.campaign import (
    create_or_update_campaign,
    derive_campaign_id,
    determine_campaign_type,
)
from resolve_core.pagination import (
    determine_pagination_path,
    should_publish_sns,
)
from resolve_core.resources import write_resources, update_routed_via
from resolve_core.tags import normalize_tags
from resolve_core.dispatch import evaluate_dispatch, load_dispatch_config
from resolve_core.routing import resolve_routing, derive_routed_via

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

_SUCCESS = {"batchItemFailures": []}
_MAX_LOG_ARN = 512
_MAX_SNS_BYTES = 200 * 1024  # 200 KB soft threshold for S3 offload
_HARD_SNS_LIMIT = 256 * 1024  # 256 KB SNS hard limit
_MAX_SNS_ATTR_BYTES = 256
_CAMPAIGN_ID_SAFE = re.compile(r"[^a-zA-Z0-9:_\-.]")
# FINDING-IMPL-018-04: canonical _ACCOUNT_ID_PATTERN is in resolve_core.routing.
# Import it from there instead of duplicating.
from resolve_core.routing import _ACCOUNT_ID_PATTERN

# Config cache keys we explicitly load — credential-adjacent keys excluded
# per SEC-S011-14 / FINDING-IMPL-02.
_ALLOWED_CONFIG_PREFIXES = (
    "FILTER_BACKUP_EVENTS",
    "DISPATCH_PRESET",
    "DISPATCH_RULE#",
    "ROUTING_DEFAULT",
    "ROUTING_STRATEGY",
    "TAG_ROUTING#",
    "SERVICE_ROUTING#",
)
_BLOCKED_CONFIG_PREFIXES = ("JIRA_", "SNOW_", "TELEMETRY")

# --- Config cache (module-level, persists across warm invocations) ---

_config_cache: dict = {}
_cache_loaded_at: float = 0.0
_CACHE_TTL_SECONDS: int = 300  # 5 minutes

# --- Boto3 resources (module-level for connection reuse) ---

_dynamodb = boto3.resource("dynamodb", region_name=_AWS_REGION)
_sns_client = boto3.client("sns", region_name=_AWS_REGION)
_s3_client = boto3.client("s3", region_name=_AWS_REGION)

_campaigns_table = _dynamodb.Table(_CAMPAIGNS_TABLE)
_resources_table = _dynamodb.Table(_RESOURCES_TABLE)
_config_table = _dynamodb.Table(_CONFIG_TABLE)


# ===================================================================
# Config Cache
# ===================================================================


def _load_config() -> dict:
    """Load configuration from ConfigTable with targeted reads.

    Uses GetItem for known singleton keys and Scan with filter for
    prefix-based keys. Discards credential-adjacent items per
    SEC-S011-14 / FINDING-IMPL-02.
    """
    config: dict = {}

    # Singleton keys via GetItem
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

    # Prefix-based keys via Scan with filter
    for prefix in ("DISPATCH_RULE#", "TAG_ROUTING#", "SERVICE_ROUTING#"):
        list_key = prefix.rstrip("#") + "_LIST"
        config[list_key] = []
        try:
            scan_kwargs: dict[str, Any] = {
                "FilterExpression": "begins_with(pk, :prefix)",
                "ExpressionAttributeValues": {":prefix": prefix},
                "ConsistentRead": False,
            }
            # FINDING-IMPL-018-01: safety bound to prevent memory exhaustion
            item_count = 0
            _MAX_SCAN_ITEMS = 1000
            while True:
                resp = _config_table.scan(**scan_kwargs)
                for item in resp.get("Items", []):
                    pk_val = item.get("pk", "")
                    # SEC-S011-14: discard credential-adjacent items
                    if isinstance(pk_val, str) and pk_val.startswith(
                        _BLOCKED_CONFIG_PREFIXES
                    ):
                        continue
                    config[list_key].append(item)
                    item_count += 1
                    if item_count >= _MAX_SCAN_ITEMS:
                        logger.warning(
                            "Config scan safety bound reached — "
                            "error_code=CONFIG_SCAN_LIMIT prefix=%s "
                            "limit=%d",
                            prefix, _MAX_SCAN_ITEMS,
                        )
                        break
                if item_count >= _MAX_SCAN_ITEMS:
                    break
                if "LastEvaluatedKey" not in resp:
                    break
                scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        except ClientError:
            logger.error(
                "Config scan failed — error_code=CONFIG_LOAD_FAILED "
                "prefix=%s", prefix,
            )

    # Validate critical config (SEC-S011-03)
    _validate_config(config)

    # Index TAG_ROUTING# items by pk for O(1) lookup in routing module.
    # resolve_routing() calls config_cache.get("TAG_ROUTING#{value}").
    for item in config.get("TAG_ROUTING_LIST", []):
        pk_val = item.get("pk", "")
        if isinstance(pk_val, str) and pk_val.startswith("TAG_ROUTING#"):
            config[pk_val] = item

    # Index SERVICE_ROUTING# items by pk for O(1) lookup.
    for item in config.get("SERVICE_ROUTING_LIST", []):
        pk_val = item.get("pk", "")
        if isinstance(pk_val, str) and pk_val.startswith("SERVICE_ROUTING#"):
            config[pk_val] = item

    return config


def _validate_config(config: dict) -> None:
    """Validate loaded config items. Log warnings for invalid values."""
    preset = config.get("DISPATCH_PRESET", {})
    mode = preset.get("mode", "all") if isinstance(preset, dict) else "all"
    if mode not in ("all", "ple_only", "custom"):
        logger.warning(
            "Invalid dispatch preset mode — "
            "error_code=CONFIG_VALIDATION_FAILED field=DISPATCH_PRESET.mode "
            "value=%s defaulting=all",
            _sanitize_log(mode),
        )
        if isinstance(preset, dict):
            preset["mode"] = "all"

    default_routing = config.get("ROUTING_DEFAULT", {})
    if isinstance(default_routing, dict):
        project = default_routing.get("jira_project", "")
        has_any_target = bool(project) or bool(
            default_routing.get("snow_assignment_group_id")
        )
        if not has_any_target:
            logger.warning(
                "Missing default routing target — "
                "error_code=CONFIG_VALIDATION_FAILED "
                "field=ROUTING_DEFAULT (no jira_project or snow_assignment_group_id)",
            )


def get_config() -> dict:
    """Return cached config, refreshing if TTL expired."""
    global _config_cache, _cache_loaded_at
    now = time.monotonic()
    # SEC-S011-12: TTL jitter (0–30s) to avoid thundering herd
    jitter = random.uniform(0, 30)
    if not _config_cache or (now - _cache_loaded_at) > (_CACHE_TTL_SECONDS + jitter):
        _config_cache = _load_config()
        _cache_loaded_at = now
    return _config_cache


def _get_account_routing(account_id: str) -> Optional[dict]:
    """Single GetItem for ROUTING#{accountId}. Not cached.

    FINDING-IMPL-017-01: validates account_id format internally.
    Private function — callers within this module only.
    """
    if not isinstance(account_id, str) or not _ACCOUNT_ID_PATTERN.fullmatch(
        account_id
    ):
        return None
    try:
        resp = _config_table.get_item(
            Key={"pk": f"ROUTING#{account_id}"},
            ConsistentRead=False,
            ProjectionExpression="pk, jira_project, jira_issue_type, "
            "snow_assignment_group_id, snow_assignment_group_name, snow_record_type, "
            "account_id, account_name",
        )
        return resp.get("Item")
    except ClientError:
        logger.error(
            "Account routing lookup failed — "
            "error_code=CONFIG_LOAD_FAILED account_id=%s",
            account_id,
        )
        return None


# ===================================================================
# Dispatch Window Evaluation
# ===================================================================


def _evaluate_dispatch(detail: dict, config: dict, actionability_result: Any) -> dict:
    """Evaluate whether this event should create tickets.

    Delegates to the shared ``resolve_core.dispatch.evaluate_dispatch``
    pure function. Builds the dispatch config dict expected by the shared
    module from the handler's cached config.

    Returns dict with keys: dispatched (bool), mode (str),
    matchedRule (str|None).
    """
    event_type_code = detail.get("eventTypeCode", "")
    event_type_category = detail.get("eventTypeCategory", "")

    preset = config.get("DISPATCH_PRESET", {})
    mode = preset.get("mode", "all") if isinstance(preset, dict) else "all"

    # Build config dict for shared module.
    dispatch_config = {
        "mode": mode,
        "rules": config.get("DISPATCH_RULE_LIST", []),
        "actionability_filter": preset.get("actionability_filter", "all_actionable") if isinstance(preset, dict) else "all_actionable",
    }

    return evaluate_dispatch(
        event_type_code,
        event_type_category,
        dispatch_config,
        actionability=actionability_result.value if actionability_result else "",
    )


# ===================================================================
# Routing Resolution
# ===================================================================


def _resolve_routing_with_live_lookup(
    detail: dict,
    envelope: dict,
    account_tags: dict,
    entities: list,
    config: dict,
) -> dict:
    """Resolve JIRA project via shared routing module.

    Enriches the config cache with a live DynamoDB lookup for the
    specific account (avoids scanning all ROUTING# items into cache),
    then delegates to ``resolve_core.routing.resolve_routing``.

    FINDING-IMPL-017-02: passes ``envelope`` for C-9 fallback.
    """
    from resolve_core.routing import extract_affected_account

    # Live lookup for the specific account — avoids full ROUTING# scan.
    affected_account = extract_affected_account(detail, envelope)
    if affected_account:
        key = f"ROUTING#{affected_account}"
        if key not in config:
            item = _get_account_routing(affected_account)
            if item is not None:
                config[key] = item

    return resolve_routing(
        detail=detail,
        envelope=envelope,
        account_tags=account_tags,
        entities=entities,
        config_cache=config,
    )


# ===================================================================
# Standardized Event Builder
# ===================================================================


def _build_standardized_event(
    detail: dict,
    entities: list,
    account_tags: dict,
    actionability_result: Any,
    campaign_id: str,
    campaign_type: str,
    action: str,
    routing: dict,
    dispatch: dict,
    envelope: dict,
    now: str,
) -> dict:
    """Build the v2.0 standardized event for SNS publish."""
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
            "resourceTags": normalize_tags(entity.get("resourceTags", {})),
        })

    return {
        "timestamp": now,
        "source": "compass",
        "version": "2.1",
        "event": {
            "eventArn": detail.get("eventArn", ""),
            "eventTypeCode": detail.get("eventTypeCode", ""),
            "eventTypeCategory": detail.get("eventTypeCategory", ""),
            "service": detail.get("service", ""),
            "region": detail.get("region", ""),
            "affectedAccount": detail.get("affectedAccount", ""),
            "startTime": parse_health_date(detail.get("startTime")) or "",
            "endTime": parse_health_date(detail.get("endTime")) or "",
            "description": extract_description(
                detail.get("eventDescription")
            ),
            "statusCode": detail.get("statusCode", ""),
            "actionability": actionability_result.value,
            "actionabilityInferred": actionability_result.was_inferred,
            "campaignId": campaign_id,
            "campaignType": campaign_type,
            "action": action,
        },
        "resources": resources,
        "accountTags": account_tags,
        "routing": routing,
        "dispatch": dispatch,
        "metadata": {
            "originalEventId": envelope.get("id", ""),
            "originalEventTime": envelope.get("time", ""),
            "processingTime": now,
            "schemaVersion": "2.1",
        },
    }


# ===================================================================
# SNS Publish (with S3 offload)
# ===================================================================


def _sanitize_sns_attr(val: str) -> str:
    """Sanitize a value for SNS message attribute (SEC-S011-05)."""
    if not isinstance(val, str) or not val:
        return "UNKNOWN"
    # Strip null bytes, truncate to 256 bytes
    cleaned = val.replace("\x00", "")
    encoded = cleaned.encode("utf-8")[:_MAX_SNS_ATTR_BYTES]
    return encoded.decode("utf-8", errors="ignore") or "UNKNOWN"


def _sanitize_s3_key(campaign_id: str) -> str:
    """Sanitize campaignId for S3 key construction (SEC-S011-06)."""
    return _CAMPAIGN_ID_SAFE.sub("", campaign_id)[:256]


def _publish_to_sns(std_event: dict, campaign_id: str) -> None:
    """Publish standardized event to SNS, with S3 offload for large payloads.

    Delegates to ``resolve_core.payload.publish_or_offload`` for the
    claim-check pattern.
    """
    from resolve_core.payload import publish_or_offload

    event_data = std_event.get("event", {})
    attrs = {
        "service": {
            "DataType": "String",
            "StringValue": _sanitize_sns_attr(event_data.get("service", "")),
        },
        "eventTypeCategory": {
            "DataType": "String",
            "StringValue": _sanitize_sns_attr(
                event_data.get("eventTypeCategory", "")
            ),
        },
        "actionability": {
            "DataType": "String",
            "StringValue": _sanitize_sns_attr(
                event_data.get("actionability", "")
            ),
        },
        "hasResources": {
            "DataType": "String",
            "StringValue": str(bool(std_event.get("resources"))).lower(),
        },
        "action": {
            "DataType": "String",
            "StringValue": _sanitize_sns_attr(
                event_data.get("action", "CREATE")
            ),
        },
        "hasJiraRouting": {
            "DataType": "String",
            "StringValue": str(bool(
                std_event.get("routing", {}).get("platforms", {}).get("jira")
            )).lower(),
        },
        "hasServicenowRouting": {
            "DataType": "String",
            "StringValue": str(bool(
                std_event.get("routing", {}).get("platforms", {}).get("servicenow")
            )).lower(),
        },
    }

    result = publish_or_offload(
        sns_client=_sns_client,
        s3_client=_s3_client,
        topic_arn=_INTEGRATION_TOPIC_ARN,
        bucket=_PAYLOAD_BUCKET,
        event_dict=std_event,
        message_attributes=attrs,
    )
    logger.info(
        "SNS publish complete — campaign_id=%s method=%s payload_bytes=%d",
        _sanitize_log(campaign_id), result["method"], result["size"],
    )


# ===================================================================
# Helpers
# ===================================================================


def _sanitize_log(val: Any) -> str:
    """Truncate and strip control chars for safe log output."""
    text = str(val) if val is not None else ""
    text = text.replace("\n", "").replace("\r", "").replace("\x00", "")
    return text[:_MAX_LOG_ARN]


def _now_iso() -> str:
    """Current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _derive_routed_via(routing_result: dict, config: dict) -> str:
    """Deprecated local shim — delegates to the shared resolve_core helper.

    Kept as a thin wrapper so any remaining call sites and tests referencing
    the processor-local name continue to work. The single source of truth for
    the write-side ``routedVia`` vocabulary now lives in
    :func:`resolve_core.routing.derive_routed_via`, shared
    with the Reconciliation Lambda so the two paths cannot drift.
    """
    return derive_routed_via(routing_result, config.get("ROUTING_STRATEGY"))


def _update_campaign_dispatch(
    campaign_id: str, dispatched: bool, dispatch_result: dict, now: str,
) -> None:
    """Update campaign with dispatch result fields.

    Writes ``dispatched``, ``dispatchMode``, ``matchedRule``, ``status``,
    and ``updatedAt`` per design Step 11 / FINDING-IMPL-09.

    Raises on DynamoDB failure so the handler returns the message to SQS
    instead of proceeding to SNS publish (FINDING-IMPL-14).
    """
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


# ===================================================================
# Main Handler
# ===================================================================


def lambda_handler(event: dict, context: Any) -> dict:
    """SQS ESM handler. batch_size=1, ReportBatchItemFailures=true.

    Returns {"batchItemFailures": []} on success, or
    {"batchItemFailures": [{"itemIdentifier": messageId}]} on failure.
    """
    records = event.get("Records", [])
    if not records:
        return _SUCCESS

    record = records[0]
    message_id = record.get("messageId", "")
    event_arn = "UNKNOWN"

    try:
        # --- PARSE SQS RECORD (SEC-S011-01) ---
        raw_body = record.get("body", "")
        try:
            envelope = json.loads(raw_body)
        except (json.JSONDecodeError, TypeError):
            logger.error(
                "JSON parse failed — error_code=PROC_JSON_PARSE_FAILED "
                "message_id=%s region=%s",
                message_id, _AWS_REGION,
            )
            return _SUCCESS  # Malformed — don't retry

        detail = envelope.get("detail")
        if not isinstance(detail, dict):
            logger.error(
                "Missing or invalid detail — "
                "error_code=PROC_EVENT_PARSE_FAILED message_id=%s",
                message_id,
            )
            return _SUCCESS  # Malformed — don't retry

        # Set correlation ID (SEC-S011-02)
        event_arn = _sanitize_log(detail.get("eventArn", "UNKNOWN"))
        now = _now_iso()

        logger.info(
            "Processing event — event_arn=%s message_id=%s region=%s",
            event_arn, message_id, _AWS_REGION,
        )

        # --- (a) FILTER BACKUP EVENT ---
        config = get_config()
        backup_config = config.get("FILTER_BACKUP_EVENTS", {})
        if should_filter_backup_event(detail, backup_config):
            logger.info(
                "Backup event filtered — event_arn=%s", event_arn,
            )
            return _SUCCESS

        # --- (b) NORMALIZE DATES ---
        for date_field in ("startTime", "endTime", "lastUpdatedTime"):
            if date_field in detail:
                detail[date_field] = parse_health_date(
                    detail[date_field]
                ) or detail[date_field]

        # --- (c) EXTRACT ENTITIES ---
        entities = extract_entities(detail)

        # --- (d) NORMALIZE TAGS ---
        account_tags = normalize_tags(extract_account_tags(detail))
        for entity in entities:
            if isinstance(entity, dict) and "resourceTags" in entity:
                entity["resourceTags"] = normalize_tags(
                    entity["resourceTags"]
                )

        # --- (e) INFER ACTIONABILITY → FILTER INFORMATIONAL ---
        actionability_result = infer_actionability(detail)
        if actionability_result.value == "INFORMATIONAL":
            logger.info(
                "INFORMATIONAL event filtered — event_arn=%s", event_arn,
            )
            return _SUCCESS

        # --- (f) COERCE PAGE FIELDS ---
        page_info = coerce_page_fields(detail)

        # --- (g) DERIVE CAMPAIGN ID ---
        campaign_id = derive_campaign_id(detail)
        campaign_type = determine_campaign_type(entities)

        logger.info(
            "Campaign derived — campaign_id=%s campaign_type=%s "
            "page=%d/%d resource_count=%d event_arn=%s",
            _sanitize_log(campaign_id), campaign_type,
            page_info.page, page_info.total_pages,
            len(entities), event_arn,
        )

        # --- (h) CREATE/UPDATE CAMPAIGN ---
        campaign_result = create_or_update_campaign(
            table=_campaigns_table,
            detail=detail,
            entities=entities,
            now=now,
            mode="eventbridge",
        )

        # --- (i) WRITE RESOURCES ---
        affected_account = detail.get("affectedAccount", "")
        event_region = detail.get("region", "")
        is_new = campaign_result.action == "CREATED"

        write_resources(
            resources_table=_resources_table,
            campaigns_table=_campaigns_table,
            campaign_id=campaign_id,
            campaign_type=campaign_type,
            entities=entities,
            account_tags=account_tags,
            affected_account=affected_account,
            event_region=event_region,
            is_new_campaign=is_new,
            now=now,
        )

        # --- PAGE > 1: append only, skip dispatch/routing/publish ---
        if page_info.page > 1:
            logger.info(
                "Page >1 append complete — campaign_id=%s page=%d "
                "event_arn=%s",
                _sanitize_log(campaign_id), page_info.page, event_arn,
            )
            return _SUCCESS

        # --- (j) EVALUATE DISPATCH WINDOW ---
        dispatch_result = _evaluate_dispatch(detail, config, actionability_result)
        # FINDING-IMPL-03: explicit boolean gate.
        if dispatch_result.get("dispatched") is not True:
            _update_campaign_dispatch(
                campaign_id, False, dispatch_result, now,
            )
            logger.info(
                "Event not dispatched — campaign_id=%s mode=%s "
                "event_arn=%s",
                _sanitize_log(campaign_id),
                dispatch_result.get("mode"),
                event_arn,
            )
            return _SUCCESS

        # --- (k) RESOLVE ROUTING ---
        routing_result = _resolve_routing_with_live_lookup(
            detail, envelope, account_tags, entities, config,
        )
        _update_campaign_dispatch(campaign_id, True, dispatch_result, now)

        platforms = routing_result.get("platforms", {})
        if not platforms:
            logger.warning(
                "No platform routing targets — "
                "error_code=PROC_ROUTING_NO_TARGETS "
                "campaign_id=%s event_arn=%s",
                _sanitize_log(campaign_id), event_arn,
            )
            update_routed_via(
                _resources_table, campaign_id, "error",
                f"No routing rule matched for account {affected_account}",
                _now_iso(),
            )
            # Still publish to SNS — event visible in dashboard as unroutable

        else:
            # Do NOT add routingTagKey or routingTagValue to this
            # log line — tag values may contain team names or org structure.
            logger.info(
                "Routing resolved — campaign_id=%s project=%s "
                "resolved_by=%s fallback=%s event_arn=%s",
                _sanitize_log(campaign_id),
                _sanitize_log(routing_result.get("resolvedProject")),
                routing_result["resolvedBy"],
                routing_result["fallbackUsed"],
                event_arn,
            )

        # --- (k.1) UPDATE RESOURCES WITH routedVia ---
        routed_via = _derive_routed_via(routing_result, config)
        routing_error = (
            f"No routing rule matched for account {affected_account}"
            if routing_result["resolvedBy"] == "error" else None
        )
        update_routed_via(
            _resources_table, campaign_id, routed_via, routing_error,
            _now_iso(),
        )

        # --- (l) BUILD STANDARDIZED EVENT ---
        action = (
            "CREATE" if campaign_result.action == "CREATED"
            else "RESOURCE_UPDATE"
        )
        std_event = _build_standardized_event(
            detail=detail,
            entities=entities,
            account_tags=account_tags,
            actionability_result=actionability_result,
            campaign_id=campaign_id,
            campaign_type=campaign_type,
            action=action,
            routing=routing_result,
            dispatch=dispatch_result,
            envelope=envelope,
            now=now,
        )

        # --- (m) PUBLISH TO SNS ---
        _publish_to_sns(std_event, campaign_id)

        logger.info(
            "Processing complete — campaign_id=%s action=%s "
            "event_arn=%s",
            _sanitize_log(campaign_id), action, event_arn,
        )
        return _SUCCESS

    except Exception as exc:
        # SEC-S011-09: log type and truncated message only
        logger.error(
            "Unhandled exception — error_code=PROC_UNHANDLED "
            "error_type=%s error_msg=%s event_arn=%s "
            "message_id=%s region=%s",
            type(exc).__name__,
            _sanitize_log(str(exc)),
            event_arn,
            message_id,
            _AWS_REGION,
        )
        return {"batchItemFailures": [{"itemIdentifier": message_id}]}
