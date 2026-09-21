"""Orphan queue notification API handlers.

Implements 2 endpoints for Amazon DynamoDB orphan ticket tracking:
  GET /api/config/routing/orphan-status  — Orphan ticket count and threshold
  GET /api/config/routing/suggestions    — Routing suggestions from moved tickets
"""
from __future__ import annotations

import json
import logging
import os

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

try:
    from resolve_core.constants import (
        ORPHAN_STATUS_KEY,
        ORPHAN_COUNT_FIELD,
        ORPHAN_ALERT_THRESHOLD,
    )
except ImportError:
    from lambdas.shared.python.resolve_core.constants import (
        ORPHAN_STATUS_KEY,
        ORPHAN_COUNT_FIELD,
        ORPHAN_ALERT_THRESHOLD,
    )

logger = logging.getLogger(__name__)

CONFIG_TABLE = os.environ.get("CONFIG_TABLE", "compass-config")
CORS_ORIGIN = os.environ.get("CORS_ALLOW_ORIGIN", "*")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": CORS_ORIGIN,
    "Access-Control-Allow-Headers": "Content-Type, x-api-key, Authorization",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
}

# threshold now sourced from resolve_core.constants
# (ORPHAN_ALERT_THRESHOLD) so it can never drift from the sync Lambda's
# alert-flag calculation. Kept as a module-level alias for readability
# in this file's existing call sites.
ORPHAN_THRESHOLD = ORPHAN_ALERT_THRESHOLD

_dynamodb = boto3.resource("dynamodb")


def _config_table():
    return _dynamodb.Table(CONFIG_TABLE)


def _success(status_code: int, body) -> dict:
    return {
        "statusCode": status_code,
        "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
        "body": json.dumps(body) if body is not None else "null",
    }


def _error(status_code: int, code: str, message: str) -> dict:
    return {
        "statusCode": status_code,
        "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
        "body": json.dumps({"error": {"code": code, "message": message}}),
    }


# ===================================================================
# GET /api/config/routing/orphan-status
# ===================================================================

def handle_orphan_status(event, context):
    """Return orphan ticket count and threshold status.

    Reads the same ConfigTable pk/field the sync Lambda writes
    (resolve_core.constants.ORPHAN_STATUS_KEY / ORPHAN_COUNT_FIELD).
    Previously this handler read a different key ("ORPHAN_STATUS") and
    field ("orphan_count") than the writer used, so the endpoint always
    reported a count of 0.
    """
    try:
        resp = _config_table().get_item(Key={"pk": ORPHAN_STATUS_KEY})
    except ClientError:
        logger.exception(
            "DynamoDB read failed for %s", ORPHAN_STATUS_KEY,
        )
        return _error(500, "SYS_INTERNAL_ERROR", "An internal error occurred.")

    item = resp.get("Item")
    if item is None:
        return _success(200, {
            "orphanCount": 0,
            "thresholdExceeded": False,
            "threshold": ORPHAN_THRESHOLD,
        })

    orphan_count = int(item.get(ORPHAN_COUNT_FIELD, 0))
    return _success(200, {
        "orphanCount": orphan_count,
        "thresholdExceeded": orphan_count > ORPHAN_THRESHOLD,
        "threshold": ORPHAN_THRESHOLD,
    })


# ===================================================================
# GET /api/config/routing/suggestions
# ===================================================================

def handle_routing_suggestions(event, context):
    """Return routing suggestions from tickets moved out of default project."""
    try:
        resp = _config_table().scan(
            FilterExpression=Key("pk").begins_with("ROUTING_SUGGESTION#"),
        )
    except ClientError:
        logger.exception("DynamoDB scan failed for ROUTING_SUGGESTION#")
        return _error(500, "SYS_INTERNAL_ERROR", "An internal error occurred.")

    suggestions = [
        {
            "accountId": item.get("account_id", ""),
            "suggestedProject": item.get("suggested_project", ""),
            "reason": item.get("reason", ""),
            "ticketsMoved": int(item.get("tickets_moved", 0)),
            "createdAt": item.get("created_at", ""),
        }
        for item in resp.get("Items", [])
    ]

    return _success(200, suggestions)
