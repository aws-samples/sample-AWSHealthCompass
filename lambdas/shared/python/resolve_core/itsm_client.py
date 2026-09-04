"""ITSMClient abstract interface for multi-platform ITSM integration.

Defines the contract that ALL ITSM platforms must implement:
- JIRA Cloud (Alpha — implemented)
- ServiceNow (Beta — planned)
- GitHub Issues (Future)
- Azure DevOps Work Items (Future)
- GitLab Issues (Future)

Design principles:
- `routing_target` is OPAQUE — each platform interprets it differently
- All external API calls raise ITSMAPIError on failure
- ContentFormatter handles platform-specific rendering
- PLATFORMS_REGISTRY enables dynamic platform discovery

Consumers: itsm_orchestrator.py, handler.py (per-platform).
Dependencies: Python stdlib only.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ITSMAPIError(Exception):
    """Common exception for all ITSM platform API failures.

    Raised by any ITSMClient method that calls an external API.
    Consumers use `retryable` to decide whether to retry or DLQ.

    Security note: `error_message` may contain platform-specific URLs,
    endpoint paths, or field names from JIRA/ServiceNow error responses.
    This is acceptable for CloudWatch structured logging (A-JIRA-7) but
    MUST be sanitized before surfacing via API Gateway responses or
    telemetry payloads to avoid leaking customer instance topology.
    """

    def __init__(self, status_code: int, error_message: str, retryable: bool):
        self.status_code = status_code
        self.error_message = error_message
        self.retryable = retryable
        super().__init__(f"ITSMAPIError({status_code}, retryable={retryable}): {error_message}")


# ---------------------------------------------------------------------------
# Dataclasses — Platform-Agnostic Ticket Types
# ---------------------------------------------------------------------------


@dataclass
class TicketCreateRequest:
    """Platform-agnostic request to create a ticket.

    Fields:
        campaign_id: Internal campaign identifier for idempotency/correlation.
        summary: Short title (JIRA=summary, SNOW=short_description, GH=title).
        description_content: Opaque content object — ContentFormatter renders it
            into platform-specific format (ADF, HTML, Markdown).
        routing_target: Opaque string interpreted per platform:
            JIRA=project key, SNOW=assignment_group sys_id,
            GitHub=org/repo, AzDO=project/team.
            Security: Implementations MUST validate/sanitize this value
            before interpolating it into URLs or query strings.
        due_date: Optional ISO date string "YYYY-MM-DD".
        urgency: 1=High, 2=Medium, 3=Low (SNOW priority input; JIRA ignores).
        impact: 1=High, 2=Medium, 3=Low (SNOW priority input; JIRA ignores).
        labels: List of string labels (JIRA=labels, SNOW=ignored, GH=labels).
        record_type: Platform-specific record type hint:
            JIRA="Task", SNOW="change_request"/"incident", GH="issue".
        correlation_id: External reference for bidirectional sync
            (JIRA=label, SNOW=correlation_id field).
    """

    campaign_id: str
    summary: str
    description_content: Any
    routing_target: str
    due_date: Optional[str] = None
    urgency: int = 2
    impact: int = 2
    labels: List[str] = field(default_factory=list)
    record_type: str = "Task"
    correlation_id: Optional[str] = None


@dataclass
class TicketCreateResponse:
    """Platform-agnostic response from ticket creation.

    Fields:
        ticket_id: Platform identifier (JIRA="PROJ-123", SNOW="INC001", GH="#42").
        ticket_url: Direct URL to the ticket in the platform UI.
        platform: Platform identifier string ("jira", "servicenow", "github").
        raw_response: Full platform response dict for debugging/logging.
    """

    ticket_id: str
    ticket_url: str
    platform: str
    raw_response: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TicketStatus:
    """Platform-agnostic ticket status from bidirectional sync.

    Fields:
        ticket_id: Platform identifier.
        normalized_status: One of "Created", "In Progress", "Closed".
        raw_status: Platform-native status string for display.
        last_updated: ISO 8601 timestamp of last status change.
        platform: Platform identifier string.
    """

    ticket_id: str
    normalized_status: str  # "Created" | "In Progress" | "Closed"
    raw_status: str
    last_updated: str
    platform: str


@dataclass
class BulkCreateFailure:
    """Single failure entry from a bulk create operation."""

    index: int
    error_message: str
    status_code: int = 0
    retryable: bool = False


@dataclass
class BulkCreateResult:
    """Result of a bulk ticket creation operation.

    Fields:
        successes: List of successfully created tickets.
        failures: List of failures with index and error detail.
    """

    successes: List[TicketCreateResponse] = field(default_factory=list)
    failures: List[BulkCreateFailure] = field(default_factory=list)


@dataclass
class ConnectionValidationResult:
    """Result of validating ITSM platform credentials/connectivity.

    Fields:
        valid: True if connection succeeded and credentials are correct.
        display_name: Authenticated user/account display name on success.
        errors: List of human-readable error messages on failure.
    """

    valid: bool
    display_name: str = ""
    errors: List[str] = field(default_factory=list)


@dataclass
class TargetValidationResult:
    """Result of validating a routing target exists and is accessible.

    Fields:
        valid: True if target exists and has required permissions.
        target_name: Human-readable name of the target on success.
        errors: List of human-readable error messages on failure.
    """

    valid: bool
    target_name: str = ""
    errors: List[str] = field(default_factory=list)


@dataclass
class ITSMPlatformConfig:
    """Configuration for a single ITSM platform connection.

    Fields:
        platform_id: Registry key ("jira", "servicenow", "github").
        display_name: Human-readable name ("JIRA Cloud", "ServiceNow").
        secret_arn: Secrets Manager ARN holding platform credentials.
        connection_validated: Whether validate_connection() has passed.
        platform_specific_config: Platform-dependent settings (e.g.
            JIRA issue_type default, SNOW change_type, GH repo owner).
    """

    platform_id: str
    display_name: str
    secret_arn: str = field(repr=False)
    connection_validated: bool = False
    platform_specific_config: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Content Formatter ABC
# ---------------------------------------------------------------------------


class ContentFormatter(ABC):
    """Abstract formatter that renders ticket content for a specific platform.

    Each platform has different description formats:
    - JIRA: Atlassian Document Format (ADF) — JSON tree
    - ServiceNow: HTML or plain text
    - GitHub: Markdown
    - Azure DevOps: HTML

    The formatter takes a platform-agnostic content object and produces
    the platform-native representation.
    """

    @abstractmethod
    def format_description(self, content: Any) -> Any:
        """Render ticket description in platform-native format.

        Args:
            content: Platform-agnostic content object (e.g. TicketContent
                dataclass from ticket_content.py).

        Returns:
            Platform-specific format:
                JIRA: dict (ADF document)
                ServiceNow: str (plain text)
                GitHub: str (Markdown)
                AzDO: str (HTML)
        """
        ...

    @abstractmethod
    def format_work_note(self, note: str) -> Any:
        """Render a work note/comment in platform-native format.

        Args:
            note: Plain text note content.

        Returns:
            Platform-specific format:
                JIRA: dict (ADF document)
                ServiceNow: str (plain text with newlines)
                GitHub: str (Markdown)
                AzDO: str (HTML)
        """
        ...


# ---------------------------------------------------------------------------
# ITSMClient ABC
# ---------------------------------------------------------------------------


class ITSMClient(ABC):
    """Abstract base class for ITSM platform integrations.

    All methods that call external APIs raise ITSMAPIError on failure.
    Implementations handle platform-specific auth, rate limiting, and
    response parsing internally.
    """

    @abstractmethod
    def create_ticket(self, request: TicketCreateRequest) -> TicketCreateResponse:
        """Create a single ticket in the ITSM platform.

        Platform mapping:
            JIRA: POST /rest/api/3/issue
            ServiceNow: POST /api/now/table/{record_type}
            GitHub: POST /repos/{owner}/{repo}/issues
            AzDO: POST /{org}/{project}/_apis/wit/workitems

        Args:
            request: Platform-agnostic ticket creation request.

        Returns:
            TicketCreateResponse with platform ticket ID and URL.

        Raises:
            ITSMAPIError: On API failure (check .retryable for retry decision).
        """
        ...

    @abstractmethod
    def bulk_create_tickets(
        self, requests: List[TicketCreateRequest]
    ) -> BulkCreateResult:
        """Create multiple tickets in batch.

        Platform mapping:
            JIRA: POST /rest/api/3/issue/bulk (50 per batch)
            ServiceNow: Sequential POSTs with rate limiting
            GitHub: Sequential POSTs (no bulk API)
            AzDO: POST /_apis/wit/$batch

        Args:
            requests: List of ticket creation requests.

        Returns:
            BulkCreateResult with successes and failures.

        Raises:
            ITSMAPIError: On unrecoverable API failure.
        """
        ...

    @abstractmethod
    def update_ticket(self, ticket_id: str, fields: Dict[str, Any]) -> None:
        """Update fields on an existing ticket.

        Platform mapping:
            JIRA: PUT /rest/api/3/issue/{key} with fields dict
            ServiceNow: PATCH /api/now/table/{table}/{sys_id}
            GitHub: PATCH /repos/{owner}/{repo}/issues/{number}
            AzDO: PATCH /_apis/wit/workitems/{id}

        Args:
            ticket_id: Platform-native ticket identifier.
            fields: Dict of field names to new values (platform-specific keys).

        Raises:
            ITSMAPIError: On API failure.
        """
        ...

    @abstractmethod
    def add_work_note(self, ticket_id: str, note: str) -> None:
        """Add an internal work note or comment to a ticket.

        Platform mapping:
            JIRA: POST /rest/api/3/issue/{key}/comment (ADF body)
            ServiceNow: PATCH with work_notes field (appends)
            GitHub: POST /repos/{owner}/{repo}/issues/{number}/comments
            AzDO: POST /_apis/wit/workitems/{id}/comments

        Args:
            ticket_id: Platform-native ticket identifier.
            note: Plain text note — formatter renders platform-specific format.

        Raises:
            ITSMAPIError: On API failure.
        """
        ...

    @abstractmethod
    def attach_file(
        self, ticket_id: str, filename: str, content: bytes, content_type: str
    ) -> None:
        """Attach a file to a ticket.

        Platform mapping:
            JIRA: POST /rest/api/3/issue/{key}/attachments (multipart)
            ServiceNow: POST /api/now/attachment/file (raw body)
            GitHub: Not natively supported (link in comment instead)
            AzDO: POST /_apis/wit/attachments

        Args:
            ticket_id: Platform-native ticket identifier.
            filename: Attachment filename (e.g. "affected-resources.csv").
            content: Raw file bytes.
            content_type: MIME type (e.g. "text/csv").

        Raises:
            ITSMAPIError: On API failure.
        """
        ...

    @abstractmethod
    def poll_status_changes(self, since: str) -> List[TicketStatus]:
        """Poll for ticket status changes since a given timestamp.

        Platform mapping:
            JIRA: POST /rest/api/3/search/jql (updated >= since)
            ServiceNow: GET /api/now/table/{table}?sys_updated_on>{since}
            GitHub: GET /repos/{owner}/{repo}/issues?since={since}&state=all
            AzDO: POST /_apis/wit/wiql (WIQL query)

        Args:
            since: ISO 8601 timestamp — only return tickets updated after this.

        Returns:
            List of TicketStatus with normalized states.

        Raises:
            ITSMAPIError: On API failure.
        """
        ...

    @abstractmethod
    def validate_connection(self) -> ConnectionValidationResult:
        """Validate that credentials are correct and API is reachable.

        Platform mapping:
            JIRA: GET /rest/api/3/myself
            ServiceNow: GET /api/now/table/sys_user?user_name=...
            GitHub: GET /user
            AzDO: GET /_apis/connectionData

        Returns:
            ConnectionValidationResult with valid flag and display name.
        """
        ...

    @abstractmethod
    def validate_routing_target(self, target: str) -> TargetValidationResult:
        """Validate that a routing target exists and is accessible.

        Platform mapping:
            JIRA: GET /rest/api/3/project/{key}
            ServiceNow: GET /api/now/table/sys_user_group/{sys_id}
            GitHub: GET /repos/{owner}/{repo}
            AzDO: GET /{org}/{project}/_apis/projects/{project}

        Args:
            target: Opaque routing target string.

        Returns:
            TargetValidationResult with valid flag and target name.
        """
        ...


# ---------------------------------------------------------------------------
# Platform Registry
# ---------------------------------------------------------------------------

# Maps platform_id to client class. Filled as platforms are implemented.
# Usage: client_class = PLATFORMS_REGISTRY[config.platform_id]
#        client = client_class(credentials)
PLATFORMS_REGISTRY: Dict[str, Optional[type]] = {
    "jira": None,  # Will be set to JiraClient after refactor (STORY-052 follow-up)
}


def get_platform_client_class(platform_id: str) -> type:
    """Look up a registered ITSMClient subclass by platform_id.

    Security: Validates the registered class is a genuine ITSMClient
    subclass before returning it, preventing a corrupted registry
    entry from injecting a non-conforming class.

    Args:
        platform_id: Registry key (e.g. "jira", "servicenow").

    Returns:
        The ITSMClient subclass for the given platform.

    Raises:
        ValueError: If platform_id is not registered or not implemented.
        TypeError: If the registered class is not an ITSMClient subclass.
    """
    cls = PLATFORMS_REGISTRY.get(platform_id)
    if cls is None:
        raise ValueError(f"Platform '{platform_id}' not registered or not implemented")
    if not isinstance(cls, type) or not issubclass(cls, ITSMClient):
        raise TypeError(
            f"Registry entry for '{platform_id}' is not a valid ITSMClient subclass"
        )
    return cls
