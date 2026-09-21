"""CMDB routing configuration API handlers.

Endpoints:
  GET  /api/config/cmdb-routing  — Return CMDB routing config
  POST /api/config/cmdb-routing  — Save CMDB routing config
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import boto3

logger = logging.getLogger("compass")

CONFIG_TABLE = os.environ.get("CONFIG_TABLE", "compass-config")
CORS_ORIGIN = os.environ.get("CORS_ALLOW_ORIGIN", "*")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": CORS_ORIGIN,
    "Access-Control-Allow-Headers": "Content-Type, x-api-key, Authorization",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
}

PK_CMDB_ROUTING = "CMDB_ROUTING"

_dynamodb = boto3.resource("dynamodb")


def _config_table():
    return _dynamodb.Table(CONFIG_TABLE)


def _success(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _error(status_code: int, code: str, message: str) -> dict:
    return {
        "statusCode": status_code,
        "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
        "body": json.dumps({"error": {"code": code, "message": message}}),
    }


def handle_cmdb_routing_get(event, context):
    """GET /api/config/cmdb-routing — return current CMDB routing config."""
    try:
        resp = _config_table().get_item(Key={"pk": PK_CMDB_ROUTING})
        item = resp.get("Item")
    except Exception:
        logger.exception("Failed to read CMDB routing config")
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to read configuration")

    if not item:
        return _success(200, {
            "enabled": False,
            "ciTable": "cmdb_ci",
            "lookupField": "object_id",
            "updatedAt": None,
        })

    return _success(200, {
        "enabled": bool(item.get("enabled", False)),
        "ciTable": item.get("ci_table", "cmdb_ci"),
        "lookupField": item.get("lookup_field", "object_id"),
        "updatedAt": item.get("updated_at"),
    })


def handle_cmdb_routing_save(event, context):
    """POST /api/config/cmdb-routing — save CMDB routing config."""
    try:
        body = json.loads(event.get("body") or "{}")
    except (json.JSONDecodeError, TypeError):
        return _error(400, "INVALID_REQUEST", "Request body must be valid JSON")

    enabled = bool(body.get("enabled", False))
    ci_table = body.get("ciTable", "cmdb_ci")
    lookup_field = body.get("lookupField", "object_id")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        _config_table().put_item(Item={
            "pk": PK_CMDB_ROUTING,
            "enabled": enabled,
            "ci_table": ci_table,
            "lookup_field": lookup_field,
            "updated_at": now,
        })
    except Exception:
        logger.exception("Failed to save CMDB routing config")
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to save configuration")

    return _success(200, {
        "enabled": enabled,
        "ciTable": ci_table,
        "lookupField": lookup_field,
        "updatedAt": now,
    })
