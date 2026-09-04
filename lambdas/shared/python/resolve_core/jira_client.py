"""JIRA Cloud REST API v3 client for ticket CRUD operations.

Handles Basic auth, exponential backoff with jitter on 429/5xx,
redirect rejection, and structured error reporting. Uses urllib3
(available in Lambda runtime) — no external dependencies.

IMPL-SEC-019-C1: __repr__ excludes credentials.
IMPL-SEC-019-C2: JiraApiError excludes auth headers.
IMPL-SEC-019-C3: Retry logs exclude headers/body.
IMPL-SEC-019-C4: URL validated on construction.
IMPL-SEC-019-C5: Redirects rejected on all requests.
IMPL-SEC-019-C6: TLS verification enabled (default urllib3).
IMPL-SEC-019-C7: Connection pool created once per instance.

Consumers: handler.py (JIRA Integration Lambda).
Dependencies: urllib3 (Lambda runtime), Python stdlib.
"""

from __future__ import annotations

import base64
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import urllib3

logger = logging.getLogger("resolve_core")

# IMPL-SEC-019-C4: Reuse the same strict pattern from api/validators.py
_ATLASSIAN_HOST_RE = re.compile(
    r"^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?\.atlassian\.net$"
)

_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 30.0
_MAX_RETRIES = 5
_MAX_RETRY_DELAY = 60
_MAX_5XX_RETRIES = 3

# HTTP status classification
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503})
_NON_RETRYABLE_STATUSES = frozenset({400, 401, 403, 404})

# Bulk create constants
_BULK_BATCH_SIZE = 50
_INTER_BATCH_DELAY = 2  # seconds between bulk batches
_TIMEOUT_GUARD_MS = 30_000  # stop if < 30s remaining

# SEC-S21-06: JIRA issue key format validation
_JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")


@dataclass
class BulkCreateResult:
    """Result of a bulk JIRA issue creation operation."""

    successes: List[Dict[str, Any]] = field(default_factory=list)
    failures: List[Dict[str, Any]] = field(default_factory=list)
    total_requested: int = 0
    total_batches: int = 0
    total_retries: int = 0
    elapsed_seconds: float = 0.0
    exhausted: bool = False


class JiraApiError(Exception):
    """JIRA API error with status and parsed response body.

    IMPL-SEC-019-C2: Does NOT store request headers or auth string.
    __str__ returns only status and endpoint path — no body dump.
    """

    def __init__(self, status: int, body: dict, url: str):
        self.status = status
        self.body = body
        self.url = url
        self.retryable = status in _RETRYABLE_STATUSES
        self.field_errors = body.get("errors", {})
        super().__init__(f"JiraApiError({status}, {url})")

    def __repr__(self) -> str:
        return f"JiraApiError(status={self.status}, url={self.url!r})"


class JiraNotFoundError(JiraApiError):
    """JIRA returned 404 — resource does not exist.

    ISEC-02: Subclasses JiraApiError, inherits safe __repr__.
    Only raised on HTTP 404 to enable E-9 ticket-not-found handling.
    """

    pass


def _validate_jira_url(url: str) -> str:
    """Validate and normalize a JIRA Cloud base URL.

    IMPL-SEC-019-C4: SSRF protection — only *.atlassian.net over HTTPS.
    Raises ValueError on invalid URL.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("JIRA base URL is required")

    url = url.strip().rstrip("/")

    if not url.startswith("https://"):
        raise ValueError("JIRA URL must start with https://")

    try:
        parsed = urlparse(url)
    except Exception:
        raise ValueError("Invalid JIRA URL format")

    if parsed.port is not None:
        raise ValueError("JIRA URL must not include a port number")

    hostname = parsed.hostname or ""
    if not _ATLASSIAN_HOST_RE.match(hostname):
        raise ValueError(
            "JIRA URL must be a JIRA Cloud instance (*.atlassian.net)"
        )

    if parsed.path and parsed.path not in ("", "/"):
        raise ValueError("JIRA URL must not include a path")

    return url


class JiraClient:
    """JIRA Cloud REST API v3 client with retry and rate limiting.

    IMPL-SEC-019-C1: __repr__ excludes credentials.
    IMPL-SEC-019-C7: PoolManager created once per instance.
    """

    def __init__(self, base_url: str, email: str, api_token: str):
        """Initialize JIRA client.

        Args:
            base_url: JIRA Cloud base URL (e.g. https://org.atlassian.net).
            email: Automation account email.
            api_token: JIRA API token.

        Raises:
            ValueError: If base_url fails validation.
        """
        self._base_url = _validate_jira_url(base_url)
        # IMPL-SEC-019-C6: Default urllib3 PoolManager verifies TLS.
        self._http = urllib3.PoolManager(
            timeout=urllib3.Timeout(
                connect=_CONNECT_TIMEOUT, read=_READ_TIMEOUT,
            ),
            retries=False,
        )
        auth_bytes = base64.b64encode(
            f"{email}:{api_token}".encode("utf-8"),
        ).decode("ascii")
        self._auth_header = f"Basic {auth_bytes}"

    def __repr__(self) -> str:
        # IMPL-SEC-019-C1: No credentials in repr
        return f"<JiraClient base_url={self._base_url!r}>"

    def __str__(self) -> str:
        return self.__repr__()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_issue(
        self,
        project_key: str,
        summary: str,
        description_adf: dict,
        labels: List[str],
        due_date: Optional[str] = None,
        issue_type: str = "Task",
    ) -> dict:
        """Create a JIRA issue via POST /rest/api/3/issue.

        Args:
            project_key: JIRA project key (e.g. "CLOUDOPS").
            summary: Issue summary (max 255 chars).
            description_adf: ADF document dict for the description.
            labels: List of label strings.
            due_date: Optional due date in "YYYY-MM-DD" format.
            issue_type: Issue type name (default "Task").

        Returns:
            Response dict with "key", "id", "self" fields.

        Raises:
            JiraApiError: On non-retryable or exhausted-retry errors.
        """
        fields: Dict[str, Any] = {
            "project": {"key": project_key},
            "summary": summary,
            "issuetype": {"name": issue_type},
            "description": description_adf,
            "labels": labels,
        }
        if due_date:
            fields["duedate"] = due_date

        return self._request_with_retry(
            "POST",
            "/rest/api/3/issue",
            body={"fields": fields},
            expected_status=201,
        )

    def add_comment(self, issue_key: str, body_adf: dict) -> dict:
        """Add a comment to a JIRA issue.

        Args:
            issue_key: Issue key (e.g. "CLOUDOPS-123").
            body_adf: ADF document dict for the comment body.

        Returns:
            Response dict with comment metadata.

        Raises:
            JiraApiError: On non-retryable or exhausted-retry errors.
        """
        return self._request_with_retry(
            "POST",
            f"/rest/api/3/issue/{issue_key}/comment",
            body={"body": body_adf},
            expected_status=201,
        )

    def update_issue(self, issue_key: str, fields: dict) -> None:
        """Update fields on an existing JIRA issue.

        ISEC-01: Uses _request_with_retry — no separate HTTP path.
        ISEC-01b: Validates issue_key against _JIRA_KEY_RE.
        ISEC-01d: Handles 204 empty response body.

        Args:
            issue_key: Issue key (e.g. "CLOUDOPS-123").
            fields: Dict of field names to new values.

        Raises:
            JiraNotFoundError: If the issue does not exist (404).
            JiraApiError: On other non-retryable or exhausted-retry errors.
            ValueError: If issue_key format is invalid.
        """
        if not isinstance(issue_key, str) or not _JIRA_KEY_RE.match(issue_key):
            raise ValueError(f"Invalid JIRA issue key format: {issue_key!r}")

        self._request_with_retry(
            "PUT",
            f"/rest/api/3/issue/{issue_key}",
            body={"fields": fields},
            expected_status=204,
        )

    def search_issues(
        self,
        jql: str,
        fields: List[str],
        max_results: int = 50,
        next_page_token: Optional[str] = None,
    ) -> dict:
        """Search issues via POST /rest/api/3/search/jql.

        STORY-122: /rest/api/3/search was permanently removed by
        Atlassian (CHANGE-2046, HTTP 410 Gone). This method now calls
        the replacement endpoint, which uses cursor-based pagination
        (nextPageToken/isLast) instead of offset-based pagination
        (startAt/total). The response body has NO "total" field —
        callers must not rely on it.

        Args:
            jql: JQL query string. Must be "bounded" (include an
                actual search restriction) — the new endpoint rejects
                fully unrestricted queries.
            fields: List of field names to return. The new endpoint
                defaults to "id" only when omitted, so callers that
                need more fields must always pass this explicitly.
            max_results: Max results per page (default 50).
            next_page_token: Cursor from a previous response's
                "nextPageToken" field. Omit/None for the first page.
                Tokens expire after 7 days (not a concern for a
                per-invocation poll).

        Returns:
            Response dict with "issues", optionally "nextPageToken"
            (absent when there is no next page), and "isLast".

        Raises:
            JiraApiError: On non-retryable or exhausted-retry errors.
        """
        body: Dict[str, Any] = {
            "jql": jql,
            "fields": fields,
            "maxResults": max_results,
        }
        if next_page_token:
            body["nextPageToken"] = next_page_token

        return self._request_with_retry(
            "POST",
            "/rest/api/3/search/jql",
            body=body,
            expected_status=200,
        )

    def count_issues(self, jql: str) -> int:
        """Get an approximate issue count via
        POST /rest/api/3/search/approximate-count.

        STORY-122: replacement for the old pattern of calling
        search_issues(jql, fields=["key"], max_results=0) and reading
        the removed "total" field. This endpoint returns only a count
        — no issues array — in a single call. Atlassian documents the
        count as an estimate (recent updates may not be immediately
        reflected), which is acceptable for directional/threshold
        checks such as the orphan-ticket alert.

        Args:
            jql: JQL query string. Must be "bounded" (same requirement
                as search_issues).

        Returns:
            Approximate count of issues matching the JQL. Non-integer
            or negative responses are treated as 0.

        Raises:
            JiraApiError: On non-retryable or exhausted-retry errors.
        """
        resp = self._request_with_retry(
            "POST",
            "/rest/api/3/search/approximate-count",
            body={"jql": jql},
            expected_status=200,
        )
        count = resp.get("count", 0)
        if not isinstance(count, int) or count < 0:
            count = 0
        return count

    # ------------------------------------------------------------------
    # Internal HTTP with retry
    # ------------------------------------------------------------------

    def _request_with_retry(
        self,
        method: str,
        path: str,
        body: dict,
        expected_status: int,
    ) -> dict:
        """Make an HTTP request with exponential backoff on retryable errors.

        IMPL-SEC-019-C3: Retry logs exclude headers and body.
        IMPL-SEC-019-C5: redirect=False on all requests.
        """
        url = f"{self._base_url}{path}"
        encoded_body = json.dumps(body, separators=(",", ":"), default=str)
        headers = {
            "Authorization": self._auth_header,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        last_status = 0
        last_body: dict = {}

        for attempt in range(_MAX_RETRIES):
            try:
                # IMPL-SEC-019-C5: No redirects
                response = self._http.request(
                    method, url,
                    body=encoded_body.encode("utf-8"),
                    headers=headers,
                    redirect=False,
                )
            except (urllib3.exceptions.MaxRetryError,
                    urllib3.exceptions.TimeoutError) as exc:
                # IMPL-SEC-019-C3: Log type only, no headers/body
                logger.warning(
                    "JIRA connection error — attempt=%d/%d path=%s "
                    "error_type=%s",
                    attempt + 1, _MAX_RETRIES, path,
                    type(exc).__name__,
                )
                if attempt < _MAX_RETRIES - 1:
                    self._backoff(attempt, None)
                    continue
                raise JiraApiError(0, {}, path)
            except Exception:
                logger.warning(
                    "JIRA unexpected error — attempt=%d/%d path=%s",
                    attempt + 1, _MAX_RETRIES, path,
                    exc_info=False,
                )
                raise JiraApiError(0, {}, path)

            status = response.status

            # IMPL-SEC-019-C5: Reject redirects
            if 300 <= status < 400:
                location = response.headers.get("Location", "REDACTED")
                logger.warning(
                    "JIRA redirect rejected — status=%d path=%s "
                    "location=%s",
                    status, path, location[:200],
                )
                raise JiraApiError(status, {}, path)

            # Parse response body
            # ISEC-01d: Handle 204 No Content (empty body)
            resp_data = response.data
            if resp_data:
                try:
                    resp_body = json.loads(resp_data.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    resp_body = {}
            else:
                resp_body = {}

            last_status = status
            last_body = resp_body

            # Success
            if status == expected_status:
                return resp_body

            # ISEC-02b: Raise JiraNotFoundError on 404
            if status == 404:
                raise JiraNotFoundError(status, resp_body, path)

            # Non-retryable
            if status in _NON_RETRYABLE_STATUSES:
                raise JiraApiError(status, resp_body, path)

            # Retryable — check limits
            if status == 429:
                max_for_status = _MAX_RETRIES
            elif status >= 500:
                max_for_status = _MAX_5XX_RETRIES
            else:
                # Unexpected status — treat as non-retryable
                raise JiraApiError(status, resp_body, path)

            if attempt >= max_for_status - 1:
                break

            # IMPL-SEC-019-C3: Log status and retry-after only
            retry_after = response.headers.get("Retry-After")
            logger.warning(
                "JIRA retryable error — status=%d attempt=%d/%d "
                "path=%s retry_after=%s",
                status, attempt + 1, _MAX_RETRIES, path,
                retry_after or "none",
            )
            self._backoff(attempt, retry_after)

        # All retries exhausted
        raise JiraApiError(last_status, last_body, path)

    def bulk_create_issues(
        self,
        issues: List[Dict[str, Any]],
        remaining_time_fn: Optional[Any] = None,
    ) -> "BulkCreateResult":
        """Create JIRA issues in bulk via POST /rest/api/3/issue/bulk.

        Batches of 50, 2-second inter-batch delay, 429 retry with
        exponential backoff + jitter. Partial failures parsed from
        the 201 response ``errors`` array.

        SEC-S21-03: Checks remaining Lambda time before each batch.
        SEC-S21-06: Validates bulk response fields before use.
        SEC-S21-10: Response parsing stays here, not in _request_with_retry.

        Args:
            issues: List of ``{"fields": {...}}`` dicts for issue creation.
            remaining_time_fn: Callable returning remaining millis
                (e.g. ``context.get_remaining_time_in_millis``). If
                remaining time < 30 000 ms, processing stops early.

        Returns:
            BulkCreateResult with successes and failures.
        """
        start = time.monotonic()
        total = len(issues)
        successes: List[Dict[str, Any]] = []
        failures: List[Dict[str, Any]] = []
        total_retries = 0
        exhausted = False

        batches = [
            issues[i : i + _BULK_BATCH_SIZE]
            for i in range(0, total, _BULK_BATCH_SIZE)
        ]

        for batch_idx, batch in enumerate(batches):
            # SEC-S21-03: Timeout guard
            if remaining_time_fn and remaining_time_fn() < _TIMEOUT_GUARD_MS:
                logger.warning(
                    "Bulk create stopped — remaining_time_ms < %d, "
                    "batches_completed=%d/%d",
                    _TIMEOUT_GUARD_MS, batch_idx, len(batches),
                )
                exhausted = True
                break

            # Inter-batch delay (skip before first batch)
            if batch_idx > 0:
                time.sleep(_INTER_BATCH_DELAY)

            payload = {"issueUpdates": batch}
            batch_retries = 0

            try:
                resp = self._request_with_retry(
                    "POST",
                    "/rest/api/3/issue/bulk",
                    body=payload,
                    expected_status=201,
                )
            except JiraApiError as exc:
                # Entire batch failed after retries
                batch_retries = _MAX_RETRIES
                total_retries += batch_retries
                for j in range(len(batch)):
                    failures.append({
                        "index": batch_idx * _BULK_BATCH_SIZE + j,
                        "httpStatus": exc.status,
                        "errorMessages": [str(exc)[:200]],
                        "fieldErrors": {},
                    })
                if exc.status == 429:
                    exhausted = True
                    break
                continue

            total_retries += batch_retries

            # --- Parse bulk response (SEC-S21-06) ---
            resp_issues = resp.get("issues") if isinstance(resp.get("issues"), list) else []
            resp_errors = resp.get("errors") if isinstance(resp.get("errors"), list) else []

            # SEC-S21-06: Response completeness warning
            if len(resp_issues) + len(resp_errors) != len(batch):
                logger.warning(
                    "Bulk response count mismatch — expected=%d "
                    "got_issues=%d got_errors=%d batch_idx=%d",
                    len(batch), len(resp_issues), len(resp_errors),
                    batch_idx,
                )

            # Collect failed element numbers for exclusion
            failed_indices: set = set()
            for err in resp_errors:
                if not isinstance(err, dict):
                    continue
                elem_num = err.get("failedElementNumber")
                # SEC-S21-06: Bounds check
                if not isinstance(elem_num, int) or elem_num < 0 or elem_num >= len(batch):
                    logger.warning(
                        "Bulk error failedElementNumber out of range — "
                        "value=%s batch_size=%d",
                        str(elem_num)[:20], len(batch),
                    )
                    continue
                failed_indices.add(elem_num)
                elem_errors = err.get("elementErrors", {})
                failures.append({
                    "index": batch_idx * _BULK_BATCH_SIZE + elem_num,
                    "httpStatus": err.get("status", 400),
                    "errorMessages": (
                        elem_errors.get("errorMessages", [])
                        if isinstance(elem_errors, dict) else []
                    ),
                    "fieldErrors": (
                        elem_errors.get("errors", {})
                        if isinstance(elem_errors, dict) else {}
                    ),
                })

            # Process successes
            success_idx = 0
            for local_idx in range(len(batch)):
                if local_idx in failed_indices:
                    continue
                if success_idx >= len(resp_issues):
                    break
                issue = resp_issues[success_idx]
                success_idx += 1
                if not isinstance(issue, dict):
                    continue

                key = issue.get("key", "")
                # SEC-S21-06: Validate key format
                if not isinstance(key, str) or not _JIRA_KEY_RE.match(key):
                    logger.warning(
                        "Bulk response issue key invalid — key=%s",
                        str(key)[:50],
                    )
                    failures.append({
                        "index": batch_idx * _BULK_BATCH_SIZE + local_idx,
                        "httpStatus": 0,
                        "errorMessages": ["Invalid issue key in response"],
                        "fieldErrors": {},
                    })
                    continue

                # SEC-S21-06: Derive ticket URL safely
                self_url = issue.get("self", "")
                if isinstance(self_url, str) and "/rest/" in self_url:
                    browse_base = self_url.split("/rest/")[0]
                else:
                    browse_base = self._base_url

                successes.append({
                    "index": batch_idx * _BULK_BATCH_SIZE + local_idx,
                    "ticketKey": key,
                    "ticketId": issue.get("id", ""),
                    "ticketUrl": f"{browse_base}/browse/{key}",
                })

            logger.info(
                "Bulk batch %d/%d — created=%d failed=%d "
                "cumulative=%d/%d",
                batch_idx + 1, len(batches),
                len(successes) - sum(
                    1 for s in successes
                    if s["index"] < batch_idx * _BULK_BATCH_SIZE
                ),
                len([
                    f for f in failures
                    if f["index"] >= batch_idx * _BULK_BATCH_SIZE
                    and f["index"] < (batch_idx + 1) * _BULK_BATCH_SIZE
                ]),
                len(successes), total,
            )

        return BulkCreateResult(
            successes=successes,
            failures=failures,
            total_requested=total,
            total_batches=len(batches),
            total_retries=total_retries,
            elapsed_seconds=time.monotonic() - start,
            exhausted=exhausted,
        )

    @staticmethod
    def _backoff(attempt: int, retry_after: Optional[str]) -> None:
        """Sleep with exponential backoff and jitter.

        SEC-S21-07: Negative Retry-After values guarded with max(delay, 0).
        """
        if retry_after:
            try:
                delay = min(int(retry_after), _MAX_RETRY_DELAY)
            except (ValueError, TypeError):
                delay = min(2 ** attempt + random.uniform(0, 1), _MAX_RETRY_DELAY)
        else:
            delay = min(2 ** attempt + random.uniform(0, 1), _MAX_RETRY_DELAY)
        # SEC-S21-07: Guard against negative values
        time.sleep(max(delay, 0))
