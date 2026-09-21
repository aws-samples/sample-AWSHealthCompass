"""ServiceNow-specific content formatter — renders TicketContent as plain text.

Implements ContentFormatter from itsm_client.py. Converts the
platform-agnostic TicketContent dataclass into structured plain text
strings for ServiceNow REST API description fields.

Plain text output is universally compatible with all ServiceNow instance
configurations regardless of field type (String vs HTML), plugin state,
or version. Section headers use '--- Title ---' pattern, resource tables
use pipe-delimited lines, and metadata uses 'Label: value' format.

FINDING-08: _sanitize() strips control characters from all interpolated fields.

Consumers: servicenow_client.py.
Dependencies: resolve_core.itsm_client, resolve_core.ticket_content.
"""

from __future__ import annotations

from typing import Any, Dict, List

from resolve_core.itsm_client import ContentFormatter
from resolve_core.ticket_content import TicketContent

# ServiceNow field size limits (per security review)
_DESCRIPTION_LIMIT = 4000
_JUSTIFICATION_LIMIT = 500
_IMPLEMENTATION_PLAN_LIMIT = 2000


class ServiceNowFormatter(ContentFormatter):
    """Renders TicketContent as ServiceNow plain text descriptions.

    Produces structured plain text using newline separators, section
    headers (--- Title ---), and pipe-delimited resource tables. Output
    is universally readable on all ServiceNow instances regardless of
    field type configuration.
    """

    def format_description(self, content: TicketContent) -> str:
        """Render ticket description as plain text string.

        Dispatches to Template A (with resources) or Template B
        (account-level) based on content.campaign_type. Truncates
        to 4000 characters per ServiceNow field limit.
        """
        if content.campaign_type == "account-level":
            raw = self._format_template_b(content)
        else:
            raw = self._format_template_a(content)
        return _truncate(raw, _DESCRIPTION_LIMIT)

    def format_work_note(self, note: str) -> str:
        """Render work note as plain text with newlines."""
        return note

    def format_change_request_fields(self, content: TicketContent) -> Dict[str, str]:
        """Return additional fields for change_request records.

        Provides justification, implementation_plan, and risk values
        derived from the ticket content.
        """
        return {
            "justification": _truncate(
                _sanitize(content.description_text), _JUSTIFICATION_LIMIT
            ),
            "implementation_plan": _truncate(
                self._build_implementation_plan(content),
                _IMPLEMENTATION_PLAN_LIMIT,
            ),
            "risk": self._calculate_risk(content),
        }

    # ------------------------------------------------------------------
    # Template A — Resource-Level (events WITH resources)
    # ------------------------------------------------------------------

    def _format_template_a(self, content: TicketContent) -> str:
        """Build plain text for Template A (resource-level events)."""
        parts: List[str] = []

        # Metadata section
        for label, value in content.metadata_pairs:
            parts.append(f"{label}: {_sanitize(value)}\n")

        # Account tags
        if content.account_tags:
            parts.append("\nAccount Tags:\n")
            for key, value in content.account_tags:
                parts.append(f"  {_sanitize(key)}: {_sanitize(value)}\n")

        # Event description
        parts.append("\n--- Event Description ---\n")
        parts.append(f"{_sanitize(content.description_text)}\n")

        # Resources
        resource_count = len(content.resources)
        parts.append(f"\n--- Affected Resources ({resource_count}) ---\n")

        if content.csv_needed:
            parts.append(
                f"[Note] {resource_count} affected resources "
                f"— see attached CSV file (affected-resources.csv).\n"
            )
        elif content.resources_by_account:
            # Multi-account grouped ticket — render per-account
            for acct_id, acct_name, acct_resources in content.resources_by_account:
                if acct_name and acct_name != acct_id:
                    display = f"{_sanitize(acct_name)} ({_sanitize(acct_id)})"
                else:
                    display = _sanitize(acct_id)
                parts.append(
                    f"\n>> Account: {display} — {len(acct_resources)} resources\n"
                )
                parts.append(self._build_resource_table_from_list(acct_resources))
        elif content.resources:
            parts.append(self._build_resource_table(content))

        # Remediation
        parts.append("\n--- Remediation ---\n")
        if content.guidance_text:
            parts.append(
                f"Please address these resources before {_sanitize(content.guidance_text)}.\n"
            )
        else:
            parts.append("Please address these resources.\n")

        return "".join(parts)

    # ------------------------------------------------------------------
    # Template B — Account-Level (events WITHOUT resources)
    # ------------------------------------------------------------------

    def _format_template_b(self, content: TicketContent) -> str:
        """Build plain text for Template B (account-level events)."""
        parts: List[str] = []

        # Metadata section
        for label, value in content.metadata_pairs:
            parts.append(f"{label}: {_sanitize(value)}\n")

        # Account tags
        if content.account_tags:
            parts.append("\nAccount Tags:\n")
            for key, value in content.account_tags:
                parts.append(f"  {_sanitize(key)}: {_sanitize(value)}\n")

        # Scope note
        parts.append(
            "\nScope: Account-level notification (no specific resources identified)\n"
        )

        # Event description
        parts.append("\n--- Event Description ---\n")
        parts.append(f"{_sanitize(content.description_text)}\n")

        # Recommended action
        parts.append("\n--- Recommended Action ---\n")
        if content.guidance_text:
            parts.append(
                f"Review this notification and take action as described above "
                f"before {_sanitize(content.guidance_text)}.\n"
            )
        else:
            parts.append(
                "Review this notification and take action as described above.\n"
            )

        return "".join(parts)

    # ------------------------------------------------------------------
    # Change Request Helpers
    # ------------------------------------------------------------------

    def _build_implementation_plan(self, content: TicketContent) -> str:
        """Build generic implementation plan text from content."""
        service = ""
        for label, value in content.metadata_pairs:
            if label == "Service":
                service = value
                break

        steps = [
            f"1. Review affected {service or 'AWS'} resources identified in this ticket.",
            "2. Test remediation steps in a non-production environment.",
            "3. Apply remediation to production resources.",
            "4. Verify resource health post-change.",
        ]
        if content.guidance_text:
            steps.append(f"5. Confirm completion before deadline: {content.guidance_text}.")
        return "\n".join(steps)

    def _calculate_risk(self, content: TicketContent) -> str:
        """Determine risk level from resource count."""
        count = len(content.resources)
        if count > 100:
            return "high"
        if count > 10:
            return "moderate"
        return "low"

    # ------------------------------------------------------------------
    # Resource Table
    # ------------------------------------------------------------------

    def _build_resource_table(self, content: TicketContent) -> str:
        """Build pipe-delimited resource list for affected resources."""
        return self._build_resource_table_from_list(content.resources)

    def _build_resource_table_from_list(self, resources: List[dict]) -> str:
        """Build pipe-delimited plain text table from a resource list.

        Each resource is rendered as one line:
            arn | STATUS | last_updated_time

        No header row — the section title provides context for columns.
        """
        lines: List[str] = []

        for r in resources:
            arn = r.get("arn") or r.get("entityValue", "")
            status = r.get("status", "")
            updated = r.get("lastUpdatedTime", "")
            lines.append(
                f"{_sanitize(arn)} | {_sanitize(status)} | {_sanitize(updated)}"
            )

        return "\n".join(lines) + "\n" if lines else ""


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _sanitize(value: Any) -> str:
    """Sanitize a value for plain text output.

    Strips control characters (ASCII 0x00-0x08, 0x0B-0x0C, 0x0E-0x1F)
    that could corrupt ServiceNow field rendering. Preserves newlines
    (0x0A), carriage returns (0x0D), and tabs (0x09) for multiline content.
    All printable characters including '<', '>', '&' pass through as-is
    since the output is plain text, not HTML.
    """
    if not value:
        return ""
    text = str(value)
    return "".join(c for c in text if c in ("\n", "\r", "\t") or ord(c) >= 0x20)


def _truncate(text: str, limit: int) -> str:
    """Truncate text to limit, appending a note if truncated."""
    if len(text) <= limit:
        return text
    suffix = "... [truncated — see attached file for full details]"
    return text[: limit - len(suffix)] + suffix
