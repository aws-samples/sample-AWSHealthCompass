"""ServiceNow Integration Lambda — entrypoint.

Receives standardized Health events from SQS (via SNS fan-out).
Instantiates ServiceNowClient + ServiceNowFormatter and delegates
to the ITSM orchestrator for ticket creation/update.

Same pattern as jira_integration/handler.py.
Trigger: SQS ServiceNow Queue (batch_size=1, ReportBatchItemFailures=true).
Runtime: Python 3.12, 256 MB, 5 min timeout, reserved concurrency 2.

Multi-platform routing — gate on routing.platforms.servicenow.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError

from resolve_core.servicenow_client import ServiceNowClient
from resolve_core.servicenow_formatter import ServiceNowFormatter
from resolve_core.itsm_client import ITSMAPIError
from resolve_core.config_schema import PK_SNOW_CONNECTION
from resolve_core.constants import TICKET_SUMMARY_PREFIX

logger = logging.getLogger("compass")
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# --- Environment ---
_CONFIG_TABLE = os.environ.get("CONFIG_TABLE", "compass-config")
_CAMPAIGNS_TABLE = os.environ.get("CAMPAIGNS_TABLE", "compass-campaigns")
_RESOURCES_TABLE = os.environ.get("RESOURCES_TABLE", "compass-resources")
_SNOW_SECRET_ARN = os.environ.get("SERVICENOW_SECRET_ARN", "")
_AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# --- Module-level resources (reused across warm invocations) ---
_dynamodb = boto3.resource("dynamodb", region_name=_AWS_REGION)
_config_table = _dynamodb.Table(_CONFIG_TABLE)

_client: ServiceNowClient | None = None
_enabled_platforms: Optional[list] = None


def _get_enabled_platforms() -> list:
    """Load and cache enabled platforms from ConfigTable.

    ServiceNow defaults to DISABLED (fail-closed) if INTEGRATIONS_ENABLED
    is missing, unlike JIRA which defaults to enabled.
    """
    global _enabled_platforms
    if _enabled_platforms is not None:
        return _enabled_platforms
    try:
        resp = _config_table.get_item(Key={"pk": "INTEGRATIONS_ENABLED"})
        item = resp.get("Item")
        if item and isinstance(item.get("platforms"), list):
            _enabled_platforms = item["platforms"]
        else:
            _enabled_platforms = ["jira"]  # backward compat — SNOW disabled by default
    except ClientError:
        _enabled_platforms = []  # fail-closed for ServiceNow
    return _enabled_platforms


def _get_client() -> ServiceNowClient:
    """Lazy-init ServiceNow client from ConfigTable."""
    global _client
    if _client:
        return _client

    resp = _config_table.get_item(Key={"pk": PK_SNOW_CONNECTION})
    conn = resp.get("Item")
    if not conn or not conn.get("validated"):
        raise RuntimeError("SNOW_CONNECTION not configured or not validated")

    instance_url = conn.get("instance_url", "")
    secret_arn = conn.get("secret_arn", _SNOW_SECRET_ARN)
    record_type = conn.get("record_type", "change_request")

    formatter = ServiceNowFormatter()
    _client = ServiceNowClient(
        instance_url=instance_url,
        secret_arn=secret_arn or _SNOW_SECRET_ARN,
        formatter=formatter,
        record_type=record_type,
    )
    return _client


def lambda_handler(event: dict, context: Any) -> dict:
    """SQS ESM handler. Processes Health events for ServiceNow ticketing.

    Returns {"batchItemFailures": []} on success, or
    {"batchItemFailures": [{"itemIdentifier": messageId}]} on failure.
    """
    # Global kill switch — fail-closed for ServiceNow
    if "servicenow" not in _get_enabled_platforms():
        logger.debug("ServiceNow platform globally disabled — skipping")
        return {"batchItemFailures": []}

    batch_item_failures = []

    for record in event.get("Records", []):
        message_id = record["messageId"]
        try:
            body = json.loads(record["body"])
            # SNS wraps the payload in a Message field
            payload = json.loads(body["Message"]) if "Message" in body else body

            # Per-event routing gate
            routing = payload.get("routing", {})
            platforms = routing.get("platforms", {})
            snow_target = platforms.get("servicenow")

            if not snow_target:
                logger.debug("No ServiceNow routing for event — skipping")
                continue  # Normal no-op — not an error

            event_arn = payload.get("event", {}).get("eventArn", "unknown")
            logger.info("snow_processing_start — event_arn=%s", event_arn)

            client = _get_client()
            _process_event(client, payload, snow_target)

            logger.info("snow_processing_complete — event_arn=%s", event_arn)

        except ITSMAPIError as exc:
            logger.error(
                "snow_api_error — message_id=%s status=%d retryable=%s msg=%s",
                message_id, exc.status_code, exc.retryable, exc.error_message[:200],
            )
            if exc.retryable:
                batch_item_failures.append({"itemIdentifier": message_id})
        except Exception:
            logger.exception("snow_processing_failed — message_id=%s", message_id)
            batch_item_failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": batch_item_failures}


def _process_event(client: ServiceNowClient, payload: dict, snow_target: dict) -> None:
    """Process a single standardized event using ServiceNow routing target.

    Args:
        client: Initialized ServiceNow client.
        payload: Full standardized event payload.
        snow_target: routing.platforms.servicenow dict with
            assignmentGroupId, assignmentGroupName, recordType.
    """
    from resolve_core.itsm_client import TicketCreateRequest
    from resolve_core.ticket_content import TicketContent

    event_data = payload.get("event", {})
    resources = payload.get("resources", [])

    campaign_id = event_data.get("campaignId", "")
    if not campaign_id:
        logger.warning("snow_no_campaign_id — skipping")
        return

    # Extract routing target from platforms map
    assignment_group_id = snow_target["assignmentGroupId"]
    record_type = snow_target.get("recordType", "change_request")

    # Build ticket content
    summary = f"{TICKET_SUMMARY_PREFIX} {event_data.get('service', '')} {event_data.get('eventTypeCode', '')} — {event_data.get('affectedAccount', '')}"
    if resources:
        summary += f" ({len(resources)} resources)"

    has_resources = bool(resources)
    campaign_type = "resource-level" if has_resources else "account-level"
    csv_needed = len(resources) > 100

    content = TicketContent(
        summary=summary[:160],
        metadata_pairs=[
            ("Service", event_data.get("service", "")),
            ("Deadline", event_data.get("startTime", "")),
            ("Account", event_data.get("affectedAccount", "")),
            ("Region", event_data.get("region", "")),
            ("Actionability", event_data.get("actionability", "")),
        ],
        description_text=event_data.get("description", ""),
        resources=resources[:100] if not csv_needed else [],
        guidance_text=event_data.get("startTime", ""),
        labels=[],
        due_date=event_data.get("startTime", "")[:10] if event_data.get("startTime") else None,
        campaign_type=campaign_type,
        csv_needed=csv_needed,
    )

    request = TicketCreateRequest(
        campaign_id=campaign_id,
        summary=summary[:160],
        description_content=content,
        routing_target=assignment_group_id,
        due_date=content.due_date,
        urgency=2,
        impact=2,
        labels=[],
        record_type=record_type,
        correlation_id=campaign_id,
    )

    ticket_resp = client.create_ticket(request)

    # Store ticket reference in DynamoDB
    resources_table = _dynamodb.Table(_RESOURCES_TABLE)
    tracking_key = f"TICKET#{event_data.get('affectedAccount', '')}"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ticket_data = {
        "ticketId": ticket_resp.ticket_id,
        "ticketStatus": "Created",
        "ticketRawStatus": "",
        "ticketUrl": ticket_resp.ticket_url,
        "ticketUpdatedAt": now,
    }

    try:
        # Primary path: nested SET into existing tickets map
        resources_table.update_item(
            Key={"campaignId": campaign_id, "trackingKey": tracking_key},
            UpdateExpression=(
                "SET #t.#platform = :ticket_data, "
                "ticketId = :tid, ticketUrl = :turl, "
                "ticketPlatform = :tp, ticketStatus = :ts"
            ),
            ExpressionAttributeNames={
                "#t": "tickets",
                "#platform": "servicenow",  # hardcoded platform key
            },
            ExpressionAttributeValues={
                ":ticket_data": ticket_data,
                ":tid": ticket_resp.ticket_id,
                ":turl": ticket_resp.ticket_url,
                ":tp": "servicenow",
                ":ts": "Created",
            },
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code == "ValidationException":
            # Fallback: tickets map doesn't exist (legacy item)
            resources_table.update_item(
                Key={"campaignId": campaign_id, "trackingKey": tracking_key},
                UpdateExpression=(
                    "SET #t = :ticket_map, "
                    "ticketId = :tid, ticketUrl = :turl, "
                    "ticketPlatform = :tp, ticketStatus = :ts"
                ),
                ExpressionAttributeNames={
                    "#t": "tickets",
                },
                ExpressionAttributeValues={
                    ":ticket_map": {"servicenow": ticket_data},
                    ":tid": ticket_resp.ticket_id,
                    ":turl": ticket_resp.ticket_url,
                    ":tp": "servicenow",
                    ":ts": "Created",
                },
            )
        else:
            raise

    logger.info(
        "snow_ticket_created — campaign=%s",
        campaign_id,
    )
