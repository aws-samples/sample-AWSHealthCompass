"""ServiceNow configuration API handlers.

Endpoints:
  POST /api/config/servicenow/test  — validate credentials
  POST /api/config/servicenow       — save credentials
  GET  /api/config/servicenow       — return connection status
  DELETE /api/config/servicenow     — remove credentials

FINDING-06: Password masked in GET responses.
FINDING-07: Rate limit 5 calls per 10 min on test endpoint.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from resolve_core.config_schema import PK_SNOW_CONNECTION
from resolve_core.constants import SERVICENOW_SECRET_DESCRIPTION
from resolve_core.servicenow_client import _validate_snow_url
from resolve_core.credential_cache import CredentialRefreshError

logger = logging.getLogger("compass")

_CONFIG_TABLE = os.environ.get("CONFIG_TABLE", "compass-config")
_SNOW_SECRET_ARN = os.environ.get("SERVICENOW_SECRET_ARN", "")
_SNOW_SECRET_NAME = "compass/servicenow-credentials"  # nosec B105 — Secrets Manager logical path, not a credential
_AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
CORS_ORIGIN = os.environ.get("CORS_ALLOW_ORIGIN", "*")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": CORS_ORIGIN,
    "Access-Control-Allow-Headers": "Content-Type, x-api-key, Authorization",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
}

_dynamodb = boto3.resource("dynamodb", region_name=_AWS_REGION)
_secrets = boto3.client("secretsmanager", region_name=_AWS_REGION)

# Rate limit: 5 calls per 10 minutes
_RATE_LIMIT_MAX = 5
_RATE_LIMIT_WINDOW_S = 600


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


# ===================================================================
# POST /api/config/servicenow/test
# ===================================================================

def handle_servicenow_test(event, context):
    """Validate ServiceNow credentials without saving.

    STORY-102: Supports partial credential testing. If clientSecret or password
    are absent from the body, reads existing values from Secrets Manager.
    FINDING-1: SSRF validation enforced on instanceUrl.
    """
    # Rate limiting (FINDING-07)
    rate_err = _check_rate_limit()
    if rate_err:
        return rate_err

    body = _parse_body(event)
    if body is None:
        return _error(400, "CFG_INVALID_REQUEST", "Request body must be valid JSON")

    # STORY-102: Detect which credential fields are present
    secret_present = (
        "clientSecret" in body
        and body["clientSecret"] is not None
        and isinstance(body.get("clientSecret"), str)
        and body["clientSecret"].strip() != ""
    )
    password_present = (
        "password" in body
        and body["password"] is not None
        and isinstance(body.get("password"), str)
        and body["password"].strip() != ""
    )

    # Validate always-required fields
    instance_url_raw = body.get("instanceUrl")
    client_id_raw = body.get("clientId")
    username_raw = body.get("username")

    if not instance_url_raw or not isinstance(instance_url_raw, str) or not instance_url_raw.strip():
        return _error(400, "CFG_MISSING_FIELD", "Field 'instanceUrl' is required.")
    if not client_id_raw or not isinstance(client_id_raw, str) or not client_id_raw.strip():
        return _error(400, "CFG_MISSING_FIELD", "Field 'clientId' is required.")
    if not username_raw or not isinstance(username_raw, str) or not username_raw.strip():
        return _error(400, "CFG_MISSING_FIELD", "Field 'username' is required.")

    # Validate credential fields only if present
    if secret_present and not body["clientSecret"].strip():
        return _error(400, "CFG_MISSING_FIELD", "Field 'clientSecret' cannot be empty when provided.")
    if password_present and not body["password"].strip():
        return _error(400, "CFG_MISSING_FIELD", "Field 'password' cannot be empty when provided.")

    instance_url = instance_url_raw.strip().rstrip("/")
    client_id = client_id_raw.strip()
    username = username_raw.strip()

    # Validate URL format (SSRF check — FINDING-1 mandatory)
    try:
        instance_url = _validate_snow_url(instance_url)
    except ValueError as exc:
        return _error(400, "CFG_INVALID_URL", str(exc))

    # STORY-102: Resolve credential values — from body or Secrets Manager
    if not secret_present or not password_present:
        stored = _read_snow_secret()
        if stored is None:
            return _success(200, {
                "valid": False,
                "errors": ["No stored credentials found. Please provide all credential fields."],
            })
        client_secret = body["clientSecret"] if secret_present else stored.get("client_secret", "")
        password = body["password"] if password_present else stored.get("password", "")
    else:
        client_secret = body["clientSecret"]
        password = body["password"]

    # Attempt OAuth token exchange + validation
    result = _validate_snow_connection(instance_url, client_id, client_secret, username, password)

    source_ip = event.get("requestContext", {}).get("identity", {}).get("sourceIp", "")
    # FINDING-3: Structured audit logging with credential_source and target_url
    credential_source = "request_body" if (secret_present and password_present) else "secrets_manager"
    logger.info(json.dumps({
        "audit": True,
        "action": "SNOW_CONNECTION_TEST",
        "credential_source": credential_source,
        "target_url": instance_url,
        "source_ip": source_ip,
        "username": username,
        "result": "success" if result["valid"] else "failed",
        "timestamp": _now_iso(),
    }))

    if result["valid"]:
        return _success(200, {
            "valid": True,
            "displayName": result.get("display_name", ""),
            "roles": ["itil"],
        })
    else:
        return _success(200, {
            "valid": False,
            "errors": result.get("errors", ["Unknown error"]),
        })


# ===================================================================
# POST /api/config/servicenow
# ===================================================================

def handle_servicenow_save(event, context):
    """Save ServiceNow credentials to Secrets Manager + ConfigTable.

    STORY-102: Supports partial credential update.
    - If clientSecret/password absent/null/empty → preserve existing in Secrets Manager.
    - If present with non-empty string → update that credential.
    - instanceUrl, clientId, username are always required.
    """
    body = _parse_body(event)
    if body is None:
        return _error(400, "CFG_INVALID_REQUEST", "Request body must be valid JSON")

    # STORY-102: Detect which credential fields are present
    secret_present = (
        "clientSecret" in body
        and body["clientSecret"] is not None
        and isinstance(body.get("clientSecret"), str)
        and body["clientSecret"].strip() != ""
    )
    password_present = (
        "password" in body
        and body["password"] is not None
        and isinstance(body.get("password"), str)
        and body["password"].strip() != ""
    )

    # Both credentials present → full save (existing behavior, AC-11 backward compat)
    if secret_present and password_present:
        # Use original full validation
        errors = _validate_snow_input(body)
        if errors:
            return _error(400, errors[0]["code"], errors[0]["message"])

        instance_url = body["instanceUrl"].strip().rstrip("/")
        client_id = body["clientId"].strip()
        client_secret = body["clientSecret"]
        username = body["username"].strip()
        password = body["password"]

        try:
            instance_url = _validate_snow_url(instance_url)
        except ValueError as exc:
            return _error(400, "CFG_INVALID_URL", str(exc))

        # Validate first
        result = _validate_snow_connection(instance_url, client_id, client_secret, username, password)
        if not result["valid"]:
            return _error(400, "CFG_VALIDATION_FAILED", "; ".join(result.get("errors", ["Validation failed"])))

        # Write to Secrets Manager
        now_iso = _now_iso()
        secret_value = json.dumps({
            "client_id": client_id,
            "client_secret": client_secret,
            "username": username,
            "password": password,
            "access_token": result.get("access_token", ""),
            "refresh_token": result.get("refresh_token", ""),
            "token_expires_at": result.get("token_expires_at", 0),
        })

        secret_arn = _write_snow_secret(secret_value)
        if not secret_arn:
            return _error(500, "CFG_SECRETS_FAILED", "Failed to store credentials.")

        # Write to ConfigTable (full put_item)
        config_table = _dynamodb.Table(_CONFIG_TABLE)
        try:
            config_table.put_item(Item={
                "pk": PK_SNOW_CONNECTION,
                "instance_url": instance_url,
                "client_id": client_id,
                "username": username,
                "secret_arn": secret_arn,
                "validated": True,
                "validated_at": now_iso,
                "validated_user": result.get("display_name", username),
                "record_type": body.get("recordType", "change_request"),
                "updated_at": now_iso,
            })
        except ClientError:
            logger.exception("DynamoDB write failed for SNOW_CONNECTION")
            return _error(500, "SYS_INTERNAL_ERROR", "Failed to save configuration.")

        source_ip = event.get("requestContext", {}).get("identity", {}).get("sourceIp", "")
        logger.warning(json.dumps({
            "audit": True,
            "action": "SNOW_CONNECTION_SAVE",
            "update_type": "full",
            "source_ip": source_ip,
            "instance_url": instance_url,
            "timestamp": now_iso,
        }))

        return _success(200, {
            "instanceUrl": instance_url,
            "validated": True,
            "validatedAt": now_iso,
            "validatedUser": result.get("display_name", username),
            "updateType": "full",
        })

    else:
        # --- STORY-102: Partial update path — preserve some/all credentials ---

        # Validate always-required fields
        instance_url_raw = body.get("instanceUrl")
        client_id_raw = body.get("clientId")
        username_raw = body.get("username")

        if not instance_url_raw or not isinstance(instance_url_raw, str) or not instance_url_raw.strip():
            return _error(400, "CFG_MISSING_FIELD", "Field 'instanceUrl' is required.")
        if not client_id_raw or not isinstance(client_id_raw, str) or not client_id_raw.strip():
            return _error(400, "CFG_MISSING_FIELD", "Field 'clientId' is required.")
        if not username_raw or not isinstance(username_raw, str) or not username_raw.strip():
            return _error(400, "CFG_MISSING_FIELD", "Field 'username' is required.")

        instance_url = instance_url_raw.strip().rstrip("/")
        client_id = client_id_raw.strip()
        username = username_raw.strip()

        # SSRF validation (FINDING-1 mandatory)
        try:
            instance_url = _validate_snow_url(instance_url)
        except ValueError as exc:
            return _error(400, "CFG_INVALID_URL", str(exc))

        # Read existing credentials from Secrets Manager (read-modify-write)
        stored = _read_snow_secret()
        if stored is None:
            return _error(400, "CONN_CREDENTIALS_MISSING",
                          "No existing credentials found. Please provide all credential fields.")

        # Merge: use body values for present fields, stored values for absent fields
        merged_secret = body["clientSecret"] if secret_present else stored.get("client_secret", "")
        merged_password = body["password"] if password_present else stored.get("password", "")

        if not merged_secret or not merged_password:
            return _error(400, "CONN_CREDENTIALS_MISSING",
                          "Stored credentials are incomplete. Please provide all credential fields.")

        # Re-validate connection with merged credentials
        result = _validate_snow_connection(instance_url, client_id, merged_secret, username, merged_password)
        if not result["valid"]:
            error_msgs = result.get("errors", ["Validation failed"])
            # Check for auth failure to provide better messaging
            if any("Authentication failed" in e or "access token" in e.lower() for e in error_msgs):
                return _error(400, "CONN_AUTH_FAILED",
                              "Authentication failed with existing credentials. "
                              "Your credentials may have expired. Please enter updated credentials.")
            return _error(400, "CFG_VALIDATION_FAILED", "; ".join(error_msgs))

        # Write merged credentials to Secrets Manager
        # TODO: Add SM version checking for optimistic locking (FINDING-2, Beta)
        now_iso = _now_iso()
        new_secret_value = json.dumps({
            "client_id": client_id,
            "client_secret": merged_secret,
            "username": username,
            "password": merged_password,
            "access_token": result.get("access_token", ""),
            "refresh_token": result.get("refresh_token", ""),
            "token_expires_at": result.get("token_expires_at", 0),
        })

        secret_arn = _write_snow_secret(new_secret_value)
        if not secret_arn:
            return _error(500, "CFG_SECRETS_FAILED", "Failed to store credentials.")

        # Update ConfigTable
        config_table = _dynamodb.Table(_CONFIG_TABLE)
        try:
            config_table.update_item(
                Key={"pk": PK_SNOW_CONNECTION},
                UpdateExpression=(
                    "SET instance_url = :url, client_id = :cid, username = :usr, "
                    "validated = :v, validated_at = :va, "
                    "validated_user = :vu, record_type = :rt, updated_at = :ua"
                ),
                ExpressionAttributeValues={
                    ":url": instance_url,
                    ":cid": client_id,
                    ":usr": username,
                    ":v": True,
                    ":va": now_iso,
                    ":vu": result.get("display_name", username),
                    ":rt": body.get("recordType", "change_request"),
                    ":ua": now_iso,
                },
                ConditionExpression="attribute_exists(pk)",
            )
        except ClientError:
            logger.exception("DynamoDB update failed for SNOW_CONNECTION (partial)")
            return _error(500, "SYS_INTERNAL_ERROR", "Failed to save configuration.")

        source_ip = event.get("requestContext", {}).get("identity", {}).get("sourceIp", "")
        logger.warning(json.dumps({
            "audit": True,
            "action": "SNOW_CONNECTION_SAVE",
            "update_type": "partial",
            "fields_updated": (
                ["instanceUrl", "clientId", "username"]
                + (["clientSecret"] if secret_present else [])
                + (["password"] if password_present else [])
            ),
            "source_ip": source_ip,
            "instance_url": instance_url,
            "timestamp": now_iso,
        }))

        return _success(200, {
            "instanceUrl": instance_url,
            "validated": True,
            "validatedAt": now_iso,
            "validatedUser": result.get("display_name", username),
            "updateType": "partial",
        })


# ===================================================================
# GET /api/config/servicenow
# ===================================================================

def handle_servicenow_get(event, context):
    """Return ServiceNow connection status. Never returns actual secrets.

    STORY-102: Returns credentialsConfigured boolean, clientId, username.
    Removed fake masked values (clientSecret/password "********").
    SEC: No credential values are ever returned — only boolean metadata
    and non-sensitive identifiers.
    """
    config_table = _dynamodb.Table(_CONFIG_TABLE)
    resp = config_table.get_item(Key={"pk": PK_SNOW_CONNECTION})
    item = resp.get("Item")

    if not item:
        return _success(200, None)

    # STORY-102: Check if credentials exist in Secrets Manager (boolean only)
    credentials_configured = False
    secret_arn = item.get("secret_arn", "")
    if secret_arn:
        try:
            secret_desc = _secrets.describe_secret(SecretId=secret_arn)
            credentials_configured = secret_desc.get("DeletedDate") is None
        except ClientError:
            credentials_configured = False

    return _success(200, {
        "instanceUrl": item.get("instance_url", ""),
        "clientId": item.get("client_id", ""),
        "username": item.get("username", ""),
        "validated": item.get("validated", False),
        "validatedAt": item.get("validated_at", ""),
        "validatedUser": item.get("validated_user", ""),
        "recordType": item.get("record_type", "change_request"),
        "credentialsConfigured": credentials_configured,
        "hasClientSecret": credentials_configured,
        "hasPassword": credentials_configured,
    })


# ===================================================================
# DELETE /api/config/servicenow
# ===================================================================

def handle_servicenow_delete(event, context):
    """Remove ServiceNow connection config and credentials."""
    params = event.get("queryStringParameters") or {}
    if params.get("confirm") != "true":
        return _error(400, "CFG_CONFIRMATION_REQUIRED",
                      "Add ?confirm=true to confirm deletion.")

    config_table = _dynamodb.Table(_CONFIG_TABLE)

    # Delete secret
    try:
        _secrets.delete_secret(
            SecretId=_SNOW_SECRET_ARN or _SNOW_SECRET_NAME,
            ForceDeleteWithoutRecovery=True,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            logger.exception("ServiceNow secret delete failed")

    # Delete config
    try:
        config_table.delete_item(Key={"pk": PK_SNOW_CONNECTION})
    except ClientError:
        logger.exception("DynamoDB delete failed for SNOW_CONNECTION")
        return _error(500, "SYS_INTERNAL_ERROR", "Failed to delete configuration.")

    source_ip = event.get("requestContext", {}).get("identity", {}).get("sourceIp", "")
    logger.warning(json.dumps({
        "audit": True,
        "action": "SNOW_CONNECTION_DELETE",
        "source_ip": source_ip,
        "timestamp": _now_iso(),
    }))

    return _success(200, {"deleted": True})


# ===================================================================
# Internal helpers
# ===================================================================

def _validate_snow_input(body: dict) -> list:
    """Validate ServiceNow input fields. Returns list of error dicts."""
    required = ["instanceUrl", "clientId", "clientSecret", "username", "password"]
    for field in required:
        val = body.get(field)
        if not val or not isinstance(val, str) or not val.strip():
            return [{"code": "CFG_MISSING_FIELD", "message": f"Field '{field}' is required."}]
    return []


def _validate_snow_connection(instance_url: str, client_id: str, client_secret: str,
                              username: str, password: str) -> dict:
    """Perform OAuth exchange + user lookup + role check. Returns result dict."""
    import urllib3

    http = urllib3.PoolManager(
        timeout=urllib3.Timeout(connect=10, read=30),
        retries=False,
    )

    # Step 1: OAuth token exchange
    # LATENT HARDENING: urlencode() applies correct application/x-www-form-urlencoded
    # semantics (space -> "+", "/" -> "%2F"), matching curl --data-urlencode. The prior
    # hand-built quote() body emitted space as "%20", corrupting secrets/passwords that
    # contain a literal space or slash for OTHER customers. No change for the current user.
    from urllib.parse import quote, urlencode
    token_url = f"{instance_url}/oauth_token.do"
    params = urlencode({
        "grant_type": "password",
        "client_id": client_id,
        "client_secret": client_secret,
        "username": username,
        "password": password,
    })

    try:
        resp = http.request(
            "POST", token_url,
            body=params.encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            # PRIMARY FIX: follow redirects on the token POST ONLY. ServiceNow can answer
            # /oauth_token.do with a 3xx that curl follows to the final 200+access_token;
            # rejecting it as != 200 was the auth-failure root cause. The authenticated
            # sys_user / sys_user_has_role GETs below stay redirect=False so they cannot
            # silently follow to a login page and mask a real auth failure.
            redirect=True,
        )
    except Exception:
        return {"valid": False, "errors": ["Cannot reach ServiceNow instance."]}

    if resp.status != 200:
        # INSTRUMENTATION: surface the real upstream signal instead of a generic message.
        # The token endpoint body on FAILURE carries no access_token; still cap it and
        # never log the request credentials (client_secret/password).
        body_snippet = ""
        try:
            body_snippet = resp.data.decode("utf-8", errors="replace")[:500]
        except Exception:
            body_snippet = "<undecodable>"
        logger.warning(json.dumps({
            "event": "SNOW_TOKEN_EXCHANGE_NON_200",
            "status": resp.status,
            "location": resp.headers.get("Location"),
            "body_snippet": body_snippet,
        }))
        return {"valid": False, "errors": [f"ServiceNow returned HTTP {resp.status} from OAuth token endpoint. Verify credentials."]}

    try:
        token_data = json.loads(resp.data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"valid": False, "errors": ["Invalid response from ServiceNow OAuth endpoint."]}

    access_token = token_data.get("access_token", "")
    if not access_token:
        return {"valid": False, "errors": ["No access token received from ServiceNow."]}

    # Step 2: User lookup
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    user_url = f"{instance_url}/api/now/table/sys_user?sysparm_query=user_name={quote(username)}&sysparm_fields=sys_id,user_name,name&sysparm_limit=1"

    try:
        user_resp = http.request("GET", user_url, headers=headers, redirect=False)
    except Exception:
        return {"valid": False, "errors": ["Connection failed during user lookup."]}

    if user_resp.status != 200:
        return {"valid": False, "errors": [f"User lookup failed: HTTP {user_resp.status}"]}

    try:
        user_data = json.loads(user_resp.data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"valid": False, "errors": ["Invalid response from user lookup."]}

    users = user_data.get("result", [])
    if not users:
        return {"valid": False, "errors": [f"User '{username}' not found in ServiceNow."]}

    user = users[0]
    display_name = user.get("name", username)
    user_sys_id = user.get("sys_id", "")

    # Step 3: Role check
    role_url = (
        f"{instance_url}/api/now/table/sys_user_has_role"
        f"?sysparm_query=user={user_sys_id}^role.name=itil&sysparm_limit=1"
    )
    try:
        role_resp = http.request("GET", role_url, headers=headers, redirect=False)
        role_data = json.loads(role_resp.data.decode("utf-8"))
        roles = role_data.get("result", [])
        if not roles:
            return {
                "valid": False,
                "display_name": display_name,
                "errors": [f"User '{username}' does not have the 'itil' role."],
            }
    except Exception:
        return {"valid": False, "errors": ["Role verification failed."]}

    return {
        "valid": True,
        "display_name": display_name,
        "access_token": access_token,
        "refresh_token": token_data.get("refresh_token", ""),
        "token_expires_at": time.time() + int(token_data.get("expires_in", 1800)),
    }


def _write_snow_secret(secret_value: str) -> str | None:
    """Write ServiceNow credentials to Secrets Manager."""
    try:
        resp = _secrets.put_secret_value(
            SecretId=_SNOW_SECRET_ARN or _SNOW_SECRET_NAME,
            SecretString=secret_value,
        )
        return resp["ARN"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            logger.exception("Secrets Manager PutSecretValue failed")
            return None

    # Create new secret
    try:
        resp = _secrets.create_secret(
            Name=_SNOW_SECRET_NAME,
            SecretString=secret_value,
            Description=SERVICENOW_SECRET_DESCRIPTION,
        )
        return resp["ARN"]
    except ClientError:
        logger.exception("Secrets Manager CreateSecret failed")
        return None


def _check_rate_limit():
    """Check rate limit for test endpoint (FINDING-07). Returns error response or None."""
    config_table = _dynamodb.Table(_CONFIG_TABLE)
    now = int(time.time())
    ttl = now + _RATE_LIMIT_WINDOW_S

    try:
        resp = config_table.update_item(
            Key={"pk": "RATE_LIMIT#servicenow_test"},
            UpdateExpression="SET #c = if_not_exists(#c, :zero) + :one, #t = :ttl",
            ExpressionAttributeNames={"#c": "call_count", "#t": "ttl"},
            ExpressionAttributeValues={":zero": 0, ":one": 1, ":ttl": ttl},
            ReturnValues="ALL_NEW",
        )
        item = resp.get("Attributes", {})
        count = int(item.get("call_count", 0))

        if count > _RATE_LIMIT_MAX:
            return _error(429, "RATE_LIMITED", "Too many test attempts. Please wait before trying again.")
    except ClientError:
        pass  # On error, allow the request (fail-open for rate limiting)

    return None


def _read_snow_secret() -> dict | None:
    """Read ServiceNow credentials from Secrets Manager. Returns parsed dict or None.

    STORY-102: Used for partial credential updates — reads existing stored
    credentials so preserved fields can be merged with new values.
    SEC: Credentials are never returned in API responses — only used internally.
    """
    try:
        resp = _secrets.get_secret_value(SecretId=_SNOW_SECRET_ARN or _SNOW_SECRET_NAME)
        return json.loads(resp["SecretString"])
    except (ClientError, json.JSONDecodeError, KeyError):
        logger.exception("ServiceNow Secrets Manager read failed")
        return None
