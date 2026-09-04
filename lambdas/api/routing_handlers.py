"""Routing configuration API handlers (STORY-028).

Implements 7 endpoints for account routing and bulk import:
  POST /api/config/routing/default
  POST /api/config/routing/accounts
  POST /api/config/routing/import
  POST /api/config/routing/import/confirm
  POST /api/config/routing/discover
  GET  /api/config/routing
  DELETE /api/config/routing/accounts/{accountId}

Security notes:
  H-1: ROUTING_ACCOUNT_PREFIX constant guards batch deletes.
  H-2: Write-before-delete on import confirm (non-atomic accepted for Alpha).
  M-1: CSV parsed with csv.reader, not str.split.
  M-2: Body size check rejects >5MB before parsing.
  M-4: Account emails stripped from /discover response.
"""
from __future__ import annotations

import base64
import csv
import io
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

import boto3
import urllib3
from botocore.exceptions import ClientError

# STORY-136: shared platform-resolution seam (single source of truth).
# STORY-137: also imports PK_SNOW_CONNECTION for DD-STRUCT-7 SNOW target
# existence validation at save time (reuses the SNOW_CONNECTION config item).
try:
    from resolve_core.config_schema import (
        PK_SNOW_CONNECTION,
        operative_platform,
        resolve_platforms,
    )
except ImportError:  # pragma: no cover — test/runtime path fallback
    from lambdas.shared.python.resolve_core.config_schema import (
        PK_SNOW_CONNECTION,
        operative_platform,
        resolve_platforms,
    )

logger = logging.getLogger()

# H-1: Constant prefix for account routing items — assert before batch delete
ROUTING_ACCOUNT_PREFIX = "ROUTING#"

# M-2: Max body size (5 MB)
_MAX_BODY_SIZE = 5_242_880

# Import preview TTL: 15 minutes
_IMPORT_TTL_SECONDS = 900

# STORY-120 (Snape Finding 3/5): ServiceNow sys_id must be a 32-char lowercase
# hex string; cap length defensively before regex match to bound worst-case
# input size handled per mapping row.
_SNOW_SYS_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_MAX_SNOW_GROUP_ID_LENGTH = 64

CONFIG_TABLE = os.environ.get("CONFIG_TABLE", "compass-config")
JIRA_SECRET_ARN = os.environ.get("JIRA_SECRET_ARN", "")
JIRA_SECRET_NAME = "compass/jira-credentials"  # nosec B105 — Secrets Manager logical path, not a credential

_dynamodb = boto3.resource("dynamodb")
_secrets = boto3.client("secretsmanager")  # AWS Secrets Manager client
_orgs = boto3.client("organizations")


# ===================================================================
# Helpers (reuse handler.py patterns)
# ===================================================================

def _config_table():
    """Return DynamoDB Table resource for ConfigTable."""
    return _dynamodb.Table(CONFIG_TABLE)


def _now_iso() -> str:
    """Current UTC time in ISO 8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_epoch() -> int:
    """Current UTC time as epoch seconds."""
    return int(datetime.now(timezone.utc).timestamp())


def _success(status_code: int, body: dict) -> dict:
    cors_origin = os.environ.get("CORS_ALLOW_ORIGIN", "*")
    return {
        "statusCode": status_code,
        "headers": {
            "Access-Control-Allow-Origin": cors_origin,
            "Access-Control-Allow-Headers": "Content-Type, x-api-key, Authorization",
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
            "Content-Type": "application/json",
        },
        "body": json.dumps(body),
    }


def _error(status_code: int, code: str, message: str) -> dict:
    cors_origin = os.environ.get("CORS_ALLOW_ORIGIN", "*")
    return {
        "statusCode": status_code,
        "headers": {
            "Access-Control-Allow-Origin": cors_origin,
            "Access-Control-Allow-Headers": "Content-Type, x-api-key, Authorization",
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
            "Content-Type": "application/json",
        },
        "body": json.dumps({"error": {"code": code, "message": message}}),
    }


def _parse_body(event: dict) -> dict | None:
    """Parse JSON body from API Gateway event. Returns None on failure."""
    raw = event.get("body")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _read_secret(secret_arn: str) -> dict | None:
    """Read JIRA credentials from Secrets Manager. Returns parsed dict or None."""
    try:
        resp = _secrets.get_secret_value(SecretId=secret_arn or JIRA_SECRET_NAME)
        return json.loads(resp["SecretString"])
    except (ClientError, json.JSONDecodeError, KeyError):
        logger.exception("Secrets Manager read failed")
        return None


def _validate_jira_project(base_url: str, email: str, api_token: str, project_key: str) -> dict:
    """Validate a JIRA project key exists via GET /rest/api/3/project/{key}.

    Returns:
        {"valid": True, "name": "..."} or {"valid": False, "reason": "..."}
    """
    url = f"{base_url}/rest/api/3/project/{project_key}"
    auth_bytes = base64.b64encode(f"{email}:{api_token}".encode("utf-8")).decode("ascii")
    headers = {
        "Authorization": f"Basic {auth_bytes}",
        "Accept": "application/json",
    }

    http = urllib3.PoolManager(
        timeout=urllib3.Timeout(connect=5.0, read=10.0),
        retries=False,
    )

    try:
        response = http.request("GET", url, headers=headers, redirect=False)
    except Exception:
        return {"valid": False, "reason": "Could not connect to JIRA"}

    if response.status == 200:
        try:
            data = json.loads(response.data.decode("utf-8"))
            return {"valid": True, "name": data.get("name", project_key)}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {"valid": True, "name": project_key}

    if response.status == 404:
        return {"valid": False, "reason": f"Project '{project_key}' not found in JIRA"}

    if response.status in (401, 403):
        return {"valid": False, "reason": "JIRA authentication failed"}

    if response.status == 429:
        return {"valid": None, "reason": "Rate limited — retry later"}

    return {"valid": False, "reason": f"JIRA returned status {response.status}"}


def _get_jira_credentials() -> tuple[str, str, str] | None:
    """Load JIRA base_url and credentials. Returns (base_url, email, token) or None."""
    try:
        resp = _config_table().get_item(Key={"pk": "JIRA_CONNECTION"})
        item = resp.get("Item")
        if not item or not item.get("validated"):
            return None
    except ClientError:
        return None

    base_url = item.get("jira_base_url", "")
    secret_arn = item.get("jira_secret_arn", "")
    creds = _read_secret(secret_arn)
    if not creds:
        return None

    return base_url, creds["email"], creds["api_token"]


# ===================================================================
# STORY-137 (DD-STRUCT-7): ServiceNow routing-target validation at save
#
# The ServiceNow twin of _get_jira_credentials / _validate_jira_project.
# Reuses the ALREADY-implemented ServiceNowClient.validate_routing_target
# (format `^[a-f0-9]{32}$` check + GET /api/now/table/sys_user_group/{sys_id}
# existence) via the SNOW_CONNECTION config item — no new IAM, no new secret,
# no alternate HTTP path (Snape MUST-15). The SNOW-secret read is already
# granted to the API Lambda (api_stack.py grant_read).
# ===================================================================

def _get_snow_client_or_none():
    """Build a ServiceNowClient from the SNOW_CONNECTION config item.

    Returns the client, or None if ServiceNow is not configured/validated.
    Mirrors servicenow_integration/handler.py:_get_client. Reads ONLY the
    SNOW_CONNECTION ConfigTable item plus the already-granted SNOW secret
    (Snape MUST-8: no new config item, no new IAM/env/secret/network).
    """
    try:
        conn = _config_table().get_item(Key={"pk": PK_SNOW_CONNECTION}).get("Item")
    except ClientError:
        logger.exception("ConfigTable read failed for SNOW_CONNECTION")
        return None

    # Require a validated connection — the ServiceNow twin of the JIRA
    # "validated" precondition in _get_jira_credentials.
    if not conn or not conn.get("validated"):
        return None

    instance_url = conn.get("instance_url", "")
    secret_arn = conn.get("secret_arn", "") or os.environ.get("SERVICENOW_SECRET_ARN", "")
    record_type = conn.get("record_type", "change_request")
    if not instance_url or not secret_arn:
        return None

    try:
        from resolve_core.servicenow_client import ServiceNowClient
        from resolve_core.servicenow_formatter import ServiceNowFormatter
    except ImportError:  # pragma: no cover — test/runtime path fallback
        from lambdas.shared.python.resolve_core.servicenow_client import ServiceNowClient
        from lambdas.shared.python.resolve_core.servicenow_formatter import ServiceNowFormatter

    try:
        return ServiceNowClient(
            instance_url=instance_url,
            secret_arn=secret_arn,
            formatter=ServiceNowFormatter(),
            record_type=record_type,
        )
    except Exception:
        # _validate_snow_url or client construction rejected the config
        # (e.g. non-service-now.com host). Treat as "not usable" → fail closed.
        logger.exception("ServiceNowClient construction failed for routing validation")
        return None


def _validate_snow_target_or_error(group_id: str):
    """DD-STRUCT-7 existence check for a single SNOW assignment-group sys_id.

    Returns None on success, or an _error(...) response on failure.

    Precondition (Snape MUST-1): the caller MUST have already run the pure
    format validators (validate_routing_*), so a malformed sys_id never
    reaches this network path. ServiceNowClient.validate_routing_target
    re-checks the 32-hex format as defense-in-depth before any GET.

    Error mapping (Luna §6.4, Snape MUST-9 — never JIRA-worded, never raw
    upstream detail):
      - no validated SNOW connection -> CFG_SNOW_NOT_CONFIGURED
      - not found / API error / transient failure -> CFG_SNOW_GROUP_NOT_FOUND
    """
    client = _get_snow_client_or_none()
    if client is None:
        # Edge §5.A — SNOW-only but SNOW not connected. The ServiceNow twin of
        # CFG_JIRA_NOT_CONFIGURED (Snape MUST-11).
        return _error(400, "CFG_SNOW_NOT_CONFIGURED",
                      "ServiceNow connection must be configured and validated "
                      "before saving routing.")

    try:
        result = client.validate_routing_target(group_id)
    except Exception:
        # Fail closed on any client-level failure (transient/5xx after retries,
        # connection error) — never fall through to persist (Snape MUST-13).
        # Do NOT surface raw upstream detail (Snape MUST-9).
        logger.exception("ServiceNow target validation raised for a routing save")
        return _error(400, "CFG_SNOW_GROUP_NOT_FOUND",
                      f"ServiceNow assignment group '{group_id}' could not be "
                      "validated in the connected ServiceNow instance.")

    if not result.valid:
        return _error(400, "CFG_SNOW_GROUP_NOT_FOUND",
                      f"ServiceNow assignment group '{group_id}' not found in "
                      "the connected ServiceNow instance.")
    return None


# ===================================================================
# POST /api/config/routing/default — Set default routing project
# ===================================================================

def handle_routing_default(event, context):
    """Save default routing (orphan queue) project.

    Design: BRD §14.2 ROUTING_DEFAULT, §14.3 step 3.
    BUG-S23-002: Platform-aware validation — skip JIRA API call for ServiceNow.
    """
    try:
        from validators import validate_routing_default
    except ImportError:
        from lambdas.api.validators import validate_routing_default

    body = _parse_body(event)
    if body is None:
        return _error(400, "CFG_INVALID_REQUEST", "Request body must be valid JSON")

    # BUG-S23-002: Read active platform to determine validation path
    platform = operative_platform(resolve_platforms(_config_table()))

    errors = validate_routing_default(body, platform=platform)
    if errors:
        return _error(400, errors[0]["code"], errors[0]["message"])

    jira_project = (body.get("jiraProject") or "").strip().upper()
    jira_issue_type = body.get("jiraIssueType", "Task").strip()

    # BUG-S23-002: Only validate against JIRA API when platform is jira
    if platform == "jira":
        jira_creds = _get_jira_credentials()
        if not jira_creds:
            return _error(400, "CFG_JIRA_NOT_CONFIGURED",
                          "JIRA connection must be configured before setting routing.")

        result = _validate_jira_project(*jira_creds, jira_project)
        if not result["valid"]:
            return _error(400, "CFG_INVALID_JIRA_PROJECT", result["reason"])
    elif platform == "servicenow":
        # STORY-137 (DD-STRUCT-7): validate the SNOW assignment-group target
        # exists. Format was already checked by validate_routing_default above
        # (Snape MUST-1: format before existence). Single-target surface →
        # top-level 400, mirroring the JIRA branch's placement.
        snow_err = _validate_snow_target_or_error(body["snowAssignmentGroupId"])
        if snow_err:
            return snow_err

    now = _now_iso()
    item = {
        "pk": "ROUTING_DEFAULT",
        "jira_issue_type": jira_issue_type,
        "updated_at": now,
    }
    # Store both JIRA and ServiceNow fields regardless of active platform
    if jira_project:
        item["jira_project"] = jira_project
    if body.get("snowAssignmentGroupId"):
        item["snow_assignment_group_id"] = body["snowAssignmentGroupId"]
    if body.get("snowAssignmentGroupName"):
        item["snow_assignment_group_name"] = body["snowAssignmentGroupName"]
    if body.get("snowRecordType"):
        item["snow_record_type"] = body["snowRecordType"]

    try:
        _config_table().put_item(Item=item)
    except ClientError:
        logger.exception("DynamoDB write failed for ROUTING_DEFAULT")
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to save default routing.")

    response = {
        "jiraProject": jira_project,
        "jiraIssueType": jira_issue_type,
        "updatedAt": now,
    }
    if item.get("snow_assignment_group_id"):
        response["snowAssignmentGroupId"] = item["snow_assignment_group_id"]
    if item.get("snow_assignment_group_name"):
        response["snowAssignmentGroupName"] = item["snow_assignment_group_name"]
    if item.get("snow_record_type"):
        response["snowRecordType"] = item["snow_record_type"]

    return _success(200, response)


# ===================================================================
# POST /api/config/routing/accounts — Add/update single account mapping
# ===================================================================

def handle_routing_accounts(event, context):
    """Add or update a single account routing mapping.

    Design: BRD §14.2 ROUTING#{accountId}.
    BUG-S23-002: Platform-aware validation — skip JIRA API call for ServiceNow.
    """
    try:
        from validators import validate_routing_account
    except ImportError:
        from lambdas.api.validators import validate_routing_account

    body = _parse_body(event)
    if body is None:
        return _error(400, "CFG_INVALID_REQUEST", "Request body must be valid JSON")

    # BUG-S23-002: Read active platform to determine validation path
    platform = operative_platform(resolve_platforms(_config_table()))

    errors = validate_routing_account(body, platform=platform)
    if errors:
        return _error(400, errors[0]["code"], errors[0]["message"])

    account_id = body["accountId"].strip()
    jira_project = (body.get("jiraProject") or "").strip().upper()
    account_name = body.get("accountName", "").strip()
    jira_issue_type = body.get("jiraIssueType", "Task").strip()

    # BUG-S23-002: Only validate against JIRA API when platform is jira
    if platform == "jira":
        jira_creds = _get_jira_credentials()
        if not jira_creds:
            return _error(400, "CFG_JIRA_NOT_CONFIGURED",
                          "JIRA connection must be configured before setting routing.")

        result = _validate_jira_project(*jira_creds, jira_project)
        if not result["valid"]:
            return _error(400, "CFG_INVALID_JIRA_PROJECT", result["reason"])
    elif platform == "servicenow":
        # STORY-137 (DD-STRUCT-7): validate the SNOW assignment-group target
        # exists. Format was already checked by validate_routing_account above
        # (Snape MUST-1: format before existence). Single-target surface →
        # top-level 400, mirroring the JIRA branch's placement.
        snow_err = _validate_snow_target_or_error(body["snowAssignmentGroupId"])
        if snow_err:
            return snow_err

    now = _now_iso()
    pk = f"{ROUTING_ACCOUNT_PREFIX}{account_id}"
    item = {
        "pk": pk,
        "account_id": account_id,
        "account_name": account_name,
        "jira_issue_type": jira_issue_type,
        "updated_at": now,
    }
    # Store both JIRA and ServiceNow fields regardless of active platform
    if jira_project:
        item["jira_project"] = jira_project
    if body.get("snowAssignmentGroupId"):
        item["snow_assignment_group_id"] = body["snowAssignmentGroupId"]
    if body.get("snowAssignmentGroupName"):
        item["snow_assignment_group_name"] = body["snowAssignmentGroupName"]
    if body.get("snowRecordType"):
        item["snow_record_type"] = body["snowRecordType"]

    try:
        _config_table().put_item(Item=item)
    except ClientError:
        logger.exception("DynamoDB write failed for %s", pk)
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to save account routing.")

    response = {
        "accountId": account_id,
        "accountName": account_name,
        "jiraProject": jira_project,
        "jiraIssueType": jira_issue_type,
        "updatedAt": now,
    }
    if item.get("snow_assignment_group_id"):
        response["snowAssignmentGroupId"] = item["snow_assignment_group_id"]
    if item.get("snow_assignment_group_name"):
        response["snowAssignmentGroupName"] = item["snow_assignment_group_name"]
    if item.get("snow_record_type"):
        response["snowRecordType"] = item["snow_record_type"]

    return _success(200, response)


# ===================================================================
# POST /api/config/routing/import — Upload CSV/JSON for preview
# ===================================================================

def handle_routing_import(event, context):
    """Parse CSV or JSON bulk import and return preview. Stores in ConfigTable with TTL.

    Design: BRD §14.6. Security: M-1 (csv.reader), M-2 (body size check).
    """
    try:
        from validators import validate_account_id_format
    except ImportError:
        from lambdas.api.validators import validate_account_id_format

    # M-2: Body size check
    raw_body = event.get("body") or ""
    if len(raw_body) > _MAX_BODY_SIZE:
        return _error(400, "CFG_PAYLOAD_TOO_LARGE",
                      "Request body exceeds 5MB limit.")

    body = _parse_body(event)
    if body is None:
        return _error(400, "CFG_INVALID_REQUEST", "Request body must be valid JSON")

    format_type = body.get("format", "").lower()
    data = body.get("data")

    if not data or not isinstance(data, str):
        return _error(400, "CFG_INVALID_REQUEST", "data field is required (string)")

    if format_type == "csv":
        mappings, parse_errors = _parse_csv_import(data)
    elif format_type == "json":
        mappings, parse_errors = _parse_json_import(data)
    else:
        return _error(400, "CFG_INVALID_REQUEST",
                      "format must be 'csv' or 'json'")

    # Validate each mapping
    valid = []
    invalid = []
    seen_accounts = {}

    for mapping in mappings:
        account_id = mapping.get("account_id", "").strip()
        jira_project = mapping.get("jira_project", "").strip().upper()
        snow_group_id = mapping.get("snow_assignment_group_id", "").strip()

        errors = []
        if not validate_account_id_format(account_id):
            errors.append("Invalid account ID (must be 12 digits)")
        if not jira_project and not snow_group_id:
            errors.append("Either a JIRA project or a ServiceNow assignment group is required")
        if snow_group_id:
            # Snape Finding 5: bound length before format check
            if len(snow_group_id) > _MAX_SNOW_GROUP_ID_LENGTH:
                errors.append(
                    f"ServiceNow assignment group ID must not exceed {_MAX_SNOW_GROUP_ID_LENGTH} characters"
                )
            # Snape Finding 3: enforce ServiceNow sys_id shape (32-char lowercase hex)
            elif not _SNOW_SYS_ID_PATTERN.match(snow_group_id):
                errors.append(
                    "ServiceNow assignment group ID must be a 32-character lowercase hex sys_id"
                )

        if errors:
            invalid.append({"accountId": account_id, "jiraProject": jira_project,
                            "errors": errors})
            continue

        # Duplicate detection — last wins
        if account_id in seen_accounts:
            invalid.append({"accountId": account_id, "jiraProject": jira_project,
                            "errors": ["Duplicate account ID (last entry wins)"],
                            "warning": True})

        entry = {"accountId": account_id, "jiraProject": jira_project}
        # Carry ServiceNow fields if present
        if mapping.get("snow_assignment_group_id"):
            entry["snowAssignmentGroupId"] = mapping["snow_assignment_group_id"]
        if mapping.get("snow_assignment_group_name"):
            entry["snowAssignmentGroupName"] = mapping["snow_assignment_group_name"]
        if mapping.get("snow_record_type"):
            entry["snowRecordType"] = mapping["snow_record_type"]

        seen_accounts[account_id] = entry
        valid.append(entry)

    # Add parse-level errors
    for err in parse_errors:
        invalid.append({"accountId": "", "jiraProject": "", "errors": [err]})

    # Store preview in ConfigTable with TTL
    import_id = str(uuid.uuid4())
    ttl = _now_epoch() + _IMPORT_TTL_SECONDS

    # Deduplicated valid list
    deduped_valid = list(seen_accounts.values())

    try:
        _config_table().put_item(Item={
            "pk": f"IMPORT#{import_id}",
            "import_id": import_id,
            "mappings": json.dumps(deduped_valid),
            "created_at": _now_iso(),
            "ttl": ttl,
        })
    except ClientError:
        logger.exception("DynamoDB write failed for IMPORT#%s", import_id)
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to store import preview.")

    return _success(200, {
        "importId": import_id,
        "valid": deduped_valid,
        "invalid": invalid,
        "totalValid": len(deduped_valid),
        "totalInvalid": len(invalid),
        "expiresIn": _IMPORT_TTL_SECONDS,
    })


def _parse_csv_import(data: str) -> tuple[list[dict], list[str]]:
    """Parse CSV data using csv.reader (M-1). Returns (mappings, errors).

    Columns: account_id, jira_project [, snow_assignment_group_id, snow_assignment_group_name, snow_record_type]
    """
    mappings = []
    errors = []
    reader = csv.reader(io.StringIO(data))

    for row_num, row in enumerate(reader, start=1):
        if not row or all(cell.strip() == "" for cell in row):
            continue
        # Skip header row
        if row_num == 1 and row[0].strip().lower() in ("account_id", "accountid"):
            continue
        if len(row) < 2:
            errors.append(f"Row {row_num}: expected at least 2 columns")
            continue
        entry = {
            "account_id": row[0].strip(),
            "jira_project": row[1].strip(),
        }
        # Optional ServiceNow columns
        if len(row) > 2 and row[2].strip():
            entry["snow_assignment_group_id"] = row[2].strip()
        if len(row) > 3 and row[3].strip():
            entry["snow_assignment_group_name"] = row[3].strip()
        if len(row) > 4 and row[4].strip():
            entry["snow_record_type"] = row[4].strip()
        mappings.append(entry)

    return mappings, errors


def _parse_json_import(data: str) -> tuple[list[dict], list[str]]:
    """Parse JSON array data. Returns (mappings, errors).

    Extracts JIRA and ServiceNow fields from each item.
    """
    try:
        items = json.loads(data)
    except json.JSONDecodeError as e:
        return [], [f"Invalid JSON: {e}"]

    if not isinstance(items, list):
        return [], ["JSON data must be an array"]

    mappings = []
    errors = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"Item {i}: must be an object")
            continue
        entry = {
            "account_id": str(item.get("account_id", item.get("accountId", ""))),
            "jira_project": str(item.get("jira_project", item.get("jiraProject", ""))),
        }
        # ServiceNow fields (optional)
        snow_group_id = item.get("snowAssignmentGroupId", item.get("snow_assignment_group_id", ""))
        if snow_group_id:
            entry["snow_assignment_group_id"] = str(snow_group_id)
        snow_group_name = item.get("snowAssignmentGroupName", item.get("snow_assignment_group_name", ""))
        if snow_group_name:
            entry["snow_assignment_group_name"] = str(snow_group_name)
        snow_record_type = item.get("snowRecordType", item.get("snow_record_type", ""))
        if snow_record_type:
            entry["snow_record_type"] = str(snow_record_type)
        mappings.append(entry)

    return mappings, errors


# ===================================================================
# POST /api/config/routing/import/confirm — Apply bulk import
# ===================================================================

def handle_routing_import_confirm(event, context):
    """Confirm and apply a previously uploaded import.

    Design: BRD §14.6 — replaces all ROUTING#* items.
    Security: H-1 (prefix constant), H-2 (write-before-delete).
    """
    body = _parse_body(event)
    if body is None:
        return _error(400, "CFG_INVALID_REQUEST", "Request body must be valid JSON")

    import_id = body.get("importId", "").strip()
    if not import_id:
        return _error(400, "CFG_INVALID_REQUEST", "importId is required")

    # Load import preview
    try:
        resp = _config_table().get_item(Key={"pk": f"IMPORT#{import_id}"})
        item = resp.get("Item")
    except ClientError:
        logger.exception("DynamoDB read failed for IMPORT#%s", import_id)
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to read import data.")

    if not item:
        return _error(404, "CFG_IMPORT_NOT_FOUND",
                      "Import not found or expired. Please re-upload.")

    # Check TTL expiry
    if item.get("ttl", 0) < _now_epoch():
        return _error(410, "CFG_IMPORT_EXPIRED",
                      "Import preview has expired. Please re-upload.")

    mappings = json.loads(item.get("mappings", "[]"))
    if not mappings:
        return _error(400, "CFG_IMPORT_EMPTY", "Import contains no valid mappings.")

    # BUG-S23-002: Only validate JIRA projects when platform is jira
    platform = operative_platform(resolve_platforms(_config_table()))

    if platform == "jira":
        jira_creds = _get_jira_credentials()
        if not jira_creds:
            return _error(400, "CFG_JIRA_NOT_CONFIGURED",
                          "JIRA connection must be configured before importing.")

        # Validate unique project keys
        unique_projects = set(m["jiraProject"] for m in mappings if m.get("jiraProject"))
        invalid_projects = []
        for project_key in unique_projects:
            result = _validate_jira_project(*jira_creds, project_key)
            if not result["valid"]:
                invalid_projects.append({"project": project_key, "reason": result["reason"]})

        if invalid_projects:
            return _error(400, "CFG_INVALID_JIRA_PROJECT", json.dumps({
                "message": "Some JIRA projects are invalid",
                "invalidProjects": invalid_projects,
            }))
    elif platform == "servicenow":
        # STORY-137 (DD-STRUCT-7): validate each UNIQUE SNOW assignment-group
        # target exists, mirroring the JIRA unique-project-key loop above.
        # Format was already enforced at preview (_SNOW_SYS_ID_PATTERN) so
        # existence-only here. Build the client ONCE; fail closed on error;
        # aggregate failures into one CFG_SNOW_GROUP_NOT_FOUND 400 (no partial
        # write — Snape MUST-13, edge §5.C).
        unique_groups = set(
            m["snowAssignmentGroupId"] for m in mappings if m.get("snowAssignmentGroupId")
        )
        if unique_groups:
            client = _get_snow_client_or_none()
            if client is None:
                return _error(400, "CFG_SNOW_NOT_CONFIGURED",
                              "ServiceNow connection must be configured and validated "
                              "before importing.")
            invalid_groups = []
            for group_id in unique_groups:
                try:
                    result = client.validate_routing_target(group_id)
                except Exception:
                    logger.exception(
                        "ServiceNow target validation raised during bulk import"
                    )
                    invalid_groups.append({
                        "group": group_id,
                        "reason": "Could not be validated in the connected "
                                  "ServiceNow instance",
                    })
                    continue
                if not result.valid:
                    invalid_groups.append({
                        "group": group_id,
                        "reason": "Not found in the connected ServiceNow instance",
                    })

            if invalid_groups:
                return _error(400, "CFG_SNOW_GROUP_NOT_FOUND", json.dumps({
                    "message": "Some ServiceNow assignment groups are invalid",
                    "invalidGroups": invalid_groups,
                }))

    # H-2: Write new mappings first
    now = _now_iso()
    table = _config_table()

    with table.batch_writer() as batch:
        for mapping in mappings:
            account_id = mapping["accountId"]
            pk = f"{ROUTING_ACCOUNT_PREFIX}{account_id}"
            item = {
                "pk": pk,
                "account_id": account_id,
                "account_name": mapping.get("accountName", ""),
                "jira_project": mapping.get("jiraProject", ""),
                "jira_issue_type": "Task",
                "updated_at": now,
            }
            if mapping.get("snowAssignmentGroupId"):
                item["snow_assignment_group_id"] = mapping["snowAssignmentGroupId"]
            if mapping.get("snowAssignmentGroupName"):
                item["snow_assignment_group_name"] = mapping["snowAssignmentGroupName"]
            if mapping.get("snowRecordType"):
                item["snow_record_type"] = mapping["snowRecordType"]
            batch.put_item(Item=item)

    # H-1: Delete old ROUTING#* items that are NOT in the new set
    new_pks = {f"{ROUTING_ACCOUNT_PREFIX}{m['accountId']}" for m in mappings}
    _delete_stale_routing_items(table, new_pks)

    # Clean up import preview
    try:
        table.delete_item(Key={"pk": f"IMPORT#{import_id}"})
    except ClientError:
        pass  # Non-critical

    return _success(200, {
        "imported": len(mappings),
        "updatedAt": now,
    })


def _delete_stale_routing_items(table, new_pks: set) -> None:
    """Delete ROUTING#* items not in new_pks.

    H-1: Assert prefix before delete — ROUTING_DEFAULT is never matched
    because scan uses begins_with('ROUTING#') which includes the '#'.
    """
    try:
        response = table.scan(
            FilterExpression="begins_with(pk, :prefix)",
            ExpressionAttributeValues={":prefix": ROUTING_ACCOUNT_PREFIX},
            ProjectionExpression="pk",
        )
        items = response.get("Items", [])
        # Handle pagination
        while response.get("LastEvaluatedKey"):
            response = table.scan(
                FilterExpression="begins_with(pk, :prefix)",
                ExpressionAttributeValues={":prefix": ROUTING_ACCOUNT_PREFIX},
                ProjectionExpression="pk",
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            items.extend(response.get("Items", []))
    except ClientError:
        logger.exception("DynamoDB scan failed during import cleanup")
        return

    stale_pks = [item["pk"] for item in items if item["pk"] not in new_pks]

    if not stale_pks:
        return

    # H-1: Assert every pk starts with ROUTING_ACCOUNT_PREFIX before delete
    with table.batch_writer() as batch:
        for pk in stale_pks:
            assert pk.startswith(ROUTING_ACCOUNT_PREFIX), f"Refusing to delete {pk}"
            batch.delete_item(Key={"pk": pk})

    logger.info("Deleted %d stale routing items", len(stale_pks))


# ===================================================================
# POST /api/config/routing/discover — Load accounts from Organizations
# ===================================================================

def handle_routing_discover(event, context):
    """Discover AWS accounts from Organizations.

    Design: BRD §14.1 Step 2 — Auto-discovery.
    Security: M-4 — strip email from response.
    """
    try:
        accounts = []
        paginator = _orgs.get_paginator("list_accounts")
        for page in paginator.paginate():
            for acct in page.get("Accounts", []):
                # M-4: Exclude email from response
                accounts.append({
                    "accountId": acct.get("Id", ""),
                    "accountName": acct.get("Name", ""),
                    "status": acct.get("Status", ""),
                })
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code == "AccessDeniedException":
            return _error(403, "CFG_ORGS_ACCESS_DENIED",
                          "Lambda does not have permission to list AWS Organizations accounts.")
        if error_code == "AWSOrganizationsNotInUseException":
            return _error(400, "CFG_ORGS_NOT_ENABLED",
                          "AWS Organizations is not enabled for this account.")
        logger.exception("Organizations ListAccounts failed")
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to list accounts from Organizations.")

    return _success(200, {
        "accounts": accounts,
        "totalAccounts": len(accounts),
    })


# ===================================================================
# GET /api/config/routing — Get all routing configuration
# ===================================================================

def handle_routing_get(event, context):
    """Return all routing configuration (default + account mappings).

    Design: BRD §14.2 — full routing config.
    """
    table = _config_table()

    # Get default routing
    try:
        resp = table.get_item(Key={"pk": "ROUTING_DEFAULT"})
        default_item = resp.get("Item")
    except ClientError:
        logger.exception("DynamoDB read failed for ROUTING_DEFAULT")
        default_item = None

    # Scan account mappings
    try:
        response = table.scan(
            FilterExpression="begins_with(pk, :prefix)",
            ExpressionAttributeValues={":prefix": ROUTING_ACCOUNT_PREFIX},
        )
        items = response.get("Items", [])
        while response.get("LastEvaluatedKey"):
            response = table.scan(
                FilterExpression="begins_with(pk, :prefix)",
                ExpressionAttributeValues={":prefix": ROUTING_ACCOUNT_PREFIX},
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            items.extend(response.get("Items", []))
    except ClientError:
        logger.exception("DynamoDB scan failed for routing accounts")
        items = []

    default_routing = None
    if default_item:
        default_routing = {
            "jiraProject": default_item.get("jira_project", ""),
            "jiraIssueType": default_item.get("jira_issue_type", "Task"),
            "updatedAt": default_item.get("updated_at", ""),
        }
        if default_item.get("snow_assignment_group_id"):
            default_routing["snowAssignmentGroupId"] = default_item["snow_assignment_group_id"]
        if default_item.get("snow_assignment_group_name"):
            default_routing["snowAssignmentGroupName"] = default_item["snow_assignment_group_name"]
        if default_item.get("snow_record_type"):
            default_routing["snowRecordType"] = default_item["snow_record_type"]

    account_mappings = []
    for item in items:
        mapping = {
            "accountId": item.get("account_id", ""),
            "accountName": item.get("account_name", ""),
            "jiraProject": item.get("jira_project", ""),
            "jiraIssueType": item.get("jira_issue_type", "Task"),
            "updatedAt": item.get("updated_at", ""),
        }
        if item.get("snow_assignment_group_id"):
            mapping["snowAssignmentGroupId"] = item["snow_assignment_group_id"]
        if item.get("snow_assignment_group_name"):
            mapping["snowAssignmentGroupName"] = item["snow_assignment_group_name"]
        if item.get("snow_record_type"):
            mapping["snowRecordType"] = item["snow_record_type"]
        account_mappings.append(mapping)

    # Sort by account ID for consistent output
    account_mappings.sort(key=lambda x: x["accountId"])

    return _success(200, {
        "default": default_routing,
        "accounts": account_mappings,
        "totalAccounts": len(account_mappings),
    })


# ===================================================================
# DELETE /api/config/routing/accounts/{accountId} — Remove single mapping
# ===================================================================

def handle_routing_accounts_delete(event, context):
    """Delete a single account routing mapping.

    Design: BRD §14.2 — per-account routing removal.
    """
    # Extract accountId from path parameters
    path_params = event.get("pathParameters") or {}
    account_id = path_params.get("accountId", "").strip()

    if not account_id:
        return _error(400, "CFG_INVALID_REQUEST", "accountId path parameter is required")

    try:

        from validators import validate_account_id_format

    except ImportError:

        from lambdas.api.validators import validate_account_id_format
    if not validate_account_id_format(account_id):
        return _error(400, "CFG_INVALID_ACCOUNT_ID", "Account ID must be exactly 12 digits")

    pk = f"{ROUTING_ACCOUNT_PREFIX}{account_id}"

    # Check if exists
    try:
        resp = _config_table().get_item(Key={"pk": pk})
        if not resp.get("Item"):
            return _error(404, "CFG_ROUTING_NOT_FOUND",
                          f"No routing mapping found for account {account_id}")
    except ClientError:
        logger.exception("DynamoDB read failed for %s", pk)
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to read routing configuration.")

    # Delete
    try:
        _config_table().delete_item(Key={"pk": pk})
    except ClientError:
        logger.exception("DynamoDB delete failed for %s", pk)
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to delete routing mapping.")

    return _success(200, {"deleted": True, "accountId": account_id})


# ===================================================================
# POST /api/config/routing/validate — Validate ITSM routing targets
# ===================================================================

def handle_routing_validate(event, context):
    """Validate JIRA project keys or ServiceNow assignment groups exist.

    STORY-084: Routing target validation during setup.
    Calls external ITSM API to confirm each target is reachable.
    """
    body = _parse_body(event)
    if not body or not body.get("targets"):
        return _error(400, "INVALID_PARAM", "targets array required")

    platform = body.get("platform", "jira")
    targets = body["targets"]
    if not isinstance(targets, list) or len(targets) > 50:
        return _error(400, "INVALID_PARAM", "targets must be array of max 50")

    unique_targets = list(set(t for t in targets if isinstance(t, str) and t.strip()))
    if not unique_targets:
        return _error(400, "INVALID_PARAM", "targets must contain non-empty strings")

    if platform == "jira":
        jira_creds = _get_jira_credentials()
        if not jira_creds:
            return _error(400, "JIRA_NOT_CONFIGURED", "JIRA connection not configured")

        base_url, email, api_token = jira_creds
        results = []
        for target in unique_targets:
            result = _validate_jira_project(base_url, email, api_token, target.strip())
            if result["valid"] is True:
                results.append({"target": target, "valid": True, "displayName": result.get("name", target)})
            elif result["valid"] is None:
                results.append({"target": target, "valid": None, "error": result.get("reason", "Rate limited")})
            else:
                results.append({"target": target, "valid": False, "error": result.get("reason", "Unknown error")})
    else:
        # STORY-137 (§4.5): ServiceNow target validation via the same
        # DD-STRUCT-7 primitive used by the save paths — no longer an
        # "accept-all" placeholder. Format is checked inside
        # validate_routing_target (Snape MUST-1/-15: goes through
        # ServiceNowClient, host-pinned, no alternate HTTP).
        client = _get_snow_client_or_none()
        if client is None:
            return _error(400, "CFG_SNOW_NOT_CONFIGURED",
                          "ServiceNow connection not configured")
        results = []
        for target in unique_targets:
            try:
                result = client.validate_routing_target(target.strip())
            except Exception:
                logger.exception("ServiceNow target validation raised")
                results.append({"target": target, "valid": False,
                                "error": "Could not be validated"})
                continue
            if result.valid:
                results.append({"target": target, "valid": True,
                                "displayName": result.target_name or target})
            else:
                # Do not surface raw upstream detail (Snape MUST-9).
                results.append({"target": target, "valid": False,
                                "error": "Assignment group not found or invalid"})

    return _success(200, {"results": results})
