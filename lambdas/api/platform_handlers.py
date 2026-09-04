"""Platform selection API handlers.

Implements:
  GET  /api/config/platform — Return active platform + connection status.
  POST /api/config/platform — Switch active ITSM platform.

Security:
  - POST requires Admin role (enforced by RBAC in handler.py).
  - Never exposes secret ARNs or credential values.
  - Strict allowlist for platform values.
"""
from __future__ import annotations

import json
import logging
import os
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

_VALID_PLATFORMS = frozenset({"jira", "servicenow"})


# ===================================================================
# GET /api/config/platform
# ===================================================================


def handle_platform_get(event, context):
    """Return active ITSM platform and connection status for both platforms.

    Response shape:
      {
        "platform": "jira",
        "switchedAt": "2026-06-10T12:00:00Z",
        "connections": {
          "jira": {"validated": true, "validatedAt": "..."},
          "servicenow": {"validated": false, "validatedAt": null}
        }
      }
    """
    config = _config_table()

    # Read current platform
    platform_item = _safe_get_item(config, "ITSM_PLATFORM")
    platform = (platform_item.get("platform") if platform_item else None) or "jira"
    switched_at = (platform_item.get("switched_at") if platform_item else None)

    # Read connection statuses — never expose secrets
    jira_item = _safe_get_item(config, "JIRA_CONNECTION")
    snow_item = _safe_get_item(config, "SNOW_CONNECTION")

    return _success(200, {
        "platform": platform,
        "switchedAt": switched_at,
        "connections": {
            "jira": {
                "validated": bool(jira_item.get("validated")) if jira_item else False,
                "validatedAt": jira_item.get("validated_at") if jira_item else None,
            },
            "servicenow": {
                "validated": bool(snow_item.get("validated")) if snow_item else False,
                "validatedAt": snow_item.get("validated_at") if snow_item else None,
            },
        },
    })


# ===================================================================
# POST /api/config/platform
# ===================================================================


def handle_platform_post(event, context):
    """Switch the active ITSM platform.

    Pre-conditions:
      - Target platform connection must be validated.

    Returns warning about existing tickets remaining in the previous platform.
    """
    body = _parse_body(event)
    if not body or not isinstance(body, dict):
        return _error(400, "CFG_INVALID_REQUEST", "Request body must be valid JSON")

    target_platform = body.get("platform", "").strip().lower()
    if target_platform not in _VALID_PLATFORMS:
        return _error(
            400, "CFG_INVALID_PLATFORM",
            f"platform must be one of: {', '.join(sorted(_VALID_PLATFORMS))}",
        )

    config = _config_table()
    source_ip = event.get("requestContext", {}).get("identity", {}).get("sourceIp", "")
    now = _now_iso()

    # Read current platform for warning message
    current_item = _safe_get_item(config, "ITSM_PLATFORM")
    current_platform = (current_item.get("platform") if current_item else None) or "jira"

    # Pre-condition: target platform connection must be validated
    if target_platform == "jira":
        conn_item = _safe_get_item(config, "JIRA_CONNECTION")
    else:
        conn_item = _safe_get_item(config, "SNOW_CONNECTION")

    if not conn_item or not conn_item.get("validated"):
        logger.warning(json.dumps({
            "audit": True,
            "action": "PLATFORM_SWITCH",
            "source_ip": source_ip,
            "target_platform": target_platform,
            "result": "rejected_not_validated",
            "timestamp": now,
        }))
        platform_label = "JIRA" if target_platform == "jira" else "ServiceNow"
        return _error(
            400, "CFG_PLATFORM_NOT_VALIDATED",
            f"{platform_label} connection must be validated before switching.",
        )

    # Write platform selection
    try:
        config.put_item(Item={
            "pk": "ITSM_PLATFORM",
            "platform": target_platform,
            "switched_at": now,
        })
        # Also write INTEGRATIONS_ENABLED for backward compat
        config.put_item(Item={
            "pk": "INTEGRATIONS_ENABLED",
            "platforms": [target_platform],
            "updated_at": now,
        })
    except ClientError:
        logger.exception("DynamoDB write failed for ITSM_PLATFORM")
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to switch platform.")

    # Audit log
    logger.warning(json.dumps({
        "audit": True,
        "action": "PLATFORM_SWITCH",
        "source_ip": source_ip,
        "target_platform": target_platform,
        "previous_platform": current_platform,
        "result": "success",
        "timestamp": now,
    }))

    # Build response with warning if switching from a platform with existing tickets
    response = {
        "platform": target_platform,
        "switchedAt": now,
    }

    if current_platform != target_platform:
        response["warning"] = (
            f"Existing tickets remain in {current_platform}. "
            f"New tickets will use {target_platform}."
        )

    return _success(200, response)


# ===================================================================
# GET /api/config/integrations
# ===================================================================


def handle_integrations_get(event, context):
    """Return enabled platforms and connection statuses."""
    config = _config_table()

    # Read INTEGRATIONS_ENABLED (or fall back to ITSM_PLATFORM)
    integ_item = _safe_get_item(config, "INTEGRATIONS_ENABLED")
    if integ_item and isinstance(integ_item.get("platforms"), list):
        platforms = integ_item["platforms"]
        updated_at = integ_item.get("updated_at")
    else:
        # Auto-migration: read legacy item
        legacy = _safe_get_item(config, "ITSM_PLATFORM")
        platform = (legacy.get("platform") if legacy else None) or "jira"
        platforms = [platform]
        updated_at = legacy.get("switched_at") if legacy else None

    # Connection statuses
    jira_item = _safe_get_item(config, "JIRA_CONNECTION")
    snow_item = _safe_get_item(config, "SNOW_CONNECTION")

    return _success(200, {
        "platforms": platforms,
        "updatedAt": updated_at,
        "connections": {
            "jira": {
                "configured": jira_item is not None,
                "validated": bool(jira_item.get("validated")) if jira_item else False,
                "validatedAt": jira_item.get("validated_at") if jira_item else None,
            },
            "servicenow": {
                "configured": snow_item is not None,
                "validated": bool(snow_item.get("validated")) if snow_item else False,
                "validatedAt": snow_item.get("validated_at") if snow_item else None,
            },
        },
    })


# ===================================================================
# PUT /api/config/integrations
# ===================================================================


def handle_integrations_put(event, context):
    """Enable/disable platforms. Pre-condition: each must be validated.

    Security hardening:
      Audit log on every PUT.
      Type check + length cap on platforms array.
    """
    body = _parse_body(event)
    if not body or not isinstance(body, dict):
        return _error(400, "CFG_INVALID_REQUEST", "Request body must be valid JSON")

    # Type check on array
    raw_platforms = body.get("platforms")
    if not isinstance(raw_platforms, list):
        return _error(400, "CFG_INVALID_REQUEST",
                      "Request body must include 'platforms' array")

    # Max length cap
    if len(raw_platforms) > 10:
        return _error(400, "CFG_INVALID_REQUEST",
                      "platforms array exceeds maximum of 10 items")

    # Element type check + allowlist filter
    requested = [p for p in raw_platforms
                 if isinstance(p, str) and p in _VALID_PLATFORMS]
    if not requested:
        return _error(400, "CFG_INVALID_REQUEST",
                      "At least one valid platform required (jira, servicenow)")

    config = _config_table()
    now = _now_iso()
    source_ip = event.get("requestContext", {}).get("identity", {}).get("sourceIp", "")

    # Validate each platform has a validated connection
    invalid = []
    for p in requested:
        conn_pk = "JIRA_CONNECTION" if p == "jira" else "SNOW_CONNECTION"
        conn = _safe_get_item(config, conn_pk)
        if not conn or not conn.get("validated"):
            invalid.append(p)

    if invalid:
        # Audit log for rejected attempt
        logger.warning(json.dumps({
            "audit": True,
            "action": "INTEGRATIONS_UPDATE",
            "source_ip": source_ip,
            "requested_platforms": requested,
            "result": "rejected_not_validated",
            "invalid_platforms": invalid,
            "timestamp": now,
        }))
        return _error(400, "CFG_PLATFORM_NOT_VALIDATED",
                      f"Connection must be validated: {', '.join(invalid)}")

    # Write INTEGRATIONS_ENABLED
    try:
        config.put_item(Item={
            "pk": "INTEGRATIONS_ENABLED",
            "platforms": requested,
            "updated_at": now,
        })
    except ClientError:
        logger.exception("DynamoDB write failed for INTEGRATIONS_ENABLED")
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to update integrations.")

    # Audit log for successful update
    logger.warning(json.dumps({
        "audit": True,
        "action": "INTEGRATIONS_UPDATE",
        "source_ip": source_ip,
        "requested_platforms": requested,
        "result": "success",
        "timestamp": now,
    }))

    return _success(200, {
        "platforms": requested,
        "updatedAt": now,
        "warning": "Disabling a platform does not close existing tickets.",
    })


# ===================================================================
# Helpers
# ===================================================================


def _config_table():
    return _dynamodb.Table(CONFIG_TABLE)


def _safe_get_item(table, pk: str) -> dict | None:
    """GetItem with error suppression. Returns Item dict or None."""
    try:
        return table.get_item(Key={"pk": pk}).get("Item")
    except ClientError:
        logger.exception("DynamoDB read failed for pk=%s", pk)
        return None


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


def _success(status_code: int, body) -> dict:
    return {
        "statusCode": status_code,
        "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
        "body": json.dumps(body, default=str) if body is not None else "null",
    }


def _error(status_code: int, code: str, message: str) -> dict:
    return {
        "statusCode": status_code,
        "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
        "body": json.dumps({"error": {"code": code, "message": message}}),
    }
