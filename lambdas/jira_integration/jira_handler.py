"""JIRA-specific handler — thin wiring layer between Lambda entrypoint and orchestrator.

STORY-054: Instantiates JiraClient + JiraFormatter, delegates to itsm_orchestrator.
This module contains JIRA-specific error handling and credential management.

Future ServiceNow Lambda would have `servicenow_handler.py` with the same pattern.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

from resolve_core.jira_client import BulkCreateResult, JiraApiError, JiraNotFoundError
from resolve_core.jira_client import JiraClient as JiraClient  # noqa: keep as module-level for patching
from resolve_core.ticket_builder import build_template_a, build_template_b

from itsm_orchestrator import (
    _SUCCESS,
    _MAX_LOG_LEN,
    _ACCOUNT_ID_RE,
    _log_error,
    _check_idempotency,
    _filter_already_ticketed,
    _write_bulk_results,
    _write_ticket_to_resources,
    _increment_tickets_created,
    create_template_a_ticket,
    create_template_b_ticket,
    update_tickets,
)

logger = logging.getLogger("compass")
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# --- Environment variables ---

_CAMPAIGNS_TABLE = os.environ.get("CAMPAIGNS_TABLE", "compass-campaigns")
_RESOURCES_TABLE = os.environ.get("RESOURCES_TABLE", "compass-resources")
_CONFIG_TABLE = os.environ.get("CONFIG_TABLE", "compass-config")
_PAYLOAD_BUCKET = os.environ.get("PAYLOAD_BUCKET", "")
_AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# --- Constants ---

_S3_KEY_RE = re.compile(r"^[a-zA-Z0-9\-_./]+$")

# --- Boto3 resources (module-level for connection reuse) ---

_dynamodb = boto3.resource("dynamodb", region_name=_AWS_REGION)
_s3_client = boto3.client("s3", region_name=_AWS_REGION)
_secrets_client = boto3.client("secretsmanager", region_name=_AWS_REGION)

_campaigns_table = _dynamodb.Table(_CAMPAIGNS_TABLE)
_resources_table = _dynamodb.Table(_RESOURCES_TABLE)
_config_table = _dynamodb.Table(_CONFIG_TABLE)

# --- Module-level caches ---

_jira_client: Optional[JiraClient] = None
_config: Optional[dict] = None
_enabled_platforms: Optional[list] = None


def _get_enabled_platforms() -> list:
    """Load and cache enabled platforms from ConfigTable (STORY-093)."""
    global _enabled_platforms
    if _enabled_platforms is not None:
        return _enabled_platforms
    try:
        resp = _config_table.get_item(Key={"pk": "INTEGRATIONS_ENABLED"})
        item = resp.get("Item")
        if item and isinstance(item.get("platforms"), list):
            _enabled_platforms = item["platforms"]
        else:
            _enabled_platforms = ["jira"]  # backward compat default — fail-open
    except ClientError:
        _enabled_platforms = ["jira"]  # fail-open for JIRA
    return _enabled_platforms


# ===================================================================
# Config + Secrets
# ===================================================================


def _get_config() -> dict:
    """Load and cache JIRA connection config + tag display keys."""
    global _config
    if _config is not None:
        return _config

    resp = _config_table.get_item(Key={"pk": "JIRA_CONNECTION"})
    conn = resp.get("Item")
    if not conn:
        raise RuntimeError("JIRA_CONNECTION not configured")

    try:
        keys_resp = _config_table.get_item(Key={"pk": "TAG_DISPLAY_KEYS"})
        keys_item = keys_resp.get("Item", {})
    except ClientError:
        keys_item = {}

    loaded = {
        "jira_base_url": conn.get("jira_base_url", ""),
        "jira_secret_arn": conn.get("jira_secret_arn", ""),
        "validated": conn.get("validated", False),
        "tag_display_keys": keys_item.get("keys", ["Owner", "Team", "Environment"]),
    }
    _config = loaded
    return loaded


def _get_jira_client() -> JiraClient:
    """Build and cache JiraClient from Secrets Manager."""
    global _jira_client
    if _jira_client is not None:
        return _jira_client

    config = _get_config()
    secret_arn = config["jira_secret_arn"]
    if not secret_arn:
        raise RuntimeError("JIRA secret ARN not configured")

    resp = _secrets_client.get_secret_value(SecretId=secret_arn)
    secret = json.loads(resp["SecretString"])
    email = secret.get("email", "")
    api_token = secret.get("api_token", "")

    if not email or not api_token:
        raise RuntimeError("JIRA credentials incomplete in secret")

    client = JiraClient(config["jira_base_url"], email, api_token)
    _jira_client = client
    return client


# ===================================================================
# JIRA-Specific: Ticket Not Found Handler
# ===================================================================


def _handle_ticket_not_found(
    ticket_id: str, campaign_id: str, resources: List[dict], request_id: str,
) -> None:
    """Handle JIRA 404 — mark resources as unknown (E-9)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _log_error(
        operation="update_ticket",
        error_code="CONN_JIRA_NOT_FOUND",
        message=f"JIRA ticket '{ticket_id}' not found — may have been deleted",
        http_status=404,
        ticket_key=ticket_id,
        campaign_id=campaign_id,
        request_id=request_id,
    )
    for resource in resources:
        tk = resource.get("trackingKey", "")
        if not tk:
            continue
        try:
            _resources_table.update_item(
                Key={"campaignId": campaign_id, "trackingKey": tk},
                UpdateExpression=(
                    "SET ticketStatus = :ts, unknownReason = :ur, "
                    "unknownAt = :ua, updatedAt = :now"
                ),
                ExpressionAttributeValues={
                    ":ts": "unknown", ":ur": "jira_404",
                    ":ua": now, ":now": now,
                },
            )
        except ClientError:
            logger.warning(
                "Failed to mark resource unknown — campaign_id=%s tracking_key=%s",
                campaign_id[:_MAX_LOG_LEN], tk[:_MAX_LOG_LEN],
            )


# ===================================================================
# JIRA-Specific: Bulk Create Orchestrator
# ===================================================================


def _create_tickets_bulk(
    payload: dict, jira: JiraClient, config: dict, request_id: str, context: Any,
) -> "BulkCreateResult":
    """Create multiple JIRA tickets via bulk API."""
    event = payload.get("event", {})
    resources = payload.get("resources", [])
    routing = payload.get("routing", {})
    account_tags = payload.get("accountTags", {})
    campaign_id = event.get("campaignId", "")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    tracking_keys: List[str] = []
    for r in resources:
        tk = r.get("trackingKey", "")
        if not tk:
            tk = r.get("arn") or r.get("entityValue", "")
        tracking_keys.append(tk)

    already_ticketed = _filter_already_ticketed(_resources_table, campaign_id, tracking_keys)
    if already_ticketed:
        logger.info(
            "Bulk idempotency — skipping %d already-ticketed resources for campaign_id=%s",
            len(already_ticketed), campaign_id[:_MAX_LOG_LEN],
        )

    issues: List[Dict[str, Any]] = []
    issue_indices: List[int] = []
    # STORY-093: Read from platforms.jira with backward compat fallback
    jira_target = routing.get("platforms", {}).get("jira", {})
    project = jira_target.get("project") or routing.get("resolvedProject", "")
    issue_type = jira_target.get("issueType") or routing.get("issueType", "Task")

    for idx, resource in enumerate(resources):
        tk = tracking_keys[idx]
        if tk in already_ticketed:
            continue
        ticket = build_template_a(
            event=event, resources=[resource], routing=routing,
            account_tags=account_tags, tag_display_keys=config.get("tag_display_keys"),
        )
        issues.append({
            "fields": {
                "project": {"key": project},
                "summary": ticket["summary"],
                "issuetype": {"name": issue_type},
                "description": ticket["description_adf"],
                "labels": ticket["labels"],
                **({"duedate": ticket["due_date"]} if ticket.get("due_date") else {}),
            },
        })
        issue_indices.append(idx)

    if not issues:
        logger.info("All resources already ticketed — campaign_id=%s", campaign_id[:_MAX_LOG_LEN])
        return BulkCreateResult(total_requested=0, total_batches=0)

    remaining_fn = None
    if context and hasattr(context, "get_remaining_time_in_millis"):
        remaining_fn = context.get_remaining_time_in_millis

    result = jira.bulk_create_issues(issues, remaining_time_fn=remaining_fn)

    for s in result.successes:
        if 0 <= s["index"] < len(issue_indices):
            s["index"] = issue_indices[s["index"]]
    for f in result.failures:
        if 0 <= f["index"] < len(issue_indices):
            f["index"] = issue_indices[f["index"]]

    for f in result.failures:
        idx = f["index"]
        tk = tracking_keys[idx] if 0 <= idx < len(tracking_keys) else "UNKNOWN"
        _log_error(
            operation="bulk_create_ticket",
            error_code="PROC_BULK_PARTIAL_FAILURE",
            message="Bulk create item failed",
            http_status=f.get("httpStatus"),
            platform_errors=f.get("fieldErrors", {}),
            routing_target=project,
            event_arn=event.get("eventArn", ""),
            campaign_id=campaign_id,
            tracking_key=tk,
            request_id=request_id,
        )

    retried_successes = _retry_failed_individually(
        result.failures, issues, issue_indices, tracking_keys,
        jira, config, campaign_id, event, request_id,
    )
    result.successes.extend(retried_successes)

    db_count = _write_bulk_results(_resources_table, campaign_id, result, tracking_keys, now)
    _increment_tickets_created(_campaigns_table, campaign_id, db_count, now)

    logger.info(
        "Bulk create complete — campaign_id=%s total=%d created=%d failed=%d "
        "retries=%d elapsed=%.1fs exhausted=%s",
        campaign_id[:_MAX_LOG_LEN], result.total_requested,
        len(result.successes), len(result.failures),
        result.total_retries, result.elapsed_seconds, result.exhausted,
    )
    return result


def _retry_failed_individually(
    failures: List[Dict[str, Any]], issues: List[Dict[str, Any]],
    issue_indices: List[int], tracking_keys: List[str],
    jira: JiraClient, config: dict, campaign_id: str, event: dict, request_id: str,
) -> List[Dict[str, Any]]:
    """Retry individually failed items from bulk create."""
    retried: List[Dict[str, Any]] = []
    still_failed: List[Dict[str, Any]] = []

    for f in failures:
        resource_idx = f["index"]
        try:
            issue_pos = issue_indices.index(resource_idx)
        except ValueError:
            still_failed.append(f)
            continue
        if issue_pos >= len(issues):
            still_failed.append(f)
            continue

        issue_fields = issues[issue_pos].get("fields", {})
        try:
            resp = jira.create_issue(
                project_key=issue_fields.get("project", {}).get("key", ""),
                summary=issue_fields.get("summary", ""),
                description_adf=issue_fields.get("description", {}),
                labels=issue_fields.get("labels", []),
                due_date=issue_fields.get("duedate"),
                issue_type=issue_fields.get("issuetype", {}).get("name", "Task"),
            )
            key = resp.get("key", "")
            self_url = resp.get("self", "")
            if isinstance(self_url, str) and "/rest/" in self_url:
                browse_base = self_url.split("/rest/")[0]
            else:
                browse_base = config.get("jira_base_url", "")
            retried.append({
                "index": resource_idx,
                "ticketKey": key,
                "ticketId": resp.get("id", ""),
                "ticketUrl": f"{browse_base}/browse/{key}",
            })
        except JiraApiError:
            still_failed.append(f)

    failures.clear()
    failures.extend(still_failed)
    return retried


# ===================================================================
# JIRA-Specific: Dashboard Invoke
# ===================================================================


def _resolve_routing_for_account(account_id: str, config_cache: dict) -> dict:
    """Resolve JIRA project for an account using cached config items."""
    if account_id:
        item = config_cache.get(f"ROUTING#{account_id}")
        if isinstance(item, dict) and item.get("jira_project"):
            return {"resolvedProject": item["jira_project"], "issueType": item.get("jira_issue_type", "Task")}
    default = config_cache.get("ROUTING_DEFAULT")
    if isinstance(default, dict) and default.get("jira_project"):
        return {"resolvedProject": default["jira_project"], "issueType": default.get("jira_issue_type", "Task")}
    return {"resolvedProject": None, "issueType": "Task"}


def _load_routing_cache(resources: list) -> dict:
    """Load routing config items for given resources."""
    cache: dict = {}
    try:
        resp = _config_table.get_item(Key={"pk": "ROUTING_DEFAULT"})
        if resp.get("Item"):
            cache["ROUTING_DEFAULT"] = resp["Item"]
    except ClientError:
        pass

    account_ids = {r.get("accountId", "") for r in resources if r.get("accountId")}
    for acct in account_ids:
        if not _ACCOUNT_ID_RE.match(acct):
            continue
        try:
            resp = _config_table.get_item(Key={"pk": f"ROUTING#{acct}"})
            if resp.get("Item"):
                cache[f"ROUTING#{acct}"] = resp["Item"]
        except ClientError:
            pass
    return cache


def _handle_dashboard_invoke(event: dict, context: Any) -> dict:
    """Handle direct invoke from the API Lambda for ticket creation."""
    campaign_id = event.get("campaignId", "")
    resources = event.get("resources", [])
    errors: List[str] = []
    tickets_created = 0
    tickets_failed = 0

    if not campaign_id:
        return {"ticketsCreated": 0, "ticketsFailed": 0, "errors": ["Missing campaignId"]}

    try:
        resp = _campaigns_table.get_item(Key={"campaignId": campaign_id})
        campaign = resp.get("Item")
    except ClientError as exc:
        logger.error("Failed to load campaign — campaign_id=%s error=%s", campaign_id[:_MAX_LOG_LEN], str(exc)[:200])
        return {"ticketsCreated": 0, "ticketsFailed": 0,
                "errors": ["Database error loading campaign"]}

    if not campaign:
        return {"ticketsCreated": 0, "ticketsFailed": 0,
                "errors": [f"Campaign '{campaign_id}' not found"]}

    event_data = {
        "campaignId": campaign_id,
        "eventArn": campaign.get("eventArn", ""),
        "service": campaign.get("service", ""),
        "eventTypeCode": campaign.get("eventTypeCode", ""),
        "eventTypeCategory": campaign.get("eventTypeCategory", ""),
        "affectedAccount": campaign.get("affectedAccount", ""),
        "startTime": campaign.get("startTime", ""),
        "latestDescription": campaign.get("latestDescription", ""),
        "actionability": campaign.get("actionability", ""),
        "region": campaign.get("region", ""),
        "campaignType": campaign.get("campaignType", ""),
    }

    try:
        config = _get_config()
        jira = _get_jira_client()
    except Exception as exc:
        logger.error("JIRA configuration error — error=%s", type(exc).__name__)
        return {"ticketsCreated": 0, "ticketsFailed": 0,
                "errors": ["Configuration error — JIRA connection not available"]}

    campaign_type = campaign.get("campaignType", "")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if campaign_type == "account-level" or not resources:
        routing_cache = _load_routing_cache([{"accountId": event_data.get("affectedAccount", "")}])
        routing = _resolve_routing_for_account(event_data.get("affectedAccount", ""), routing_cache)
        if not routing["resolvedProject"]:
            return {"ticketsCreated": 0, "ticketsFailed": 0, "errors": ["No routing configured"]}

        ticket = build_template_b(
            event=event_data, routing=routing,
            account_tags=campaign.get("accountTags", {}),
            tag_display_keys=config.get("tag_display_keys"),
        )
        try:
            result = jira.create_issue(
                project_key=routing["resolvedProject"],
                summary=ticket["summary"],
                description_adf=ticket["description_adf"],
                labels=ticket["labels"],
                due_date=ticket["due_date"],
                issue_type=routing["issueType"],
            )
            ticket_key = result.get("key", "")
            ticket_url = f"{config.get('jira_base_url', '')}/browse/{ticket_key}"
            tracking_key = f"ACCOUNT#{event_data.get('affectedAccount', '')}"
            try:
                _resources_table.update_item(
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
                        ":ticket_data": {
                            "ticketId": ticket_key,
                            "ticketStatus": "Created",
                            "ticketRawStatus": "",
                            "ticketUrl": ticket_url,
                            "ticketUpdatedAt": now,
                        },
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
                        _resources_table.update_item(
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
                                ":ticket_map": {"jira": {
                                    "ticketId": ticket_key,
                                    "ticketStatus": "Created",
                                    "ticketRawStatus": "",
                                    "ticketUrl": ticket_url,
                                    "ticketUpdatedAt": now,
                                }},
                                ":tid": ticket_key, ":turl": ticket_url,
                                ":ts": "Created", ":tua": now,
                                ":tp": "jira",
                            },
                        )
                    except ClientError:
                        pass
                # else: silently pass (original behavior)
            _increment_tickets_created(_campaigns_table, campaign_id, 1, now)
            tickets_created = 1
        except JiraApiError as exc:
            tickets_failed = 1
            logger.error("Dashboard ticket creation failed — campaign_id=%s status=%d error=%s",
                         campaign_id[:_MAX_LOG_LEN], exc.status, str(exc)[:200])
            errors.append(f"Ticket creation failed (HTTP {exc.status})")

        return {"ticketsCreated": tickets_created, "ticketsFailed": tickets_failed, "errors": errors}

    routing_cache = _load_routing_cache(resources)
    unticketed = [r for r in resources if not r.get("ticketId") or r.get("ticketId") == "none"]
    if not unticketed:
        return {"ticketsCreated": 0, "ticketsFailed": 0, "errors": ["All resources already have tickets"]}

    groups: Dict[str, List[dict]] = {}
    for r in unticketed:
        account_id = r.get("accountId", "")
        routing = _resolve_routing_for_account(account_id, routing_cache)
        project = routing.get("resolvedProject")
        if not project:
            tickets_failed += 1
            errors.append(f"No routing for account {account_id}")
            continue
        groups.setdefault(project, []).append(r)

    for project, group_resources in groups.items():
        routing = {"resolvedProject": project, "issueType": "Task"}
        ticket = build_template_a(
            event=event_data, resources=group_resources, routing=routing,
            account_tags=campaign.get("accountTags", {}),
            tag_display_keys=config.get("tag_display_keys"),
        )
        try:
            result = jira.create_issue(
                project_key=project, summary=ticket["summary"],
                description_adf=ticket["description_adf"],
                labels=ticket["labels"], due_date=ticket["due_date"],
                issue_type=routing["issueType"],
            )
            ticket_key = result.get("key", "")
            ticket_url = f"{config.get('jira_base_url', '')}/browse/{ticket_key}"
            _write_ticket_to_resources(_resources_table, campaign_id, group_resources, ticket_key, ticket_url, now)
            _increment_tickets_created(_campaigns_table, campaign_id, 1, now)
            tickets_created += 1
        except JiraApiError as exc:
            tickets_failed += 1
            logger.error("Dashboard ticket creation failed — campaign_id=%s project=%s status=%d error=%s",
                         campaign_id[:_MAX_LOG_LEN], project, exc.status, str(exc)[:200])
            errors.append(f"Ticket creation failed for project {project} (HTTP {exc.status})")

    return {"ticketsCreated": tickets_created, "ticketsFailed": tickets_failed, "errors": errors}


# ===================================================================
# JIRA-Specific: Update Tickets (wraps orchestrator with JIRA errors)
# ===================================================================


def _update_tickets_jira(
    payload: dict, jira: JiraClient, config: dict, request_id: str,
) -> bool:
    """JIRA-specific wrapper around orchestrator's update_tickets.

    Translates JiraApiError/JiraNotFoundError into ITSMAPIError for
    the orchestrator, but also handles JIRA-specific error semantics.
    """
    from resolve_core.itsm_client import ITSMAPIError

    def _jira_update_issue(ticket_id, fields):
        try:
            jira.update_issue(ticket_id, fields)
        except JiraNotFoundError:
            raise ITSMAPIError(404, f"Ticket {ticket_id} not found", retryable=False)
        except JiraApiError as exc:
            raise ITSMAPIError(exc.status, str(exc), retryable=exc.retryable)

    def _jira_add_comment(ticket_id, comment_body):
        try:
            jira.add_comment(ticket_id, comment_body)
        except JiraNotFoundError:
            raise ITSMAPIError(404, f"Ticket {ticket_id} not found", retryable=False)
        except JiraApiError as exc:
            raise ITSMAPIError(exc.status, str(exc), retryable=exc.retryable)

    def _on_not_found(ticket_id, campaign_id, ticket_resources, req_id):
        _handle_ticket_not_found(ticket_id, campaign_id, ticket_resources, req_id)

    def _on_auth_failure():
        global _jira_client
        _jira_client = None

    return update_tickets(
        payload=payload,
        update_issue_fn=_jira_update_issue,
        add_comment_fn=_jira_add_comment,
        config=config,
        resources_table=_resources_table,
        campaigns_table=_campaigns_table,
        request_id=request_id,
        on_not_found=_on_not_found,
        on_auth_failure=_on_auth_failure,
    )


# ===================================================================
# Main Handler Entry Point
# ===================================================================


def handle(event: dict, context: Any) -> dict:
    """JIRA-specific Lambda handler logic.

    Called by handler.lambda_handler. Handles SQS/SNS parsing,
    gate checks, and dispatches to template-specific creation.
    """
    # Dashboard direct invoke
    if event.get("source") == "dashboard":
        return _handle_dashboard_invoke(event, context)

    # SQS ESM path
    records = event.get("Records", [])
    if not records:
        return _SUCCESS

    # STORY-093: Global kill switch
    if "jira" not in _get_enabled_platforms():
        logger.debug("JIRA platform globally disabled — skipping all events")
        return _SUCCESS

    record = records[0]
    message_id = record.get("messageId", "")
    request_id = getattr(context, "aws_request_id", "") if context else ""
    event_arn = "UNKNOWN"

    try:
        # Parse SQS → SNS → payload
        raw_body = record.get("body", "")
        try:
            sns_envelope = json.loads(raw_body)
        except (json.JSONDecodeError, TypeError):
            logger.error("JSON parse failed — message_id=%s", message_id)
            return _SUCCESS

        message_str = sns_envelope.get("Message", raw_body)
        try:
            payload = json.loads(message_str)
        except (json.JSONDecodeError, TypeError):
            logger.error("SNS message parse failed — message_id=%s", message_id)
            return _SUCCESS

        # S3 offload
        if payload.get("_s3Ref") or (payload.get("s3_bucket") and payload.get("s3_key")):
            from resolve_core.payload import resolve_payload
            try:
                payload = resolve_payload(
                    s3_client=_s3_client,
                    message_body=payload,
                    expected_bucket=_PAYLOAD_BUCKET,
                )
            except ValueError as exc:
                _log_error(
                    operation="fetch_s3_payload",
                    error_code="PROC_INVALID_S3_REF",
                    message=str(exc)[:200],
                    event_arn=event_arn,
                    campaign_id=payload.get("campaignId", ""),
                    disposition="message_deleted",
                    request_id=request_id,
                )
                return _SUCCESS
            except ClientError as exc:
                error_code = exc.response.get("Error", {}).get("Code", "")
                if error_code in ("NoSuchKey", "AccessDenied", "NoSuchBucket"):
                    _log_error(
                        operation="fetch_s3_payload",
                        error_code=f"S3_OFFLOAD_{error_code.upper()}",
                        message=str(exc)[:200],
                        event_arn=event_arn,
                        campaign_id=payload.get("campaignId", ""),
                        disposition="message_deleted",
                        request_id=request_id,
                    )
                    return _SUCCESS
                _log_error(
                    operation="fetch_s3_payload",
                    error_code="S3_OFFLOAD_FETCH_FAILED",
                    message=str(exc)[:200],
                    event_arn=event_arn,
                    campaign_id=payload.get("campaignId", ""),
                    request_id=request_id,
                )
                return {"batchItemFailures": [{"itemIdentifier": message_id}]}

        # Extract event data
        event_data = payload.get("event", {})
        metadata = payload.get("metadata", {})
        event_arn = str(event_data.get("eventArn", "UNKNOWN"))[:_MAX_LOG_LEN]

        # Gate: schema version
        schema_version = str(metadata.get("schemaVersion", ""))
        if not schema_version.startswith("2."):
            _log_error(
                operation="schema_check",
                error_code="PROC_SCHEMA_VERSION_UNSUPPORTED",
                message=f"Unsupported schema version: {schema_version}",
                event_arn=event_arn,
                disposition="message_deleted",
                request_id=request_id,
            )
            return _SUCCESS

        # Gate: dispatch
        dispatch = payload.get("dispatch", {})
        if dispatch.get("dispatched") is not True:
            logger.info("Event not dispatched — skipping — event_arn=%s", event_arn)
            return _SUCCESS

        # Gate: routing (STORY-093: multi-platform routing)
        routing = payload.get("routing", {})
        platforms = routing.get("platforms", {})
        jira_target = platforms.get("jira")

        if not jira_target:
            # Backward compat bridge: schema v2.0 events have resolvedProject
            resolved_project = routing.get("resolvedProject")
            if resolved_project:
                jira_target = {
                    "project": resolved_project,
                    "issueType": routing.get("issueType", "Task"),
                }
            else:
                logger.debug(
                    "No JIRA routing for event — skipping — event_arn=%s",
                    event_arn,
                )
                return _SUCCESS

        # Gate: config
        try:
            config = _get_config()
        except Exception as exc:
            _log_error(
                operation="load_config",
                error_code="CFG_JIRA_NOT_CONFIGURED",
                message=str(type(exc).__name__),
                event_arn=event_arn,
                request_id=request_id,
            )
            return {"batchItemFailures": [{"itemIdentifier": message_id}]}

        if not config.get("validated"):
            _log_error(
                operation="config_check",
                error_code="CFG_JIRA_NOT_VALIDATED",
                message="JIRA connection not validated",
                event_arn=event_arn,
                disposition="message_deleted",
                request_id=request_id,
            )
            return _SUCCESS

        # Gate: JIRA client
        try:
            jira = _get_jira_client()
        except Exception as exc:
            _log_error(
                operation="load_credentials",
                error_code="CONN_SECRETS_FAILED",
                message=type(exc).__name__,
                event_arn=event_arn,
                request_id=request_id,
            )
            return {"batchItemFailures": [{"itemIdentifier": message_id}]}

        receive_count = record.get("attributes", {}).get("ApproximateReceiveCount", "1")
        logger.info(
            "Processing message — message_id=%s event_arn=%s receive_count=%s",
            message_id, event_arn, receive_count,
        )

        # Dispatch by action
        action = str(metadata.get("action", "CREATE"))
        campaign_type = event_data.get("campaignType", "")
        resources = payload.get("resources", [])

        if action == "RESOURCE_UPDATE":
            success = _update_tickets_jira(payload, jira, config, request_id)
            if not success:
                return {"batchItemFailures": [{"itemIdentifier": message_id}]}
            return _SUCCESS

        if action not in ("CREATE", "RECONCILE"):
            logger.warning("Unknown action — action=%s event_arn=%s message_id=%s", action[:50], event_arn, message_id)
            return _SUCCESS

        # Route by campaign type
        if campaign_type == "resource-level":
            tickets_to_create = payload.get("tickets", [])
            if len(tickets_to_create) > 1:
                result = _create_tickets_bulk(payload, jira, config, request_id, context)
                if result.exhausted:
                    return {"batchItemFailures": [{"itemIdentifier": message_id}]}
            else:
                create_template_a_ticket(
                    payload, client=None, create_issue_fn=jira.create_issue,
                    config=config, resources_table=_resources_table,
                    campaigns_table=_campaigns_table, request_id=request_id,
                )
        elif campaign_type == "account-level":
            create_template_b_ticket(
                payload, client=None, create_issue_fn=jira.create_issue,
                config=config, resources_table=_resources_table,
                campaigns_table=_campaigns_table, request_id=request_id,
            )
        else:
            logger.warning("Unknown campaign type — campaign_type=%s event_arn=%s", campaign_type, event_arn)

        return _SUCCESS

    except JiraNotFoundError:
        campaign_id = ""
        try:
            p = json.loads(json.loads(record.get("body", "{}")).get("Message", "{}"))
            campaign_id = p.get("event", {}).get("campaignId", "")
        except Exception:
            pass
        _log_error(
            operation="create_ticket",
            error_code="CONN_JIRA_NOT_FOUND",
            message="JIRA returned 404 during ticket creation",
            http_status=404,
            event_arn=event_arn,
            campaign_id=campaign_id,
            disposition="message_deleted",
            request_id=request_id,
        )
        return _SUCCESS

    except JiraApiError as exc:
        campaign_id = ""
        project = ""
        try:
            p = json.loads(json.loads(record.get("body", "{}")).get("Message", "{}"))
            campaign_id = p.get("event", {}).get("campaignId", "")
            project = p.get("routing", {}).get("resolvedProject", "")
        except Exception:
            pass

        if exc.status == 401:
            global _jira_client
            _jira_client = None

        if exc.retryable:
            _log_error(
                operation="create_ticket",
                error_code="CONN_JIRA_RETRYABLE",
                message=f"JIRA {exc.status} after retries",
                http_status=exc.status,
                routing_target=project,
                event_arn=event_arn,
                campaign_id=campaign_id,
                request_id=request_id,
            )
            return {"batchItemFailures": [{"itemIdentifier": message_id}]}

        if exc.status == 401:
            _log_error(
                operation="create_ticket",
                error_code="CONN_JIRA_AUTH_FAILED",
                message="JIRA credentials invalid — returning to queue",
                http_status=exc.status,
                routing_target=project,
                event_arn=event_arn,
                campaign_id=campaign_id,
                request_id=request_id,
            )
            return {"batchItemFailures": [{"itemIdentifier": message_id}]}

        _log_error(
            operation="create_ticket",
            error_code=f"CONN_JIRA_{exc.status}",
            message=f"JIRA returned {exc.status}",
            http_status=exc.status,
            platform_errors=exc.field_errors,
            routing_target=project,
            event_arn=event_arn,
            campaign_id=campaign_id,
            disposition="message_deleted",
            request_id=request_id,
        )
        return _SUCCESS

    except Exception as exc:
        logger.error(
            "Unhandled exception — error_type=%s error_msg=%s event_arn=%s message_id=%s",
            type(exc).__name__, str(exc)[:_MAX_LOG_LEN], event_arn, message_id,
            exc_info=False,
        )
        return {"batchItemFailures": [{"itemIdentifier": message_id}]}
