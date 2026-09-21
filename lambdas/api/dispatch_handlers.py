"""Dispatch window & activation API handlers.

Implements 6 endpoints for Amazon DynamoDB dispatch configuration:
  POST   /api/config/dispatch                — Save dispatch preset + rules
  GET    /api/config/dispatch                — Get dispatch configuration
  PUT    /api/config/dispatch/rules/{ruleId} — Update single rule
  DELETE /api/config/dispatch/rules/{ruleId} — Delete single rule
  GET    /api/config/status                  — Full configuration status
  POST   /api/config/activate                — Validate prerequisites & activate

Security notes:
  Pattern regex enforced before every DynamoDB write.
  No jira_secret_arn in any API response (allowlist).
  ruleId validated before pk construction.
  No customer_account_id in logs or responses.
  FINDING-IMPL-01: Empty prefix guard in evaluate_dispatch.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

try:
    from resolve_core.dispatch import (
        validate_dispatch_pattern,
        validate_event_categories,
        validate_rule_id,
    )
except ImportError:
    from lambdas.shared.python.resolve_core.dispatch import (
        validate_dispatch_pattern,
        validate_event_categories,
        validate_rule_id,
    )

# shared platform-resolution seam (single source of truth).
# consumes resolve_platforms/operative_platform + extract_routing_target;
# it MUST NOT re-derive platform resolution.
try:
    from resolve_core.config_schema import (
        extract_routing_target,
        operative_platform,
        resolve_platforms,
    )
except ImportError:
    from lambdas.shared.python.resolve_core.config_schema import (
        extract_routing_target,
        operative_platform,
        resolve_platforms,
    )

logger = logging.getLogger(__name__)

CONFIG_TABLE = os.environ.get("CONFIG_TABLE", "compass-config")
CORS_ORIGIN = os.environ.get("CORS_ALLOW_ORIGIN", "*")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": CORS_ORIGIN,
    "Access-Control-Allow-Headers": "Content-Type, x-api-key, Authorization",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
}

_VALID_MODES = ("all", "ple_only", "custom")

_dynamodb = boto3.resource("dynamodb")
_sts = boto3.client("sts")

# Cached account ID (never changes during Lambda lifetime)
_account_id: str | None = None


# ===================================================================
# Helpers
# ===================================================================

def _config_table():
    return _dynamodb.Table(CONFIG_TABLE)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _success(status_code: int, body) -> dict:
    return {
        "statusCode": status_code,
        "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
        "body": json.dumps(body) if body is not None else "null",
    }


def _error(status_code: int, code: str, message: str, detail: str | None = None) -> dict:
    err = {"code": code, "message": message}
    if detail:
        err["detail"] = detail
    return {
        "statusCode": status_code,
        "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
        "body": json.dumps({"error": err}),
    }


def _parse_body(event: dict) -> dict | None:
    raw = event.get("body")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _get_account_id() -> str:
    """Get AWS account ID via STS (cached)."""
    global _account_id
    if _account_id is None:
        _account_id = _sts.get_caller_identity()["Account"]
    return _account_id


def _gen_rule_id() -> str:
    return f"rule-{uuid.uuid4().hex[:8]}"


def _dispatch_warning(mode, rules) -> str | None:
    """Generate warning text"""
    if mode is None:
        return "Dispatch window not configured. Default behavior: all actionable events create tickets."
    if mode == "custom":
        if not rules:
            return "Custom mode selected but no rules defined. No tickets will be created."
        if all(not r.get("enabled", False) for r in rules):
            return "No dispatch rules are enabled. No tickets will be created for any events."
    return None


def _rule_to_api(item: dict) -> dict:
    """Convert DynamoDB rule item to API response format (camelCase)."""
    return {
        "ruleId": item.get("rule_id", ""),
        "eventTypePattern": item.get("event_type_pattern", ""),
        "eventCategories": item.get("event_categories", []),
        "enabled": bool(item.get("enabled", False)),
    }


def _scan_dispatch_rules() -> list[dict]:
    """Scan all DISPATCH_RULE# items with ProjectionExpression."""
    rules = []
    scan_kwargs = {
        "FilterExpression": Attr("pk").begins_with("DISPATCH_RULE#"),
        "ProjectionExpression": "pk, rule_id, event_type_pattern, event_categories, enabled, created_at, updated_at",
    }
    while True:
        resp = _config_table().scan(**scan_kwargs)
        rules.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    rules.sort(key=lambda r: r.get("rule_id", ""))
    return rules


# ===================================================================
# POST /api/config/dispatch
# ===================================================================

def handle_dispatch_save(event, context):
    """Save dispatch preset mode and optional custom rules."""
    body = _parse_body(event)
    if body is None:
        return _error(400, "CFG_INVALID_REQUEST", "Request body must be valid JSON")

    mode = body.get("mode", "")
    if not isinstance(mode, str) or mode not in _VALID_MODES:
        return _error(400, "CFG_INVALID_DISPATCH_MODE",
                      "mode must be 'all', 'ple_only', or 'custom'.")

    # Actionability filter
    actionability_filter = body.get("actionabilityFilter", "all_actionable")
    if actionability_filter not in ("all_actionable", "action_required_only"):
        return _error(400, "INVALID_PARAM", "actionabilityFilter must be 'all_actionable' or 'action_required_only'")

    rules_input = body.get("rules", [])
    now = _now_iso()

    # Custom mode requires at least one rule
    if mode == "custom":
        if not isinstance(rules_input, list) or len(rules_input) == 0:
            return _error(400, "CFG_INVALID_DISPATCH_PATTERN",
                          "Custom mode requires at least one rule. Use 'all' mode to match all events.")

        # Validate each rule
        for i, rule in enumerate(rules_input):
            if not isinstance(rule, dict):
                return _error(400, "CFG_INVALID_REQUEST", f"Rule at index {i} must be an object.")

            pattern = rule.get("eventTypePattern", "")
            if not validate_dispatch_pattern(pattern):
                return _error(400, "CFG_INVALID_DISPATCH_PATTERN",
                              f"Event type pattern must start with 'AWS_'. Got: '{pattern}'.")

            categories = rule.get("eventCategories", [])
            if not validate_event_categories(categories):
                invalid = [c for c in categories if c not in ("scheduledChange", "accountNotification")]
                msg = (f"Event category must be 'scheduledChange' or 'accountNotification'. Got: '{invalid[0]}'."
                       if invalid else "eventCategories must contain at least one category.")
                return _error(400, "CFG_INVALID_EVENT_CATEGORY", msg)

            if not isinstance(rule.get("enabled"), bool):
                return _error(400, "CFG_INVALID_REQUEST", f"Rule at index {i}: 'enabled' must be a boolean.")

            # Validate ruleId if provided
            rule_id = rule.get("ruleId", "")
            if rule_id and not validate_rule_id(rule_id):
                return _error(400, "CFG_INVALID_REQUEST",
                              f"ruleId must match [a-zA-Z0-9_-] and be 1-64 chars. Got: '{rule_id}'.")

    # Write DISPATCH_PRESET
    try:
        _config_table().put_item(Item={
            "pk": "DISPATCH_PRESET",
            "mode": mode,
            "actionability_filter": actionability_filter,
            "updated_at": now,
        })
    except ClientError:
        logger.exception("Failed to write DISPATCH_PRESET")
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to save dispatch configuration.")

    # Write rules for custom mode
    rules_written = []
    if mode == "custom":
        for rule in rules_input:
            rule_id = rule.get("ruleId") or _gen_rule_id()
            try:
                _config_table().put_item(Item={
                    "pk": f"DISPATCH_RULE#{rule_id}",
                    "rule_id": rule_id,
                    "event_type_pattern": rule["eventTypePattern"],
                    "event_categories": rule["eventCategories"],
                    "enabled": rule["enabled"],
                    "created_at": now,
                    "updated_at": now,
                })
                rules_written.append({
                    "ruleId": rule_id,
                    "eventTypePattern": rule["eventTypePattern"],
                    "eventCategories": rule["eventCategories"],
                    "enabled": rule["enabled"],
                })
            except ClientError:
                logger.exception("Failed to write dispatch rule %s", rule_id)
                return _error(500, "SYS_INTERNAL_ERROR", f"Failed to save rule '{rule_id}'.")

    warning = _dispatch_warning(mode, rules_written)
    return _success(200, {"data": {"mode": mode, "rules": rules_written, "warning": warning}})


# ===================================================================
# GET /api/config/dispatch
# ===================================================================

def handle_dispatch_get(event, context):
    """Return current dispatch configuration."""
    try:
        resp = _config_table().get_item(Key={"pk": "DISPATCH_PRESET"})
    except ClientError:
        logger.exception("Failed to read DISPATCH_PRESET")
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to read dispatch configuration.")

    preset = resp.get("Item")
    if not preset:
        return _success(200, {"data": {
            "mode": None,
            "rules": [],
            "actionabilityFilter": "all_actionable",
            "warning": _dispatch_warning(None, []),
        }})

    mode = preset.get("mode")
    rules = []
    if mode == "custom":
        rules = _scan_dispatch_rules()

    api_rules = [_rule_to_api(r) for r in rules]
    warning = _dispatch_warning(mode, rules)
    return _success(200, {"data": {
        "mode": mode,
        "rules": api_rules,
        "actionabilityFilter": preset.get("actionability_filter", "all_actionable"),
        "warning": warning,
    }})


# ===================================================================
# PUT /api/config/dispatch/rules/{ruleId}
# ===================================================================

def handle_dispatch_rule_update(event, context):
    """Update a single dispatch rule (partial update)."""
    rule_id = (event.get("pathParameters") or {}).get("ruleId", "")

    # Validate ruleId before pk construction
    if not validate_rule_id(rule_id):
        return _error(400, "CFG_INVALID_REQUEST",
                      "ruleId must match [a-zA-Z0-9_-] and be 1-64 chars.")

    body = _parse_body(event)
    if not body:
        return _error(400, "CFG_INVALID_REQUEST",
                      "Request body must contain at least one field to update.")

    # Validate provided fields
    if "eventTypePattern" in body:
        if not validate_dispatch_pattern(body["eventTypePattern"]):
            return _error(400, "CFG_INVALID_DISPATCH_PATTERN",
                          f"Event type pattern must start with 'AWS_'. Got: '{body['eventTypePattern']}'.")

    if "eventCategories" in body:
        if not validate_event_categories(body["eventCategories"]):
            invalid = [c for c in body["eventCategories"]
                       if c not in ("scheduledChange", "accountNotification")]
            msg = (f"Event category must be 'scheduledChange' or 'accountNotification'. Got: '{invalid[0]}'."
                   if invalid else "eventCategories must contain at least one category.")
            return _error(400, "CFG_INVALID_EVENT_CATEGORY", msg)

    if "enabled" in body and not isinstance(body["enabled"], bool):
        return _error(400, "CFG_INVALID_REQUEST", "'enabled' must be a boolean.")

    # Build update expression
    now = _now_iso()
    expr_parts = ["#ua = :ua"]
    names = {"#ua": "updated_at"}
    values = {":ua": now}

    field_map = {
        "eventTypePattern": "event_type_pattern",
        "eventCategories": "event_categories",
        "enabled": "enabled",
    }
    for api_field, db_field in field_map.items():
        if api_field in body:
            placeholder = f":{db_field.replace('_', '')}"
            names[f"#{db_field}"] = db_field
            values[placeholder] = body[api_field]
            expr_parts.append(f"#{db_field} = {placeholder}")

    try:
        resp = _config_table().update_item(
            Key={"pk": f"DISPATCH_RULE#{rule_id}"},
            UpdateExpression="SET " + ", ".join(expr_parts),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ConditionExpression="attribute_exists(pk)",
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _error(404, "CFG_RULE_NOT_FOUND", f"Dispatch rule '{rule_id}' not found.")
        logger.exception("Failed to update dispatch rule %s", rule_id)
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to update rule.")

    item = resp.get("Attributes", {})
    return _success(200, {"data": _rule_to_api(item)})


# ===================================================================
# DELETE /api/config/dispatch/rules/{ruleId}
# ===================================================================

def handle_dispatch_rule_delete(event, context):
    """Delete a single dispatch rule."""
    rule_id = (event.get("pathParameters") or {}).get("ruleId", "")

    # Validate ruleId before pk construction
    if not validate_rule_id(rule_id):
        return _error(400, "CFG_INVALID_REQUEST",
                      "ruleId must match [a-zA-Z0-9_-] and be 1-64 chars.")

    try:
        _config_table().delete_item(
            Key={"pk": f"DISPATCH_RULE#{rule_id}"},
            ConditionExpression="attribute_exists(pk)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _error(404, "CFG_RULE_NOT_FOUND", f"Dispatch rule '{rule_id}' not found.")
        logger.exception("Failed to delete dispatch rule %s", rule_id)
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to delete rule.")

    return _success(200, {"data": {"deleted": True, "ruleId": rule_id}})


# ===================================================================
# GET /api/config/status
# ===================================================================

def handle_config_status(event, context):
    """Full configuration status for Review & Activate step."""
    table = _config_table()

    # Batch read fixed keys
    # add SNOW_CONNECTION so readiness can gate on the operative
    # platform's connection without an extra round-trip. Platform resolution
    # itself routes through resolve_platforms() below (single source of truth).
    try:
        batch_resp = _dynamodb.batch_get_item(RequestItems={
            CONFIG_TABLE: {
                "Keys": [
                    {"pk": "JIRA_CONNECTION"},
                    {"pk": "SNOW_CONNECTION"},
                    {"pk": "ROUTING_DEFAULT"},
                    {"pk": "ROUTING_STRATEGY"},
                    {"pk": "DISPATCH_PRESET"},
                ],
            }
        })
    except ClientError:
        logger.exception("Failed to batch read config status")
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to read configuration.")

    items = {item["pk"]: item for item in batch_resp.get("Responses", {}).get(CONFIG_TABLE, [])}

    jira = items.get("JIRA_CONNECTION")
    snow = items.get("SNOW_CONNECTION")
    routing_default = items.get("ROUTING_DEFAULT")
    routing_strategy = items.get("ROUTING_STRATEGY")
    dispatch_preset = items.get("DISPATCH_PRESET")

    # resolve the operative platform via the shared
    # seam. resolve_platforms fails safe to ["jira"] on ClientError, so a
    # transient ConfigTable error routes readiness through the JIRA predicates
    # (which a SNOW-only deploy cannot satisfy) — never a spurious ready:true.
    platform = operative_platform(resolve_platforms(table))
    conn_item = snow if platform == "servicenow" else jira

    # Count account mappings and tag mappings
    account_count = 0
    tag_count = 0
    try:
        resp = table.scan(
            FilterExpression=Attr("pk").begins_with("ROUTING#"),
            Select="COUNT",
        )
        account_count = resp.get("Count", 0)

        resp = table.scan(
            FilterExpression=Attr("pk").begins_with("TAG_ROUTING#"),
            Select="COUNT",
        )
        tag_count = resp.get("Count", 0)
    except ClientError:
        logger.exception("Failed to count routing items")

    # Dispatch rule counts
    custom_rule_count = 0
    enabled_rule_count = 0
    dispatch_mode = dispatch_preset.get("mode") if dispatch_preset else None
    if dispatch_mode == "custom":
        rules = _scan_dispatch_rules()
        custom_rule_count = len(rules)
        enabled_rule_count = sum(1 for r in rules if r.get("enabled"))

    # Build response —: allowlist JIRA fields (no jira_secret_arn)
    # read ONLY the `validated` flag from the connection item; never
    # surface snow_secret_arn / secret / token / password (SNOW allowlist
    # mirrors the JIRA one — no new field added to the response).

    # per-platform connection predicate — bool(validated).
    # Absence or falsy `validated` -> not complete (no default-to-true).
    connection_complete = bool(conn_item and conn_item.get("validated"))
    # real-target predicate, not presence-only.
    # extract_routing_target returns None unless the platform's required field
    # is truthy (SNOW: snow_assignment_group_id; JIRA: jira_project), so a
    # stale/foreign ROUTING_DEFAULT cannot falsely satisfy readiness.
    routing_target = (
        extract_routing_target(routing_default, platform)
        if routing_default is not None
        else None
    )
    routing_complete = routing_target is not None
    dispatch_complete = dispatch_preset is not None

    # Retained for backward-compat response fields (jiraConnection{} block).
    jira_complete = bool(jira and jira.get("validated"))

    strategy = routing_strategy.get("mode") if routing_strategy else None
    routing_tag_key = routing_strategy.get("tag_key") if routing_strategy else None

    # platform-selected warning wording. JIRA/dual
    # branch is the VERBATIM legacy string set; SNOW-
    # only branch is ServiceNow-worded. W4/W5 (dispatch) are platform-agnostic.
    _WARN = {
        "jira": {
            "W1": "JIRA connection not configured. Complete Step 1 before activating.",
            "W2": "JIRA connection not validated. Test the connection before activating.",
            "W3": "Default JIRA project not configured. Tickets cannot be created until a default project is set.",
        },
        "servicenow": {
            "W1": "ServiceNow connection not configured. Complete Step 1 before activating.",
            "W2": "ServiceNow connection not validated. Test the connection before activating.",
            "W3": "Default ServiceNow assignment group not configured. Tickets cannot be created until a default assignment group is set.",
        },
    }
    w = _WARN[platform]

    warnings = []
    if not conn_item:
        warnings.append(w["W1"])
    elif not conn_item.get("validated"):
        warnings.append(w["W2"])
    if not routing_complete:
        warnings.append(w["W3"])
    if dispatch_mode is None:
        warnings.append("Dispatch window not configured. Using default: all actionable events will create tickets.")
    elif dispatch_mode == "custom" and enabled_rule_count == 0:
        warnings.append("No dispatch rules are enabled. No tickets will be created for any events.")

    ready = connection_complete and routing_complete

    return _success(200, {"data": {
        "jiraConnection": {
            "complete": jira_complete,
            "validated": bool(jira.get("validated")) if jira else False,
            "baseUrl": jira.get("jira_base_url", "") if jira else None,
            "validatedUser": jira.get("validated_user", "") if jira else None,
            "validatedAt": jira.get("validated_at", "") if jira else None,
        },
        "routing": {
            "complete": routing_complete,
            "strategy": strategy,
            "defaultProject": routing_default.get("jira_project") if routing_default else None,
            "snowAssignmentGroupId": routing_default.get("snow_assignment_group_id") if routing_default else None,
            "snowRecordType": routing_default.get("snow_record_type") if routing_default else None,
            "accountMappings": account_count,
            "tagMappings": tag_count,
            "routingTagKey": routing_tag_key,
        },
        "dispatch": {
            "complete": dispatch_complete,
            "mode": dispatch_mode,
            "customRuleCount": custom_rule_count,
            "enabledRuleCount": enabled_rule_count,
        },
        "ready": ready,
        "warnings": warnings,
    }})


# ===================================================================
# POST /api/config/activate
# ===================================================================

def handle_activate(event, context):
    """Validate prerequisites and activate the integration."""
    table = _config_table()

    # gate against the operative platform.
    # resolve_platforms fails safe to ["jira"] on ClientError, so a transient
    # read error routes gating through the JIRA predicates (unsatisfiable for a
    # SNOW-only deploy) and never activates on error.
    platform = operative_platform(resolve_platforms(table))

    # platform-aware activation error triplets. JIRA branch is
    # the VERBATIM legacy code/message/detail; SNOW
    # branch reuses CFG_SNOW_NOT_CONFIGURED (registered elsewhere) and
    # introduces CFG_SNOW_NOT_VALIDATED / CFG_DEFAULT_GROUP_MISSING as analogues.
    _ERR = {
        "jira": {
            "conn_pk": "JIRA_CONNECTION",
            "conn_missing": ("CFG_JIRA_NOT_CONFIGURED",
                             "JIRA connection not configured.",
                             "Complete Step 1 (JIRA Connection) before activating."),
            "conn_not_validated": ("CFG_JIRA_NOT_VALIDATED",
                                   "JIRA connection not validated.",
                                   "Test the JIRA connection in Step 1 before activating."),
            "routing_missing": ("CFG_DEFAULT_PROJECT_MISSING",
                                "Default JIRA project not configured.",
                                "Set a default project in Step 2 (Routing) before activating."),
        },
        "servicenow": {
            "conn_pk": "SNOW_CONNECTION",
            "conn_missing": ("CFG_SNOW_NOT_CONFIGURED",
                             "ServiceNow connection not configured.",
                             "Complete Step 1 (ServiceNow Connection) before activating."),
            "conn_not_validated": ("CFG_SNOW_NOT_VALIDATED",
                                   "ServiceNow connection not validated.",
                                   "Test the ServiceNow connection in Step 1 before activating."),
            "routing_missing": ("CFG_DEFAULT_GROUP_MISSING",
                                "Default ServiceNow assignment group not configured.",
                                "Set a default assignment group in Step 2 (Routing) before activating."),
        },
    }
    errspec = _ERR[platform]

    # Prerequisite 1: operative platform's connection configured and validated.
    # read only the `validated` flag; never surface/log the secret ARN.
    try:
        resp = table.get_item(Key={"pk": errspec["conn_pk"]})
    except ClientError:
        logger.exception("Failed to read %s", errspec["conn_pk"])
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to read configuration.")

    conn = resp.get("Item")
    if not conn:
        return _error(400, *errspec["conn_missing"])
    if not conn.get("validated"):
        return _error(400, *errspec["conn_not_validated"])

    # Prerequisite 2: default routing target present for THIS platform.
    # real target via extract_routing_target, not presence.
    try:
        resp = table.get_item(Key={"pk": "ROUTING_DEFAULT"})
    except ClientError:
        logger.exception("Failed to read ROUTING_DEFAULT")
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to read configuration.")

    routing_default = resp.get("Item")
    if routing_default is None or extract_routing_target(routing_default, platform) is None:
        return _error(400, *errspec["routing_missing"])

    # Auto-default dispatch if not configured (design)
    now = _now_iso()
    try:
        table.put_item(
            Item={"pk": "DISPATCH_PRESET", "mode": "all", "updated_at": now},
            ConditionExpression="attribute_not_exists(pk)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            logger.exception("Failed to write default DISPATCH_PRESET")

    # Read final dispatch mode
    try:
        resp = table.get_item(Key={"pk": "DISPATCH_PRESET"})
        dispatch_mode = resp.get("Item", {}).get("mode", "all")
    except ClientError:
        dispatch_mode = "all"

    # Count mappings for summary
    account_count = 0
    tag_count = 0
    try:
        r = table.scan(FilterExpression=Attr("pk").begins_with("ROUTING#"), Select="COUNT")
        account_count = r.get("Count", 0)
        r = table.scan(FilterExpression=Attr("pk").begins_with("TAG_ROUTING#"), Select="COUNT")
        tag_count = r.get("Count", 0)
    except ClientError:
        pass

    return _success(200, {"data": {
        "activated": True,
        "activatedAt": now,
        "dispatchMode": dispatch_mode,
        "summary": {
            # `conn` is the operative platform's connection item.
            # For JIRA/dual this is JIRA_CONNECTION (byte-identical to legacy);
            # for SNOW-only it lacks jira_base_url -> "". Shape
            # unchanged; secret ARN never read.
            "jiraBaseUrl": conn.get("jira_base_url", ""),
            "defaultProject": routing_default.get("jira_project", ""),
            "snowAssignmentGroupId": routing_default.get("snow_assignment_group_id", ""),
            "snowRecordType": routing_default.get("snow_record_type", ""),
            "accountMappings": account_count,
            "tagMappings": tag_count,
            "dispatchMode": dispatch_mode,
        },
    }})
