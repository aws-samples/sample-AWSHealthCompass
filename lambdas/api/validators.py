"""Input validation for API endpoints.

Pure functions — no AWS SDK calls, no side effects.
SSRF protection.
Field length limits.
Routing validation.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

# Strict hostname regex — only {subdomain}.atlassian.net
_ATLASSIAN_HOST_RE = re.compile(
    r"^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?\.atlassian\.net$"
)

# Field length limits
_MAX_URL_LEN = 253
_MAX_EMAIL_LEN = 254
_MAX_TOKEN_LEN = 512


def validate_jira_input(body: dict | None) -> list[dict]:
    """Validate JIRA connection input fields.

    Returns empty list on success, list of error dicts on failure.
    Fails fast — returns on first error.
    """
    if not body or not isinstance(body, dict):
        return [{"code": "CFG_INVALID_REQUEST", "message": "Request body is required"}]

    base_url = body.get("baseUrl")
    email = body.get("email")
    api_token = body.get("apiToken")

    # --- baseUrl ---
    if not base_url or not isinstance(base_url, str) or not base_url.strip():
        return [{"code": "CFG_INVALID_JIRA_URL", "message": "baseUrl is required"}]

    base_url = base_url.strip()

    if len(base_url) > _MAX_URL_LEN:
        return [{"code": "CFG_INVALID_JIRA_URL", "message": f"baseUrl exceeds {_MAX_URL_LEN} characters"}]

    url_error = _validate_jira_url(base_url)
    if url_error:
        return [url_error]

    # --- email ---
    if not email or not isinstance(email, str) or not email.strip():
        return [{"code": "CFG_INVALID_EMAIL", "message": "email is required"}]

    email = email.strip()

    if len(email) > _MAX_EMAIL_LEN:
        return [{"code": "CFG_INVALID_EMAIL", "message": f"email exceeds {_MAX_EMAIL_LEN} characters"}]

    if "@" not in email:
        return [{"code": "CFG_INVALID_EMAIL", "message": "email must contain @"}]

    # --- apiToken ---
    if not api_token or not isinstance(api_token, str) or not api_token.strip():
        return [{"code": "CFG_INVALID_API_TOKEN", "message": "apiToken is required"}]

    if len(api_token) > _MAX_TOKEN_LEN:
        return [{"code": "CFG_INVALID_API_TOKEN", "message": f"apiToken exceeds {_MAX_TOKEN_LEN} characters"}]

    return []


def normalize_base_url(url: str) -> str:
    """Strip trailing slash from a validated base URL."""
    return url.strip().rstrip("/")


def _validate_jira_url(url: str) -> dict | None:
    """Validate JIRA Cloud URL. Returns error dict or None.

    SSRF protection checklist:
    - Scheme must be https
    - No explicit port
    - Hostname must match *.atlassian.net exactly
    - No path beyond /
    - No IP addresses
    """
    if not url.startswith("https://"):
        return {
            "code": "CFG_INVALID_JIRA_URL",
            "message": "JIRA URL must start with https://",
        }

    try:
        parsed = urlparse(url)
    except Exception:
        return {"code": "CFG_INVALID_JIRA_URL", "message": "Invalid URL format"}

    # No explicit port
    if parsed.port is not None:
        return {
            "code": "CFG_INVALID_JIRA_URL",
            "message": "JIRA URL must not include a port number",
        }

    hostname = parsed.hostname or ""

    # Strict hostname match
    if not _ATLASSIAN_HOST_RE.match(hostname):
        return {
            "code": "CFG_INVALID_JIRA_URL",
            "message": (
                "JIRA URL must be a JIRA Cloud instance (*.atlassian.net). "
                "Data Center/Server is not supported in Beta."
            ),
        }

    # No path beyond /
    if parsed.path and parsed.path not in ("", "/"):
        return {
            "code": "CFG_INVALID_JIRA_URL",
            "message": "JIRA URL must not include a path (use base URL only)",
        }

    return None


# ===================================================================
# Routing validation
# ===================================================================

_ACCOUNT_ID_RE = re.compile(r"^\d{12}$")
_JIRA_PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,9}$")
_MAX_PROJECT_KEY_LEN = 10
_MAX_ACCOUNT_NAME_LEN = 128


def validate_account_id_format(account_id: str) -> bool:
    """Check if account_id is exactly 12 digits."""
    return bool(isinstance(account_id, str) and _ACCOUNT_ID_RE.match(account_id))


def validate_jira_project_key_format(key: str) -> bool:
    """Check if key matches JIRA project key format (uppercase, starts with letter)."""
    if not isinstance(key, str):
        return False
    return bool(_JIRA_PROJECT_KEY_RE.match(key.strip().upper()))


def validate_routing_default(body: dict | None, *, platform: str = "jira") -> list[dict]:
    """Validate default routing input.

    BUG-S23-002: Platform-aware validation.
    - platform='jira': require jiraProject
    - platform='servicenow': require snowAssignmentGroupId, jiraProject optional
    Optional: jiraIssueType, snowAssignmentGroupId, snowAssignmentGroupName, snowRecordType
    """
    if not body or not isinstance(body, dict):
        return [{"code": "CFG_INVALID_REQUEST", "message": "Request body is required"}]

    if platform == "servicenow":
        # Require snowAssignmentGroupId for ServiceNow
        group_id = body.get("snowAssignmentGroupId")
        if not group_id or not isinstance(group_id, str) or not group_id.strip():
            return [{"code": "CFG_INVALID_SNOW_GROUP_ID",
                     "message": "snowAssignmentGroupId is required when platform is servicenow"}]
        if not _SNOW_GROUP_ID_RE.match(group_id):
            return [{"code": "CFG_INVALID_SNOW_GROUP_ID",
                     "message": "snowAssignmentGroupId must be a 32-character lowercase hex string"}]
        # jiraProject is optional for ServiceNow — validate format only if provided
        jira_project = body.get("jiraProject")
        if jira_project and isinstance(jira_project, str) and jira_project.strip():
            if not validate_jira_project_key_format(jira_project):
                return [{"code": "CFG_INVALID_JIRA_PROJECT",
                         "message": "jiraProject must be 2-10 uppercase alphanumeric characters starting with a letter"}]
    else:
        # JIRA platform: require jiraProject
        jira_project = body.get("jiraProject")
        if not jira_project or not isinstance(jira_project, str) or not jira_project.strip():
            return [{"code": "CFG_INVALID_JIRA_PROJECT", "message": "jiraProject is required"}]
        if not validate_jira_project_key_format(jira_project):
            return [{"code": "CFG_INVALID_JIRA_PROJECT",
                     "message": "jiraProject must be 2-10 uppercase alphanumeric characters starting with a letter"}]

    # Optional: ServiceNow routing fields
    snow_errors = validate_snow_routing_fields(body)
    if snow_errors:
        return snow_errors

    return []


def validate_routing_account(body: dict | None, *, platform: str = "jira") -> list[dict]:
    """Validate single account routing input.

    BUG-S23-002: Platform-aware validation.
    - platform='jira': require accountId + jiraProject
    - platform='servicenow': require accountId + snowAssignmentGroupId, jiraProject optional
    Optional: accountName, jiraIssueType
    """
    if not body or not isinstance(body, dict):
        return [{"code": "CFG_INVALID_REQUEST", "message": "Request body is required"}]

    account_id = body.get("accountId")
    if not account_id or not isinstance(account_id, str) or not account_id.strip():
        return [{"code": "CFG_INVALID_ACCOUNT_ID", "message": "accountId is required"}]

    if not validate_account_id_format(account_id.strip()):
        return [{"code": "CFG_INVALID_ACCOUNT_ID",
                 "message": "accountId must be exactly 12 digits"}]

    if platform == "servicenow":
        # Require snowAssignmentGroupId for ServiceNow
        group_id = body.get("snowAssignmentGroupId")
        if not group_id or not isinstance(group_id, str) or not group_id.strip():
            return [{"code": "CFG_INVALID_SNOW_GROUP_ID",
                     "message": "snowAssignmentGroupId is required when platform is servicenow"}]
        if not _SNOW_GROUP_ID_RE.match(group_id):
            return [{"code": "CFG_INVALID_SNOW_GROUP_ID",
                     "message": "snowAssignmentGroupId must be a 32-character lowercase hex string"}]
        # jiraProject is optional — validate format only if provided
        jira_project = body.get("jiraProject")
        if jira_project and isinstance(jira_project, str) and jira_project.strip():
            if not validate_jira_project_key_format(jira_project):
                return [{"code": "CFG_INVALID_JIRA_PROJECT",
                         "message": "jiraProject must be 2-10 uppercase alphanumeric characters starting with a letter"}]
    else:
        # JIRA platform: require jiraProject
        jira_project = body.get("jiraProject")
        if not jira_project or not isinstance(jira_project, str) or not jira_project.strip():
            return [{"code": "CFG_INVALID_JIRA_PROJECT", "message": "jiraProject is required"}]
        if not validate_jira_project_key_format(jira_project):
            return [{"code": "CFG_INVALID_JIRA_PROJECT",
                     "message": "jiraProject must be 2-10 uppercase alphanumeric characters starting with a letter"}]

    # Optional: accountName length check
    account_name = body.get("accountName", "")
    if isinstance(account_name, str) and len(account_name) > _MAX_ACCOUNT_NAME_LEN:
        return [{"code": "CFG_INVALID_ACCOUNT_NAME",
                 "message": f"accountName exceeds {_MAX_ACCOUNT_NAME_LEN} characters"}]

    # Optional: ServiceNow routing fields
    snow_errors = validate_snow_routing_fields(body)
    if snow_errors:
        return snow_errors

    return []


# ===================================================================
# ServiceNow routing field validation
# ===================================================================

_SNOW_GROUP_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_VALID_SNOW_RECORD_TYPES = frozenset({"incident", "change_request"})
_MAX_SNOW_GROUP_NAME_LEN = 128


def validate_snow_routing_fields(body: dict) -> list[dict]:
    """Validate optional ServiceNow routing fields when present.

    Fields are optional — only validated if non-empty in the request.
    Returns empty list on success, list of error dicts on failure.
    """
    # C-1: snow_assignment_group_id must be 32 lowercase hex chars
    group_id = body.get("snowAssignmentGroupId")
    if group_id is not None and group_id != "":
        if not isinstance(group_id, str) or not _SNOW_GROUP_ID_RE.match(group_id):
            return [{"code": "CFG_INVALID_SNOW_GROUP_ID",
                     "message": "snowAssignmentGroupId must be a 32-character lowercase hex string"}]

    # C-2: snow_record_type must be "incident" or "change_request"
    record_type = body.get("snowRecordType")
    if record_type is not None and record_type != "":
        if record_type not in _VALID_SNOW_RECORD_TYPES:
            return [{"code": "CFG_INVALID_SNOW_RECORD_TYPE",
                     "message": "snowRecordType must be 'incident' or 'change_request'"}]

    # C-3: snow_assignment_group_name length limit
    group_name = body.get("snowAssignmentGroupName")
    if group_name is not None and group_name != "":
        if not isinstance(group_name, str) or len(group_name) > _MAX_SNOW_GROUP_NAME_LEN:
            return [{"code": "CFG_INVALID_SNOW_GROUP_NAME",
                     "message": f"snowAssignmentGroupName exceeds {_MAX_SNOW_GROUP_NAME_LEN} characters"}]

    return []
