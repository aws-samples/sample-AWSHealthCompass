"""Platform-agnostic ITSM orchestration — routes events to ticket creation.

STORY-054: Extracted from handler.py. Contains ZERO platform-specific imports.
Receives ITSMClient and ContentFormatter via dependency injection.

Responsibilities:
- Parse SQS/SNS message envelopes
- Fetch S3 offloaded payloads
- Gate checks (schema, dispatch, routing, config)
- Idempotency checks via DynamoDB
- Dispatch to create/update ticket flows
- Write results to DynamoDB
- Structured error logging

Consumers: jira_handler.py, servicenow_handler.py (Beta).
Dependencies: resolve_core.itsm_client (ABC only), resolve_core.ticket_builder,
    resolve_core.payload, boto3, Python stdlib.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

from resolve_core.itsm_client import ContentFormatter, ITSMAPIError, ITSMClient
from resolve_core.ticket_builder import (
    build_burndown_comment,
    build_template_a,
    build_template_b,
    build_update_summary,
)

logger = logging.getLogger("compass")

# --- Constants ---

_SUCCESS = {"batchItemFailures": []}
_MAX_LOG_LEN = 512
_ACCOUNT_ID_RE = re.compile(r"^\d{12}$")
_COMMENT_COOLDOWN_HOURS = 23


# ===================================================================
# Structured Error Logging
# ===================================================================


def _log_error(
    operation: str,
    error_code: str,
    message: str,
    http_status: Optional[int] = None,
    platform_errors: Optional[dict] = None,
    routing_target: Optional[str] = None,
    ticket_key: Optional[str] = None,
    event_arn: str = "",
    campaign_id: str = "",
    affected_account: str = "",
    tracking_key: str = "",
    disposition: Optional[str] = None,
    request_id: str = "",
) -> None:
    """Emit a structured error log entry (platform-agnostic)."""
    safe_errors = {}
    if isinstance(platform_errors, dict):
        for k, v in platform_errors.items():
            safe_errors[str(k)[:100]] = str(v)[:200]

    entry = {
        "level": "ERROR",
        "source": "itsm-integration",
        "operation": operation,
        "errorCode": error_code,
        "message": message,
        "detail": {
            "httpStatus": http_status,
            "platformErrors": safe_errors,
            "routingTarget": routing_target,
            "ticketKey": ticket_key,
        },
        "context": {
            "eventArn": event_arn[:_MAX_LOG_LEN],
            "campaignId": campaign_id[:_MAX_LOG_LEN],
            "affectedAccount": affected_account[:12],
            "trackingKey": tracking_key[:_MAX_LOG_LEN],
        },
        "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        "requestId": request_id,
    }
    if disposition:
        entry["disposition"] = disposition
    logger.error(json.dumps(entry, default=str))


# ===================================================================
# Idempotency Check
# ===================================================================


def _check_idempotency(resources_table, campaign_id: str, tracking_key: str) -> Optional[str]:
    """Return existing ticketId if found, else None."""
    if not campaign_id or not tracking_key:
        return None
    try:
        resp = resources_table.get_item(
            Key={"campaignId": campaign_id, "trackingKey": tracking_key},
            ProjectionExpression="ticketId",
        )
        item = resp.get("Item")
        if item and item.get("ticketId") and item.get("ticketId") != "none":
            return item["ticketId"]
    except ClientError:
        pass
    return None


# ===================================================================
# DynamoDB Writes
# ===================================================================


def _write_ticket_to_resources(
    resources_table, campaign_id: str, resources: list,
    ticket_key: str, ticket_url: str, now: str,
) -> None:
    """Write ticket metadata to ResourcesTable for each resource."""
    if not ticket_key:
        return
    ticket_data = {
        "ticketId": ticket_key,
        "ticketStatus": "Created",
        "ticketRawStatus": "",
        "ticketUrl": ticket_url,
        "ticketUpdatedAt": now,
    }
    for resource in resources:
        tracking_key = resource.get("trackingKey", "")
        if not tracking_key:
            tracking_key = resource.get("arn") or resource.get("entityValue", "")
        if not tracking_key:
            continue
        try:
            # Primary path: nested SET into existing tickets map
            resources_table.update_item(
                Key={"campaignId": campaign_id, "trackingKey": tracking_key},
                UpdateExpression=(
                    "SET #t.#platform = :ticket_data, "
                    "ticketId = :tid, ticketUrl = :turl, "
                    "ticketStatus = :ts, ticketUpdatedAt = :tua, "
                    "ticketPlatform = :tp"
                ),
                ExpressionAttributeNames={
                    "#t": "tickets",
                    "#platform": "jira",  # SEC-111-1: hardcoded platform key
                },
                ExpressionAttributeValues={
                    ":ticket_data": ticket_data,
                    ":tid": ticket_key, ":turl": ticket_url,
                    ":ts": "Created", ":tua": now,
                    ":tp": "jira",
                },
            )
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code == "ValidationException":
                # Fallback: tickets map doesn't exist (pre-STORY-111 item)
                try:
                    resources_table.update_item(
                        Key={"campaignId": campaign_id, "trackingKey": tracking_key},
                        UpdateExpression=(
                            "SET #t = :ticket_map, "
                            "ticketId = :tid, ticketUrl = :turl, "
                            "ticketStatus = :ts, ticketUpdatedAt = :tua, "
                            "ticketPlatform = :tp"
                        ),
                        ExpressionAttributeNames={
                            "#t": "tickets",
                        },
                        ExpressionAttributeValues={
                            ":ticket_map": {"jira": ticket_data},
                            ":tid": ticket_key, ":turl": ticket_url,
                            ":ts": "Created", ":tua": now,
                            ":tp": "jira",
                        },
                    )
                except ClientError:
                    logger.warning(
                        "Failed to write ticket to resource (fallback) — "
                        "campaign_id=%s tracking_key=%s",
                        campaign_id[:_MAX_LOG_LEN],
                        tracking_key[:_MAX_LOG_LEN],
                    )
            else:
                logger.warning(
                    "Failed to write ticket to resource — "
                    "campaign_id=%s tracking_key=%s",
                    campaign_id[:_MAX_LOG_LEN],
                    tracking_key[:_MAX_LOG_LEN],
                )


def _increment_tickets_created(
    campaigns_table, campaign_id: str, count: int, now: str,
) -> None:
    """Increment ticketsCreated counter on CampaignsTable."""
    if count <= 0:
        return
    try:
        campaigns_table.update_item(
            Key={"campaignId": campaign_id},
            UpdateExpression="ADD ticketsCreated :n SET updatedAt = :now",
            ExpressionAttributeValues={":n": count, ":now": now},
        )
    except ClientError:
        logger.warning(
            "Failed to increment ticketsCreated — campaign_id=%s count=%d",
            campaign_id[:_MAX_LOG_LEN], count,
        )


# ===================================================================
# Bulk Idempotency Pre-Flight
# ===================================================================


def _filter_already_ticketed(resources_table, campaign_id: str, tracking_keys: List[str]) -> set:
    """Return set of tracking_keys that already have a ticketId."""
    existing: set = set()
    if not campaign_id or not tracking_keys:
        return existing
    try:
        resp = resources_table.query(
            KeyConditionExpression="campaignId = :cid",
            ExpressionAttributeValues={":cid": campaign_id},
            ProjectionExpression="trackingKey, ticketId",
        )
        for item in resp.get("Items", []):
            if item.get("ticketId"):
                existing.add(item["trackingKey"])
        while resp.get("LastEvaluatedKey"):
            resp = resources_table.query(
                KeyConditionExpression="campaignId = :cid",
                ExpressionAttributeValues={":cid": campaign_id},
                ProjectionExpression="trackingKey, ticketId",
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            for item in resp.get("Items", []):
                if item.get("ticketId"):
                    existing.add(item["trackingKey"])
    except ClientError:
        logger.warning(
            "Idempotency pre-flight query failed — campaign_id=%s",
            campaign_id[:_MAX_LOG_LEN],
        )
    return existing


# ===================================================================
# Bulk Write-Back
# ===================================================================


def _write_bulk_results(
    resources_table, campaign_id: str, result, tracking_keys: List[str], now: str,
) -> int:
    """Write bulk create successes to ResourcesTable. Returns DB write count."""
    db_write_count = 0
    for success in result.successes:
        idx = success["index"]
        if idx < 0 or idx >= len(tracking_keys):
            continue
        tk = tracking_keys[idx]
        ticket_data = {
            "ticketId": success["ticketKey"],
            "ticketStatus": "Created",
            "ticketRawStatus": "",
            "ticketUrl": success["ticketUrl"],
            "ticketUpdatedAt": now,
        }
        try:
            # Primary path: nested SET into existing tickets map
            resources_table.update_item(
                Key={"campaignId": campaign_id, "trackingKey": tk},
                UpdateExpression=(
                    "SET #t.#platform = :ticket_data, "
                    "ticketId = :tid, ticketUrl = :turl, "
                    "ticketStatus = :ts, ticketUpdatedAt = :tua, "
                    "ticketPlatform = :tp"
                ),
                ExpressionAttributeNames={
                    "#t": "tickets",
                    "#platform": "jira",  # SEC-111-1: hardcoded platform key
                },
                ConditionExpression=(
                    "attribute_not_exists(ticketId) OR ticketId = :empty"
                ),
                ExpressionAttributeValues={
                    ":ticket_data": ticket_data,
                    ":tid": success["ticketKey"],
                    ":turl": success["ticketUrl"],
                    ":ts": "Created", ":tua": now, ":empty": "",
                    ":tp": "jira",
                },
            )
            db_write_count += 1
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code == "ConditionalCheckFailedException":
                logger.info(
                    "PROC_TICKET_DUPLICATE — campaign_id=%s tracking_key=%s",
                    campaign_id[:_MAX_LOG_LEN], tk[:_MAX_LOG_LEN],
                )
            elif error_code == "ValidationException":
                # Fallback: tickets map doesn't exist (pre-STORY-111 item)
                try:
                    resources_table.update_item(
                        Key={"campaignId": campaign_id, "trackingKey": tk},
                        UpdateExpression=(
                            "SET #t = :ticket_map, "
                            "ticketId = :tid, ticketUrl = :turl, "
                            "ticketStatus = :ts, ticketUpdatedAt = :tua, "
                            "ticketPlatform = :tp"
                        ),
                        ExpressionAttributeNames={
                            "#t": "tickets",
                        },
                        ConditionExpression=(
                            "attribute_not_exists(ticketId) OR ticketId = :empty"
                        ),
                        ExpressionAttributeValues={
                            ":ticket_map": {"jira": ticket_data},
                            ":tid": success["ticketKey"],
                            ":turl": success["ticketUrl"],
                            ":ts": "Created", ":tua": now, ":empty": "",
                            ":tp": "jira",
                        },
                    )
                    db_write_count += 1
                except ClientError:
                    logger.warning(
                        "Failed to write bulk ticket result (fallback) — "
                        "campaign_id=%s tracking_key=%s",
                        campaign_id[:_MAX_LOG_LEN], tk[:_MAX_LOG_LEN],
                    )
            else:
                logger.warning(
                    "Failed to write bulk ticket result — "
                    "campaign_id=%s tracking_key=%s",
                    campaign_id[:_MAX_LOG_LEN], tk[:_MAX_LOG_LEN],
                )
    return db_write_count


# ===================================================================
# Template B Tracking Write
# ===================================================================


def _write_template_b_tracking(
    resources_table, campaign_id: str, tracking_key: str,
    account_id: str, region: str,
    ticket_key: str, ticket_url: str, now: str,
) -> None:
    """Write Template B tracking record to ResourcesTable."""
    import time as _time
    expires_at = int(_time.time()) + (180 * 86400)
    try:
        resources_table.put_item(
            Item={
                "campaignId": campaign_id,
                "trackingKey": tracking_key,
                "entityValue": None,
                "accountId": account_id,
                "region": region,
                "healthStatus": None,
                "lastUpdatedTime": None,
                "resourceTags": {},
                "ticketId": ticket_key,
                "ticketUrl": ticket_url,
                "ticketStatus": "Created",
                "ticketRawStatus": None,
                "ticketUpdatedAt": now,
                "ticketPlatform": "jira",
                "tickets": {
                    "jira": {
                        "ticketId": ticket_key,
                        "ticketStatus": "Created",
                        "ticketRawStatus": "",
                        "ticketUrl": ticket_url,
                        "ticketUpdatedAt": now,
                    },
                },
                "createdAt": now,
                "updatedAt": now,
                "expiresAt": expires_at,
            },
            ConditionExpression=(
                "attribute_not_exists(trackingKey) OR ticketId = :null"
            ),
            ExpressionAttributeValues={":null": None},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.warning(
                "Template B tracking record already exists — "
                "campaign_id=%s tracking_key=%s",
                campaign_id[:_MAX_LOG_LEN], tracking_key[:_MAX_LOG_LEN],
            )
        else:
            logger.warning(
                "Failed to write Template B tracking — "
                "campaign_id=%s tracking_key=%s ticket=%s",
                campaign_id[:_MAX_LOG_LEN], tracking_key[:_MAX_LOG_LEN], ticket_key,
            )


# ===================================================================
# Resource Update Helpers
# ===================================================================


def _is_ple(event_data: dict) -> bool:
    """Check if the event is a Planned Lifecycle Event."""
    code = event_data.get("eventTypeCode", "")
    return isinstance(code, str) and code.endswith("_PLANNED_LIFECYCLE_EVENT")


def _get_resources_by_ticket(resources_table, campaign_id: str) -> dict:
    """Query ResourcesTable grouped by ticketId."""
    resources_by_ticket: Dict[str, List[dict]] = {}
    try:
        resp = resources_table.query(
            KeyConditionExpression="campaignId = :cid",
            ExpressionAttributeValues={":cid": campaign_id},
        )
        items = resp.get("Items", [])
        while resp.get("LastEvaluatedKey"):
            resp = resources_table.query(
                KeyConditionExpression="campaignId = :cid",
                ExpressionAttributeValues={":cid": campaign_id},
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items.extend(resp.get("Items", []))
    except ClientError:
        logger.warning("Failed to query resources for campaign_id=%s", campaign_id[:_MAX_LOG_LEN])
        return {}

    for item in items:
        tid = item.get("ticketId")
        if not tid or item.get("ticketStatus") == "unknown":
            continue
        resources_by_ticket.setdefault(tid, []).append(item)
    return resources_by_ticket


def _should_post_comment(campaigns_table, campaign_id: str, is_fully_resolved: bool) -> bool:
    """Check burndown comment frequency guard."""
    if is_fully_resolved:
        return True
    try:
        resp = campaigns_table.get_item(
            Key={"campaignId": campaign_id},
            ProjectionExpression="lastBurndownCommentAt",
        )
        item = resp.get("Item", {})
    except ClientError:
        return True

    last_ts = item.get("lastBurndownCommentAt")
    if not last_ts or not isinstance(last_ts, str):
        return True
    try:
        last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
        return elapsed >= _COMMENT_COOLDOWN_HOURS * 3600
    except (ValueError, TypeError):
        return True


def _detect_newly_resolved(incoming: List[dict], stored: List[dict]) -> List[str]:
    """Detect resources that changed to RESOLVED."""
    stored_status: Dict[str, str] = {}
    for r in stored:
        tk = r.get("trackingKey", "")
        if tk:
            stored_status[tk] = r.get("healthStatus", "")

    newly_resolved = []
    for r in incoming:
        if r.get("healthStatus") != "RESOLVED" and r.get("status") != "RESOLVED":
            continue
        arn = r.get("arn") or r.get("entityValue", "")
        tk = r.get("trackingKey", "") or arn
        if tk and stored_status.get(tk) not in ("RESOLVED",):
            newly_resolved.append(str(arn)[:2048])
    return newly_resolved


def _update_last_burndown(campaigns_table, campaign_id: str) -> None:
    """Write lastBurndownCommentAt to CampaignsTable."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        campaigns_table.update_item(
            Key={"campaignId": campaign_id},
            UpdateExpression="SET lastBurndownCommentAt = :ts, updatedAt = :ts",
            ExpressionAttributeValues={":ts": now},
        )
    except ClientError:
        logger.warning("Failed to update lastBurndownCommentAt — campaign_id=%s", campaign_id[:_MAX_LOG_LEN])


def _write_back_resource_statuses(resources_table, campaign_id: str, incoming: List[dict]) -> None:
    """Write-back updated resource statuses to ResourcesTable."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for r in incoming:
        arn = r.get("arn") or r.get("entityValue", "")
        tk = r.get("trackingKey", "") or arn
        if not tk:
            continue
        health_status = r.get("healthStatus") or r.get("status", "")
        if not isinstance(health_status, str):
            health_status = "UNKNOWN"
        last_updated = r.get("lastUpdatedTime", now)
        try:
            resources_table.update_item(
                Key={"campaignId": campaign_id, "trackingKey": tk},
                UpdateExpression=(
                    "SET healthStatus = :hs, lastUpdatedTime = :lu, updatedAt = :now"
                ),
                ExpressionAttributeValues={
                    ":hs": health_status, ":lu": str(last_updated), ":now": now,
                },
            )
        except ClientError:
            logger.warning(
                "Failed to write-back resource status — campaign_id=%s tracking_key=%s",
                campaign_id[:_MAX_LOG_LEN], tk[:_MAX_LOG_LEN],
            )


# ===================================================================
# Ticket Creation — Template A (Resource-Level)
# ===================================================================


def create_template_a_ticket(
    payload: dict,
    client: ITSMClient,
    create_issue_fn: Callable,
    config: dict,
    resources_table,
    campaigns_table,
    request_id: str,
) -> str:
    """Create a ticket for a resource-level campaign.

    Args:
        client: ITSMClient instance (unused directly — kept for interface parity).
        create_issue_fn: Platform-specific single-issue creation callable.
        config: Platform config dict with base_url, tag_display_keys.

    Returns: ticket key string.
    """
    event = payload.get("event", {})
    resources = payload.get("resources", [])
    routing = payload.get("routing", {})
    account_tags = payload.get("accountTags", {})
    campaign_id = event.get("campaignId", "")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Idempotency check
    if resources:
        first = resources[0]
        idem_key = first.get("trackingKey", "")
        if not idem_key:
            idem_key = first.get("arn") or first.get("entityValue", "")
        existing = _check_idempotency(resources_table, campaign_id, idem_key)
        if existing:
            logger.info(
                "Duplicate ticket skipped — campaign_id=%s ticket=%s",
                campaign_id[:_MAX_LOG_LEN], existing,
            )
            return existing

    # Build ticket payload (platform-specific rendering happens in create_issue_fn)
    ticket = build_template_a(
        event=event, resources=resources, routing=routing,
        account_tags=account_tags,
        tag_display_keys=config.get("tag_display_keys"),
    )

    project = routing.get("resolvedProject", "")
    issue_type = routing.get("issueType", "Task")

    # Create via platform-specific function
    result = create_issue_fn(
        project_key=project,
        summary=ticket["summary"],
        description_adf=ticket["description_adf"],
        labels=ticket["labels"],
        due_date=ticket["due_date"],
        issue_type=issue_type,
    )

    ticket_key = result.get("key", "")
    base_url = config.get("jira_base_url", "") or config.get("base_url", "")
    ticket_url = f"{base_url}/browse/{ticket_key}" if ticket_key else ""

    _write_ticket_to_resources(resources_table, campaign_id, resources, ticket_key, ticket_url, now)
    _increment_tickets_created(campaigns_table, campaign_id, 1, now)

    logger.info(
        "Ticket created — campaign_id=%s ticket=%s project=%s resource_count=%d",
        campaign_id[:_MAX_LOG_LEN], ticket_key, project, len(resources),
    )
    return ticket_key


# ===================================================================
# Ticket Creation — Template B (Account-Level)
# ===================================================================


def create_template_b_ticket(
    payload: dict,
    client: ITSMClient,
    create_issue_fn: Callable,
    config: dict,
    resources_table,
    campaigns_table,
    request_id: str,
) -> Optional[str]:
    """Create a ticket for an account-level campaign (Template B)."""
    event = payload.get("event", {})
    routing = payload.get("routing", {})
    account_tags = payload.get("accountTags", {})
    campaign_id = event.get("campaignId", "")
    affected_account = event.get("affectedAccount", "")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if not isinstance(affected_account, str) or not _ACCOUNT_ID_RE.match(affected_account):
        _log_error(
            operation="create_ticket_template_b",
            error_code="PROC_TICKET_CREATE_FAILED",
            message="Invalid affectedAccount format",
            event_arn=event.get("eventArn", ""),
            campaign_id=campaign_id,
            disposition="message_deleted",
            request_id=request_id,
        )
        return None

    tracking_key = f"ACCOUNT#{affected_account}"

    existing = _check_idempotency(resources_table, campaign_id, tracking_key)
    if existing:
        logger.info(
            "Duplicate ticket skipped (Template B) — campaign_id=%s ticket=%s",
            campaign_id[:_MAX_LOG_LEN], existing,
        )
        return existing

    ticket = build_template_b(
        event=event, routing=routing, account_tags=account_tags,
        tag_display_keys=config.get("tag_display_keys"),
    )

    project = routing.get("resolvedProject", "")
    issue_type = routing.get("issueType", "Task")

    result = create_issue_fn(
        project_key=project,
        summary=ticket["summary"],
        description_adf=ticket["description_adf"],
        labels=ticket["labels"],
        due_date=ticket["due_date"],
        issue_type=issue_type,
    )

    ticket_key = result.get("key", "")
    base_url = config.get("jira_base_url", "") or config.get("base_url", "")
    ticket_url = f"{base_url}/browse/{ticket_key}" if ticket_key else ""

    _write_template_b_tracking(
        resources_table, campaign_id, tracking_key,
        affected_account, event.get("region", ""),
        ticket_key, ticket_url, now,
    )
    _increment_tickets_created(campaigns_table, campaign_id, 1, now)

    logger.info(
        "Ticket created (Template B) — campaign_id=%s ticket=%s project=%s account=%s",
        campaign_id[:_MAX_LOG_LEN], ticket_key, project, affected_account,
    )
    return ticket_key


# ===================================================================
# Ticket Update (RESOURCE_UPDATE)
# ===================================================================


def update_tickets(
    payload: dict,
    update_issue_fn: Callable,
    add_comment_fn: Callable,
    config: dict,
    resources_table,
    campaigns_table,
    request_id: str,
    on_not_found: Optional[Callable] = None,
    on_auth_failure: Optional[Callable] = None,
) -> bool:
    """Update existing tickets for a RESOURCE_UPDATE action.

    Returns True if all succeeded, False if retryable failure on all tickets.
    Platform-specific error classes are handled by the caller.
    """
    event_data = payload.get("event", {})
    campaign = payload.get("campaign", event_data)
    resources = payload.get("resources", [])
    campaign_id = str(event_data.get("campaignId", ""))

    # Gate: account-level campaigns have no resource burndown
    campaign_type = str(event_data.get("campaignType", campaign.get("campaignType", "")))
    if campaign_type == "account-level":
        logger.info("RESOURCE_UPDATE skipped — account-level — campaign_id=%s", campaign_id[:_MAX_LOG_LEN])
        return True

    # Gate: Non-PLE events are static
    if not _is_ple(event_data):
        logger.info("RESOURCE_UPDATE skipped — non-PLE — campaign_id=%s", campaign_id[:_MAX_LOG_LEN])
        return True

    if not isinstance(resources, list):
        logger.warning("RESOURCE_UPDATE skipped — resources not a list — campaign_id=%s", campaign_id[:_MAX_LOG_LEN])
        return True

    pending_count = campaign.get("pendingCount", 0)
    resolved_count = campaign.get("resolvedCount", 0)
    total_count = campaign.get("totalResourceCount", 0)

    resources_by_ticket = _get_resources_by_ticket(resources_table, campaign_id)
    if not resources_by_ticket:
        logger.info("RESOURCE_UPDATE — no ticketed resources — campaign_id=%s", campaign_id[:_MAX_LOG_LEN])
        _write_back_resource_statuses(resources_table, campaign_id, resources)
        return True

    all_stored = []
    for ticket_resources in resources_by_ticket.values():
        all_stored.extend(ticket_resources)
    newly_resolved_arns = _detect_newly_resolved(resources, all_stored)

    is_fully_resolved = (
        isinstance(total_count, (int, float)) and int(total_count) > 0
        and isinstance(resolved_count, (int, float)) and int(resolved_count) >= int(total_count)
    )

    updated_summary = build_update_summary(event_data, pending_count, resolved_count)

    had_retryable_failure = False
    for ticket_id, ticket_resources in resources_by_ticket.items():
        try:
            update_issue_fn(ticket_id, {"summary": updated_summary})

            if newly_resolved_arns or is_fully_resolved:
                if _should_post_comment(campaigns_table, campaign_id, is_fully_resolved):
                    comment_body = build_burndown_comment(
                        campaign={"totalResourceCount": total_count},
                        pending_count=pending_count,
                        resolved_count=resolved_count,
                        newly_resolved_arns=newly_resolved_arns,
                    )
                    if comment_body:
                        add_comment_fn(ticket_id, comment_body)
                        _update_last_burndown(campaigns_table, campaign_id)

        except ITSMAPIError as exc:
            if exc.status_code == 404 and on_not_found:
                on_not_found(ticket_id, campaign_id, ticket_resources, request_id)
            elif exc.status_code == 401 and on_auth_failure:
                on_auth_failure()
                raise
            elif exc.retryable:
                had_retryable_failure = True
                _log_error(
                    operation="update_ticket",
                    error_code="CONN_ITSM_RETRYABLE",
                    message=f"ITSM {exc.status_code} during ticket update",
                    http_status=exc.status_code,
                    ticket_key=ticket_id,
                    campaign_id=campaign_id,
                    request_id=request_id,
                )
            else:
                _log_error(
                    operation="update_ticket",
                    error_code=f"CONN_ITSM_{exc.status_code}",
                    message=f"ITSM {exc.status_code} during ticket update",
                    http_status=exc.status_code,
                    ticket_key=ticket_id,
                    campaign_id=campaign_id,
                    disposition="skipped",
                    request_id=request_id,
                )
        except Exception:
            # Re-raise non-ITSMAPIError exceptions for platform handler
            raise

    _write_back_resource_statuses(resources_table, campaign_id, resources)

    logger.info(
        "RESOURCE_UPDATE complete — campaign_id=%s tickets=%d newly_resolved=%d",
        campaign_id[:_MAX_LOG_LEN], len(resources_by_ticket), len(newly_resolved_arns),
    )
    return not had_retryable_failure
