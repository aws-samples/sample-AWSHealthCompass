"""Tag routing configuration API handlers (STORY-029).

Implements 5 endpoints for tag-based routing:
  POST   /api/config/routing/strategy      — Set routing strategy (account or tag)
  GET    /api/config/routing/tags           — List all tag routing mappings
  POST   /api/config/routing/tags           — Create/update tag routing mappings
  DELETE /api/config/routing/tags/{tagValue} — Delete single tag mapping
  GET    /api/config/routing/tag-preview    — Preview tag value distribution
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
from collections import Counter
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

try:
    from routing_handlers import (
        _config_table,
        _error,
        _get_jira_credentials,
        _get_snow_client_or_none,
        _now_iso,
        _parse_body,
        _success,
        _validate_jira_project,
    )
except ImportError:
    from lambdas.api.routing_handlers import (
        _config_table,
        _error,
        _get_jira_credentials,
        _get_snow_client_or_none,
        _now_iso,
        _parse_body,
        _success,
        _validate_jira_project,
    )

# STORY-137: SNOW format validator (call site only; body unchanged — MUST-140-5).
try:
    from validators import validate_snow_routing_fields
except ImportError:
    from lambdas.api.validators import validate_snow_routing_fields

# STORY-136 seam (§2.1): resolve the operative platform. This handler MUST NOT
# read ITSM_PLATFORM directly or re-derive precedence (Dumbledore §1, §8.2-1).
try:
    from resolve_core.config_schema import operative_platform, resolve_platforms
except ImportError:
    from lambdas.shared.python.resolve_core.config_schema import (
        operative_platform,
        resolve_platforms,
    )

logger = logging.getLogger()

TAG_ROUTING_PREFIX = "TAG_ROUTING#"

CAMPAIGNS_TABLE = os.environ.get("CAMPAIGNS_TABLE", "compass-campaigns")
RESOURCES_TABLE = os.environ.get("RESOURCES_TABLE", "compass-resources")

_dynamodb = boto3.resource("dynamodb")

_VALID_MODES = ("account", "tag")
_VALID_TAG_SOURCES = ("resource", "account", "both")
_MAX_TAG_KEY_LEN = 128

# ---------------------------------------------------------------------------
# STORY-125 (SR-125-1/2/3/14/15): server-authoritative validation of the
# user-controlled tag VALUE and issue TYPE.
#
# The tag value becomes the ``TAG_ROUTING#{value}`` DynamoDB partition-key
# suffix AND can flow downstream into JIRA labels/JQL/CloudWatch logs, so it is
# treated as hostile input. The client (React) is NOT the trust boundary — the
# endpoints are reachable by hand-crafted HTTP — so this validation is
# authoritative and must behave correctly regardless of any client-side check.
#
# Key faithfulness (SR-125-3): the value is validated and REJECTED on failure,
# never silently sanitized/transformed before storage. It is persisted verbatim
# (after ``.strip()`` only) so it exactly equals what
# ``resolve_core.routing.extract_tag_value(...)`` produces at routing time
# (which also only ``.strip()``s). Applying a label/charset transform to the
# stored key would silently break routing matches for real AWS tag values that
# contain uppercase, spaces, ``=``, ``:`` or ``/``.
_MAX_TAG_VALUE_LEN = 256  # SR-125-1/15: authoritative user-facing char bound
_MAX_PK_BYTES = 2048  # DynamoDB hard partition-key byte limit (backstop only)
_MAX_ISSUE_TYPE_LEN = 128  # SR-125-14: bound the client-controlled issue type
# SR-125-2: reject C0 controls (incl. \t \n \r), DEL (\x7f) and C1 controls.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# SR-125-5: cap sanitized log values (mirrors resolve_core.campaign primitive).
_MAX_LOG_VALUE_LEN = 256


def _sanitize_log(val) -> str:
    """Strip control chars and truncate a value for safe log output (SR-125-5).

    Prevents CloudWatch log-line forgery when a user-controlled value (or a
    ``pk`` embedding one) is logged. Mirrors ``resolve_core.campaign._sanitize_log``.
    """
    text = str(val) if val is not None else ""
    text = text.replace("\n", "").replace("\r", "").replace("\x00", "")
    return text[:_MAX_LOG_VALUE_LEN]


def _tag_value_rejection(tag_value: str) -> str | None:
    """Return a neutral rejection reason for a tag value, or None if acceptable.

    Enforces SR-125-1 (length) and SR-125-2 (control chars). Does NOT mutate the
    value — the caller stores it verbatim after ``.strip()`` (SR-125-3).
    """
    if _CONTROL_CHAR_RE.search(tag_value):
        return "tagValue contains control characters"
    if len(tag_value) > _MAX_TAG_VALUE_LEN:
        return f"tagValue exceeds {_MAX_TAG_VALUE_LEN} characters"
    # Hard backstop: the composed pk must never exceed DynamoDB's 2048-byte key
    # limit regardless of multibyte content (an over-length pk would otherwise
    # surface as an opaque write failure).
    if len((TAG_ROUTING_PREFIX + tag_value).encode("utf-8")) > _MAX_PK_BYTES:
        return f"tagValue exceeds {_MAX_TAG_VALUE_LEN} characters"
    return None


def _issue_type_rejection(issue_type: str) -> str | None:
    """Return a neutral rejection reason for a JIRA issue type, or None (SR-125-14).

    LOW severity: issue type does not reach the pk/JQL/labels/unsanitized logs
    and is JSON-escaped at ticket-create time. This bound is defense-in-depth
    (prevents item bloat / stray control chars). Issue-type existence validation
    against JIRA is intentionally NOT done here (follow-on, not RT-03).
    """
    if _CONTROL_CHAR_RE.search(issue_type):
        return "jiraIssueType contains control characters"
    if len(issue_type) > _MAX_ISSUE_TYPE_LEN:
        return f"jiraIssueType exceeds {_MAX_ISSUE_TYPE_LEN} characters"
    return None


# ===================================================================
# POST /api/config/routing/strategy
# ===================================================================

def handle_routing_strategy(event, context):
    """Save routing strategy (account-based or tag-based)."""
    body = _parse_body(event)
    if body is None:
        return _error(400, "CFG_INVALID_REQUEST", "Request body must be valid JSON")

    mode = body.get("mode", "").strip().lower()
    if mode not in _VALID_MODES:
        return _error(400, "CFG_INVALID_REQUEST",
                      f"mode must be one of: {', '.join(_VALID_MODES)}")

    tag_key = body.get("tagKey", "").strip()
    tag_source = body.get("tagSource", "account").strip().lower()

    if mode == "tag":
        if not tag_key:
            return _error(400, "CFG_INVALID_REQUEST",
                          "tagKey is required when mode is 'tag'")
        if len(tag_key) > _MAX_TAG_KEY_LEN:
            return _error(400, "CFG_INVALID_REQUEST",
                          f"tagKey exceeds {_MAX_TAG_KEY_LEN} characters")

    if tag_source not in _VALID_TAG_SOURCES:
        return _error(400, "CFG_INVALID_REQUEST",
                      f"tagSource must be one of: {', '.join(_VALID_TAG_SOURCES)}")

    now = _now_iso()
    try:
        _config_table().put_item(Item={
            "pk": "ROUTING_STRATEGY",
            "mode": mode,
            "tag_key": tag_key if mode == "tag" else "",
            "tag_source": tag_source if mode == "tag" else "",
            "updated_at": now,
        })
    except ClientError:
        logger.exception("DynamoDB write failed for ROUTING_STRATEGY")
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to save routing strategy.")

    return _success(200, {
        "mode": mode,
        "tagKey": tag_key if mode == "tag" else None,
        "tagSource": tag_source if mode == "tag" else None,
        "updatedAt": now,
    })


# ===================================================================
# GET /api/config/routing/tags
# ===================================================================

def handle_tag_mappings_get(event, context):
    """List all tag routing mappings."""
    table = _config_table()
    try:
        response = table.scan(
            FilterExpression="begins_with(pk, :prefix)",
            ExpressionAttributeValues={":prefix": TAG_ROUTING_PREFIX},
        )
        items = response.get("Items", [])
        while response.get("LastEvaluatedKey"):
            response = table.scan(
                FilterExpression="begins_with(pk, :prefix)",
                ExpressionAttributeValues={":prefix": TAG_ROUTING_PREFIX},
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            items.extend(response.get("Items", []))
    except ClientError:
        logger.exception("DynamoDB scan failed for tag routing mappings")
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to read tag routing mappings.")

    mappings = []
    for item in items:
        mappings.append({
            "tagValue": item.get("tag_value", ""),
            "jiraProject": item.get("jira_project", ""),
            "jiraIssueType": item.get("jira_issue_type", "Task"),
            # STORY-140 (AC-140.5): additive SNOW target keys. Empty-string when
            # absent, never null (mirrors STORY-137 §3.2 / Luna §6.6). No shape
            # breakage for existing JIRA consumers (AC-140.6).
            "snowAssignmentGroupId": item.get("snow_assignment_group_id", ""),
            "snowAssignmentGroupName": item.get("snow_assignment_group_name", ""),
            "snowRecordType": item.get("snow_record_type", "change_request"),
            "updatedAt": item.get("updated_at", ""),
        })

    mappings.sort(key=lambda x: x["tagValue"])

    return _success(200, {"mappings": mappings, "total": len(mappings)})


# ===================================================================
# POST /api/config/routing/tags
# ===================================================================

def handle_tag_mappings_save(event, context):
    """Create or update tag routing mappings (platform-aware — STORY-140).

    Consumes the STORY-136 seam to resolve the operative platform, then branches:
      - "jira": byte-identical to pre-epic (JIRA-cred gate, jiraProject required,
        _validate_jira_project, tag_value/jira_project/jira_issue_type persist).
      - "servicenow": no JIRA-cred gate, no jiraProject requirement; requires
        snowAssignmentGroupId; format via validate_snow_routing_fields; existence
        via _get_snow_client_or_none() + validate_routing_target; persists the
        snow_* shape the routing engine already reads (routing.py — NO engine
        change, AC-140.4/§3).
    """
    body = _parse_body(event)
    if body is None:
        return _error(400, "CFG_INVALID_REQUEST", "Request body must be valid JSON")

    mappings = body.get("mappings")
    if not mappings or not isinstance(mappings, list):
        return _error(400, "CFG_INVALID_REQUEST",
                      "mappings array is required")

    # STORY-136 seam (§2.1): resolve the operative platform ONCE, before the row
    # loop. dual/absent -> "jira" (operative_platform rule). Never read
    # ITSM_PLATFORM directly (Dumbledore §1 consumption boundary).
    platform = operative_platform(resolve_platforms(_config_table()))

    if platform == "servicenow":
        return _save_tag_mappings_servicenow(mappings)
    return _save_tag_mappings_jira(mappings)


def _save_tag_mappings_jira(mappings):
    """JIRA branch — BYTE-IDENTICAL to the pre-epic save path (AC-140.6)."""
    # Validate JIRA connection
    jira_creds = _get_jira_credentials()
    if not jira_creds:
        return _error(400, "CFG_JIRA_NOT_CONFIGURED",
                      "JIRA connection must be configured before setting tag routing.")

    # Validate unique project keys against JIRA
    unique_projects = set()
    for m in mappings:
        proj = m.get("jiraProject", "").strip().upper()
        if proj:
            unique_projects.add(proj)

    project_valid = {}
    for proj in unique_projects:
        result = _validate_jira_project(*jira_creds, proj)
        project_valid[proj] = result

    # Process each mapping
    created = 0
    updated = 0
    validation_errors = []
    now = _now_iso()
    table = _config_table()

    for m in mappings:
        tag_value = m.get("tagValue", "").strip()
        jira_project = m.get("jiraProject", "").strip().upper()
        jira_issue_type = m.get("jiraIssueType", "Task").strip()

        if not tag_value:
            validation_errors.append({"tagValue": tag_value, "reason": "tagValue is required"})
            continue

        # SR-125-1/2/9: server-authoritative tag-value validation (length +
        # control chars). Runs AFTER the empty-check and BEFORE JIRA project
        # validation so it never disturbs the _validate_jira_project ordering
        # (SR-125-8). Rejections are per-row (SR-125-4), never a blanket 400.
        tag_reason = _tag_value_rejection(tag_value)
        if tag_reason:
            validation_errors.append({"tagValue": tag_value, "reason": tag_reason})
            continue

        # SR-125-14: bound the client-controlled issue type (LOW, defense-in-depth).
        issue_type_reason = _issue_type_rejection(jira_issue_type)
        if issue_type_reason:
            validation_errors.append({"tagValue": tag_value, "reason": issue_type_reason})
            continue

        if not jira_project:
            validation_errors.append({"tagValue": tag_value, "reason": "jiraProject is required"})
            continue

        proj_result = project_valid.get(jira_project, {})
        if not proj_result.get("valid"):
            validation_errors.append({
                "tagValue": tag_value,
                "jiraProject": jira_project,
                "reason": proj_result.get("reason", "Invalid JIRA project"),
            })
            continue

        # Write to DynamoDB. SR-125-3: tag_value stored VERBATIM (strip only),
        # never label/charset-transformed, so the stored key matches the
        # read-side extracted value exactly.
        pk = f"{TAG_ROUTING_PREFIX}{tag_value}"
        try:
            resp = table.put_item(
                Item={
                    "pk": pk,
                    "tag_value": tag_value,
                    "jira_project": jira_project,
                    "jira_issue_type": jira_issue_type,
                    "updated_at": now,
                },
                ReturnValues="ALL_OLD",
            )
            if resp.get("Attributes"):
                updated += 1
            else:
                created += 1
        except ClientError:
            # SR-125-5: sanitize the user-controlled pk before logging to
            # prevent CloudWatch log-line forgery. Client sees a generic reason.
            logger.exception("DynamoDB write failed for %s", _sanitize_log(pk))
            validation_errors.append({"tagValue": tag_value, "reason": "Database write failed"})

    return _success(200, {
        "created": created,
        "updated": updated,
        "validationErrors": validation_errors,
    })


def _save_tag_mappings_servicenow(mappings):
    """ServiceNow branch (STORY-140 §2.2) — SNOW target capture on TAG_ROUTING#.

    - NO _get_jira_credentials() gate; NO jiraProject requirement (AC-140.2).
    - Top-level 400 CFG_SNOW_NOT_CONFIGURED when no validated SNOW connection
      (§2.3 precondition — the ServiceNow twin of the JIRA-cred gate).
    - Per-row: SR-125 tagValue first (MUST-140-1) -> SNOW format
      (validate_snow_routing_fields + snowAssignmentGroupId presence) -> SNOW
      existence (validate_routing_target). Format-before-existence (§2.4).
    - Each unique sys_id validated for existence at most once per request
      (§4.3), mirroring the JIRA unique-project pre-pass.
    - Persists snow_assignment_group_id / snow_assignment_group_name /
      snow_record_type — the EXACT snake_case shape routing.py already reads
      (§3). No engine change, no new error code.
    """
    # §2.3 precondition — no validated SNOW connection is a whole-request
    # failure, not a per-row one. Top-level 400, never CFG_JIRA_NOT_CONFIGURED.
    snow_client = _get_snow_client_or_none()
    if snow_client is None:
        return _error(400, "CFG_SNOW_NOT_CONFIGURED",
                      "ServiceNow connection must be configured and validated "
                      "before saving tag routing.")

    created = 0
    updated = 0
    validation_errors = []
    now = _now_iso()
    table = _config_table()

    # §4.3 / MUST-140-3: cache existence results per unique sys_id so a repeated
    # target is validated (and its sys_user_group looked up) at most once.
    existence_cache: dict[str, bool] = {}

    for m in mappings:
        tag_value = m.get("tagValue", "").strip()
        group_id = m.get("snowAssignmentGroupId", "").strip()
        group_name = m.get("snowAssignmentGroupName", "").strip()
        record_type = m.get("snowRecordType", "").strip() or "change_request"

        # 1) empty-check + SR-125 tag-value validation — UNCHANGED and
        # platform-independent (MUST-140-1). Runs FIRST, before any target work.
        if not tag_value:
            validation_errors.append({"tagValue": tag_value, "reason": "tagValue is required"})
            continue
        tag_reason = _tag_value_rejection(tag_value)
        if tag_reason:
            validation_errors.append({"tagValue": tag_value, "reason": tag_reason})
            continue

        # 2) SNOW FORMAT layer — reuse the existing validator (body unchanged,
        # MUST-140-5). Cheap, no network call. SNOW-worded codes only.
        fmt_errors = validate_snow_routing_fields(m)
        if fmt_errors:
            validation_errors.append({
                "tagValue": tag_value,
                "field": "snowAssignmentGroupId",
                "code": fmt_errors[0]["code"],
                "reason": fmt_errors[0]["message"],
            })
            continue

        # snowAssignmentGroupId is REQUIRED on the SNOW branch (jiraProject is
        # never required here — AC-140.2). validate_snow_routing_fields only
        # validates when present, so enforce presence explicitly.
        if not group_id:
            validation_errors.append({
                "tagValue": tag_value,
                "field": "snowAssignmentGroupId",
                "code": "CFG_INVALID_SNOW_GROUP_ID",
                "reason": "snowAssignmentGroupId is required",
            })
            continue

        # 3) SNOW EXISTENCE layer — DD-STRUCT-7 (§4, recommended). Only after
        # format passes (§2.4). Fail-closed per row (Snape MUST-13). Unique
        # sys_id validated once (existence_cache).
        if group_id not in existence_cache:
            try:
                result = snow_client.validate_routing_target(group_id)
                existence_cache[group_id] = bool(getattr(result, "valid", False))
            except Exception:
                # Do NOT surface raw upstream detail (Snape MUST-9). Fail closed.
                logger.exception(
                    "ServiceNow target validation raised for tag save %s",
                    _sanitize_log(group_id),
                )
                existence_cache[group_id] = False
        if not existence_cache[group_id]:
            validation_errors.append({
                "tagValue": tag_value,
                "field": "snowAssignmentGroupId",
                "code": "CFG_SNOW_GROUP_NOT_FOUND",
                "reason": f"ServiceNow assignment group '{group_id}' not found "
                          "in the connected ServiceNow instance.",
            })
            continue

        # 4) Persist the engine-consumed shape (§3, AC-140.4). No jira_project
        # written for a SNOW-only row. tag_value stored VERBATIM (SR-125-3);
        # used ONLY as a literal attribute value under the fixed TAG_ROUTING#
        # prefix, never in a query expression (MUST-140-2).
        pk = f"{TAG_ROUTING_PREFIX}{tag_value}"
        item = {
            "pk": pk,
            "tag_value": tag_value,
            "snow_assignment_group_id": group_id,
            "snow_record_type": record_type,
            "updated_at": now,
        }
        if group_name:
            item["snow_assignment_group_name"] = group_name
        try:
            resp = table.put_item(Item=item, ReturnValues="ALL_OLD")
            if resp.get("Attributes"):
                updated += 1
            else:
                created += 1
        except ClientError:
            logger.exception("DynamoDB write failed for %s", _sanitize_log(pk))
            validation_errors.append({"tagValue": tag_value, "reason": "Database write failed"})

    return _success(200, {
        "created": created,
        "updated": updated,
        "validationErrors": validation_errors,
    })


# ===================================================================
# DELETE /api/config/routing/tags/{tagValue}
# ===================================================================

def handle_tag_mapping_delete(event, context):
    """Delete a single tag routing mapping."""
    path_params = event.get("pathParameters") or {}
    raw_value = path_params.get("tagValue", "")
    tag_value = urllib.parse.unquote(raw_value)

    if not tag_value:
        return _error(400, "CFG_INVALID_REQUEST", "tagValue path parameter is required")

    pk = f"{TAG_ROUTING_PREFIX}{tag_value}"

    try:
        resp = _config_table().delete_item(
            Key={"pk": pk},
            ReturnValues="ALL_OLD",
        )
    except ClientError:
        # SR-125-5: the delete path decodes a URL-encoded tagValue that can
        # contain newlines; sanitize before logging to prevent log forgery.
        logger.exception("DynamoDB delete failed for %s", _sanitize_log(pk))
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to delete tag routing mapping.")

    if not resp.get("Attributes"):
        return _error(404, "CFG_TAG_ROUTING_NOT_FOUND",
                      f"No tag routing mapping found for value '{tag_value}'")

    return _success(200, {"deleted": True, "tagValue": tag_value})


# ===================================================================
# GET /api/config/routing/tag-preview
# ===================================================================

def handle_tag_preview(event, context):
    """Preview tag value distribution across campaigns/resources."""
    params = event.get("queryStringParameters") or {}
    key = params.get("key", "").strip()
    source = params.get("source", "account").strip().lower()

    if not key:
        return _error(400, "CFG_INVALID_REQUEST", "key query parameter is required")

    if len(key) > _MAX_TAG_KEY_LEN:
        return _error(400, "CFG_INVALID_REQUEST",
                      f"key exceeds {_MAX_TAG_KEY_LEN} characters")

    if source not in _VALID_TAG_SOURCES:
        return _error(400, "CFG_INVALID_REQUEST",
                      f"source must be one of: {', '.join(_VALID_TAG_SOURCES)}")

    counts: Counter = Counter()
    total_scanned = 0
    untagged_count = 0
    warning = None

    try:
        if source in ("account", "both"):
            scanned, untagged, tag_counts = _scan_account_tags(key)
            total_scanned += scanned
            untagged_count += untagged
            counts.update(tag_counts)

        if source in ("resource", "both"):
            scanned, untagged, tag_counts = _scan_resource_tags(key)
            total_scanned += scanned
            untagged_count += untagged
            counts.update(tag_counts)
    except ClientError:
        logger.exception("DynamoDB scan failed for tag preview")
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to scan for tag values.")

    if total_scanned == 0:
        warning = "No data found. Ingest Health events first."

    values = [{"value": v, "count": c} for v, c in counts.most_common()]

    return _success(200, {
        "values": values,
        "totalScanned": total_scanned,
        "untaggedCount": untagged_count,
        "warning": warning,
    })


def _scan_account_tags(key: str) -> tuple[int, int, Counter]:
    """Scan campaigns table for accountTags[key] values."""
    table = _dynamodb.Table(CAMPAIGNS_TABLE)
    counts: Counter = Counter()
    total = 0
    untagged = 0

    response = table.scan(ProjectionExpression="accountTags")
    while True:
        for item in response.get("Items", []):
            total += 1
            tags = item.get("accountTags") or {}
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except (json.JSONDecodeError, TypeError):
                    tags = {}
            value = tags.get(key)
            if value:
                counts[value] += 1
            else:
                untagged += 1

        if not response.get("LastEvaluatedKey"):
            break
        response = table.scan(
            ProjectionExpression="accountTags",
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )

    return total, untagged, counts


def _scan_resource_tags(key: str) -> tuple[int, int, Counter]:
    """Scan resources table for resourceTags[key] values."""
    table = _dynamodb.Table(RESOURCES_TABLE)
    counts: Counter = Counter()
    total = 0
    untagged = 0

    response = table.scan(ProjectionExpression="resourceTags")
    while True:
        for item in response.get("Items", []):
            total += 1
            tags = item.get("resourceTags") or {}
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except (json.JSONDecodeError, TypeError):
                    tags = {}
            value = tags.get(key)
            if value:
                counts[value] += 1
            else:
                untagged += 1

        if not response.get("LastEvaluatedKey"):
            break
        response = table.scan(
            ProjectionExpression="resourceTags",
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )

    return total, untagged, counts
