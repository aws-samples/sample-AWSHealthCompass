"""Routing coverage API handlers (STORY-071).

Implements 2 endpoints for routing coverage metrics:
  GET /api/routing/coverage             — Aggregated routing coverage breakdown
  GET /api/routing/coverage/unroutable  — List of unroutable resources
  GET /api/metrics/routing-coverage     — Alias for coverage endpoint (CDK route)
"""
from __future__ import annotations

import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

RESOURCES_TABLE = os.environ.get("RESOURCES_TABLE", "compass-resources")
CAMPAIGNS_TABLE = os.environ.get("CAMPAIGNS_TABLE", "compass-campaigns")
CORS_ORIGIN = os.environ.get("CORS_ALLOW_ORIGIN", "*")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": CORS_ORIGIN,
    "Access-Control-Allow-Headers": "Content-Type, x-api-key, Authorization",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
}

# SEC-071-04: Max scan pages to prevent runaway
_MAX_SCAN_PAGES = 10
_UNROUTABLE_LIMIT = 100

_dynamodb = boto3.resource("dynamodb")


def _resources_table():
    return _dynamodb.Table(RESOURCES_TABLE)


def _campaigns_table():
    return _dynamodb.Table(CAMPAIGNS_TABLE)


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
# GET /api/routing/coverage
# ===================================================================

def handle_routing_coverage(event, context):
    """Scan ResourcesTable and aggregate routedVia field values."""
    breakdown = {
        "resourceTag": 0,
        "accountTag": 0,
        "account": 0,
        "service": 0,
        "default": 0,
        "failed": 0,
    }
    total = 0

    try:
        scan_kwargs = {
            "ProjectionExpression": "routedVia",
        }
        pages = 0
        while pages < _MAX_SCAN_PAGES:
            resp = _resources_table().scan(**scan_kwargs)
            pages += 1
            for item in resp.get("Items", []):
                total += 1
                routed_via = item.get("routedVia")
                if routed_via in ("resourceTag", "accountTag", "account", "service", "default"):
                    breakdown[routed_via] += 1
                else:
                    # error, missing, null → failed
                    breakdown["failed"] += 1

            if "LastEvaluatedKey" not in resp:
                break
            scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    except ClientError:
        logger.exception("DynamoDB scan failed for routing coverage")
        return _error(500, "SYS_INTERNAL_ERROR", "An internal error occurred.")

    if total == 0:
        return _success(200, {
            "coveragePercent": 0,
            "totalResources": 0,
            "routedResources": 0,
            "breakdown": breakdown,
            "message": "No resources tracked yet. Ingest Health events to see routing coverage.",
        })

    routed = total - breakdown["failed"]
    coverage_pct = round((routed / total) * 100, 1)

    return _success(200, {
        "coveragePercent": coverage_pct,
        "totalResources": total,
        "routedResources": routed,
        "breakdown": breakdown,
        "message": None,
    })


# ===================================================================
# GET /api/routing/coverage/unroutable
# ===================================================================

def handle_unroutable(event, context):
    """Scan ResourcesTable for resources with routedVia=error or missing."""
    resources = []
    total_unroutable = 0

    try:
        scan_kwargs = {
            "ProjectionExpression": "campaignId, trackingKey, accountId, routedVia, routingError",
        }
        pages = 0
        while pages < _MAX_SCAN_PAGES:
            resp = _resources_table().scan(**scan_kwargs)
            pages += 1
            for item in resp.get("Items", []):
                routed_via = item.get("routedVia")
                if routed_via not in ("resourceTag", "accountTag", "account", "service", "default"):
                    total_unroutable += 1
                    if len(resources) < _UNROUTABLE_LIMIT:
                        resources.append({
                            "resourceArn": item.get("trackingKey", ""),
                            "accountId": item.get("accountId", ""),
                            "campaignId": item.get("campaignId", ""),
                            "reason": item.get("routingError") or _default_reason(item),
                            "service": _extract_service(item.get("campaignId", "")),
                        })

            if "LastEvaluatedKey" not in resp:
                break
            scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    except ClientError:
        logger.exception("DynamoDB scan failed for unroutable resources")
        return _error(500, "SYS_INTERNAL_ERROR", "An internal error occurred.")

    return _success(200, {
        "unroutableCount": total_unroutable,
        "resources": resources,
    })


def _default_reason(item: dict) -> str:
    """Generate a default reason string for unroutable resources."""
    account_id = item.get("accountId", "unknown")
    return f"No routing rule matched for account {account_id}"


def _extract_service(campaign_id: str) -> str:
    """Extract service name from campaignId (format: SERVICE:version or eventArn)."""
    if ":" in campaign_id:
        return campaign_id.split(":")[0].upper()
    return ""
