"""Dry-run routing test API handler (STORY-077).

Implements: POST /api/test/route — read-only routing simulation with trace.
"""
from __future__ import annotations

import json
import logging
import os
import re

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
_ACCOUNT_ID_RE = re.compile(r"^\d{12}$")


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


def handle_test_route(event, context):
    """POST /api/test/route — dry-run routing test. Read-only."""
    # Parse body
    raw = event.get("body")
    if not raw:
        return _error(400, "INVALID_REQUEST", "Request body must be valid JSON")
    try:
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError
    except (json.JSONDecodeError, TypeError, ValueError):
        return _error(400, "INVALID_REQUEST", "Request body must be valid JSON")

    # Validate required fields
    account_id = body.get("accountId", "")
    if not _ACCOUNT_ID_RE.match(str(account_id)):
        return _error(400, "INVALID_ACCOUNT_ID", "accountId must be exactly 12 digits")

    service = body.get("service", "")
    if not service or not isinstance(service, str):
        return _error(400, "INVALID_SERVICE", "service is required and must be a non-empty string")

    resource_tags = body.get("resourceTags", {})
    account_tags = body.get("accountTags", {})

    if not isinstance(resource_tags, dict) or len(resource_tags) > 50:
        return _error(400, "INVALID_TAGS", "resourceTags must be a dict with max 50 keys")
    if not isinstance(account_tags, dict) or len(account_tags) > 50:
        return _error(400, "INVALID_TAGS", "accountTags must be a dict with max 50 keys")

    # Read routing config
    try:
        table = _config_table()
        strategy = _get_item(table, "ROUTING_STRATEGY")
        account_route = _get_item(table, f"ROUTING#{account_id}")
        default_route = _get_item(table, "ROUTING_DEFAULT")
    except ClientError:
        logger.exception("DynamoDB read failed during test route")
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to read routing configuration")

    # Walk fallback chain
    fallback_chain = []
    resolved_target = None
    resolved_via = None
    step = 0

    # Step 1: Tag routing (if enabled)
    tag_mode = (strategy or {}).get("mode", "account")
    tag_key = (strategy or {}).get("tag_key", "")

    if tag_mode == "tag" and tag_key:
        step += 1
        # Check resourceTags first
        resource_tag_value = resource_tags.get(tag_key)
        if resource_tag_value:
            tag_route = _get_item(table, f"TAG_ROUTING#{resource_tag_value}")
            if tag_route:
                resolved_target = tag_route.get("jira_project")
                resolved_via = "resourceTag"
                fallback_chain.append({
                    "step": step,
                    "method": "resourceTag",
                    "checked": f"{tag_key}={resource_tag_value}",
                    "result": f"MATCHED → {resolved_target}",
                })
            else:
                fallback_chain.append({
                    "step": step,
                    "method": "resourceTag",
                    "checked": f"{tag_key}={resource_tag_value}",
                    "result": "NO_MAPPING",
                })
        else:
            fallback_chain.append({
                "step": step,
                "method": "resourceTag",
                "checked": f"{tag_key}=(not present)",
                "result": "TAG_NOT_FOUND",
            })

        # Check accountTags if not yet resolved
        if not resolved_target:
            step += 1
            account_tag_value = account_tags.get(tag_key)
            if account_tag_value:
                tag_route = _get_item(table, f"TAG_ROUTING#{account_tag_value}")
                if tag_route:
                    resolved_target = tag_route.get("jira_project")
                    resolved_via = "accountTag"
                    fallback_chain.append({
                        "step": step,
                        "method": "accountTag",
                        "checked": f"{tag_key}={account_tag_value}",
                        "result": f"MATCHED → {resolved_target}",
                    })
                else:
                    fallback_chain.append({
                        "step": step,
                        "method": "accountTag",
                        "checked": f"{tag_key}={account_tag_value}",
                        "result": "NO_MAPPING",
                    })
            else:
                fallback_chain.append({
                    "step": step,
                    "method": "accountTag",
                    "checked": f"{tag_key}=(not present)",
                    "result": "TAG_NOT_FOUND",
                })

    # Step 2: Account routing
    if not resolved_target:
        step += 1
        if account_route:
            resolved_target = account_route.get("jira_project")
            resolved_via = "account"
            fallback_chain.append({
                "step": step,
                "method": "account",
                "checked": f"ROUTING#{account_id}",
                "result": f"MATCHED → {resolved_target}",
            })
        else:
            fallback_chain.append({
                "step": step,
                "method": "account",
                "checked": f"ROUTING#{account_id}",
                "result": "NO_MAPPING",
            })

    # Step 3: Default routing
    if not resolved_target:
        step += 1
        if default_route:
            resolved_target = default_route.get("jira_project")
            resolved_via = "default"
            fallback_chain.append({
                "step": step,
                "method": "default",
                "checked": "ROUTING_DEFAULT",
                "result": f"MATCHED → {resolved_target}",
            })
        else:
            fallback_chain.append({
                "step": step,
                "method": "default",
                "checked": "ROUTING_DEFAULT",
                "result": "NO_MAPPING",
            })

    # Build response
    resolved = resolved_target is not None
    suggestion = None
    if not resolved:
        resolved_via = "error"
        suggestion = "No routing configuration found. Configure a default project via POST /api/config/routing/default."

    return _success(200, {
        "resolved": resolved,
        "target": resolved_target,
        "resolvedVia": resolved_via,
        "fallbackChain": fallback_chain,
        "suggestion": suggestion,
    })


def _get_item(table, pk: str) -> dict | None:
    """Get a single item from ConfigTable by pk. Returns item or None."""
    resp = table.get_item(Key={"pk": pk})
    return resp.get("Item")
