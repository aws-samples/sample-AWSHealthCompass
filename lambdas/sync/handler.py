"""Sync Lambda — polls JIRA for ticket status changes (A-SYNC-1..4).

Hourly Amazon EventBridge schedule. Reads all Resolve-labelled JIRA tickets
updated since last sync, maps statusCategory.key to normalized states,
updates Amazon DynamoDB ResourcesTable, recalculates campaign completion,
and counts orphan tickets.

Triggers:
  - EventBridge schedule rule: rate(1 hour)

SECURITY CONSTRAINTS:
-: All JIRA-sourced fields validated before DynamoDB write.
-: JQL built from hardcoded labels + validated timestamp only.
-: Credentials cached with TTL; invalidated on 401.
-: SYNC_STATE.last_sync_at validated on read.
-: 200ms inter-page delay on JQL pagination.
-: No raw JIRA responses or credentials in logs.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

from resolve_core.campaign import recalculate_completion, update_campaign_status
from resolve_core.config_schema import operative_platform, resolve_platforms
from resolve_core.constants import (
    COMPASS_LABEL,
    ORPHAN_LABEL,
    ORPHAN_STATUS_KEY,
    ORPHAN_COUNT_FIELD,
    ORPHAN_ALERT_THRESHOLD,
)
from resolve_core.jira_client import JiraApiError, JiraClient
from resolve_core.status_mapping import normalize_status

logger = logging.getLogger("compass")
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# --- Environment variables ---

_CONFIG_TABLE = os.environ.get("CONFIG_TABLE", "compass-config")
_CAMPAIGNS_TABLE = os.environ.get("CAMPAIGNS_TABLE", "compass-campaigns")
_RESOURCES_TABLE = os.environ.get("RESOURCES_TABLE", "compass-resources")
_JIRA_SECRET_ARN = os.environ.get("JIRA_SECRET_ARN", "")
_AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# --- Constants (hardcoded, never from config) ---

_PAGE_SIZE = 50
_MAX_TOTAL = 10_000
_INTER_PAGE_DELAY_S = 0.2  # / IMPL-F04

_CACHE_TTL_S = 3600  # 60 minutes
_DEFAULT_LOOKBACK_H = 24
_MAX_LOG_LEN = 256

# Validation patterns
_JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
_ISO_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_JQL_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
_CTRL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# statusCategory allowlist — delegated to resolve_core.status_mapping

# SYNC_STATE.last_sync_status allowlist
_VALID_SYNC_STATUSES = frozenset({"success", "partial", "failed", "skipped"})

# --- Boto3 resources (module-level for connection reuse) ---

_dynamodb = boto3.resource("dynamodb", region_name=_AWS_REGION)
_secrets_client = boto3.client("secretsmanager", region_name=_AWS_REGION)

_config_table = _dynamodb.Table(_CONFIG_TABLE)
_campaigns_table = _dynamodb.Table(_CAMPAIGNS_TABLE)
_resources_table = _dynamodb.Table(_RESOURCES_TABLE)

# --- Module-level cache ---

_jira_client: Optional[JiraClient] = None
_client_created_at: float = 0.0


# ===================================================================
# Validation helpers
# ===================================================================


def _sanitize_log(val: Any) -> str:
    """Truncate and strip control chars for safe log output."""
    text = str(val) if val is not None else ""
    text = _CTRL_CHAR_RE.sub("", text)
    return text[:_MAX_LOG_LEN]


def _validate_issue_key(key: Any) -> Optional[str]:
    """Validate JIRA issue key format. Returns key or None."""
    if isinstance(key, str) and _JIRA_KEY_RE.match(key):
        return key
    return None


def _sanitize_raw_status(name: Any) -> str:
    """Strip control chars and truncate status name to 255."""
    if not isinstance(name, str):
        return ""
    return _CTRL_CHAR_RE.sub("", name)[:255]


def _normalize_status_category(key: Any) -> str:
    """Map statusCategory.key to normalized state via shared module."""
    if isinstance(key, str):
        try:
            return normalize_status("jira", key)
        except ValueError:
            pass
    logger.warning(
        "Unknown statusCategory mapped to Created — value=%s",
        _sanitize_log(key),
    )
    return "Created"


def _validate_jira_timestamp(ts: Any, fallback: str) -> str:
    """Parse JIRA timestamp to ISO 8601. Returns fallback on failure."""
    if not isinstance(ts, str) or not ts:
        return fallback
    # JIRA returns "2026-06-15T14:30:00.000+0000" — normalize
    try:
        # Strip milliseconds and timezone offset for parsing
        clean = ts.replace("+0000", "+00:00").replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return fallback


def _validate_last_sync_at(val: Any) -> Optional[str]:
    """Validate last_sync_at is ISO 8601 UTC and not in the future."""
    if not isinstance(val, str) or not _ISO_TS_RE.match(val):
        return None
    try:
        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        if dt > datetime.now(timezone.utc):
            logger.warning("last_sync_at is in the future — falling back to default")
            return None
        return val
    except (ValueError, TypeError):
        return None


def _format_jql_date(iso_ts: str) -> str:
    """Convert ISO 8601 to JQL date format 'YYYY-MM-DD HH:mm'."""
    dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    jql_date = dt.strftime("%Y-%m-%d %H:%M")
    if not _JQL_DATE_RE.match(jql_date):
        raise ValueError(f"Invalid JQL date: {jql_date}")
    return jql_date


def _now_iso() -> str:
    """Current UTC time as ISO 8601."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_lookback() -> str:
    """ISO 8601 timestamp 24h ago."""
    dt = datetime.now(timezone.utc) - timedelta(hours=_DEFAULT_LOOKBACK_H)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ===================================================================
# JQL builders (no dynamic label values)
# ===================================================================


def _build_sync_jql(jql_date: str) -> str:
    """Build JQL for sync query with pre-validated date."""
    if not _JQL_DATE_RE.match(jql_date):
        raise ValueError(f"Invalid JQL date format: {jql_date}")
    return (
        f'labels = "{COMPASS_LABEL}" '
        f'AND updated >= "{jql_date}" '
        f"ORDER BY updated ASC"
    )


def _build_orphan_jql() -> str:
    """Build JQL for orphan count. No dynamic components."""
    return (
        f'labels = "{COMPASS_LABEL}" '
        f'AND labels = "{ORPHAN_LABEL}"'
    )


# ===================================================================
# Credential management (IMPL-F03)
# ===================================================================


def _invalidate_client() -> None:
    """Clear cached client on 401 to force re-read from Secrets Manager."""
    global _jira_client, _client_created_at
    _jira_client = None
    _client_created_at = 0.0


def _get_jira_client() -> JiraClient:
    """Load JIRA credentials and build client with TTL cache.

    IMPL-F03: Validates secret ARN from ConfigTable against env var.: Cache invalidated on 401; TTL 60 minutes.
    """
    global _jira_client, _client_created_at

    if _jira_client and (time.time() - _client_created_at) < _CACHE_TTL_S:
        return _jira_client

    # Read JIRA_CONNECTION from ConfigTable
    resp = _config_table.get_item(Key={"pk": "JIRA_CONNECTION"})
    conn = resp.get("Item")
    if not conn or not conn.get("validated"):
        raise RuntimeError("JIRA_CONNECTION not configured or not validated")

    base_url = conn.get("jira_base_url", "")
    secret_arn = conn.get("jira_secret_arn", "")

    # IMPL-F03: Validate secret ARN against trusted env var
    if _JIRA_SECRET_ARN and secret_arn != _JIRA_SECRET_ARN:
        logger.warning(
            "Secret ARN mismatch — using env var. "
            "config_arn_prefix=%s env_arn_prefix=%s",
            _sanitize_log(secret_arn[:40]),
            _sanitize_log(_JIRA_SECRET_ARN[:40]),
        )
        secret_arn = _JIRA_SECRET_ARN

    if not secret_arn:
        raise RuntimeError("JIRA secret ARN not available")

    # Read credentials from Secrets Manager
    sm_resp = _secrets_client.get_secret_value(SecretId=secret_arn)
    secret = json.loads(sm_resp["SecretString"])
    email = secret.get("email", "")
    api_token = secret.get("api_token", "")

    if not email or not api_token:
        raise RuntimeError("JIRA credentials incomplete in secret")

    client = JiraClient(base_url, email, api_token)
    _jira_client = client
    _client_created_at = time.time()
    return client


# ===================================================================
# Phase 2: Build ticket index from ResourcesTable
# ===================================================================


def _build_ticket_index() -> Dict[str, Dict[str, str]]:
    """Scan ResourcesTable for items with ticketId → in-memory index.

    Reads from `tickets` map (dual-platform) with flat-field fallback
    for backward compatibility with legacy items.

    Returns dict: ticketId → {campaignId, trackingKey, platform, ticketStatus, ticketRawStatus}.
    """
    index: Dict[str, Dict[str, str]] = {}
    scan_kwargs: Dict[str, Any] = {
        "FilterExpression": "attribute_exists(ticketId) AND ticketId <> :null",
        "ExpressionAttributeValues": {":null": None},
        "ProjectionExpression": (
            "campaignId, trackingKey, ticketId, ticketStatus, "
            "ticketRawStatus, ticketPlatform, tickets"
        ),
    }

    while True:
        resp = _resources_table.scan(**scan_kwargs)
        for item in resp.get("Items", []):
            campaign_id = item.get("campaignId", "")
            tracking_key = item.get("trackingKey", "")

            # Primary path: iterate tickets map for multi-platform entries
            tickets_map = item.get("tickets")
            if isinstance(tickets_map, dict) and tickets_map:
                for platform, ticket_data in tickets_map.items():
                    if not isinstance(ticket_data, dict):
                        continue
                    ticket_id = ticket_data.get("ticketId")
                    if ticket_id and isinstance(ticket_id, str):
                        index[ticket_id] = {
                            "campaignId": campaign_id,
                            "trackingKey": tracking_key,
                            "platform": platform,
                            "ticketStatus": ticket_data.get("ticketStatus"),
                            "ticketRawStatus": ticket_data.get("ticketRawStatus"),
                        }
            else:
                # Fallback: read flat fields for items without tickets map
                ticket_id = item.get("ticketId")
                if ticket_id and isinstance(ticket_id, str):
                    index[ticket_id] = {
                        "campaignId": campaign_id,
                        "trackingKey": tracking_key,
                        "platform": item.get("ticketPlatform", "jira"),
                        "ticketStatus": item.get("ticketStatus"),
                        "ticketRawStatus": item.get("ticketRawStatus"),
                    }

        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    return index


# ===================================================================
# Phase 3: JIRA poll with pagination
# ===================================================================


def _poll_jira(client: JiraClient, jql: str) -> List[dict]:
    """Execute paginated JQL search via /rest/api/3/search/jql.

    the old /rest/api/3/search endpoint (offset-based
    startAt/total pagination) was removed by Atlassian (HTTP 410).
    This now uses the replacement endpoint's cursor-based pagination
    (nextPageToken/isLast). There is no "total" field in the new
    response, so the _MAX_TOTAL safety cap is enforced directly
    against the accumulated result count instead of a server-reported
    total. / IMPL-F04: 200ms delay between pages.
    Safety cap at _MAX_TOTAL accumulated results.
    """
    all_issues: List[dict] = []
    next_page_token: Optional[str] = None

    while True:
        resp = client.search_issues(
            jql=jql,
            fields=["status", "labels", "updated"],
            max_results=_PAGE_SIZE,
            next_page_token=next_page_token,
        )

        issues = resp.get("issues") if isinstance(resp.get("issues"), list) else []
        all_issues.extend(issues)

        # cap enforced against accumulated count — no
        # server-reported "total" exists under the new API.
        if len(all_issues) >= _MAX_TOTAL:
            logger.warning("JQL results capped at %d", _MAX_TOTAL)
            break

        next_page_token = resp.get("nextPageToken")
        is_last = resp.get("isLast", not next_page_token)

        if is_last or not next_page_token or not issues:
            break

        time.sleep(_INTER_PAGE_DELAY_S)

    return all_issues


# ===================================================================
# Phase 4: Status update
# ===================================================================


def _process_ticket(
    issue: dict,
    ticket_index: Dict[str, Dict[str, str]],
    now: str,
) -> Optional[str]:
    """Process a single JIRA issue. Returns affected campaignId or None.: Validates all JIRA-sourced fields before DynamoDB write.
    """
    # Validate issue key
    key = _validate_issue_key(issue.get("key"))
    if not key:
        logger.warning(
            "Invalid JIRA issue key skipped — raw=%s",
            _sanitize_log(issue.get("key")),
        )
        return None

    # Look up in index
    entry = ticket_index.get(key)
    if not entry:
        # Ticket not tracked in ResourcesTable — skip silently
        return None

    # Extract and validate status
    fields = issue.get("fields") or {}
    status_field = fields.get("status") or {}
    category_key = (status_field.get("statusCategory") or {}).get("key")
    normalized = _normalize_status_category(category_key)
    raw_status = _sanitize_raw_status(
        (status_field.get("name") if isinstance(status_field.get("name"), str) else "")
    )
    updated_at = _validate_jira_timestamp(fields.get("updated"), now)

    # Skip write if status unchanged
    if entry.get("ticketStatus") == normalized and entry.get("ticketRawStatus") == raw_status:
        return None

    # Update ResourcesTable — write to both nested tickets map and flat fields
    campaign_id = entry["campaignId"]
    tracking_key = entry["trackingKey"]
    platform = entry.get("platform", "jira")

    try:
        _resources_table.update_item(
            Key={"campaignId": campaign_id, "trackingKey": tracking_key},
            UpdateExpression=(
                "SET tickets.#platform.#ts = :ts, "
                "tickets.#platform.#trs = :trs, "
                "tickets.#platform.#tua = :tua, "
                "#ts = :ts, #trs = :trs, "
                "#tua = :tua, #ua = :ua"
            ),
            ExpressionAttributeNames={
                "#platform": platform,  # value from index (jira/servicenow)
                "#ts": "ticketStatus",
                "#trs": "ticketRawStatus",
                "#tua": "ticketUpdatedAt",
                "#ua": "updatedAt",
            },
            ExpressionAttributeValues={
                ":ts": normalized,
                ":trs": raw_status,
                ":tua": updated_at,
                ":ua": now,
            },
            ConditionExpression="attribute_exists(campaignId)",
        )
        return campaign_id
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code == "ValidationException":
            # Fallback: tickets map doesn't exist (legacy item) —
            # write flat fields only; nested map will be created on next ticket write
            try:
                _resources_table.update_item(
                    Key={"campaignId": campaign_id, "trackingKey": tracking_key},
                    UpdateExpression=(
                        "SET #ts = :ts, #trs = :trs, #tua = :tua, #ua = :ua"
                    ),
                    ExpressionAttributeNames={
                        "#ts": "ticketStatus",
                        "#trs": "ticketRawStatus",
                        "#tua": "ticketUpdatedAt",
                        "#ua": "updatedAt",
                    },
                    ExpressionAttributeValues={
                        ":ts": normalized,
                        ":trs": raw_status,
                        ":tua": updated_at,
                        ":ua": now,
                    },
                    ConditionExpression="attribute_exists(campaignId)",
                )
                return campaign_id
            except ClientError:
                logger.error(
                    "Resource ticket status update failed (fallback) — "
                    "error_code=SYNC_RESOURCE_UPDATE_FAILED "
                    "ticket=%s campaign=%s",
                    key, _sanitize_log(campaign_id),
                )
                return None
        elif error_code == "ConditionalCheckFailedException":
            logger.warning(
                "Resource not found for ticket update — ticket=%s campaign=%s",
                key, _sanitize_log(campaign_id),
            )
        else:
            logger.error(
                "Resource ticket status update failed — "
                "error_code=SYNC_RESOURCE_UPDATE_FAILED "
                "ticket=%s campaign=%s exception_type=%s",
                key, _sanitize_log(campaign_id), type(exc).__name__,
            )
        return None


# ===================================================================
# Phase 5: Campaign aggregation
# ===================================================================


def _aggregate_campaign(campaign_id: str, now: str) -> None:
    """Recalculate ticket counts and completion for a campaign.

    Two-phase write: unconditional counts, then conditional COMPLETED.
    """
    # Query all resources for this campaign
    tickets_created = 0
    tickets_closed = 0
    tickets_in_progress = 0

    query_kwargs: Dict[str, Any] = {
        "KeyConditionExpression": "campaignId = :cid",
        "ExpressionAttributeValues": {":cid": campaign_id},
        "ProjectionExpression": "ticketId, ticketStatus",
    }

    while True:
        resp = _resources_table.query(**query_kwargs)
        for item in resp.get("Items", []):
            if item.get("ticketId"):
                tickets_created += 1
                status = item.get("ticketStatus")
                if status == "Closed":
                    tickets_closed += 1
                elif status == "In Progress":
                    tickets_in_progress += 1
        if "LastEvaluatedKey" not in resp:
            break
        query_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    # Calculate completion percentage
    if tickets_created > 0:
        tickets_closed = min(tickets_closed, tickets_created)
        completion_pct = round((tickets_closed / tickets_created) * 100, 1)
    else:
        completion_pct = 0.0

    # Clamp to valid range
    completion_pct = max(0.0, min(100.0, completion_pct))

    # Phase A: Unconditional count update
    try:
        _campaigns_table.update_item(
            Key={"campaignId": campaign_id},
            UpdateExpression=(
                "SET #tc = :tc, #tip = :tip, #tcc = :tcc, "
                "#cp = :cp, #ua = :ua"
            ),
            ExpressionAttributeNames={
                "#tc": "ticketsCreated",
                "#tip": "ticketsInProgress",
                "#tcc": "ticketsClosed",
                "#cp": "completionPct",
                "#ua": "updatedAt",
            },
            ExpressionAttributeValues={
                ":tc": tickets_created,
                ":tip": tickets_in_progress,
                ":tcc": tickets_closed,
                ":cp": completion_pct,
                ":ua": now,
            },
            ConditionExpression="attribute_exists(campaignId)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.warning(
                "Campaign not found for aggregation — campaign=%s",
                _sanitize_log(campaign_id),
            )
            return
        logger.error(
            "Campaign aggregation failed — "
            "error_code=SYNC_CAMPAIGN_UPDATE_FAILED "
            "campaign=%s exception_type=%s",
            _sanitize_log(campaign_id), type(exc).__name__,
        )
        return

    # Phase B: Conditional COMPLETED transition
    if tickets_created > 0 and tickets_closed == tickets_created:
        update_campaign_status(
            _campaigns_table, campaign_id, "COMPLETED", now,
        )


# ===================================================================
# Phase 6: Orphan detection
# ===================================================================


def _count_orphans(client: JiraClient) -> int:
    """Count orphan tickets via /rest/api/3/search/approximate-count.

    the old max_results=0/total trick against
    /rest/api/3/search no longer works (endpoint removed, and its
    replacement /rest/api/3/search/jql has no "total" field at any
    maxResults). This calls the dedicated count endpoint instead,
    which returns {"count": N} directly. Returns count (capped at
    100K).
    """
    jql = _build_orphan_jql()
    count = client.count_issues(jql=jql)
    return min(count, 100_000)


# ===================================================================
# State persistence
# ===================================================================


def _write_sync_state(
    status: str,
    last_sync_at: Optional[str],
    tickets_synced: int,
    transitions: int,
    errors: int,
    now: str,
) -> None:
    """Write SYNC_STATE to ConfigTable."""
    if status not in _VALID_SYNC_STATUSES:
        status = "failed"

    item: Dict[str, Any] = {
        "pk": "SYNC_STATE",
        "last_sync_status": status,
        "tickets_synced": max(tickets_synced, 0),
        "status_transitions": max(transitions, 0),
        "errors": max(errors, 0),
        "updated_at": now,
    }
    # Only update last_sync_at on success or partial
    if last_sync_at and status in ("success", "partial"):
        item["last_sync_at"] = last_sync_at

    _config_table.put_item(Item=item)


def _write_orphan_count(count: int, now: str) -> None:
    """Write orphan count to ConfigTable with alert flag.

    pk/field names come from resolve_core.constants so the
    reader (lambdas/api/orphan_handlers.py) and writer never drift.
    """
    count = max(0, min(count, 100_000))
    _config_table.put_item(Item={
        "pk": ORPHAN_STATUS_KEY,
        ORPHAN_COUNT_FIELD: count,
        "alert": count > ORPHAN_ALERT_THRESHOLD,
        "updated_at": now,
    })


# ===================================================================
# Platform detection (Beta)
# platform resolution moved to the shared seam
# (resolve_core.config_schema.resolve_platforms / operative_platform).
# The former local _get_active_platform reader was retired here.
# ===================================================================


# ===================================================================
# ServiceNow sync (Beta)
# ===================================================================


def _sync_servicenow(now: str, start_time: float) -> dict:
    """Run bidirectional sync against ServiceNow."""
    logger.info("sync_servicenow_started")

    # Check ServiceNow configuration
    try:
        resp = _config_table.get_item(Key={"pk": "SNOW_CONNECTION"})
    except ClientError:
        _write_sync_state("failed", None, 0, 0, 1, now)
        return {"status": "failed", "reason": "config_read_error"}

    conn = resp.get("Item")
    if not conn or not conn.get("validated"):
        logger.info("sync_skipped — ServiceNow not configured")
        _write_sync_state("skipped", None, 0, 0, 0, now)
        return {"status": "skipped", "reason": "servicenow_not_configured"}

    # Read last sync timestamp
    try:
        state_resp = _config_table.get_item(Key={"pk": "SYNC_STATE"})
    except ClientError:
        state_resp = {}

    state_item = state_resp.get("Item", {})
    last_sync_at = _validate_last_sync_at(state_item.get("last_sync_at"))
    if not last_sync_at:
        last_sync_at = _default_lookback()

    # Build ServiceNow client
    try:
        from resolve_core.servicenow_client import ServiceNowClient
        from resolve_core.servicenow_formatter import ServiceNowFormatter

        instance_url = conn.get("instance_url", "")
        secret_arn = conn.get("secret_arn", os.environ.get("SERVICENOW_SECRET_ARN", ""))

        formatter = ServiceNowFormatter()
        snow_client = ServiceNowClient(
            instance_url=instance_url,
            secret_arn=secret_arn,
            formatter=formatter,
        )
    except Exception as exc:
        logger.error(
            "ServiceNow client init failed — exception_type=%s",
            type(exc).__name__,
        )
        _write_sync_state("failed", None, 0, 0, 1, now)
        return {"status": "failed", "reason": "snow_auth_failed"}

    # Build ticket index (same as JIRA sync)
    try:
        ticket_index = _build_ticket_index()
    except ClientError:
        _write_sync_state("failed", None, 0, 0, 1, now)
        return {"status": "failed", "reason": "scan_failed"}

    logger.info("snow_index_built — ticket_count=%d", len(ticket_index))

    # Poll ServiceNow for status changes
    try:
        from resolve_core.itsm_client import ITSMAPIError
        statuses = snow_client.poll_status_changes(last_sync_at)
    except Exception as exc:
        logger.error(
            "ServiceNow poll failed — exception_type=%s", type(exc).__name__,
        )
        _write_sync_state("failed", None, 0, 0, 1, now)
        return {"status": "failed", "reason": "snow_poll_failed"}

    logger.info("snow_poll_complete — records=%d", len(statuses))

    # Process status changes
    affected_campaigns: set = set()
    transitions = 0
    errors = 0

    for ticket_status in statuses:
        ticket_id = ticket_status.ticket_id
        entry = ticket_index.get(ticket_id)
        if not entry:
            continue

        normalized = ticket_status.normalized_status
        raw_status = ticket_status.raw_status

        # Skip if unchanged
        if entry.get("ticketStatus") == normalized and entry.get("ticketRawStatus") == raw_status:
            continue

        campaign_id = entry["campaignId"]
        tracking_key = entry["trackingKey"]

        try:
            _resources_table.update_item(
                Key={"campaignId": campaign_id, "trackingKey": tracking_key},
                UpdateExpression="SET #ts = :ts, #trs = :trs, #tua = :tua, #ua = :ua",
                ExpressionAttributeNames={
                    "#ts": "ticketStatus",
                    "#trs": "ticketRawStatus",
                    "#tua": "ticketUpdatedAt",
                    "#ua": "updatedAt",
                },
                ExpressionAttributeValues={
                    ":ts": normalized,
                    ":trs": raw_status,
                    ":tua": ticket_status.last_updated,
                    ":ua": now,
                },
                ConditionExpression="attribute_exists(campaignId)",
            )
            affected_campaigns.add(campaign_id)
            transitions += 1
        except ClientError:
            errors += 1

    # Campaign aggregation (same as JIRA)
    for campaign_id in affected_campaigns:
        try:
            _aggregate_campaign(campaign_id, now)
        except ClientError:
            errors += 1

    # Persist state
    sync_status = "success" if errors == 0 else "partial"
    sync_now = _now_iso()
    _write_sync_state(sync_status, sync_now, len(statuses), transitions, errors, sync_now)

    elapsed_ms = int((time.monotonic() - start_time) * 1000)
    logger.info(
        "snow_sync_complete — status=%s records_polled=%d transitions=%d "
        "errors=%d campaigns_updated=%d duration_ms=%d",
        sync_status, len(statuses), transitions, errors,
        len(affected_campaigns), elapsed_ms,
    )

    return {
        "status": sync_status,
        "tickets_synced": len(statuses),
        "status_transitions": transitions,
        "campaigns_updated": len(affected_campaigns),
        "errors": errors,
        "duration_ms": elapsed_ms,
    }


# ===================================================================
# Handler
# ===================================================================


def lambda_handler(event: Any, context: Any) -> dict:
    """Sync Lambda entry point. Triggered hourly by EventBridge."""
    start_time = time.monotonic()
    now = _now_iso()

    logger.info("sync_started — region=%s", _AWS_REGION)

    # --- Phase 0: Platform detection ---
    # resolve via the shared seam. operative_platform keeps this
    # behavior-preserving (single-platform branch) until a later change rewires the
    # sync loop to iterate the resolved array.
    active_platform = operative_platform(resolve_platforms(_config_table))

    if active_platform == "servicenow":
        return _sync_servicenow(now, start_time)

    # Default: JIRA sync (original behavior)

    # --- Phase 1: Preflight ---

    # Check JIRA configuration
    try:
        resp = _config_table.get_item(Key={"pk": "JIRA_CONNECTION"})
    except ClientError as exc:
        logger.error(
            "ConfigTable read failed — error_code=SYNC_CONFIG_READ_FAILED "
            "exception_type=%s", type(exc).__name__,
        )
        _write_sync_state("failed", None, 0, 0, 1, now)
        return {"status": "failed", "reason": "config_read_error"}

    conn = resp.get("Item")
    if not conn or not conn.get("validated"):
        logger.info("sync_skipped — JIRA not configured")
        _write_sync_state("skipped", None, 0, 0, 0, now)
        return {"status": "skipped", "reason": "jira_not_configured"}

    # Read last sync timestamp (validate on read)
    try:
        state_resp = _config_table.get_item(Key={"pk": "SYNC_STATE"})
    except ClientError:
        state_resp = {}

    state_item = state_resp.get("Item", {})
    last_sync_at = _validate_last_sync_at(state_item.get("last_sync_at"))
    if not last_sync_at:
        last_sync_at = _default_lookback()
        logger.info(
            "No valid last_sync_at — using default lookback=%dh",
            _DEFAULT_LOOKBACK_H,
        )

    # Load JIRA client (cached with TTL)
    try:
        client = _get_jira_client()
    except (RuntimeError, ClientError, Exception) as exc:
        logger.error(
            "JIRA client initialization failed — "
            "error_code=SYNC_AUTH_FAILED exception_type=%s",
            type(exc).__name__,
        )
        _write_sync_state("failed", None, 0, 0, 1, now)
        return {"status": "failed", "reason": "auth_failed"}

    # --- Phase 2: Build ticket index ---

    try:
        ticket_index = _build_ticket_index()
    except ClientError as exc:
        logger.error(
            "ResourcesTable scan failed — "
            "error_code=SYNC_SCAN_FAILED exception_type=%s",
            type(exc).__name__,
        )
        _write_sync_state("failed", None, 0, 0, 1, now)
        return {"status": "failed", "reason": "scan_failed"}

    logger.info("index_built — ticket_count=%d", len(ticket_index))

    # --- Phase 3: JIRA poll ---

    try:
        jql_date = _format_jql_date(last_sync_at)
        jql = _build_sync_jql(jql_date)
        issues = _poll_jira(client, jql)
    except JiraApiError as exc:
        # Invalidate client on 401
        if exc.status == 401:
            _invalidate_client()
        logger.error(
            "JIRA poll failed — error_code=SYNC_JIRA_POLL_FAILED "
            "http_status=%d", exc.status,
        )
        _write_sync_state("failed", None, 0, 0, 1, now)
        return {"status": "failed", "reason": "jira_poll_failed"}
    except ValueError as exc:
        logger.error(
            "JQL date conversion failed — error_code=SYNC_DATE_ERROR "
            "detail=%s", _sanitize_log(str(exc)),
        )
        _write_sync_state("failed", None, 0, 0, 1, now)
        return {"status": "failed", "reason": "date_error"}

    logger.info("jira_poll_complete — issues=%d", len(issues))

    # --- Phase 4: Status updates ---

    affected_campaigns: set = set()
    transitions = 0
    errors = 0

    for issue in issues:
        if not isinstance(issue, dict):
            continue
        try:
            campaign_id = _process_ticket(issue, ticket_index, now)
            if campaign_id:
                affected_campaigns.add(campaign_id)
                transitions += 1
        except ClientError:
            errors += 1

    logger.info(
        "status_update_complete — transitions=%d errors=%d",
        transitions, errors,
    )

    # --- Phase 5: Campaign aggregation ---

    for campaign_id in affected_campaigns:
        try:
            _aggregate_campaign(campaign_id, now)
        except ClientError as exc:
            logger.error(
                "Campaign aggregation error — "
                "error_code=SYNC_AGGREGATION_FAILED "
                "campaign=%s exception_type=%s",
                _sanitize_log(campaign_id), type(exc).__name__,
            )
            errors += 1

    logger.info(
        "aggregation_complete — campaigns_updated=%d",
        len(affected_campaigns),
    )

    # --- Phase 6: Orphan detection ---

    orphan_count = 0
    try:
        orphan_count = _count_orphans(client)
        _write_orphan_count(orphan_count, now)
        logger.info(
            "orphan_count_updated — count=%d alert=%s",
            orphan_count, orphan_count > ORPHAN_ALERT_THRESHOLD,
        )
    except (JiraApiError, ClientError) as exc:
        logger.warning(
            "Orphan count failed — error_code=SYNC_ORPHAN_FAILED "
            "exception_type=%s", type(exc).__name__,
        )
        # Non-fatal — continue to persist state

    # --- Phase 7: Persist state ---

    sync_status = "success" if errors == 0 else "partial"
    sync_now = _now_iso()
    _write_sync_state(
        status=sync_status,
        last_sync_at=sync_now,
        tickets_synced=len(issues),
        transitions=transitions,
        errors=errors,
        now=sync_now,
    )

    elapsed_ms = int((time.monotonic() - start_time) * 1000)

    logger.info(
        "sync_complete — status=%s tickets_synced=%d transitions=%d "
        "errors=%d campaigns_updated=%d orphan_count=%d "
        "duration_ms=%d",
        sync_status, len(issues), transitions, errors,
        len(affected_campaigns), orphan_count, elapsed_ms,
    )

    return {
        "status": sync_status,
        "tickets_synced": len(issues),
        "status_transitions": transitions,
        "campaigns_updated": len(affected_campaigns),
        "orphan_count": orphan_count,
        "orphan_alert": orphan_count > ORPHAN_ALERT_THRESHOLD,
        "errors": errors,
        "duration_ms": elapsed_ms,
    }
