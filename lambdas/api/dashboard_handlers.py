"""Dashboard API handlers for STORY-038.

Implements 10 endpoints: campaigns, config, routing, dispatch, and operations.
Self-contained — no imports from handler.py to avoid circular dependency.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from urllib.parse import unquote

import boto3
from botocore.exceptions import ClientError

try:
    from resolve_core.grouping import group_resources
except ImportError:
    from lambdas.shared.python.resolve_core.grouping import group_resources

# STORY-136: shared platform-resolution seam (single source of truth).
try:
    from resolve_core.config_schema import operative_platform, resolve_platforms
except ImportError:
    from lambdas.shared.python.resolve_core.config_schema import (
        operative_platform,
        resolve_platforms,
    )

# STORY-118 (King Yip Finding 3, ACCEPT AS DEBT per Dumbledore): this module
# reads/writes TWO independent status-like attributes on the SAME
# CampaignsTable item — `status` (the campaign state machine, owned by
# resolve_core.campaign / Processor / Reconciliation) and `campaignStatus`
# (the STORY-114 ticketing lock, owned exclusively by handle_create_tickets
# below). They are NOT the same concept and must never be conflated. See
# resolve_core.constants.CAMPAIGN_STATE_FIELD / TICKETING_LOCK_FIELD for the
# full writeup. Do not rename or merge either attribute — see tracker.
try:
    from resolve_core.constants import CAMPAIGN_STATE_FIELD, TICKETING_LOCK_FIELD
except ImportError:
    from lambdas.shared.python.resolve_core.constants import (
        CAMPAIGN_STATE_FIELD,
        TICKETING_LOCK_FIELD,
    )

logger = logging.getLogger(__name__)

# Environment variables
CONFIG_TABLE = os.environ.get("CONFIG_TABLE", "compass-config")
CAMPAIGNS_TABLE = os.environ.get("CAMPAIGNS_TABLE", "compass-campaigns")
RESOURCES_TABLE = os.environ.get("RESOURCES_TABLE", "compass-resources")
JIRA_SECRET_ARN = os.environ.get("JIRA_SECRET_ARN", "")
RECONCILIATION_FUNCTION_NAME = os.environ.get("RECONCILIATION_FUNCTION_NAME", "")
SYNC_FUNCTION_NAME = os.environ.get("SYNC_FUNCTION_NAME", "")
JIRA_FUNCTION_NAME = os.environ.get("JIRA_FUNCTION_NAME", "")
CORS_ORIGIN = os.environ.get("CORS_ALLOW_ORIGIN", "*")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": CORS_ORIGIN,
    "Access-Control-Allow-Headers": "Content-Type, x-api-key, Authorization",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
}

# AWS SDK clients
_dynamodb = boto3.resource("dynamodb")
_lambda_client = boto3.client("lambda")
_orgs_client = boto3.client("organizations")
_secrets_client = boto3.client("secretsmanager")


# ===================================================================
# Response helpers (local copies to avoid circular import)
# ===================================================================

def _success(status_code: int, body) -> dict:
    return {
        "statusCode": status_code,
        "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
        "body": json.dumps(body, default=str) if body is not None else "null",
    }


def _error(status_code: int, code: str, message: str, reason: str | None = None) -> dict:
    body = {"error": {"code": code, "message": message}}
    if reason:
        body["error"]["reason"] = reason
    return {
        "statusCode": status_code,
        "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _parse_body(event: dict) -> dict | None:
    raw = event.get("body")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Stale lock threshold — a ticketing lock older than this is considered abandoned.
# API Gateway Lambda max execution is 29 seconds; 600s gives ~20x safety margin.
TICKETING_LOCK_TTL_SECONDS = 600  # 10 minutes


def _is_lock_stale(locked_at: str | None) -> bool:
    """Return True if the lock timestamp is absent or older than TICKETING_LOCK_TTL_SECONDS.

    Missing timestamp indicates a pre-fix stuck campaign (backward compat) — treat as stale.
    """
    if not locked_at:
        return True
    try:
        lock_time = datetime.fromisoformat(locked_at.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - lock_time).total_seconds()
        return age > TICKETING_LOCK_TTL_SECONDS
    except (ValueError, TypeError):
        return True  # Unparseable timestamp — treat as stale


def _lock_age_seconds(locked_at: str | None) -> float:
    """Return lock age in seconds, or -1 if unparseable/missing."""
    if not locked_at:
        return -1
    try:
        lock_time = datetime.fromisoformat(locked_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - lock_time).total_seconds()
    except (ValueError, TypeError):
        return -1


def _config_table():
    return _dynamodb.Table(CONFIG_TABLE)


# ===================================================================
# Campaign Response Transformers
# ===================================================================

def _format_campaign(item: dict) -> dict:
    """Transform DynamoDB campaign item to dashboard Campaign schema."""
    total = int(item.get("totalResourceCount", 0) or 0)
    resolved = int(item.get("resolvedCount", 0) or 0)
    service = item.get("service", "")
    event_type = item.get("eventTypeCode", "")
    deprecated_version = item.get("deprecatedVersion", "")

    title = f"{service} {deprecated_version}" if deprecated_version else f"{service} {event_type}"

    return {
        "campaignId": item.get("campaignId", ""),
        "eventArn": item.get("eventArn", ""),
        "title": title.strip(),
        "service": service,
        "deprecatedVersion": deprecated_version,
        "eventTypeCode": event_type,
        "description": item.get("description", ""),
        "deadline": item.get("startTime", ""),
        "actionability": item.get("actionability", ""),
        # `status` — CAMPAIGN STATE MACHINE (ACTIVE/COMPLETED/PARTIAL/FILTERED).
        # See resolve_core.constants.CAMPAIGN_STATE_FIELD (STORY-118 / King
        # Finding 3). Do NOT confuse with `campaignStatus` below.
        "status": (item.get(CAMPAIGN_STATE_FIELD, "active") or "active").lower(),
        "hasResources": item.get("campaignType") == "resource-level",
        "totalResources": total,
        "resolvedResources": resolved,
        "ticketedResources": int(item.get("ticketsCreated", 0) or 0),
        "ticketsClosedResources": int(item.get("ticketsClosed", 0) or 0),
        "affectedAccount": item.get("affectedAccount", ""),
        "createdAt": item.get("createdAt", ""),
        # `campaignStatus` — STORY-114 TICKETING LOCK state
        # (TICKETING_IN_PROGRESS/TICKETED/TICKETING_FAILED), unrelated to the
        # `status` field above. Deliberately surfaced under a different API
        # response key ("ticketingStatus", not "status") so this same naming
        # collision doesn't leak into the dashboard contract. See
        # resolve_core.constants.TICKETING_LOCK_FIELD (STORY-118 / King
        # Finding 3) — accepted as tech debt, do not rename.
        "ticketingStatus": item.get(TICKETING_LOCK_FIELD),
    }


# STORY-136: platform resolution moved to the shared seam
# (resolve_core.config_schema.resolve_platforms / operative_platform).
# The former local _get_active_platform reader was retired; call-sites now
# call operative_platform(resolve_platforms(_config_table())) directly.


def _format_resource(item: dict, platform: str | None = None) -> dict:
    """Transform DynamoDB resource item to dashboard Resource schema."""
    ticket_id = item.get("ticketId") or item.get("jiraTicketKey") or ""
    ticket_status = item.get("ticketStatus", "none")
    ticket_status_name = item.get("jiraStatusName") or ""

    result = {
        "resourceArn": item.get("entityValue", item.get("resourceArn", "")),
        "accountId": item.get("accountId", ""),
        "region": item.get("region", ""),
        "healthStatus": item.get("status", item.get("healthStatus", "PENDING")),
        # Platform-agnostic fields
        "ticketId": ticket_id,
        "ticketStatus": ticket_status,
        "ticketPlatform": item.get("ticketPlatform") or platform or "jira",
        "ticketUrl": item.get("ticketUrl", ""),
        # Deprecated aliases (backward compat)
        "jiraTicketKey": ticket_id,
        "jiraStatusName": ticket_status_name,
        "tags": item.get("tags", {}),
        "lastUpdated": item.get("lastUpdatedTime", ""),
    }

    # Include per-platform tickets map if present (STORY-111)
    tickets_map = item.get("tickets")
    if isinstance(tickets_map, dict) and tickets_map:
        result["tickets"] = tickets_map

    return result


# ===================================================================
# Campaign Handlers
# ===================================================================

def handle_campaigns_list(event, context):
    """GET /api/campaigns — list all campaigns sorted by createdAt desc."""
    try:
        table = _dynamodb.Table(CAMPAIGNS_TABLE)
        resp = table.scan()
        items = resp.get("Items", [])
    except ClientError:
        logger.exception("Failed to scan CampaignsTable")
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to read campaigns.")

    campaigns = [_format_campaign(item) for item in items]
    campaigns.sort(key=lambda c: c.get("createdAt", ""), reverse=True)
    return _success(200, campaigns)


def handle_campaign_detail(event, context):
    """GET /api/campaigns/{id} — campaign detail with resources and breakdown."""
    campaign_id = unquote((event.get("pathParameters") or {}).get("id", ""))
    if not campaign_id:
        return _error(400, "INVALID_PARAM", "Campaign ID is required.")

    # Get campaign
    try:
        campaigns_table = _dynamodb.Table(CAMPAIGNS_TABLE)
        resp = campaigns_table.get_item(Key={"campaignId": campaign_id})
    except ClientError:
        logger.exception("Failed to get campaign %s", campaign_id)
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to read campaign.")

    campaign = resp.get("Item")
    if not campaign:
        return _error(404, "NOT_FOUND", f"Campaign '{campaign_id}' not found.")

    # Get resources
    try:
        from boto3.dynamodb.conditions import Key
        resources_table = _dynamodb.Table(RESOURCES_TABLE)
        res_resp = resources_table.query(
            KeyConditionExpression=Key("campaignId").eq(campaign_id)
        )
        resources = res_resp.get("Items", [])
    except ClientError:
        logger.exception("Failed to query resources for %s", campaign_id)
        resources = []

    # Compute breakdown by accountId
    breakdown = {}
    for r in resources:
        acct = r.get("accountId", "unknown")
        if acct not in breakdown:
            breakdown[acct] = {"total": 0, "pending": 0, "resolved": 0}
        breakdown[acct]["total"] += 1
        status = (r.get("status", "PENDING") or "PENDING").upper()
        if status == "RESOLVED":
            breakdown[acct]["resolved"] += 1
        else:
            breakdown[acct]["pending"] += 1

    platform = operative_platform(resolve_platforms(_config_table()))
    result = _format_campaign(campaign)
    result["resources"] = [_format_resource(r, platform) for r in resources]
    result["groupBreakdown"] = breakdown
    return _success(200, result)


# ===================================================================
# Campaign Grouping (STORY-074)
# ===================================================================

_VALID_GROUP_STRATEGIES = ("per-account", "per-tag-value", "single")
_TAG_KEY_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def _resolve_group_routing_target(group: dict, config_table) -> str:
    """Resolve JIRA project for a group using first account in the group."""
    account_ids = list(group.get("account_ids") or [])
    if not account_ids:
        return "UNKNOWN"
    first_account = account_ids[0]
    try:
        item = config_table.get_item(Key={"pk": f"ROUTING#{first_account}"}).get("Item")
        if item:
            return item.get("jira_project", "UNKNOWN")
    except ClientError:
        pass
    try:
        default = config_table.get_item(Key={"pk": "ROUTING_DEFAULT"}).get("Item")
        return (default or {}).get("jira_project", "UNKNOWN")
    except ClientError:
        return "UNKNOWN"


def handle_group_preview(event, context):
    """GET /api/campaigns/{id}/group-preview — preview ticket grouping."""
    campaign_id = unquote((event.get("pathParameters") or {}).get("id", ""))
    if not campaign_id:
        return _error(400, "INVALID_PARAM", "Campaign ID is required.")

    params = event.get("queryStringParameters") or {}
    strategy = params.get("strategy", "")
    tag_key = params.get("tagKey")

    # Validate strategy
    if strategy not in _VALID_GROUP_STRATEGIES:
        return _error(400, "INVALID_PARAM",
                      f"strategy must be one of: {', '.join(_VALID_GROUP_STRATEGIES)}")

    # Validate tagKey requirement and format (SEC-074-01)
    if strategy == "per-tag-value":
        if not tag_key:
            return _error(400, "INVALID_PARAM", "tagKey is required for per-tag-value strategy.")
        if not _TAG_KEY_PATTERN.match(tag_key):
            return _error(400, "INVALID_PARAM",
                          "tagKey must be alphanumeric, hyphens, or underscores (max 128 chars).")

    # Query resources for campaign
    try:
        from boto3.dynamodb.conditions import Key
        resources_table = _dynamodb.Table(RESOURCES_TABLE)
        resources = []
        query_kwargs = {"KeyConditionExpression": Key("campaignId").eq(campaign_id)}
        while True:
            resp = resources_table.query(**query_kwargs)
            resources.extend(resp.get("Items", []))
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
            query_kwargs["ExclusiveStartKey"] = last_key
    except ClientError:
        logger.exception("Failed to query resources for %s", campaign_id)
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to read resources.")

    if not resources:
        return _error(404, "NOT_FOUND", f"Campaign '{campaign_id}' has no resources.")

    # Group resources
    try:
        groups = group_resources(resources, strategy, tag_key)
    except ValueError as e:
        return _error(400, "GROUPING_ERROR", str(e))

    # Check if tag-based grouping produced only 'untagged' results
    if strategy == "per-tag-value" and len(groups) == 1 and groups[0]["label"] == "untagged":
        return _error(400, "TAG_KEY_NOT_FOUND",
                      f"Tag key '{tag_key}' has no values on any resources in this campaign")

    # Resolve routing targets
    config = _config_table()
    preview_groups = []
    total_resources = 0
    for g in groups:
        resource_count = len(g["resources"])
        total_resources += resource_count
        preview_groups.append({
            "label": g["label"],
            "routingTarget": _resolve_group_routing_target(g, config),
            "resourceCount": resource_count,
            "accountIds": sorted(g["account_ids"]),
        })

    return _success(200, {
        "groups": preview_groups,
        "totalGroups": len(preview_groups),
        "totalResources": total_resources,
    })


def handle_create_tickets(event, context):
    """POST /api/campaigns/{id}/create-tickets — publish to SNS Integration Topic.

    BUG-S23-017: Replaced direct JIRA Lambda invoke with SNS publish.
    Integration Lambdas (JIRA, ServiceNow) subscribe to the topic and
    handle ticket creation independently via their SQS queues.
    """
    campaign_id = unquote((event.get("pathParameters") or {}).get("id", ""))
    if not campaign_id:
        return _error(400, "INVALID_PARAM", "Campaign ID is required.")

    integration_topic_arn = os.environ.get("INTEGRATION_TOPIC_ARN", "")
    payload_bucket = os.environ.get("PAYLOAD_BUCKET", "")
    if not integration_topic_arn:
        return _error(500, "SYS_CONFIG_ERROR", "Integration topic not configured.")

    # Validate campaign exists
    try:
        campaigns_table = _dynamodb.Table(CAMPAIGNS_TABLE)
        resp = campaigns_table.get_item(Key={"campaignId": campaign_id})
    except ClientError:
        logger.exception("Failed to get campaign %s", campaign_id)
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to read campaign.")

    campaign = resp.get("Item")
    if not campaign:
        return _error(404, "NOT_FOUND", f"Campaign '{campaign_id}' not found.")

    # Idempotency guard: reject if ticketing is actively in progress (non-stale lock)
    #
    # STORY-118 (King Yip Finding 3): this checks `campaignStatus`
    # (TICKETING_LOCK_FIELD) — the STORY-114 ticketing lock — NOT `status`
    # (CAMPAIGN_STATE_FIELD), the campaign state machine written by
    # resolve_core.campaign / Processor / Reconciliation. The two are
    # unrelated despite the similar names; see resolve_core/constants.py.
    is_stale_override = False
    if campaign.get(TICKETING_LOCK_FIELD) == "TICKETING_IN_PROGRESS":
        locked_at = campaign.get("ticketingLockedAt")
        if _is_lock_stale(locked_at):
            # Stale lock detected — log and allow override via compare-and-swap
            logger.info(json.dumps({
                "event": "stale_lock_override",
                "campaignId": campaign_id,
                "originalLockedAt": locked_at or "missing",
                "staleLockAge": _lock_age_seconds(locked_at),
            }))
            is_stale_override = True
        else:
            return _error(
                409, "CONFLICT",
                "Ticket creation is currently in progress for this campaign. Please wait.",
                reason="TICKETING_IN_PROGRESS",
            )

    # Validate at least one ITSM platform is configured
    config = _config_table()
    try:
        integ_item = config.get_item(Key={"pk": "INTEGRATIONS_ENABLED"}).get("Item")
    except ClientError:
        integ_item = None
    enabled_platforms = (integ_item.get("platforms") if integ_item else None) or ["jira"]

    # Validate routing exists
    try:
        routing_default = config.get_item(Key={"pk": "ROUTING_DEFAULT"}).get("Item")
    except ClientError:
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to read routing config.")

    if not routing_default:
        return _error(400, "ROUTING_NOT_CONFIGURED", "Default routing not configured.")

    # Get resources
    try:
        from boto3.dynamodb.conditions import Key as DDBKey
        resources_table = _dynamodb.Table(RESOURCES_TABLE)
        resources = []
        query_kwargs = {"KeyConditionExpression": DDBKey("campaignId").eq(campaign_id)}
        while True:
            res_resp = resources_table.query(**query_kwargs)
            resources.extend(res_resp.get("Items", []))
            last_key = res_resp.get("LastEvaluatedKey")
            if not last_key:
                break
            query_kwargs["ExclusiveStartKey"] = last_key
    except ClientError:
        logger.exception("Failed to query resources for %s", campaign_id)
        resources = []

    # Parse grouping from request body (optional)
    body = _parse_body(event) or {}
    grouping = body.get("grouping") or {"strategy": "per-account"}
    strategy = grouping.get("strategy", "per-account")
    tag_key = grouping.get("tagKey")

    if strategy not in _VALID_GROUP_STRATEGIES:
        return _error(400, "INVALID_PARAM",
                      f"grouping.strategy must be one of: {', '.join(_VALID_GROUP_STRATEGIES)}")

    if strategy == "per-tag-value":
        if not tag_key:
            return _error(400, "INVALID_PARAM", "grouping.tagKey is required for per-tag-value strategy.")
        if not _TAG_KEY_PATTERN.match(tag_key):
            return _error(400, "INVALID_PARAM",
                          "grouping.tagKey must be alphanumeric, hyphens, or underscores (max 128 chars).")

    # Group resources
    try:
        groups = group_resources(resources, strategy, tag_key)
    except ValueError as e:
        return _error(400, "GROUPING_ERROR", str(e))

    # Acquire ticketing lock — branched ConditionExpression (Snape pre-impl review):
    #   Normal path: reject if another request holds an active lock
    #   Stale override path: compare-and-swap confirms stale value unchanged
    #
    # STORY-118 (King Yip Finding 3, ACCEPT AS DEBT): the attribute name here
    # is `campaignStatus` (TICKETING_LOCK_FIELD), NOT `status`
    # (CAMPAIGN_STATE_FIELD, the campaign state machine field owned by
    # resolve_core.campaign). They coexist on the same CampaignsTable item
    # but track unrelated concerns. Do not rename either — see
    # resolve_core/constants.py for the full explanation. The f-strings below
    # produce byte-identical expression text to the literal "campaignStatus"
    # used prior to STORY-118; only the source-of-truth moved to a named
    # constant.
    lock_time = _now_iso()
    try:
        if is_stale_override:
            # Compare-and-swap: confirm the value is still TICKETING_IN_PROGRESS
            # (hasn't been freshly re-acquired by another concurrent request)
            condition = f"{TICKETING_LOCK_FIELD} = :expected"
            expr_values = {
                ":expected": "TICKETING_IN_PROGRESS",
                ":s": "TICKETING_IN_PROGRESS",
                ":lt": lock_time,
                ":ua": lock_time,
            }
        else:
            # Normal acquisition: reject if already locked
            condition = f"attribute_not_exists({TICKETING_LOCK_FIELD}) OR {TICKETING_LOCK_FIELD} <> :s"
            expr_values = {
                ":s": "TICKETING_IN_PROGRESS",
                ":lt": lock_time,
                ":ua": lock_time,
            }

        campaigns_table.update_item(
            Key={"campaignId": campaign_id},
            UpdateExpression=f"SET {TICKETING_LOCK_FIELD} = :s, ticketingLockedAt = :lt, updatedAt = :ua",
            ConditionExpression=condition,
            ExpressionAttributeValues=expr_values,
        )
        logger.info(json.dumps({
            "event": "ticketing_lock_acquired",
            "campaignId": campaign_id,
            "lockedAt": lock_time,
            "staleOverride": is_stale_override,
        }))
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return _error(
                409, "CONFLICT",
                "Ticket creation is currently in progress for this campaign. Please wait.",
                reason="TICKETING_IN_PROGRESS",
            )
        logger.exception("Failed to set ticketing lock on campaign %s", campaign_id)
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to lock campaign for ticketing.")

    # === BEGIN LOCK-PROTECTED SECTION ===
    # Default to failed; set to TICKETED on success path
    terminal_status = "TICKETING_FAILED"
    published_groups = []
    errors = []

    try:
        # Build routing config cache for account lookups
        routing_cache = _build_routing_cache(config, groups)

        # Publish one SNS message per group
        from resolve_core.routing import resolve_account_routing
        from resolve_core.payload import publish_or_offload

        sns_client = boto3.client("sns")
        s3_client = boto3.client("s3")
        now = _now_iso()

        for g in groups:
            # Idempotency: filter out resources that already have a ticket
            unticketed = [r for r in g["resources"]
                          if not r.get("ticketId") or r.get("ticketId") == "none"]
            if not unticketed:
                published_groups.append({"label": g["label"], "skipped": True, "reason": "all_ticketed"})
                continue

            # Resolve routing for this group's first account
            account_ids = list(g.get("account_ids") or set())
            first_account = account_ids[0] if account_ids else campaign.get("affectedAccount", "")
            routing_result = resolve_account_routing(first_account, routing_cache)

            if routing_result.get("resolvedBy") == "error":
                errors.append({"group": g["label"], "error": "No routing configured"})
                continue

            # Build standardized event payload (v2.1 schema)
            std_event = {
                "timestamp": now,
                "source": "compass",
                "version": "2.1",
                "event": {
                    "eventArn": campaign.get("eventArn", ""),
                    "eventTypeCode": campaign.get("eventTypeCode", ""),
                    "eventTypeCategory": campaign.get("eventTypeCategory", ""),
                    "service": campaign.get("service", ""),
                    "region": campaign.get("region", ""),
                    "affectedAccount": first_account,
                    "startTime": campaign.get("startTime", ""),
                    "endTime": campaign.get("endTime", ""),
                    "description": campaign.get("latestDescription", ""),
                    "statusCode": campaign.get("statusCode", "open"),
                    "actionability": campaign.get("actionability", "ACTION_REQUIRED"),
                    "actionabilityInferred": False,
                    "campaignId": campaign_id,
                    "campaignType": campaign.get("campaignType", "resource-level"),
                    "action": "CREATE",
                },
                "resources": [
                    {
                        "arn": r.get("resourceArn", r.get("entityValue", "")),
                        "entityValue": r.get("entityValue", ""),
                        "accountId": r.get("accountId", ""),
                        "status": r.get("status", "PENDING"),
                        "lastUpdatedTime": r.get("lastUpdatedTime", ""),
                        "resourceTags": r.get("resourceTags", {}),
                    }
                    for r in unticketed
                ],
                "accountTags": campaign.get("accountTags", {}),
                "routing": routing_result,
                "dispatch": {"dispatched": True, "mode": "dashboard", "matchedRule": None},
                "metadata": {
                    "originalEventId": campaign.get("eventArn", ""),
                    "originalEventTime": campaign.get("startTime", ""),
                    "processingTime": now,
                    "schemaVersion": "2.1",
                },
            }

            # SNS message attributes for integration filtering
            has_jira = bool(routing_result.get("platforms", {}).get("jira"))
            has_snow = bool(routing_result.get("platforms", {}).get("servicenow"))
            message_attributes = {
                "service": {"DataType": "String", "StringValue": campaign.get("service", "UNKNOWN") or "UNKNOWN"},
                "eventTypeCategory": {"DataType": "String", "StringValue": campaign.get("eventTypeCategory", "scheduledChange") or "scheduledChange"},
                "actionability": {"DataType": "String", "StringValue": campaign.get("actionability", "ACTION_REQUIRED") or "ACTION_REQUIRED"},
                "hasResources": {"DataType": "String", "StringValue": str(bool(unticketed)).lower()},
                "action": {"DataType": "String", "StringValue": "CREATE"},
                "hasJiraRouting": {"DataType": "String", "StringValue": str(has_jira).lower()},
                "hasServicenowRouting": {"DataType": "String", "StringValue": str(has_snow).lower()},
            }

            try:
                publish_or_offload(
                    sns_client=sns_client,
                    s3_client=s3_client,
                    topic_arn=integration_topic_arn,
                    bucket=payload_bucket,
                    event_dict=std_event,
                    message_attributes=message_attributes,
                )
                published_groups.append({"label": g["label"], "resourceCount": len(unticketed)})
            except (ClientError, Exception) as e:
                logger.exception("Failed to publish SNS for group %s", g["label"])
                errors.append({"group": g["label"], "error": str(type(e).__name__)})

        # Determine terminal status based on results:
        # Any published group (even partial) = TICKETED; all-skipped also = TICKETED
        if published_groups:
            terminal_status = "TICKETED"
        elif not errors:
            # No publishes but also no errors — all groups were skipped (already ticketed)
            terminal_status = "TICKETED"
        else:
            # Zero groups published and at least one error
            terminal_status = "TICKETING_FAILED"

        # Update each resource's ticketGroupKey
        try:
            for g in groups:
                for r in g["resources"]:
                    tracking_key = r.get("trackingKey") or r.get("resourceArn") or r.get("entityValue", "")
                    if tracking_key:
                        resources_table.update_item(
                            Key={"campaignId": campaign_id, "trackingKey": tracking_key},
                            UpdateExpression="SET ticketGroupKey = :gk",
                            ExpressionAttributeValues={":gk": g["label"]},
                        )
        except ClientError:
            logger.exception("Failed to update resource ticketGroupKey fields")

    except Exception:
        logger.exception("Unhandled error during ticketing for campaign %s", campaign_id)
        terminal_status = "TICKETING_FAILED"

    finally:
        # ALWAYS release the lock with a terminal state + grouping metadata
        #
        # STORY-118 (King Yip Finding 3): writes `campaignStatus`
        # (TICKETING_LOCK_FIELD) only. The unrelated `status` state-machine
        # field (CAMPAIGN_STATE_FIELD, owned by resolve_core.campaign) is
        # never touched by the ticketing lock lifecycle — this finally block
        # must stay scoped to the lock field.
        try:
            update_expr = f"SET {TICKETING_LOCK_FIELD} = :cs, updatedAt = :ua"
            expr_values = {":cs": terminal_status, ":ua": _now_iso()}

            # Include grouping metadata if we computed it
            if groups is not None:
                update_expr += ", groupingStrategy = :gs, groupCount = :gc"
                expr_values[":gs"] = strategy
                expr_values[":gc"] = len(groups)
                if tag_key:
                    update_expr += ", groupingTagKey = :gtk"
                    expr_values[":gtk"] = tag_key

            campaigns_table.update_item(
                Key={"campaignId": campaign_id},
                UpdateExpression=update_expr,
                ExpressionAttributeValues=expr_values,
            )
            logger.info(json.dumps({
                "event": "ticketing_lock_released",
                "campaignId": campaign_id,
                "terminalStatus": terminal_status,
            }))
        except ClientError:
            logger.exception(
                "CRITICAL: Failed to release ticketing lock on campaign %s", campaign_id
            )
    # === END LOCK-PROTECTED SECTION ===

    return _success(200, {
        "published": len(published_groups),
        "groups": published_groups,
        "errors": errors,
    })


def _build_routing_cache(config_table, groups: list) -> dict:
    """Load routing config items for all accounts referenced by groups.

    Returns a dict keyed by DynamoDB pk (e.g. "ROUTING#111111111111",
    "ROUTING_DEFAULT") suitable for resolve_account_routing().
    """
    cache = {}
    # Load default routing
    try:
        default = config_table.get_item(Key={"pk": "ROUTING_DEFAULT"}).get("Item")
        if default:
            cache["ROUTING_DEFAULT"] = default
    except ClientError:
        pass

    # Load per-account routing for all accounts in all groups
    seen_accounts = set()
    for g in groups:
        for aid in (g.get("account_ids") or []):
            seen_accounts.add(aid)

    for account_id in seen_accounts:
        pk = f"ROUTING#{account_id}"
        try:
            item = config_table.get_item(Key={"pk": pk}).get("Item")
            if item:
                cache[pk] = item
        except ClientError:
            pass

    # Load routing strategy (for tag routing awareness)
    try:
        strategy = config_table.get_item(Key={"pk": "ROUTING_STRATEGY"}).get("Item")
        if strategy:
            cache["ROUTING_STRATEGY"] = strategy
    except ClientError:
        pass

    return cache


# ===================================================================
# Config Summary Helpers (STORY-100)
# ===================================================================

def _derive_setup_complete(jira_item, snow_item, routing_item, dispatch_item) -> bool:
    """Setup is complete when: ≥1 validated ITSM + routing default + dispatch preset.

    Returns True only when all three conditions are met:
    (a) At least one ITSM connection validated (JIRA or ServiceNow)
    (b) ROUTING_DEFAULT item exists
    (c) DISPATCH_PRESET item exists
    """
    itsm_validated = (
        (jira_item.get("validated", False) if jira_item else False)
        or (snow_item.get("validated", False) if snow_item else False)
    )
    routing_configured = routing_item is not None
    dispatch_configured = dispatch_item is not None
    return itsm_validated and routing_configured and dispatch_configured


def _derive_last_modified(*items) -> str:
    """Return the most recent timestamp across all config items.

    Checks 'updated_at', 'validated_at', and 'consented_at' fields on each item.
    ISO 8601 timestamps are lexicographically sortable (YYYY-MM-DDTHH:MM:SSZ).
    Returns empty string if no valid timestamps are found.
    """
    timestamps = []
    timestamp_fields = ("updated_at", "validated_at", "consented_at")
    for item in items:
        if not item:
            continue
        for field in timestamp_fields:
            val = item.get(field)
            if val and isinstance(val, str) and val.strip():
                timestamps.append(val)
    if not timestamps:
        return ""
    return max(timestamps)


# ===================================================================
# Config Handlers
# ===================================================================

def handle_config_summary(event, context):
    """GET /api/config/summary — aggregated configuration status.

    Returns a single response containing all configuration state needed
    by the dashboard summary page (STORY-095). Each DynamoDB read is
    independently wrapped in try/except — a failure on any single item
    degrades that section to safe defaults without failing the request.
    """
    config = _config_table()

    # --- Existing reads ---

    # ITSM platform selection (STORY-055: defaults to "jira" if missing)
    try:
        platform_item = config.get_item(Key={"pk": "ITSM_PLATFORM"}).get("Item")
    except ClientError:
        platform_item = None
    platform = (platform_item.get("platform") if platform_item else None) or "jira"

    # STORY-136 (AC-136.3): authoritative platforms array sourced from the same
    # INTEGRATIONS_ENABLED store that GET /config/integrations reads, via the
    # shared seam. resolve_platforms never raises and fails safe to ["jira"] on
    # ClientError (AC-136.8) — so this must NOT be able to 500 the endpoint.
    # The legacy scalar `platform` above is retained unchanged (AC-136.4).
    platforms = resolve_platforms(config)

    # JIRA connection
    try:
        jira_item = config.get_item(Key={"pk": "JIRA_CONNECTION"}).get("Item")
    except ClientError:
        jira_item = None

    # Routing default
    try:
        routing_item = config.get_item(Key={"pk": "ROUTING_DEFAULT"}).get("Item")
    except ClientError:
        routing_item = None

    # Dispatch preset
    try:
        dispatch_item = config.get_item(Key={"pk": "DISPATCH_PRESET"}).get("Item")
    except ClientError:
        dispatch_item = None

    # Count routing mappings
    try:
        from boto3.dynamodb.conditions import Attr
        count_resp = config.scan(
            FilterExpression=Attr("pk").begins_with("ROUTING#"),
            Select="COUNT",
        )
        account_mapping_count = count_resp.get("Count", 0)
    except ClientError:
        account_mapping_count = 0

    # STORY-132: credentialsConfigured reflects onboarding state, NOT the mere
    # existence of the auto-generated Secrets Manager secret resource (which CDK
    # creates unconditionally at deploy — core_stack.py:256-259). It is true only
    # when a JIRA_CONNECTION item exists AND that connection has been validated
    # (BRD Q-23 / §14.1: "connected" == passed Test Connection). Reuses jira_item
    # already fetched above; adds no AWS call and no IAM surface.
    credentials_configured = bool(jira_item and jira_item.get("validated") is True)

    # --- New reads (STORY-100) ---

    # ServiceNow connection
    # SECURITY: Only extract allowlisted fields from SNOW_CONNECTION.
    # This item contains `secret_arn` pointing to ServiceNow credentials.
    # Never spread, dump, or iterate over the raw item.
    try:
        snow_item = config.get_item(Key={"pk": "SNOW_CONNECTION"}).get("Item")
    except ClientError:
        logger.warning("Failed to read SNOW_CONNECTION from ConfigTable")
        snow_item = None

    # Routing strategy (tag routing configuration)
    try:
        strategy_item = config.get_item(Key={"pk": "ROUTING_STRATEGY"}).get("Item")
    except ClientError:
        logger.warning("Failed to read ROUTING_STRATEGY from ConfigTable")
        strategy_item = None

    # Telemetry consent
    try:
        telemetry_item = config.get_item(Key={"pk": "TELEMETRY"}).get("Item")
    except ClientError:
        logger.warning("Failed to read TELEMETRY from ConfigTable")
        telemetry_item = None

    # Count dispatch rules (total and enabled)
    # At expected volume (<50 rules), no pagination handling needed.
    try:
        from boto3.dynamodb.conditions import Attr  # noqa: F811 — may re-import if first scan succeeded
        rules_resp = config.scan(
            FilterExpression=Attr("pk").begins_with("DISPATCH_RULE#"),
            ProjectionExpression="pk, enabled",
        )
        dispatch_rules = rules_resp.get("Items", [])
        custom_rule_count = len(dispatch_rules)
        enabled_rule_count = sum(
            1 for r in dispatch_rules if r.get("enabled", False)
        )
    except ClientError:
        logger.warning("Failed to scan DISPATCH_RULE# items from ConfigTable")
        custom_rule_count = 0
        enabled_rule_count = 0

    # --- Response assembly ---

    return _success(200, {
        "platforms": platforms,
        "platform": platform,
        "jira": {
            "baseUrl": jira_item.get("jira_base_url", "") if jira_item else "",
            "validated": jira_item.get("validated", False) if jira_item else False,
            "validatedAt": jira_item.get("validated_at", "") if jira_item else "",
            "credentialsConfigured": credentials_configured,
            # STORY-100: JIRA validated user email
            "validatedUserEmail": jira_item.get("validated_user_email", "") if jira_item else "",
        },
        # STORY-100: ServiceNow connection status
        # SECURITY: Allowlist extraction only — see SEC-100-M1.
        "servicenow": {
            "instanceUrl": snow_item.get("instance_url", "") if snow_item else "",
            "validated": snow_item.get("validated", False) if snow_item else False,
            "validatedAt": snow_item.get("validated_at", "") if snow_item else "",
            "authType": snow_item.get("auth_type", "") if snow_item else "",
        },
        "routing": {
            "defaultProject": routing_item.get("jira_project", "") if routing_item else "",
            "snowAssignmentGroupId": routing_item.get("snow_assignment_group_id", "") if routing_item else "",
            "snowRecordType": routing_item.get("snow_record_type", "") if routing_item else "",
            "accountMappingCount": account_mapping_count,
            # STORY-100: Tag routing status
            "tagRouting": {
                "enabled": (strategy_item.get("mode") == "tag") if strategy_item else False,
                "tagKey": strategy_item.get("tag_key", "") if strategy_item else "",
                # STORY-124 (RT-02): additive sibling — surfaces the persisted
                # tag source (resource / account / both) so the wizard + modal
                # selection round-trips truthfully. Absent/empty -> "account",
                # matching the engine's SR-018-06 default. Does NOT alter the
                # STORY-113 enabled/tagKey shape.
                "tagSource": strategy_item.get("tag_source", "account") if strategy_item else "account",
            },
        },
        "dispatch": {
            "mode": dispatch_item.get("mode", "") if dispatch_item else "",
            # STORY-100: Dispatch window detail
            "actionabilityFilter": dispatch_item.get("actionability_filter", "") if dispatch_item else "",
            "customRuleCount": custom_rule_count,
            "enabledRuleCount": enabled_rule_count,
        },
        # STORY-100: System info
        "system": {
            "setupComplete": _derive_setup_complete(jira_item, snow_item, routing_item, dispatch_item),
            "telemetryConsent": telemetry_item.get("consent", False) if telemetry_item else False,
            "lastModified": _derive_last_modified(
                jira_item, snow_item, routing_item, dispatch_item, telemetry_item, strategy_item
            ),
        },
    })


def handle_routing_default(event, context):
    """POST /api/config/routing/default — save default routing project."""
    body = _parse_body(event)
    if body is None:
        return _error(400, "INVALID_REQUEST", "Request body must be valid JSON.")

    jira_project = (body.get("jiraProject") or "").strip()
    if not jira_project:
        return _error(400, "INVALID_PARAM", "jiraProject is required and must be non-empty.")

    jira_issue_type = (body.get("jiraIssueType") or "Task").strip()

    try:
        _config_table().put_item(Item={
            "pk": "ROUTING_DEFAULT",
            "jira_project": jira_project,
            "jira_issue_type": jira_issue_type,
            "updated_at": _now_iso(),
        })
    except ClientError:
        logger.exception("Failed to write ROUTING_DEFAULT")
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to save routing default.")

    return _success(200, {"jiraProject": jira_project, "jiraIssueType": jira_issue_type})


def handle_routing_discover(event, context):
    """POST /api/config/routing/discover — list AWS Organizations accounts."""
    accounts = []
    try:
        paginator = _orgs_client.get_paginator("list_accounts")
        for page in paginator.paginate():
            for acct in page.get("Accounts", []):
                accounts.append({
                    "accountId": acct.get("Id", ""),
                    "accountName": acct.get("Name", ""),
                    "status": acct.get("Status", ""),
                })
    except ClientError as e:
        logger.exception("Failed to list organizations accounts")
        return _error(502, "ORGS_ERROR", f"Failed to list accounts: {e.response['Error']['Message']}")

    return _success(200, accounts)


def handle_routing_import(event, context):
    """POST /api/config/routing/import — bulk import account-to-project mappings."""
    body = _parse_body(event)
    if body is None:
        return _error(400, "INVALID_REQUEST", "Request body must be valid JSON.")

    mappings = body.get("mappings", [])
    replace = body.get("replace", False)

    if not isinstance(mappings, list) or not mappings:
        return _error(400, "INVALID_PARAM", "mappings must be a non-empty array.")

    # Validate mappings
    valid = []
    errors = []
    for i, m in enumerate(mappings):
        acct_id = str(m.get("accountId", "")).strip()
        project = (m.get("jiraProject") or "").strip()

        if not re.match(r"^\d{12}$", acct_id):
            errors.append({"index": i, "reason": f"Invalid accountId: '{acct_id}' (must be 12 digits)"})
            continue
        if not project:
            errors.append({"index": i, "reason": "jiraProject is required"})
            continue
        valid.append({"accountId": acct_id, "jiraProject": project})

    config = _config_table()

    # If replace, delete existing ROUTING# items
    if replace:
        try:
            from boto3.dynamodb.conditions import Attr
            scan_resp = config.scan(
                FilterExpression=Attr("pk").begins_with("ROUTING#"),
                ProjectionExpression="pk",
            )
            for item in scan_resp.get("Items", []):
                config.delete_item(Key={"pk": item["pk"]})
        except ClientError:
            logger.exception("Failed to delete existing routing items")
            errors.append({"index": -1, "reason": "Failed to delete existing mappings"})

    # Batch write valid mappings
    imported = 0
    now = _now_iso()
    for i in range(0, len(valid), 25):
        batch = valid[i:i + 25]
        try:
            with config.batch_writer() as writer:
                for m in batch:
                    writer.put_item(Item={
                        "pk": f"ROUTING#{m['accountId']}",
                        "account_id": m["accountId"],
                        "jira_project": m["jiraProject"],
                        "jira_issue_type": "Task",
                        "updated_at": now,
                    })
            imported += len(batch)
        except ClientError:
            logger.exception("Batch write failed for routing import")
            for m in batch:
                errors.append({"index": -1, "reason": f"Write failed for account {m['accountId']}"})

    return _success(200, {
        "imported": imported,
        "failed": len(errors),
        "errors": errors,
    })


def handle_dispatch_save(event, context):
    """POST /api/config/dispatch — save dispatch window configuration."""
    body = _parse_body(event)
    if body is None:
        return _error(400, "INVALID_REQUEST", "Request body must be valid JSON.")

    mode = (body.get("mode") or "").strip()
    if mode not in ("all", "ple_only", "custom"):
        return _error(400, "INVALID_PARAM", "mode must be one of: all, ple_only, custom")

    rules = body.get("rules", [])
    config = _config_table()

    # Write dispatch preset
    try:
        config.put_item(Item={
            "pk": "DISPATCH_PRESET",
            "mode": mode,
            "updated_at": _now_iso(),
        })
    except ClientError:
        logger.exception("Failed to write DISPATCH_PRESET")
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to save dispatch preset.")

    rules_written = 0
    errors = []

    # If custom mode with rules, replace existing rules
    if mode == "custom" and rules:
        # Delete existing DISPATCH_RULE# items
        try:
            from boto3.dynamodb.conditions import Attr
            scan_resp = config.scan(
                FilterExpression=Attr("pk").begins_with("DISPATCH_RULE#"),
                ProjectionExpression="pk",
            )
            for item in scan_resp.get("Items", []):
                config.delete_item(Key={"pk": item["pk"]})
        except ClientError:
            logger.exception("Failed to delete existing dispatch rules")
            errors.append({"reason": "Failed to delete existing rules"})

        # Write new rules
        now = _now_iso()
        for i, rule in enumerate(rules):
            rule_id = f"rule-{i + 1:03d}"
            try:
                config.put_item(Item={
                    "pk": f"DISPATCH_RULE#{rule_id}",
                    "rule_id": rule_id,
                    "event_type_pattern": rule.get("eventTypePattern", ""),
                    "event_categories": rule.get("eventCategories", []),
                    "enabled": rule.get("enabled", True),
                    "updated_at": now,
                })
                rules_written += 1
            except ClientError:
                logger.exception("Failed to write dispatch rule %s", rule_id)
                errors.append({"index": i, "reason": f"Failed to write rule {rule_id}"})

    return _success(200, {
        "mode": mode,
        "rulesWritten": rules_written,
        "errors": errors,
    })


# ===================================================================
# Operations Handlers
# ===================================================================

def handle_reconcile(event, context):
    """POST /api/reconcile — trigger reconciliation Lambda async."""
    if not RECONCILIATION_FUNCTION_NAME:
        return _error(500, "SYS_CONFIG_ERROR", "Reconciliation function not configured.")

    try:
        _lambda_client.invoke(
            FunctionName=RECONCILIATION_FUNCTION_NAME,
            InvocationType="Event",
            Payload=json.dumps({"source": "dashboard", "triggeredAt": _now_iso()}).encode("utf-8"),
        )
    except ClientError:
        logger.exception("Failed to invoke reconciliation Lambda")
        return _error(502, "INVOKE_FAILED", "Failed to trigger reconciliation.")

    return _success(202, {"status": "triggered", "functionName": RECONCILIATION_FUNCTION_NAME})


def handle_sync(event, context):
    """POST /api/sync — trigger sync Lambda async."""
    if not SYNC_FUNCTION_NAME:
        return _error(500, "SYS_CONFIG_ERROR", "Sync function not configured.")

    try:
        _lambda_client.invoke(
            FunctionName=SYNC_FUNCTION_NAME,
            InvocationType="Event",
            Payload=json.dumps({"source": "dashboard", "triggeredAt": _now_iso()}).encode("utf-8"),
        )
    except ClientError:
        logger.exception("Failed to invoke sync Lambda")
        return _error(502, "INVOKE_FAILED", "Failed to trigger sync.")

    return _success(202, {"status": "triggered", "functionName": SYNC_FUNCTION_NAME})


# ===================================================================
# Test Tools Handlers
# ===================================================================

def handle_campaign_resources(event, context):
    """GET /api/campaigns/{id}/resources — paginated, filtered resource list."""
    import base64

    campaign_id = unquote((event.get("pathParameters") or {}).get("id", ""))
    if not campaign_id:
        return _error(400, "INVALID_PARAM", "Campaign ID is required.")

    params = event.get("queryStringParameters") or {}

    # Validate limit
    raw_limit = params.get("limit", "50")
    try:
        limit = int(raw_limit)
    except (ValueError, TypeError):
        return _error(400, "INVALID_PARAM", "limit must be an integer.")
    if limit < 1 or limit > 200:
        return _error(400, "INVALID_PARAM", "limit must be between 1 and 200.")

    # Validate status filter
    status_filter = params.get("status")
    if status_filter and status_filter not in ("PENDING", "RESOLVED", "IMPAIRED", "UNKNOWN"):
        return _error(400, "INVALID_PARAM", "status must be one of: PENDING, RESOLVED, IMPAIRED, UNKNOWN.")

    # Validate accountId filter
    account_filter = params.get("accountId")
    if account_filter and not re.match(r"^\d{12}$", account_filter):
        return _error(400, "INVALID_PARAM", "accountId must be exactly 12 digits.")

    # Validate nextToken (base64-encoded integer offset)
    offset = 0
    raw_token = params.get("nextToken")
    if raw_token:
        try:
            decoded = base64.urlsafe_b64decode(raw_token).decode("utf-8")
            offset = int(decoded)
        except (ValueError, TypeError, Exception):
            return _error(400, "INVALID_PARAM", "nextToken is invalid.")
        if offset < 0:
            return _error(400, "INVALID_PARAM", "nextToken is invalid.")

    # Verify campaign exists
    try:
        campaigns_table = _dynamodb.Table(CAMPAIGNS_TABLE)
        resp = campaigns_table.get_item(Key={"campaignId": campaign_id})
    except ClientError:
        logger.exception("Failed to get campaign %s", campaign_id)
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to read campaign.")

    campaign = resp.get("Item")
    if not campaign:
        return _error(404, "NOT_FOUND", f"Campaign '{campaign_id}' not found.")

    total_count = int(campaign.get("totalResourceCount", 0) or 0)

    # Query all resources for campaign
    try:
        from boto3.dynamodb.conditions import Key
        resources_table = _dynamodb.Table(RESOURCES_TABLE)
        all_resources = []
        query_kwargs = {"KeyConditionExpression": Key("campaignId").eq(campaign_id)}
        while True:
            res_resp = resources_table.query(**query_kwargs)
            all_resources.extend(res_resp.get("Items", []))
            last_key = res_resp.get("LastEvaluatedKey")
            if not last_key:
                break
            query_kwargs["ExclusiveStartKey"] = last_key
    except ClientError:
        logger.exception("Failed to query resources for %s", campaign_id)
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to read resources.")

    # Apply filters
    if status_filter or account_filter:
        filtered = []
        for r in all_resources:
            if status_filter:
                r_status = (r.get("status", r.get("healthStatus", "")) or "").upper()
                if r_status != status_filter:
                    continue
            if account_filter:
                if r.get("accountId", "") != account_filter:
                    continue
            filtered.append(r)
    else:
        filtered = all_resources

    filtered_count = len(filtered)

    # Paginate
    page = filtered[offset:offset + limit]
    has_more = offset + limit < filtered_count
    next_token = None
    if has_more:
        next_token = base64.urlsafe_b64encode(str(offset + limit).encode("utf-8")).decode("utf-8")

    platform = operative_platform(resolve_platforms(_config_table()))

    return _success(200, {
        "campaignId": campaign_id,
        "resources": [_format_resource(r, platform) for r in page],
        "nextToken": next_token,
        "totalCount": total_count,
        "filteredCount": filtered_count,
    })


def handle_campaign_breakdown(event, context):
    """GET /api/campaigns/{id}/breakdown — team/status breakdown for a campaign."""
    campaign_id = unquote((event.get("pathParameters") or {}).get("id", ""))
    if not campaign_id:
        return _error(400, "INVALID_PARAM", "Campaign ID is required.")

    # Validate groupBy param
    params = event.get("queryStringParameters") or {}
    group_by = params.get("groupBy", "accountId")
    if group_by not in ("accountId", "ticketStatus"):
        return _error(400, "INVALID_PARAM", "groupBy must be one of: accountId, ticketStatus")

    # Verify campaign exists
    try:
        campaigns_table = _dynamodb.Table(CAMPAIGNS_TABLE)
        resp = campaigns_table.get_item(Key={"campaignId": campaign_id})
    except ClientError:
        logger.exception("Failed to get campaign %s", campaign_id)
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to read campaign.")

    campaign = resp.get("Item")
    if not campaign:
        return _error(404, "NOT_FOUND", f"Campaign '{campaign_id}' not found.")

    campaign_type = campaign.get("campaignType", "resource-level")

    # Query all resources (paginated)
    try:
        from boto3.dynamodb.conditions import Key
        resources_table = _dynamodb.Table(RESOURCES_TABLE)
        resources = []
        query_kwargs = {"KeyConditionExpression": Key("campaignId").eq(campaign_id)}
        while True:
            res_resp = resources_table.query(**query_kwargs)
            resources.extend(res_resp.get("Items", []))
            last_key = res_resp.get("LastEvaluatedKey")
            if not last_key:
                break
            query_kwargs["ExclusiveStartKey"] = last_key
    except ClientError:
        logger.exception("Failed to query resources for %s", campaign_id)
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to read resources.")

    # Handle account-level campaigns with no resources
    if not resources and campaign_type == "account-level":
        acct = campaign.get("affectedAccount", "unknown")
        status = (campaign.get("status", "active") or "active").lower()
        resolved = 1 if status == "resolved" else 0
        pending = 0 if resolved else 1
        resources = [{"accountId": acct, "status": "RESOLVED" if resolved else "PENDING", "ticketStatus": "none"}]

    # Compute totals
    total_resources = len(resources)
    resolved_resources = sum(1 for r in resources if (r.get("status") or "PENDING").upper() == "RESOLVED")
    completion_pct = round((resolved_resources / total_resources) * 100, 1) if total_resources > 0 else 0.0

    # Build groups
    if group_by == "accountId":
        groups = _breakdown_by_account(resources)
    else:
        groups = _breakdown_by_ticket_status(resources)

    return _success(200, {
        "campaignId": campaign_id,
        "campaignType": campaign_type,
        "totalResources": total_resources,
        "resolvedResources": resolved_resources,
        "completionPct": completion_pct,
        "groups": groups,
    })


def _breakdown_by_account(resources: list) -> list:
    """Group resources by accountId, enrich with account names, sort by pending desc."""
    groups = {}
    for r in resources:
        acct = r.get("accountId", "unknown")
        if acct not in groups:
            groups[acct] = {"total": 0, "pending": 0, "resolved": 0, "ticketStatus": {}}
        groups[acct]["total"] += 1
        if (r.get("status") or "PENDING").upper() == "RESOLVED":
            groups[acct]["resolved"] += 1
        else:
            groups[acct]["pending"] += 1
        ts = r.get("ticketStatus", "none")
        groups[acct]["ticketStatus"][ts] = groups[acct]["ticketStatus"].get(ts, 0) + 1

    # Enrich account names from ConfigTable
    account_names = _get_account_names(list(groups.keys()))

    result = []
    for acct_id, data in groups.items():
        total = data["total"]
        resolved = data["resolved"]
        result.append({
            "groupKey": acct_id,
            "groupLabel": account_names.get(acct_id, acct_id),
            "total": total,
            "pending": data["pending"],
            "resolved": resolved,
            "completionPct": round((resolved / total) * 100, 1) if total > 0 else 0.0,
            "ticketStatus": data["ticketStatus"],
        })

    result.sort(key=lambda g: (-g["pending"], -g["total"], g["groupKey"]))
    return result


def _breakdown_by_ticket_status(resources: list) -> list:
    """Group resources by ticketStatus."""
    label_map = {"none": "No Ticket", "Created": "Created", "In Progress": "In Progress", "Closed": "Closed"}
    groups = {}
    for r in resources:
        ts = r.get("ticketStatus", "none")
        if ts not in groups:
            groups[ts] = {"total": 0, "pending": 0, "resolved": 0, "ticketStatus": {}}
        groups[ts]["total"] += 1
        if (r.get("status") or "PENDING").upper() == "RESOLVED":
            groups[ts]["resolved"] += 1
        else:
            groups[ts]["pending"] += 1
        groups[ts]["ticketStatus"][ts] = groups[ts]["ticketStatus"].get(ts, 0) + 1

    result = []
    for ts_key, data in groups.items():
        total = data["total"]
        resolved = data["resolved"]
        result.append({
            "groupKey": ts_key,
            "groupLabel": label_map.get(ts_key, ts_key),
            "total": total,
            "pending": data["pending"],
            "resolved": resolved,
            "completionPct": round((resolved / total) * 100, 1) if total > 0 else 0.0,
            "ticketStatus": data["ticketStatus"],
        })

    result.sort(key=lambda g: (-g["pending"], -g["total"], g["groupKey"]))
    return result


def _get_account_names(account_ids: list) -> dict:
    """BatchGetItem from ConfigTable to resolve account IDs to names."""
    if not account_ids:
        return {}
    names = {}
    config_table_name = CONFIG_TABLE
    # BatchGetItem supports max 100 keys per call
    for i in range(0, len(account_ids), 100):
        batch = account_ids[i:i + 100]
        keys = [{"pk": f"ROUTING#{acct}"} for acct in batch]
        try:
            resp = boto3.client("dynamodb").batch_get_item(
                RequestItems={
                    config_table_name: {
                        "Keys": [{"pk": {"S": f"ROUTING#{acct}"}} for acct in batch],
                        "ProjectionExpression": "pk, account_name",
                    }
                }
            )
            for item in resp.get("Responses", {}).get(config_table_name, []):
                pk = item.get("pk", {}).get("S", "")
                acct_id = pk.replace("ROUTING#", "") if pk.startswith("ROUTING#") else ""
                name = item.get("account_name", {}).get("S", "")
                if acct_id and name:
                    names[acct_id] = name
        except ClientError:
            logger.exception("BatchGetItem failed for account names")
    return names


def handle_generate_events(event, context):
    """POST /api/generate-events — trigger event generator Lambda async."""
    body = _parse_body(event)
    if body is None:
        return _error(400, "INVALID_REQUEST", "Request body must be valid JSON.")

    single_event = body.get("singleEvent")
    count = body.get("count")

    if single_event is None and count not in (10, 100, 1000):
        return _error(400, "INVALID_PARAM", "count must be one of: 10, 100, 1000")

    generator_fn = os.environ.get("EVENT_GENERATOR_FUNCTION_NAME", "")
    if not generator_fn:
        return _error(400, "NOT_CONFIGURED", "Event generator not configured")

    payload = {"count": 1, "singleEvent": single_event} if single_event else {"count": count}

    try:
        _lambda_client.invoke(
            FunctionName=generator_fn,
            InvocationType="Event",
            Payload=json.dumps(payload).encode("utf-8"),
        )
    except ClientError:
        logger.exception("Failed to invoke event generator Lambda")
        return _error(502, "INVOKE_FAILED", "Failed to trigger event generator.")

    return _success(202, {"status": "triggered", "count": 1 if single_event else count, "published": 1 if single_event else count})


# ===================================================================
# PUT /api/config/platform — Switch active ITSM platform (STORY-055)
# ===================================================================

_VALID_PLATFORMS = frozenset({"jira", "servicenow"})


def handle_platform_switch(event, context):
    """Switch the active ITSM platform.

    Pre-condition: target platform's connection must be validated.
    Security C-4: audit log entry for every switch attempt.
    """
    body = _parse_body(event)
    if not body or not isinstance(body, dict):
        return _error(400, "CFG_INVALID_REQUEST", "Request body must be valid JSON")

    target_platform = body.get("platform", "").strip().lower()
    if target_platform not in _VALID_PLATFORMS:
        return _error(400, "CFG_INVALID_PLATFORM",
                      f"platform must be one of: {', '.join(sorted(_VALID_PLATFORMS))}")

    config = _config_table()
    source_ip = event.get("requestContext", {}).get("identity", {}).get("sourceIp", "")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Pre-condition: target platform connection must be validated
    if target_platform == "jira":
        try:
            item = config.get_item(Key={"pk": "JIRA_CONNECTION"}).get("Item")
        except ClientError:
            item = None
        if not item or not item.get("validated"):
            logger.warning(json.dumps({
                "audit": True,
                "action": "PLATFORM_SWITCH",
                "source_ip": source_ip,
                "target_platform": target_platform,
                "result": "rejected_not_validated",
                "timestamp": now,
            }))
            return _error(400, "CFG_PLATFORM_NOT_VALIDATED",
                          "JIRA connection must be validated before switching.")
    elif target_platform == "servicenow":
        try:
            item = config.get_item(Key={"pk": "SNOW_CONNECTION"}).get("Item")
        except ClientError:
            item = None
        if not item or not item.get("validated"):
            logger.warning(json.dumps({
                "audit": True,
                "action": "PLATFORM_SWITCH",
                "source_ip": source_ip,
                "target_platform": target_platform,
                "result": "rejected_not_validated",
                "timestamp": now,
            }))
            return _error(400, "CFG_PLATFORM_NOT_VALIDATED",
                          "ServiceNow connection must be validated before switching.")

    # Write platform selection
    try:
        config.put_item(Item={
            "pk": "ITSM_PLATFORM",
            "platform": target_platform,
            "switched_at": now,
        })
    except ClientError:
        logger.exception("DynamoDB write failed for ITSM_PLATFORM")
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to switch platform.")

    # Security C-4: audit log
    logger.warning(json.dumps({
        "audit": True,
        "action": "PLATFORM_SWITCH",
        "source_ip": source_ip,
        "target_platform": target_platform,
        "result": "success",
        "timestamp": now,
    }))

    return _success(200, {"platform": target_platform, "switchedAt": now})


# ===================================================================
# Setup Timer Handlers (STORY-079: Setup Time Measurement, B-CFG-2)
# ===================================================================

def handle_setup_timer_start(event, context):
    """POST /api/config/setup-timer/start — record setup start (once only)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        _config_table().put_item(
            Item={"pk": "SETUP_TIMER", "setup_started_at": now},
            ConditionExpression="attribute_not_exists(pk)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            pass  # Already started, ignore
        else:
            raise
    return _success(200, {"started": True})


def handle_setup_timer_complete(event, context):
    """POST /api/config/setup-timer/complete — record setup completion."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        _config_table().update_item(
            Key={"pk": "SETUP_TIMER"},
            UpdateExpression="SET setup_completed_at = :t",
            ExpressionAttributeValues={":t": now},
        )
    except ClientError:
        logger.exception("Failed to update setup timer")
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to update timer.")
    return _success(200, {"completed": True})


def handle_setup_timer_get(event, context):
    """GET /api/config/setup-timer — read timer and compute duration."""
    try:
        item = _config_table().get_item(Key={"pk": "SETUP_TIMER"}).get("Item")
    except ClientError:
        logger.exception("Failed to read setup timer")
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to read timer.")
    if not item:
        return _success(200, {"started": False, "completed": False, "durationMinutes": None})
    started = item.get("setup_started_at")
    completed = item.get("setup_completed_at")
    duration = None
    if started and completed:
        s = datetime.fromisoformat(started.replace("Z", "+00:00"))
        c = datetime.fromisoformat(completed.replace("Z", "+00:00"))
        duration = round((c - s).total_seconds() / 60, 1)
    return _success(200, {
        "started": bool(started),
        "completed": bool(completed),
        "startedAt": started,
        "completedAt": completed,
        "durationMinutes": duration,
    })


# ===================================================================
# Telemetry Status Handler (STORY-080: Beta Telemetry)
# ===================================================================

def handle_telemetry_status(event, context):
    """GET /api/config/telemetry — read latest telemetry payload."""
    try:
        item = _config_table().get_item(Key={"pk": "TELEMETRY_LATEST"}).get("Item")
    except ClientError:
        logger.exception("Failed to read telemetry status")
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to read telemetry.")
    if not item:
        return _success(200, {"collected": False})
    item.pop("pk", None)
    return _success(200, {"collected": True, **item})


# ===================================================================
# CMDB Routing Config (STORY-087: B-SNOW-3)
# ===================================================================

def handle_cmdb_config_get(event, context):
    """GET /api/config/cmdb-routing — read CMDB routing configuration."""
    try:
        item = _config_table().get_item(Key={"pk": "CMDB_ROUTING"}).get("Item")
    except ClientError:
        logger.exception("Failed to read CMDB routing config")
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to read CMDB config.")
    if not item:
        return _success(200, {
            "enabled": False,
            "ciClass": "cmdb_ci",
            "lookupField": "object_id",
            "cacheTtlHours": 24,
        })
    return _success(200, {
        "enabled": item.get("enabled", False),
        "ciClass": item.get("ci_class", "cmdb_ci"),
        "lookupField": item.get("lookup_field", "object_id"),
        "cacheTtlHours": item.get("cache_ttl_hours", 24),
    })


def handle_cmdb_config_save(event, context):
    """POST /api/config/cmdb-routing — save CMDB routing configuration."""
    body = _parse_body(event)
    if not body:
        return _error(400, "INVALID_PARAM", "Request body required")

    item = {
        "pk": "CMDB_ROUTING",
        "enabled": bool(body.get("enabled", False)),
        "ci_class": body.get("ciClass", "cmdb_ci"),
        "lookup_field": body.get("lookupField", "object_id"),
        "cache_ttl_hours": int(body.get("cacheTtlHours", 24)),
        "platform": "servicenow",
    }
    try:
        _config_table().put_item(Item=item)
    except ClientError:
        logger.exception("Failed to save CMDB routing config")
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to save CMDB config.")
    return _success(200, {"saved": True})


# ===================================================================
# Service-Based Routing Config (STORY-088: S-9)
# ===================================================================

def handle_service_routing_get(event, context):
    """GET /api/config/routing/services — list service routing rules."""
    try:
        resp = _config_table().scan(
            FilterExpression="begins_with(pk, :prefix)",
            ExpressionAttributeValues={":prefix": "SERVICE_ROUTING#"},
            ProjectionExpression="pk, service, routing_target, issue_type",
        )
        items = [
            {
                "service": item.get("service", ""),
                "routingTarget": item.get("routing_target", ""),
                "issueType": item.get("issue_type", "Task"),
            }
            for item in resp.get("Items", [])
        ]
    except ClientError:
        logger.exception("Failed to read service routing config")
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to read service routing.")
    return _success(200, {"services": items})


def handle_service_routing_save(event, context):
    """POST /api/config/routing/services — save a service routing rule."""
    body = _parse_body(event)
    if not body or not body.get("service") or not body.get("routingTarget"):
        return _error(400, "INVALID_PARAM", "service and routingTarget required")
    service = body["service"].upper().strip()
    target = body["routingTarget"].strip()
    issue_type = body.get("issueType", "Task").strip() or "Task"
    try:
        _config_table().put_item(Item={
            "pk": f"SERVICE_ROUTING#{service}",
            "service": service,
            "routing_target": target,
            "issue_type": issue_type,
        })
    except ClientError:
        logger.exception("Failed to save service routing config")
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to save service routing.")
    return _success(200, {"saved": True, "service": service, "routingTarget": target})


def handle_service_routing_delete(event, context):
    """DELETE /api/config/routing/services/{service} — delete a service routing rule."""
    service = (event.get("pathParameters") or {}).get("service", "").upper().strip()
    if not service:
        return _error(400, "INVALID_PARAM", "Service name required")
    try:
        _config_table().delete_item(Key={"pk": f"SERVICE_ROUTING#{service}"})
    except ClientError:
        logger.exception("Failed to delete service routing config")
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to delete service routing.")
    return _success(200, {"deleted": True})


# ===================================================================
# GET /api/routing/orphans (STORY-089: Orphan Queue Visibility)
# ===================================================================

def handle_orphan_metrics(event, context):
    """Return orphan queue metrics and routing suggestions."""
    resources_table = _dynamodb.Table(RESOURCES_TABLE)

    # Scan for resources routed via default (orphan queue)
    orphan_accounts = {}  # {account_id: {count, firstSeen}}
    scan_kwargs = {
        "ProjectionExpression": "accountId, routedVia, createdAt",
        "FilterExpression": "routedVia = :d",
        "ExpressionAttributeValues": {":d": "default"},
    }
    pages = 0
    while pages < 10:
        resp = resources_table.scan(**scan_kwargs)
        pages += 1
        for item in resp.get("Items", []):
            aid = item.get("accountId", "unknown")
            if aid not in orphan_accounts:
                orphan_accounts[aid] = {"count": 0, "firstSeen": item.get("createdAt", "")}
            orphan_accounts[aid]["count"] += 1
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    accounts = []
    for aid, data in sorted(orphan_accounts.items(), key=lambda x: x[1]["count"], reverse=True):
        accounts.append({
            "accountId": aid,
            # STORY-133 (Q4/BR-6/AC-4): this is a per-account count of
            # ResourcesTable rows with routedVia == "default" (a RESOURCE
            # tally, one increment per resource row above), not a ticket
            # count. Renamed from the misleading "ticketCount".
            "resourceCount": data["count"],
            "firstSeen": data["firstSeen"],
        })

    # Read suggestions from ConfigTable (populated by sync Lambda)
    suggestions = []
    try:
        suggestion_resp = _config_table().scan(
            FilterExpression="begins_with(pk, :prefix)",
            ExpressionAttributeValues={":prefix": "ROUTING_SUGGESTION#"},
        )
        for item in suggestion_resp.get("Items", []):
            suggestions.append({
                "accountId": item.get("account_id", ""),
                "suggestedTarget": item.get("suggested_target", ""),
                "reason": item.get("reason", ""),
            })
    except ClientError:
        logger.exception("Failed to read routing suggestions")

    return _success(200, {
        # STORY-133 (Q1/Q4/BR-6/AC-4): this endpoint is the per-account
        # RESOURCE breakdown for the "which accounts to map" workflow — it is
        # NOT the headline orphan count. The headline TICKET count is served by
        # GET /api/config/routing/orphan-status (orphan_handlers.handle_orphan_status),
        # which the dashboard card/banner consume. The top-level sum here is a
        # count of default-routed RESOURCE rows; renamed from the ticket-implying
        # "orphanCount".
        "defaultRoutedResourceCount": sum(d["count"] for d in orphan_accounts.values()),
        "accounts": accounts[:50],
        "suggestions": suggestions,
    })