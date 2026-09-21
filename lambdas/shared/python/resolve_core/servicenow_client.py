"""ServiceNow REST API client for ticket CRUD operations.

Implements ITSMClient interface. Handles OAuth 2.0 (ROPC grant),
token refresh via credential_cache, exponential backoff with jitter
on 429/5xx, SSRF protection, and structured error reporting.

FINDING-03: URL must end with .service-now.com (SSRF protection).
FINDING-06: Password stored for ROPC flow — documented risk.
FINDING-08: HTML-escape handled by ServiceNowFormatter, not here.

Consumers: handler.py (ServiceNow Integration Lambda), sync/handler.py.
Dependencies: urllib3 (Lambda runtime), resolve_core.credential_cache.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, quote, urlencode

import urllib3

from resolve_core.credential_cache import (
    CachedCredentialProvider,
    CredentialRefreshError,
    get_credential_provider,
)
from resolve_core.itsm_client import (
    BulkCreateFailure,
    BulkCreateResult,
    ConnectionValidationResult,
    ContentFormatter,
    ITSMAPIError,
    ITSMClient,
    TargetValidationResult,
    TicketCreateRequest,
    TicketCreateResponse,
    TicketStatus,
)
from resolve_core.status_mapping import normalize_status

logger = logging.getLogger("resolve_core")

# SSRF protection: only *.service-now.com over HTTPS
_SNOW_HOST_RE = re.compile(
    r"^[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?\.service-now\.com$"
)

_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 30.0
_MAX_RETRIES = 5
_MAX_RETRY_DELAY = 60
_INTER_CREATE_DELAY = 0.2  # 200ms between sequential creates
_PAGE_SIZE = 200

_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503})


def _validate_snow_url(url: str) -> str:
    """Validate and normalize a ServiceNow instance URL.

    FINDING-03: SSRF protection — only *.service-now.com over HTTPS.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("ServiceNow instance URL is required")

    url = url.strip().rstrip("/")
    if not url.startswith("https://"):
        raise ValueError("ServiceNow URL must start with https://")

    parsed = urlparse(url)
    if parsed.port is not None:
        raise ValueError("ServiceNow URL must not include a port number")

    hostname = parsed.hostname or ""
    if not _SNOW_HOST_RE.match(hostname):
        raise ValueError(
            "ServiceNow URL must be a valid instance (*.service-now.com)"
        )

    if parsed.path and parsed.path not in ("", "/"):
        raise ValueError("ServiceNow URL must not include a path")

    if parsed.query or parsed.fragment:
        raise ValueError("ServiceNow URL must not include query strings or fragments")

    return f"https://{hostname}"


class ServiceNowClient(ITSMClient):
    """ServiceNow REST API client with OAuth 2.0 and retry logic."""

    def __init__(
        self,
        instance_url: str,
        secret_arn: str,
        formatter: ContentFormatter,
        record_type: str = "change_request",
    ):
        """Initialize ServiceNow client.

        Args:
            instance_url: ServiceNow instance URL (https://org.service-now.com).
            secret_arn: Secrets Manager ARN for OAuth credentials.
            formatter: ContentFormatter for description rendering.
            record_type: Default record type ("incident" or "change_request").
        """
        self._instance_url = _validate_snow_url(instance_url)
        self._secret_arn = secret_arn
        self._formatter = formatter
        self._record_type = record_type
        self._http = urllib3.PoolManager(
            timeout=urllib3.Timeout(connect=_CONNECT_TIMEOUT, read=_READ_TIMEOUT),
            retries=False,
        )
        self._credential_provider = get_credential_provider(
            secret_arn, refresh_callback=self._refresh_token, ttl_seconds=1500,
        )

    def __repr__(self) -> str:
        return f"<ServiceNowClient instance_url={self._instance_url!r}>"

    # ------------------------------------------------------------------
    # OAuth Token Management
    # ------------------------------------------------------------------

    def _get_access_token(self) -> str:
        """Get valid access token from cache or trigger refresh.

        BUG-S23-018: Previously called invalidate() which reset the cache,
        causing get_credentials() to re-read Secrets Manager (same stale
        token) instead of invoking the refresh callback. Now calls
        _password_grant() directly when token is empty or expired.
        """
        creds = self._credential_provider.get_credentials()
        token = creds.get("access_token", "")
        expires_at = creds.get("token_expires_at", 0)

        # Token is valid — return it
        if token and time.time() < float(expires_at) - 60:
            return token

        # Token empty or expired — perform OAuth refresh directly
        new_creds = self._refresh_token(creds)
        self._credential_provider.update(new_creds)
        return new_creds.get("access_token", "")

    def _refresh_token(self, current_creds: dict) -> dict:
        """Refresh OAuth token using refresh_token grant.

        Called by CachedCredentialProvider when TTL expires.
        Falls back to full password grant if refresh fails.
        """
        refresh_token = current_creds.get("refresh_token")
        client_id = current_creds.get("client_id", "")
        client_secret = current_creds.get("client_secret", "")

        if refresh_token:
            try:
                return self._token_exchange(
                    grant_type="refresh_token",
                    client_id=client_id,
                    client_secret=client_secret,
                    refresh_token=refresh_token,
                    current_creds=current_creds,
                )
            except (ITSMAPIError, CredentialRefreshError):
                logger.warning("Refresh token failed, falling back to password grant")

        # Full re-auth with password
        return self._password_grant(current_creds)

    def _password_grant(self, creds: dict) -> dict:
        """Perform full OAuth password grant."""
        return self._token_exchange(
            grant_type="password",
            client_id=creds.get("client_id", ""),
            client_secret=creds.get("client_secret", ""),
            username=creds.get("username", ""),
            password=creds.get("password", ""),
            current_creds=creds,
        )

    def _token_exchange(self, *, grant_type: str, client_id: str,
                        client_secret: str, current_creds: dict,
                        refresh_token: str = "", username: str = "",
                        password: str = "") -> dict:
        """Exchange credentials for an OAuth token at /oauth_token.do."""
        url = f"{self._instance_url}/oauth_token.do"

        # LATENT HARDENING: urlencode() applies correct form semantics (space -> "+",
        # "/" -> "%2F"), matching curl --data-urlencode. Protects customers whose
        # secret/password contains a literal space or slash. No behavior change otherwise.
        fields = {"grant_type": grant_type, "client_id": client_id, "client_secret": client_secret}
        if grant_type == "refresh_token":
            fields["refresh_token"] = refresh_token
        elif grant_type == "password":
            fields["username"] = username
            fields["password"] = password
        params = urlencode(fields)

        response = self._http.request(
            "POST", url,
            body=params.encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            # PRIMARY FIX: follow redirects on the token POST ONLY (ServiceNow may answer
            # /oauth_token.do with a 3xx that must be followed to the final 200+token).
            # Authenticated table GETs elsewhere stay redirect=False to avoid masking auth
            # failures behind a login redirect.
            redirect=True,
        )

        if response.status != 200:
            # INSTRUMENTATION: log the real upstream signal; never log request creds.
            body_snippet = ""
            try:
                body_snippet = response.data.decode("utf-8", errors="replace")[:500]
            except Exception:
                body_snippet = "<undecodable>"
            logger.warning(json.dumps({
                "event": "SNOW_TOKEN_EXCHANGE_NON_200",
                "status": response.status,
                "location": response.headers.get("Location"),
                "body_snippet": body_snippet,
            }))
            raise CredentialRefreshError(
                f"Token exchange failed: ServiceNow returned HTTP {response.status}"
            )

        data = json.loads(response.data.decode("utf-8"))
        if "access_token" not in data:
            raise CredentialRefreshError("No access_token in response")

        # Build updated credentials preserving static fields
        updated = dict(current_creds)
        updated["access_token"] = data["access_token"]
        updated["refresh_token"] = data.get("refresh_token", refresh_token)
        updated["token_expires_at"] = time.time() + int(data.get("expires_in", 1800))
        return updated

    # ------------------------------------------------------------------
    # ITSMClient Implementation
    # ------------------------------------------------------------------

    def create_ticket(self, request: TicketCreateRequest) -> TicketCreateResponse:
        """Create a single ticket (incident or change_request)."""
        table = request.record_type if request.record_type in ("incident", "change_request") else self._record_type
        body = self._build_create_body(request)

        resp = self._request_with_retry(
            "POST", f"/api/now/table/{table}", body=body,
        )

        result = resp.get("result", {})
        sys_id = result.get("sys_id", "")
        number = result.get("number", "")

        return TicketCreateResponse(
            ticket_id=number,
            ticket_url=f"{self._instance_url}/nav_to.do?uri=/{table}.do?sys_id={sys_id}",
            platform="servicenow",
            raw_response=result,
        )

    def bulk_create_tickets(self, requests: List[TicketCreateRequest]) -> BulkCreateResult:
        """Create tickets sequentially with 200ms delay between calls."""
        result = BulkCreateResult()

        for idx, req in enumerate(requests):
            if idx > 0:
                time.sleep(_INTER_CREATE_DELAY)
            try:
                ticket = self.create_ticket(req)
                result.successes.append(ticket)
            except ITSMAPIError as exc:
                result.failures.append(BulkCreateFailure(
                    index=idx,
                    error_message=exc.error_message[:500],
                    status_code=exc.status_code,
                    retryable=exc.retryable,
                ))

        return result

    def update_ticket(self, ticket_id: str, fields: Dict[str, Any]) -> None:
        """Update fields on an existing ticket via PATCH.

        ticket_id format: "{table}/{sys_id}" (e.g. "incident/abc123").
        """
        table, sys_id = self._parse_ticket_ref(ticket_id)
        self._request_with_retry("PATCH", f"/api/now/table/{table}/{sys_id}", body=fields)

    def add_work_note(self, ticket_id: str, note: str) -> None:
        """Add a work note by PATCHing the work_notes field."""
        table, sys_id = self._parse_ticket_ref(ticket_id)
        formatted = self._formatter.format_work_note(note)
        self._request_with_retry(
            "PATCH", f"/api/now/table/{table}/{sys_id}",
            body={"work_notes": formatted},
        )

    def attach_file(self, ticket_id: str, filename: str, content: bytes, content_type: str) -> None:
        """Attach a file to a ticket via /api/now/attachment/file."""
        table, sys_id = self._parse_ticket_ref(ticket_id)
        url = (
            f"{self._instance_url}/api/now/attachment/file"
            f"?table_name={quote(table)}&table_sys_id={quote(sys_id)}"
            f"&file_name={quote(filename)}"
        )
        token = self._get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type,
            "Accept": "application/json",
        }

        response = self._http.request("POST", url, body=content, headers=headers, redirect=False)

        if response.status == 401:
            # BUG-S23-018: Perform real re-auth instead of invalidate + re-read
            try:
                creds = self._credential_provider.get_credentials()
                new_creds = self._password_grant(creds)
                self._credential_provider.update(new_creds)
                token = new_creds.get("access_token", "")
            except (CredentialRefreshError, Exception):
                raise ITSMAPIError(401, "Attachment auth failed after re-auth attempt", False)
            headers["Authorization"] = f"Bearer {token}"
            response = self._http.request("POST", url, body=content, headers=headers, redirect=False)

        if response.status not in (200, 201):
            raise ITSMAPIError(response.status, f"Attachment failed: HTTP {response.status}", response.status in _RETRYABLE_STATUSES)

    def poll_status_changes(self, since: str) -> List[TicketStatus]:
        """Poll for ticket status changes since a given timestamp."""
        results: List[TicketStatus] = []

        for table in ("incident", "change_request"):
            platform_key = f"servicenow_{table.replace('change_request', 'change')}"
            query = (
                f"correlation_idISNOTEMPTY"
                f"^sys_updated_on>{since}"
            )
            offset = 0

            while True:
                params = (
                    f"sysparm_query={quote(query)}"
                    f"&sysparm_fields=sys_id,number,state,correlation_id,sys_updated_on"
                    f"&sysparm_limit={_PAGE_SIZE}&sysparm_offset={offset}"
                )
                resp = self._request_with_retry(
                    "GET", f"/api/now/table/{table}?{params}", body=None,
                )

                records = resp.get("result", [])
                if not isinstance(records, list):
                    break

                for record in records:
                    state = str(record.get("state", ""))
                    normalized = normalize_status(platform_key, state)
                    results.append(TicketStatus(
                        ticket_id=record.get("number", ""),
                        normalized_status=normalized,
                        raw_status=state,
                        last_updated=record.get("sys_updated_on", ""),
                        platform="servicenow",
                    ))

                if len(records) < _PAGE_SIZE:
                    break
                offset += _PAGE_SIZE
                time.sleep(0.2)  # Inter-page delay

        return results

    def validate_connection(self) -> ConnectionValidationResult:
        """Validate OAuth exchange + user lookup + itil role check."""
        try:
            # Force fresh token exchange
            self._credential_provider.invalidate()
            creds = self._credential_provider.get_credentials()
            username = creds.get("username", "")

            # Query sys_user for the integration user
            query = f"user_name={quote(username)}"
            resp = self._request_with_retry(
                "GET",
                f"/api/now/table/sys_user?sysparm_query={query}&sysparm_fields=sys_id,user_name,name&sysparm_limit=1",
                body=None,
            )
            users = resp.get("result", [])
            if not users:
                return ConnectionValidationResult(
                    valid=False, errors=[f"User '{username}' not found in ServiceNow."],
                )

            user = users[0]
            display_name = user.get("name", username)
            user_sys_id = user.get("sys_id", "")

            # Verify itil role
            role_query = f"user={user_sys_id}^role.name=itil"
            role_resp = self._request_with_retry(
                "GET",
                f"/api/now/table/sys_user_has_role?sysparm_query={quote(role_query)}&sysparm_limit=1",
                body=None,
            )
            roles = role_resp.get("result", [])
            if not roles:
                return ConnectionValidationResult(
                    valid=False,
                    display_name=display_name,
                    errors=[f"User '{username}' does not have the 'itil' role."],
                )

            return ConnectionValidationResult(valid=True, display_name=display_name)

        except ITSMAPIError as exc:
            return ConnectionValidationResult(
                valid=False, errors=[f"ServiceNow API error: {exc.error_message}"],
            )
        except CredentialRefreshError as exc:
            return ConnectionValidationResult(
                valid=False, errors=[f"Authentication failed: {str(exc)}"],
            )

    def validate_routing_target(self, target: str) -> TargetValidationResult:
        """Validate that an assignment group sys_id exists."""
        # Sanitize: sys_id should be 32 hex chars
        if not re.match(r"^[a-f0-9]{32}$", target):
            return TargetValidationResult(
                valid=False, errors=["Invalid assignment group sys_id format."],
            )

        try:
            resp = self._request_with_retry(
                "GET",
                f"/api/now/table/sys_user_group/{target}?sysparm_fields=sys_id,name",
                body=None,
            )
            result = resp.get("result", {})
            if not result or not result.get("sys_id"):
                return TargetValidationResult(
                    valid=False, errors=["Assignment group not found."],
                )
            return TargetValidationResult(valid=True, target_name=result.get("name", target))
        except ITSMAPIError as exc:
            if exc.status_code == 404:
                return TargetValidationResult(
                    valid=False, errors=["Assignment group not found."],
                )
            return TargetValidationResult(
                valid=False, errors=[f"API error: {exc.error_message}"],
            )

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------

    def _build_create_body(self, request: TicketCreateRequest) -> dict:
        """Build ServiceNow create request body from TicketCreateRequest."""
        description = self._formatter.format_description(request.description_content)

        body: Dict[str, Any] = {
            "short_description": request.summary[:160],
            "description": description,
            "assignment_group": request.routing_target,
            "correlation_id": request.correlation_id or request.campaign_id,
            "correlation_display": f"Resolve Campaign: {request.campaign_id}",
        }

        if request.due_date:
            body["due_date"] = f"{request.due_date} 00:00:00"

        # Set urgency/impact for priority calculation
        body["urgency"] = str(request.urgency)
        body["impact"] = str(request.impact)

        # Record-type-specific fields
        table = request.record_type if request.record_type in ("incident", "change_request") else self._record_type
        if table == "change_request":
            body["type"] = "standard"
            body["category"] = "Cloud"
            # Merge change request fields from formatter if available
            if hasattr(self._formatter, "format_change_request_fields"):
                cr_fields = self._formatter.format_change_request_fields(
                    request.description_content
                )
                body.update(cr_fields)
        else:
            body["category"] = "Cloud"
            body["subcategory"] = "AWS"

        return body

    def _parse_ticket_ref(self, ticket_id: str) -> tuple:
        """Parse 'table/sys_id' format. Falls back to incident."""
        if "/" in ticket_id:
            parts = ticket_id.split("/", 1)
            return parts[0], parts[1]
        return self._record_type, ticket_id

    def _request_with_retry(self, method: str, path: str, body: Optional[dict]) -> dict:
        """Make HTTP request with exponential backoff on retryable errors."""
        url = f"{self._instance_url}{path}" if not path.startswith("http") else path

        last_status = 0
        last_body: dict = {}

        for attempt in range(_MAX_RETRIES):
            token = self._get_access_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }

            if body is not None and method in ("POST", "PATCH", "PUT"):
                headers["Content-Type"] = "application/json"
                encoded = json.dumps(body, separators=(",", ":"), default=str).encode("utf-8")
            else:
                encoded = None

            try:
                response = self._http.request(
                    method, url,
                    body=encoded,
                    headers=headers,
                    redirect=False,
                )
            except (urllib3.exceptions.MaxRetryError, urllib3.exceptions.TimeoutError):
                logger.warning(
                    "ServiceNow connection error — attempt=%d/%d path=%s",
                    attempt + 1, _MAX_RETRIES, path[:200],
                )
                if attempt < _MAX_RETRIES - 1:
                    self._backoff(attempt, None)
                    continue
                raise ITSMAPIError(0, "Connection failed", True)

            status = response.status

            # Reject redirects
            if 300 <= status < 400:
                raise ITSMAPIError(status, "Redirect rejected", False)

            # Parse body
            try:
                resp_body = json.loads(response.data.decode("utf-8")) if response.data else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                resp_body = {}

            last_status = status
            last_body = resp_body

            # Success
            if 200 <= status < 300:
                return resp_body

            # 401 — attempt full re-auth once (BUG-S23-018)
            if status == 401 and attempt == 0:
                try:
                    creds = self._credential_provider.get_credentials()
                    new_creds = self._password_grant(creds)
                    self._credential_provider.update(new_creds)
                except (CredentialRefreshError, Exception):
                    error_msg = resp_body.get("error", {}).get("message", f"HTTP {status}")
                    raise ITSMAPIError(status, error_msg, False)
                continue

            # Non-retryable
            if status in (400, 401, 403, 404):
                error_msg = resp_body.get("error", {}).get("message", f"HTTP {status}")
                raise ITSMAPIError(status, error_msg, False)

            # Retryable
            if status in _RETRYABLE_STATUSES:
                if attempt >= _MAX_RETRIES - 1:
                    break
                retry_after = response.headers.get("Retry-After")
                logger.warning(
                    "ServiceNow retryable error — status=%d attempt=%d/%d path=%s",
                    status, attempt + 1, _MAX_RETRIES, path[:200],
                )
                self._backoff(attempt, retry_after)
                continue

            # Unknown status
            error_msg = resp_body.get("error", {}).get("message", f"HTTP {status}")
            raise ITSMAPIError(status, error_msg, False)

        # Exhausted
        error_msg = last_body.get("error", {}).get("message", f"HTTP {last_status}")
        raise ITSMAPIError(last_status, error_msg, last_status in _RETRYABLE_STATUSES)

    @staticmethod
    def _backoff(attempt: int, retry_after: Optional[str]) -> None:
        """Sleep with exponential backoff and jitter."""
        if retry_after:
            try:
                delay = min(int(retry_after), _MAX_RETRY_DELAY)
            except (ValueError, TypeError):
                delay = min(2 ** attempt + random.uniform(0, 1), _MAX_RETRY_DELAY)
        else:
            delay = min(2 ** attempt + random.uniform(0, 1), _MAX_RETRY_DELAY)
        time.sleep(max(delay, 0))
