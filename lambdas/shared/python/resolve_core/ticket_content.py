"""Platform-agnostic ticket content model.

Defines the TicketContent dataclass that holds all data needed to render
a ticket description in any ITSM platform format. This is the bridge
between event processing (ticket_builder.py) and platform-specific
rendering (jira_formatter.py, snow_formatter.py, etc.).

Extracted from ticket_builder.py to enable multi-platform support.

Consumers: ticket_builder.py, jira_formatter.py, snow_formatter.py (Beta).
Dependencies: Python stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class TicketContent:
    """Platform-agnostic content model for an ITSM ticket.

    Attributes:
        summary: Short title for the ticket (max 255 chars).
        metadata_pairs: Ordered key-value pairs for the metadata section.
            E.g. [("Service", "EKS"), ("Deadline", "2026-08-27"), ...].
        description_text: Event description body text.
        resources: List of resource dicts with keys: arn, status, lastUpdated.
            Empty list for account-level tickets.
        guidance_text: Remediation guidance text.
        labels: List of JIRA/platform labels.
        due_date: ISO date string "YYYY-MM-DD" or None.
        campaign_type: "resource-level" or "account-level".
        csv_needed: True if resources > 100 (attachment instead of inline).
        account_tags: Ordered key-value pairs for account tags section.
        routing_info: Routing metadata for resource table rendering.
        resources_by_account: When a grouped ticket spans multiple accounts,
            list of (account_id, account_name, resources) tuples for
            per-account rendering. None for single-account tickets.
    """

    summary: str
    metadata_pairs: List[Tuple[str, str]]
    description_text: str
    resources: List[dict]
    guidance_text: str
    labels: List[str]
    due_date: Optional[str]
    campaign_type: str  # "resource-level" or "account-level"
    csv_needed: bool
    account_tags: List[Tuple[str, str]] = field(default_factory=list)
    routing_info: dict = field(default_factory=dict)
    resources_by_account: Optional[List[Tuple[str, str, List[dict]]]] = None
