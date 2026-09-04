"""Lightweight JIRA Cloud client for connection validation.

Uses urllib3 (available in Lambda runtime). No retries — this is a
one-shot validation call with a 10-second timeout.

IMPL-SEC-027-C2: Credential redaction — no token in any log statement.
IMPL-SEC-027-H4: TLS certificate verification must NOT be disabled.
IMPL-SEC-027-M2: Sanitize displayName from JIRA (strip HTML).
"""
from __future__ import annotations

import base64
import json
import logging
import re

import urllib3

logger = logging.getLogger("resolve_core")

_CONNECT_TIMEOUT = 5.0
_READ_TIMEOUT = 10.0

# IMPL-SEC-027-M2: Strip HTML tags from user-controlled strings
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MAX_DISPLAY_NAME_LEN = 200


def validate_connection(base_url: str, email: str, api_token: str) -> dict:
    """Call GET {base_url}/rest/api/3/myself to validate JIRA credentials.

    Returns:
        {"success": True, "displayName": "...", "accountId": "..."}
        or {"success": False, "errorCode": "CONN_JIRA_*", "httpStatus": int, "message": "..."}

    IMPL-SEC-027-C2: Never logs api_token. Catches all exceptions and
    returns sanitized error dicts — no raw exception messages that might
    contain credentials.
    """
    url = f"{base_url}/rest/api/3/myself"

    # IMPL-SEC-027-C2: Build auth header — never log this value
    auth_bytes = base64.b64encode(f"{email}:{api_token}".encode("utf-8")).decode("ascii")
    headers = {
        "Authorization": f"Basic {auth_bytes}",
        "Accept": "application/json",
    }

    # IMPL-SEC-027-H4: Default urllib3 PoolManager verifies TLS certificates.
    # Do NOT pass cert_reqs='CERT_NONE'.
    http = urllib3.PoolManager(
        timeout=urllib3.Timeout(connect=_CONNECT_TIMEOUT, read=_READ_TIMEOUT),
        retries=False,  # No retries — user can click "Test" again
    )

    try:
        # IMPL-SEC-027-C1: No redirects — valid JIRA Cloud does not redirect /myself
        response = http.request("GET", url, headers=headers, redirect=False)
    except urllib3.exceptions.MaxRetryError:
        logger.warning("JIRA unreachable: %s", base_url)
        return {
            "success": False,
            "errorCode": "CONN_JIRA_UNREACHABLE",
            "httpStatus": 0,
            "message": "Could not connect to JIRA. Check the URL and try again.",
        }
    except urllib3.exceptions.TimeoutError:
        logger.warning("JIRA timeout: %s", base_url)
        return {
            "success": False,
            "errorCode": "CONN_JIRA_UNREACHABLE",
            "httpStatus": 0,
            "message": "Connection to JIRA timed out. Check the URL and try again.",
        }
    except Exception:
        # IMPL-SEC-027-C2: Catch-all — never expose raw exception message
        # which might contain credentials from the HTTP library internals.
        logger.warning("JIRA connection error: %s", base_url, exc_info=False)
        return {
            "success": False,
            "errorCode": "CONN_JIRA_UNREACHABLE",
            "httpStatus": 0,
            "message": "Could not connect to JIRA. Check the URL and try again.",
        }

    status = response.status

    if status == 200:
        try:
            data = json.loads(response.data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {
                "success": False,
                "errorCode": "CONN_JIRA_SERVER_ERROR",
                "httpStatus": status,
                "message": "JIRA returned an invalid response.",
            }

        display_name = _sanitize_display_name(data.get("displayName", ""))
        account_id = str(data.get("accountId", ""))

        logger.info("JIRA connection validated: %s (user: %s)", base_url, display_name)
        return {
            "success": True,
            "displayName": display_name,
            "accountId": account_id,
        }

    # IMPL-SEC-027-H2: Merge 401/403 into single error code
    if status in (401, 403):
        logger.warning("JIRA auth failed (%d): %s", status, base_url)
        return {
            "success": False,
            "errorCode": "CONN_JIRA_AUTH_FAILED",
            "httpStatus": status,
            "message": "JIRA authentication failed. Check email and API token.",
        }

    if status >= 500:
        logger.warning("JIRA server error (%d): %s", status, base_url)
        return {
            "success": False,
            "errorCode": "CONN_JIRA_SERVER_ERROR",
            "httpStatus": status,
            "message": f"JIRA returned server error ({status}). Try again later.",
        }

    # Unexpected status
    logger.warning("JIRA unexpected status (%d): %s", status, base_url)
    return {
        "success": False,
        "errorCode": "CONN_JIRA_SERVER_ERROR",
        "httpStatus": status,
        "message": f"JIRA returned unexpected status ({status}).",
    }


def _sanitize_display_name(value: str) -> str:
    """Strip HTML tags and truncate displayName.

    IMPL-SEC-027-M2: displayName is user-controlled in JIRA.
    """
    if not isinstance(value, str):
        return ""
    cleaned = _HTML_TAG_RE.sub("", value).strip()
    return cleaned[:_MAX_DISPLAY_NAME_LEN]
