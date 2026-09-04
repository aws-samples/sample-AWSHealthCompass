"""Telemetry API handlers — client-side telemetry event recording.

STORY-086: Beta Telemetry P1 Metrics (T-B-4 through T-B-8).
Security: User identity hashed (SHA-256 of Cognito sub). No PII stored.
All telemetry is consent-gated at the aggregation layer.
"""
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

CONFIG_TABLE = os.environ.get("CONFIG_TABLE", "compass-config")
CORS_ORIGIN = os.environ.get("CORS_ALLOW_ORIGIN", "*")
CORS_HEADERS = {
    "Access-Control-Allow-Origin": CORS_ORIGIN,
    "Access-Control-Allow-Headers": "Content-Type, x-api-key, Authorization",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
}

_dynamodb = boto3.resource("dynamodb")


def _success(code, body):
    return {
        "statusCode": code,
        "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def handle_telemetry_session(event, context):
    """POST /api/telemetry/session — record dashboard session (T-B-4).

    Records a unique-user-per-day entry using a hashed Cognito sub.
    TTL: 30 days. Best-effort — failures never surface to the caller.
    """
    authorizer = event.get("requestContext", {}).get("authorizer", {})
    user_sub = authorizer.get("sub", "")
    if not user_sub:
        return _success(200, {"recorded": False})

    hashed_sub = hashlib.sha256(user_sub.encode()).hexdigest()[:16]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ttl = int(time.time()) + (30 * 86400)

    try:
        _dynamodb.Table(CONFIG_TABLE).put_item(Item={
            "pk": f"SESSION#{today}#{hashed_sub}",
            "date": today,
            "hashedSub": hashed_sub,
            "ttl": ttl,
        })
    except ClientError:
        logger.debug("Session telemetry write failed", exc_info=True)

    return _success(200, {"recorded": True})


def handle_telemetry_event(event, context):
    """POST /api/telemetry/event — record UI interaction event (T-B-5).

    Tracks filter usage and dashboard interactions as atomic counters.
    TTL: 7 days. Best-effort — failures never surface to the caller.
    """
    body = json.loads(event.get("body", "{}") or "{}")
    event_name = body.get("event", "")
    if not event_name or len(event_name) > 100:
        return _success(200, {"recorded": False})

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ttl = int(time.time()) + (7 * 86400)

    try:
        _dynamodb.Table(CONFIG_TABLE).update_item(
            Key={"pk": f"TELE_EVENT#{today}#{event_name}"},
            UpdateExpression="SET #c = if_not_exists(#c, :zero) + :one, #t = :ttl, #d = :date",
            ExpressionAttributeNames={"#c": "count", "#t": "ttl", "#d": "date"},
            ExpressionAttributeValues={":zero": 0, ":one": 1, ":ttl": ttl, ":date": today},
        )
    except ClientError:
        logger.debug("Event telemetry write failed", exc_info=True)

    return _success(200, {"recorded": True})
