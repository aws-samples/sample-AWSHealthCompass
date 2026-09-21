"""Resolve API Lambda — route handler.

Implements JIRA connection endpoints plus status endpoint.
Backed by Amazon API Gateway with API key authentication.

SECURITY:
  Do NOT log event["headers"] — contains x-api-key.
  Do NOT log event["body"] on credential endpoints — contains JIRA token.
  MUST validate JIRA credentials before AWS Secrets Manager write.
  MUST validate JIRA URL: HTTPS + .atlassian.net only.
  MUST log every Secrets Manager write with request ID.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from resolve_core.constants import JIRA_SECRET_DESCRIPTION

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

CORS_ORIGIN = os.environ.get("CORS_ALLOW_ORIGIN", "*")
CONFIG_TABLE = os.environ.get("CONFIG_TABLE", "compass-config")
JIRA_SECRET_ARN = os.environ.get("JIRA_SECRET_ARN", "")
JIRA_SECRET_NAME = "compass/jira-credentials"  # nosec B105 — Secrets Manager logical path, not a credential
SYNC_FUNCTION_NAME = os.environ.get("SYNC_FUNCTION_NAME", "")
JIRA_FUNCTION_NAME = os.environ.get("JIRA_FUNCTION_NAME", "")

# Warn on wildcard CORS origin at cold start.
if CORS_ORIGIN == "*":
    logger.warning("CORS_ALLOW_ORIGIN is '*' — set to dashboard domain for production")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": CORS_ORIGIN,
    "Access-Control-Allow-Headers": "Content-Type, x-api-key, Authorization",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
}

# AWS SDK clients — reused across warm invocations
_dynamodb = boto3.resource("dynamodb")
_secrets = boto3.client("secretsmanager")


# ===================================================================
# Route dispatch
# ===================================================================

_ROUTES: dict[tuple[str, str], callable] = {}


def _route(method: str, path: str):
    """Decorator to register a handler for (method, path)."""
    def decorator(fn):
        _ROUTES[(method, path)] = fn
        return fn
    return decorator


def lambda_handler(event, context):
    """Route API Gateway proxy requests."""
    method = event.get("httpMethod", "")
    path = event.get("resource", "")
    request_id = event.get("requestContext", {}).get("requestId", "")

    # Log only safe fields — never headers or body.
    logger.info(json.dumps({
        "message": "API request",
        "method": method,
        "path": path,
        "requestId": request_id,
    }))

    handler_fn = _ROUTES.get((method, path))
    if handler_fn:
        try:
            # RBAC enforcement
            rbac_error = _check_rbac(event)
            if rbac_error:
                return rbac_error
            return handler_fn(event, context)
        except Exception:
            # Never expose raw exception details to caller
            logger.exception("Unhandled error in %s %s", method, path)
            return _error(500, "SYS_INTERNAL_ERROR", "An internal error occurred.")

    return _error(501, "NOT_IMPLEMENTED", f"Endpoint {method} {path} not yet implemented")


# ===================================================================
# RBAC Enforcement
# ===================================================================

def _check_rbac(event: dict) -> dict | None:
    """Enforce role-based access control. Returns error response or None if allowed.

    Rules:
    - GET requests: allowed for Admins or Viewers
    - POST/PUT/DELETE: require Admins group or api_key auth method
    - No groups at all: deny (fail-closed)
    """
    method = event.get("httpMethod", "")
    authorizer = event.get("requestContext", {}).get("authorizer") or {}

    # If no authorizer context present (e.g. OPTIONS, or authorizer not attached), allow
    if not authorizer:
        return None

    auth_method = authorizer.get("authMethod", "")
    groups_str = authorizer.get("groups", "")
    groups = [g.strip() for g in groups_str.split(",") if g.strip()]

    # API key auth method grants Admins-equivalent access
    if auth_method == "api_key":
        return None

    # Fail-closed: no groups = no access
    if not groups:
        return _error(403, "FORBIDDEN", "Insufficient permissions")

    # Read operations: any authenticated group member
    if method == "GET":
        return None

    # Write operations: require Admins
    if "Admins" not in groups:
        return _error(403, "FORBIDDEN", "Insufficient permissions")

    return None


# ===================================================================
# GET /api/status
# ===================================================================

@_route("GET", "/api/status")
def handle_status(event, context):
    """Health check endpoint."""
    return _success(200, {
        "status": "ok",
        "version": "beta",
        "configured": False,
    })


# ===================================================================
# POST /api/config/jira — Save and validate JIRA connection
# ===================================================================

@_route("POST", "/api/config/jira")
def handle_jira_save(event, context):
    """Save JIRA connection: validate credentials, store in Secrets Manager + ConfigTable.

    Supports partial credential update.
    - If apiToken is absent/null/empty → preserve existing credentials in Secrets Manager.
    - If apiToken is present with a non-empty string → full credential update (existing behavior).

    Security review applied.
    """
    try:
        from validators import validate_jira_input, normalize_base_url
        from jira_client import validate_connection
    except ImportError:
        from lambdas.api.validators import validate_jira_input, normalize_base_url
        from lambdas.api.jira_client import validate_connection

    request_id = event.get("requestContext", {}).get("requestId", "")
    source_ip = event.get("requestContext", {}).get("identity", {}).get("sourceIp", "")

    # Parse body
    body = _parse_body(event)
    if body is None:
        return _error(400, "CFG_INVALID_REQUEST", "Request body must be valid JSON")

    # Detect whether credential should be preserved or updated
    api_token_value = body.get("apiToken")
    credential_present = (
        "apiToken" in body
        and api_token_value is not None
        and isinstance(api_token_value, str)
        and api_token_value.strip() != ""
    )

    if credential_present:
        # --- Full credential update path (existing behavior, backward compat) ---
        errors = validate_jira_input(body)
        if errors:
            return _error(400, errors[0]["code"], errors[0]["message"])

        base_url = normalize_base_url(body["baseUrl"])
        email = body["email"].strip()
        api_token = body["apiToken"]

        # Validate against JIRA
        result = validate_connection(base_url, email, api_token)

        if not result["success"]:
            status_code = 502 if result["errorCode"] in ("CONN_JIRA_UNREACHABLE", "CONN_JIRA_SERVER_ERROR") else 400
            return _error(status_code, result["errorCode"], result["message"])

        # Write to Secrets Manager
        now_iso = _now_iso()
        secret_value = json.dumps({"email": email, "api_token": api_token})
        secret_arn = _write_secret(secret_value)

        if secret_arn is None:
            return _error(500, "CONN_SECRETS_FAILED", "Failed to store credentials. Please retry.")

        # Write to ConfigTable (full put_item)
        try:
            _config_table().put_item(Item={
                "pk": "JIRA_CONNECTION",
                "jira_base_url": base_url,
                "jira_email": email,
                "jira_secret_arn": secret_arn,
                "validated": True,
                "validated_at": now_iso,
                "validated_user": result["displayName"],
                "updated_at": now_iso,
            })
        except ClientError:
            logger.exception("DynamoDB write failed for JIRA_CONNECTION")
            _delete_secret_safe()
            return _error(500, "SYS_INTERNAL_ERROR", "Failed to save configuration. Please retry.")

        # Audit log
        logger.warning(json.dumps({
            "audit": True,
            "action": "JIRA_CONNECTION_SAVE",
            "update_type": "full",
            "source_ip": source_ip,
            "request_id": request_id,
            "jira_base_url": base_url,
            "timestamp": now_iso,
        }))

        return _success(200, {
            "baseUrl": base_url,
            "validated": True,
            "validatedAt": now_iso,
            "validatedUser": result["displayName"],
            "updateType": "full",
        })

    else:
        # --- Partial update path — preserve credentials ---
        # Validate non-credential fields only
        base_url_raw = body.get("baseUrl")
        email_raw = body.get("email")

        if not base_url_raw or not isinstance(base_url_raw, str) or not base_url_raw.strip():
            return _error(400, "CFG_INVALID_JIRA_URL", "baseUrl is required")

        if not email_raw or not isinstance(email_raw, str) or not email_raw.strip():
            return _error(400, "CFG_INVALID_EMAIL", "email is required")

        # Validate URL format (SSRF protection — FINDING-1 mandatory)
        try:
            from validators import _validate_jira_url
        except ImportError:
            from lambdas.api.validators import _validate_jira_url

        base_url = normalize_base_url(base_url_raw)
        url_error = _validate_jira_url(base_url)
        if url_error:
            return _error(400, url_error["code"], url_error["message"])

        email = email_raw.strip()
        if "@" not in email:
            return _error(400, "CFG_INVALID_EMAIL", "email must contain @")

        # Read existing config to get secret_arn
        item = _read_jira_config()
        if item is None:
            return _error(400, "CONN_CREDENTIALS_MISSING",
                          "No existing JIRA connection found. Please provide all credentials.")

        secret_arn = item.get("jira_secret_arn", "")
        if not secret_arn:
            return _error(400, "CONN_CREDENTIALS_MISSING",
                          "No stored credentials found. Please provide API token.")

        # Read stored credentials from Secrets Manager
        creds = _read_secret(secret_arn)
        if creds is None:
            return _error(400, "CONN_CREDENTIALS_MISSING",
                          "Stored credentials could not be found. Please enter new credentials.")

        stored_token = creds.get("api_token", "")
        if not stored_token:
            return _error(400, "CONN_CREDENTIALS_MISSING",
                          "Stored credentials are incomplete. Please enter new credentials.")

        # Re-validate connection with stored credentials + new URL/email
        result = validate_connection(base_url, email, stored_token)

        if not result["success"]:
            error_code = result["errorCode"]
            if error_code == "CONN_AUTH_FAILED":
                msg = ("Authentication failed with existing credentials. "
                       "Your API token may have expired or been revoked. "
                       "Please enter updated credentials.")
            elif error_code in ("CONN_JIRA_UNREACHABLE", "CONN_JIRA_SERVER_ERROR"):
                return _error(502, error_code, result["message"])
            else:
                msg = ("Connection failed with existing credentials. "
                       "The new URL may require different credentials.")
            return _error(400, error_code, msg)

        # Update ConfigTable (partial — only non-credential fields)
        now_iso = _now_iso()
        try:
            _config_table().update_item(
                Key={"pk": "JIRA_CONNECTION"},
                UpdateExpression=(
                    "SET jira_base_url = :url, jira_email = :em, "
                    "validated = :v, validated_at = :va, "
                    "validated_user = :vu, updated_at = :ua"
                ),
                ExpressionAttributeValues={
                    ":url": base_url,
                    ":em": email,
                    ":v": True,
                    ":va": now_iso,
                    ":vu": result["displayName"],
                    ":ua": now_iso,
                },
                ConditionExpression="attribute_exists(pk)",
            )
        except ClientError:
            logger.exception("DynamoDB update failed for JIRA_CONNECTION (partial)")
            return _error(500, "SYS_INTERNAL_ERROR", "Failed to save configuration. Please retry.")

        # Update SM if email changed (email is part of the credential pair)
        stored_email = creds.get("email", "")
        if email != stored_email:
            new_secret = json.dumps({"email": email, "api_token": stored_token})
            _write_secret(new_secret)

        # Audit log
        logger.warning(json.dumps({
            "audit": True,
            "action": "JIRA_CONNECTION_SAVE",
            "update_type": "partial",
            "source_ip": source_ip,
            "request_id": request_id,
            "jira_base_url": base_url,
            "timestamp": now_iso,
        }))

        return _success(200, {
            "baseUrl": base_url,
            "validated": True,
            "validatedAt": now_iso,
            "validatedUser": result["displayName"],
            "updateType": "partial",
        })


# ===================================================================
# GET /api/config/jira — Read connection status
# ===================================================================

@_route("GET", "/api/config/jira")
def handle_jira_get(event, context):
    """Return JIRA connection status. Never returns credentials.

    Added email, credentialsConfigured, hasApiToken fields.
    Security: No credential values are ever returned — only boolean metadata.
    """
    item = _read_jira_config()
    if item is None:
        return _success(200, None)

    # Check if credentials exist in Secrets Manager (boolean only)
    credentials_configured = False
    secret_arn = item.get("jira_secret_arn", "")
    if secret_arn:
        try:
            secret_desc = _secrets.describe_secret(SecretId=secret_arn)
            credentials_configured = secret_desc.get("DeletedDate") is None
        except ClientError:
            credentials_configured = False

    return _success(200, {
        "baseUrl": item.get("jira_base_url", ""),
        "email": item.get("jira_email", ""),
        "validated": item.get("validated", False),
        "validatedAt": item.get("validated_at", ""),
        "validatedUser": item.get("validated_user", ""),
        "credentialsConfigured": credentials_configured,
        "hasApiToken": credentials_configured,
    })


# ===================================================================
# POST /api/config/jira/test — Re-test existing connection
# ===================================================================

@_route("POST", "/api/config/jira/test")
def handle_jira_test(event, context):
    """Test existing JIRA connection. Returns 200 with status even on JIRA failure.

    Accepts optional body with baseUrl/email overrides for Test Connection
    with stored credentials. If apiToken is provided, uses it instead of stored token.
    FINDING-1: SSRF validation enforced on baseUrl override.
    """
    try:
        from jira_client import validate_connection
        from validators import normalize_base_url, _validate_jira_url
    except ImportError:
        from lambdas.api.jira_client import validate_connection
        from lambdas.api.validators import normalize_base_url, _validate_jira_url

    request_id = event.get("requestContext", {}).get("requestId", "")
    source_ip = event.get("requestContext", {}).get("identity", {}).get("sourceIp", "")

    # Read existing config
    item = _read_jira_config()
    if item is None:
        return _error(400, "CFG_JIRA_NOT_CONFIGURED", "JIRA connection not configured. Use POST /api/config/jira first.")

    base_url = item.get("jira_base_url", "")
    secret_arn = item.get("jira_secret_arn", "")

    # Read credentials from Secrets Manager
    creds = _read_secret(secret_arn)
    if creds is None:
        _update_validated(False)
        return _success(200, {
            "status": "failed",
            "errorCode": "CONN_SECRETS_FAILED",
            "message": "Could not read stored credentials. Re-save the connection.",
        })

    # Accept optional body overrides
    body = _parse_body(event) or {}

    # Determine test URL (override or stored)
    test_url = base_url
    if body.get("baseUrl") and isinstance(body["baseUrl"], str) and body["baseUrl"].strip():
        override_url = normalize_base_url(body["baseUrl"])
        # FINDING-1 (mandatory): SSRF validation on URL override
        url_error = _validate_jira_url(override_url)
        if url_error:
            return _error(400, url_error["code"], url_error["message"])
        test_url = override_url

    # Determine test email (override or stored)
    test_email = creds.get("email", "")
    if body.get("email") and isinstance(body["email"], str) and body["email"].strip():
        test_email = body["email"].strip()

    # Determine test token: if apiToken provided in body, use it; otherwise use stored
    api_token_value = body.get("apiToken")
    credential_from_body = (
        "apiToken" in body
        and api_token_value is not None
        and isinstance(api_token_value, str)
        and api_token_value.strip() != ""
    )
    test_token = api_token_value if credential_from_body else creds.get("api_token", "")

    # Call JIRA
    result = validate_connection(test_url, test_email, test_token)
    now_iso = _now_iso()

    if result["success"]:
        _update_validated(True, now_iso, result["displayName"])
        response_data = {
            "status": "connected",
            "user": result["displayName"],
            "accountId": result["accountId"],
            "validatedAt": now_iso,
        }
    else:
        _update_validated(False)
        response_data = {
            "status": "failed",
            "errorCode": result["errorCode"],
            "httpStatus": result.get("httpStatus", 0),
            "message": result["message"],
        }

    # FINDING-3: Structured audit logging with credential_source and target_url
    logger.info(json.dumps({
        "audit": True,
        "action": "JIRA_CONNECTION_TEST",
        "credential_source": "request_body" if credential_from_body else "secrets_manager",
        "target_url": test_url,
        "source_ip": source_ip,
        "request_id": request_id,
        "result_status": response_data["status"],
        "timestamp": now_iso,
    }))

    return _success(200, response_data)


# ===================================================================
# DELETE /api/config/jira — Remove connection
# ===================================================================

@_route("DELETE", "/api/config/jira")
def handle_jira_delete(event, context):
    """Remove JIRA connection config and Secrets Manager secret.

    Security review applied.
    """
    request_id = event.get("requestContext", {}).get("requestId", "")
    source_ip = event.get("requestContext", {}).get("identity", {}).get("sourceIp", "")

    # Require ?confirm=true
    params = event.get("queryStringParameters") or {}
    if params.get("confirm") != "true":
        return _error(400, "CFG_CONFIRMATION_REQUIRED",
                      "This action permanently deletes JIRA credentials. Add ?confirm=true to confirm.")

    item = _read_jira_config()
    if item is None:
        return _success(200, {"deleted": True})

    # Delete secret (ForceDeleteWithoutRecovery per design D3)
    _delete_secret_safe()

    # Delete config item
    try:
        _config_table().delete_item(Key={"pk": "JIRA_CONNECTION"})
    except ClientError:
        logger.exception("DynamoDB delete failed for JIRA_CONNECTION")
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to delete configuration.")

    now_iso = _now_iso()

    # Audit log at WARN level
    logger.warning(json.dumps({
        "audit": True,
        "action": "JIRA_CONNECTION_DELETE",
        "source_ip": source_ip,
        "request_id": request_id,
        "timestamp": now_iso,
    }))

    return _success(200, {"deleted": True})


# ===================================================================
# Dashboard endpoints
# ===================================================================

try:
    from lambdas.api.dashboard_handlers import (
        handle_campaigns_list, handle_campaign_detail, handle_campaign_resources,
        handle_campaign_breakdown, handle_create_tickets,
        handle_config_summary, handle_routing_default, handle_routing_discover,
        handle_routing_import, handle_dispatch_save, handle_reconcile, handle_sync,
        handle_generate_events, handle_group_preview,
    )
except ImportError:
    from dashboard_handlers import (
        handle_campaigns_list, handle_campaign_detail, handle_campaign_resources,
        handle_campaign_breakdown, handle_create_tickets,
        handle_config_summary, handle_routing_default, handle_routing_discover,
        handle_routing_import, handle_dispatch_save, handle_reconcile, handle_sync,
        handle_generate_events, handle_group_preview,
    )

_ROUTES[("GET", "/api/campaigns")] = handle_campaigns_list
_ROUTES[("GET", "/api/campaigns/{id}")] = handle_campaign_detail
_ROUTES[("GET", "/api/campaigns/{id}/resources")] = handle_campaign_resources
_ROUTES[("GET", "/api/campaigns/{id}/breakdown")] = handle_campaign_breakdown
_ROUTES[("GET", "/api/campaigns/{id}/group-preview")] = handle_group_preview
_ROUTES[("POST", "/api/campaigns/{id}/create-tickets")] = handle_create_tickets
_ROUTES[("GET", "/api/config/summary")] = handle_config_summary
_ROUTES[("POST", "/api/config/routing/default")] = handle_routing_default
_ROUTES[("POST", "/api/config/routing/discover")] = handle_routing_discover
_ROUTES[("POST", "/api/config/routing/import")] = handle_routing_import
_ROUTES[("POST", "/api/reconcile")] = handle_reconcile
_ROUTES[("POST", "/api/sync")] = handle_sync
_ROUTES[("POST", "/api/generate-events")] = handle_generate_events


# ===================================================================
# DynamoDB helpers
# ===================================================================

def _config_table():
    """Return DynamoDB Table resource for ConfigTable."""
    return _dynamodb.Table(CONFIG_TABLE)


def _read_jira_config() -> dict | None:
    """GetItem pk=JIRA_CONNECTION. Returns item dict or None."""
    try:
        resp = _config_table().get_item(Key={"pk": "JIRA_CONNECTION"})
        return resp.get("Item")
    except ClientError:
        logger.exception("DynamoDB read failed for JIRA_CONNECTION")
        return None


def _update_validated(validated: bool, validated_at: str | None = None,
                      validated_user: str | None = None) -> None:
    """Update only the validated/validated_at/validated_user fields."""
    expr_parts = ["#v = :v", "#ua = :ua"]
    names = {"#v": "validated", "#ua": "updated_at"}
    values: dict = {":v": validated, ":ua": _now_iso()}

    if validated_at:
        expr_parts.append("#va = :va")
        names["#va"] = "validated_at"
        values[":va"] = validated_at

    if validated_user:
        expr_parts.append("#vu = :vu")
        names["#vu"] = "validated_user"
        values[":vu"] = validated_user

    try:
        _config_table().update_item(
            Key={"pk": "JIRA_CONNECTION"},
            UpdateExpression="SET " + ", ".join(expr_parts),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ConditionExpression="attribute_exists(pk)",
        )
    except ClientError:
        logger.exception("DynamoDB update_validated failed")


# ===================================================================
# Secrets Manager helpers
# ===================================================================

def _write_secret(secret_value: str) -> str | None:
    """Write JIRA credentials to Secrets Manager.

    Tries PutSecretValue first (update existing), falls back to
    CreateSecret on ResourceNotFoundException (first time or after delete).

    Handles InvalidRequestException after recent delete
    with a single retry after 1 second.

    Returns secret ARN on success, None on failure.
    """
    # Try PutSecretValue first (most common path)
    try:
        resp = _secrets.put_secret_value(
            SecretId=JIRA_SECRET_ARN or JIRA_SECRET_NAME,
            SecretString=secret_value,
        )
        logger.info("Secrets Manager PutSecretValue: %s", resp["ARN"])
        return resp["ARN"]
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code != "ResourceNotFoundException":
            logger.exception("Secrets Manager PutSecretValue failed")
            return None

    # Secret doesn't exist — create it
    return _create_secret_with_retry(secret_value)


def _create_secret_with_retry(secret_value: str) -> str | None:
    """CreateSecret with one retry for InvalidRequestException.

    After ForceDeleteWithoutRecovery, Secrets Manager
    has eventual consistency. Retry once after 1 second.
    """
    for attempt in range(2):
        try:
            resp = _secrets.create_secret(
                Name=JIRA_SECRET_NAME,
                SecretString=secret_value,
                Description=JIRA_SECRET_DESCRIPTION,
            )
            logger.info("Secrets Manager CreateSecret: %s", resp["ARN"])
            return resp["ARN"]
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "InvalidRequestException" and attempt == 0:
                logger.warning("CreateSecret InvalidRequestException — retrying in 1s")
                time.sleep(1)
                continue
            logger.exception("Secrets Manager CreateSecret failed")
            return None
    return None


def _read_secret(secret_arn: str) -> dict | None:
    """Read JIRA credentials from Secrets Manager. Returns parsed dict or None."""
    try:
        resp = _secrets.get_secret_value(SecretId=secret_arn or JIRA_SECRET_NAME)
        return json.loads(resp["SecretString"])
    except (ClientError, json.JSONDecodeError, KeyError):
        logger.exception("Secrets Manager read failed")
        return None


def _delete_secret_safe() -> None:
    """Delete JIRA secret with ForceDeleteWithoutRecovery. Ignores not-found."""
    try:
        _secrets.delete_secret(
            SecretId=JIRA_SECRET_ARN or JIRA_SECRET_NAME,
            ForceDeleteWithoutRecovery=True,
        )
        logger.info("Secrets Manager secret deleted")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return
        logger.exception("Secrets Manager delete failed")


# ===================================================================
# Response helpers
# ===================================================================

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


def _now_iso() -> str:
    """Current UTC time in ISO 8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ===================================================================
# Routing endpoints — delegated to routing_handlers.py
# ===================================================================

try:
    from routing_handlers import (  # noqa: E402
        handle_routing_default as _handle_routing_default,
        handle_routing_accounts as _handle_routing_accounts,
        handle_routing_import as _handle_routing_import,
        handle_routing_import_confirm as _handle_routing_import_confirm,
        handle_routing_discover as _handle_routing_discover,
        handle_routing_get as _handle_routing_get,
        handle_routing_accounts_delete as _handle_routing_accounts_delete,
        handle_routing_validate as _handle_routing_validate,
    )
except ImportError:
    from lambdas.api.routing_handlers import (  # noqa: E402
        handle_routing_default as _handle_routing_default,
        handle_routing_accounts as _handle_routing_accounts,
        handle_routing_import as _handle_routing_import,
        handle_routing_import_confirm as _handle_routing_import_confirm,
        handle_routing_discover as _handle_routing_discover,
        handle_routing_get as _handle_routing_get,
        handle_routing_accounts_delete as _handle_routing_accounts_delete,
        handle_routing_validate as _handle_routing_validate,
    )

_ROUTES[("POST", "/api/config/routing/default")] = _handle_routing_default
_ROUTES[("POST", "/api/config/routing/accounts")] = _handle_routing_accounts
_ROUTES[("POST", "/api/config/routing/import")] = _handle_routing_import
_ROUTES[("POST", "/api/config/routing/import/confirm")] = _handle_routing_import_confirm
_ROUTES[("POST", "/api/config/routing/discover")] = _handle_routing_discover
_ROUTES[("GET", "/api/config/routing")] = _handle_routing_get
_ROUTES[("DELETE", "/api/config/routing/accounts/{accountId}")] = _handle_routing_accounts_delete
_ROUTES[("POST", "/api/config/routing/validate")] = _handle_routing_validate

# ===================================================================
# Tag routing endpoints — delegated to tag_routing_handlers.py
# ===================================================================

try:
    from tag_routing_handlers import (  # noqa: E402
        handle_routing_strategy as _handle_routing_strategy,
        handle_tag_mappings_get as _handle_tag_mappings_get,
        handle_tag_mappings_save as _handle_tag_mappings_save,
        handle_tag_mapping_delete as _handle_tag_mapping_delete,
        handle_tag_preview as _handle_tag_preview,
    )
except ImportError:
    from lambdas.api.tag_routing_handlers import (  # noqa: E402
        handle_routing_strategy as _handle_routing_strategy,
        handle_tag_mappings_get as _handle_tag_mappings_get,
        handle_tag_mappings_save as _handle_tag_mappings_save,
        handle_tag_mapping_delete as _handle_tag_mapping_delete,
        handle_tag_preview as _handle_tag_preview,
    )

_ROUTES[("POST", "/api/config/routing/strategy")] = _handle_routing_strategy
_ROUTES[("GET", "/api/config/routing/tags")] = _handle_tag_mappings_get
_ROUTES[("POST", "/api/config/routing/tags")] = _handle_tag_mappings_save
_ROUTES[("DELETE", "/api/config/routing/tags/{tagValue}")] = _handle_tag_mapping_delete
_ROUTES[("GET", "/api/config/routing/tag-preview")] = _handle_tag_preview

# ===================================================================
# Dispatch & Activation endpoints — delegated to dispatch_handlers.py
# ===================================================================

try:
    from dispatch_handlers import (  # noqa: E402
        handle_dispatch_save as _handle_dispatch_save_030,
        handle_dispatch_get as _handle_dispatch_get,
        handle_dispatch_rule_update as _handle_dispatch_rule_update,
        handle_dispatch_rule_delete as _handle_dispatch_rule_delete,
        handle_config_status as _handle_config_status,
        handle_activate as _handle_activate,
    )
except ImportError:
    from lambdas.api.dispatch_handlers import (  # noqa: E402
        handle_dispatch_save as _handle_dispatch_save_030,
        handle_dispatch_get as _handle_dispatch_get,
        handle_dispatch_rule_update as _handle_dispatch_rule_update,
        handle_dispatch_rule_delete as _handle_dispatch_rule_delete,
        handle_config_status as _handle_config_status,
        handle_activate as _handle_activate,
    )

_ROUTES[("POST", "/api/config/dispatch")] = _handle_dispatch_save_030
_ROUTES[("GET", "/api/config/dispatch")] = _handle_dispatch_get
_ROUTES[("PUT", "/api/config/dispatch/rules/{ruleId}")] = _handle_dispatch_rule_update
_ROUTES[("DELETE", "/api/config/dispatch/rules/{ruleId}")] = _handle_dispatch_rule_delete
_ROUTES[("GET", "/api/config/status")] = _handle_config_status
_ROUTES[("POST", "/api/config/activate")] = _handle_activate

# ===================================================================
# Orphan queue notification endpoints
# ===================================================================

try:
    from orphan_handlers import (  # noqa: E402
        handle_orphan_status as _handle_orphan_status,
        handle_routing_suggestions as _handle_routing_suggestions,
    )
except ImportError:
    from lambdas.api.orphan_handlers import (  # noqa: E402
        handle_orphan_status as _handle_orphan_status,
        handle_routing_suggestions as _handle_routing_suggestions,
    )

_ROUTES[("GET", "/api/config/routing/orphan-status")] = _handle_orphan_status
_ROUTES[("GET", "/api/config/routing/suggestions")] = _handle_routing_suggestions

# ===================================================================
# ServiceNow configuration endpoints
# ===================================================================

try:
    from servicenow_handlers import (  # noqa: E402
        handle_servicenow_test as _handle_servicenow_test,
        handle_servicenow_save as _handle_servicenow_save,
        handle_servicenow_get as _handle_servicenow_get,
        handle_servicenow_delete as _handle_servicenow_delete,
    )
except ImportError:
    from lambdas.api.servicenow_handlers import (  # noqa: E402
        handle_servicenow_test as _handle_servicenow_test,
        handle_servicenow_save as _handle_servicenow_save,
        handle_servicenow_get as _handle_servicenow_get,
        handle_servicenow_delete as _handle_servicenow_delete,
    )

_ROUTES[("POST", "/api/config/servicenow/test")] = _handle_servicenow_test
_ROUTES[("POST", "/api/config/servicenow")] = _handle_servicenow_save
_ROUTES[("GET", "/api/config/servicenow")] = _handle_servicenow_get
_ROUTES[("DELETE", "/api/config/servicenow")] = _handle_servicenow_delete

# ===================================================================
# Routing coverage endpoints
# ===================================================================

try:
    from coverage_handlers import (  # noqa: E402
        handle_routing_coverage as _handle_routing_coverage,
        handle_unroutable as _handle_unroutable,
    )
except ImportError:
    from lambdas.api.coverage_handlers import (  # noqa: E402
        handle_routing_coverage as _handle_routing_coverage,
        handle_unroutable as _handle_unroutable,
    )

_ROUTES[("GET", "/api/routing/coverage")] = _handle_routing_coverage
_ROUTES[("GET", "/api/routing/coverage/unroutable")] = _handle_unroutable
_ROUTES[("GET", "/api/metrics/routing-coverage")] = _handle_routing_coverage

# ===================================================================
# Platform switch endpoint and platform selection
# ===================================================================

try:
    from dashboard_handlers import handle_platform_switch as _handle_platform_switch  # noqa: E402
except ImportError:
    from lambdas.api.dashboard_handlers import handle_platform_switch as _handle_platform_switch  # noqa: E402

try:
    from platform_handlers import (  # noqa: E402
        handle_platform_get as _handle_platform_get,
        handle_platform_post as _handle_platform_post,
        handle_integrations_get as _handle_integrations_get,
        handle_integrations_put as _handle_integrations_put,
    )
except ImportError:
    from lambdas.api.platform_handlers import (  # noqa: E402
        handle_platform_get as _handle_platform_get,
        handle_platform_post as _handle_platform_post,
        handle_integrations_get as _handle_integrations_get,
        handle_integrations_put as _handle_integrations_put,
    )

_ROUTES[("PUT", "/api/config/platform")] = _handle_platform_switch
_ROUTES[("GET", "/api/config/platform")] = _handle_platform_get
_ROUTES[("POST", "/api/config/platform")] = _handle_platform_post
_ROUTES[("GET", "/api/config/integrations")] = _handle_integrations_get
_ROUTES[("PUT", "/api/config/integrations")] = _handle_integrations_put

# ===================================================================
# Test/dry-run endpoints
# ===================================================================

try:
    from test_handlers import handle_test_route as _handle_test_route  # noqa: E402
except ImportError:
    from lambdas.api.test_handlers import handle_test_route as _handle_test_route  # noqa: E402

_ROUTES[("POST", "/api/test/route")] = _handle_test_route

# ===================================================================
# Setup timer endpoints
# ===================================================================

try:
    from dashboard_handlers import (  # noqa: E402
        handle_setup_timer_start as _handle_setup_timer_start,
        handle_setup_timer_complete as _handle_setup_timer_complete,
        handle_setup_timer_get as _handle_setup_timer_get,
    )
except ImportError:
    from lambdas.api.dashboard_handlers import (  # noqa: E402
        handle_setup_timer_start as _handle_setup_timer_start,
        handle_setup_timer_complete as _handle_setup_timer_complete,
        handle_setup_timer_get as _handle_setup_timer_get,
    )

_ROUTES[("POST", "/api/config/setup-timer/start")] = _handle_setup_timer_start
_ROUTES[("POST", "/api/config/setup-timer/complete")] = _handle_setup_timer_complete
_ROUTES[("GET", "/api/config/setup-timer")] = _handle_setup_timer_get

# ===================================================================
# Telemetry status endpoint
# ===================================================================

try:
    from dashboard_handlers import handle_telemetry_status as _handle_telemetry_status  # noqa: E402
except ImportError:
    from lambdas.api.dashboard_handlers import handle_telemetry_status as _handle_telemetry_status  # noqa: E402

_ROUTES[("GET", "/api/config/telemetry")] = _handle_telemetry_status

# ===================================================================
# Telemetry session/event endpoints
# ===================================================================

try:
    from telemetry_handlers import (  # noqa: E402
        handle_telemetry_session as _handle_telemetry_session,
        handle_telemetry_event as _handle_telemetry_event,
    )
except ImportError:
    from lambdas.api.telemetry_handlers import (  # noqa: E402
        handle_telemetry_session as _handle_telemetry_session,
        handle_telemetry_event as _handle_telemetry_event,
    )

_ROUTES[("POST", "/api/telemetry/session")] = _handle_telemetry_session
_ROUTES[("POST", "/api/telemetry/event")] = _handle_telemetry_event

# ===================================================================
# CMDB routing config endpoints
# ===================================================================

try:
    from dashboard_handlers import (  # noqa: E402
        handle_cmdb_config_get as _handle_cmdb_config_get,
        handle_cmdb_config_save as _handle_cmdb_config_save,
    )
except ImportError:
    from lambdas.api.dashboard_handlers import (  # noqa: E402
        handle_cmdb_config_get as _handle_cmdb_config_get,
        handle_cmdb_config_save as _handle_cmdb_config_save,
    )

_ROUTES[("GET", "/api/config/cmdb-routing")] = _handle_cmdb_config_get
_ROUTES[("POST", "/api/config/cmdb-routing")] = _handle_cmdb_config_save

# ===================================================================
# Service-based routing config endpoints
# ===================================================================

try:
    from dashboard_handlers import (  # noqa: E402
        handle_service_routing_get as _handle_service_routing_get,
        handle_service_routing_save as _handle_service_routing_save,
        handle_service_routing_delete as _handle_service_routing_delete,
    )
except ImportError:
    from lambdas.api.dashboard_handlers import (  # noqa: E402
        handle_service_routing_get as _handle_service_routing_get,
        handle_service_routing_save as _handle_service_routing_save,
        handle_service_routing_delete as _handle_service_routing_delete,
    )

_ROUTES[("GET", "/api/config/routing/services")] = _handle_service_routing_get
_ROUTES[("POST", "/api/config/routing/services")] = _handle_service_routing_save
_ROUTES[("DELETE", "/api/config/routing/services/{service}")] = _handle_service_routing_delete

# ===================================================================
# Orphan queue visibility endpoint
# ===================================================================

try:
    from dashboard_handlers import handle_orphan_metrics as _handle_orphan_metrics  # noqa: E402
except ImportError:
    from lambdas.api.dashboard_handlers import handle_orphan_metrics as _handle_orphan_metrics  # noqa: E402

_ROUTES[("GET", "/api/routing/orphans")] = _handle_orphan_metrics
