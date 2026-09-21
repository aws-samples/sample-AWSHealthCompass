"""JIRA-specific content formatter — renders TicketContent as ADF.

Implements ContentFormatter from itsm_client.py. Converts the
platform-agnostic TicketContent dataclass into Atlassian Document Format
(ADF) JSON structures for JIRA Cloud REST API v3.

ADF construction logic extracted from ticket_builder.py.

Consumers: ticket_builder.py (build_jira_ticket).
Dependencies: resolve_core.adf_builder, resolve_core.itsm_client.
"""

from __future__ import annotations

from typing import Any, List, Optional

from resolve_core.adf_builder import (
    adf_bold_value,
    adf_code,
    adf_doc,
    adf_heading,
    adf_paragraph,
    adf_rule,
    adf_table_rich,
)
from resolve_core.itsm_client import ContentFormatter
from resolve_core.ticket_content import TicketContent

# Resource status sort order: PENDING first (most urgent)
_STATUS_ORDER = {"PENDING": 0, "IMPAIRED": 1, "UNKNOWN": 2, "RESOLVED": 3}


class JiraFormatter(ContentFormatter):
    """Renders TicketContent as JIRA ADF documents."""

    def format_description(self, content: TicketContent) -> dict:
        """Render ticket description as ADF document.

        Dispatches to Template A (resource-level) or Template B
        (account-level) based on content.campaign_type.
        """
        if content.campaign_type == "account-level":
            return self._format_template_b(content)
        return self._format_template_a(content)

    def format_work_note(self, note: str) -> dict:
        """Render a work note as an ADF document."""
        return adf_doc([adf_paragraph([note])])

    # ------------------------------------------------------------------
    # Template A — Resource-Level
    # ------------------------------------------------------------------

    def _format_template_a(self, content: TicketContent) -> dict:
        """Build ADF for Template A (events WITH resources)."""
        sections: List[dict] = []

        # 1. Campaign Details heading + metadata
        sections.append(adf_heading("Campaign Details", 3))
        for label, value in content.metadata_pairs:
            sections.append(adf_bold_value(f"{label}: ", value))

        # 2. Account Tags (conditional)
        if content.account_tags:
            sections.append(adf_rule())
            sections.append(adf_heading("Account Tags", 3))
            for key, value in content.account_tags:
                sections.append(adf_bold_value(f"{key}: ", value))

        # 3. Event Description
        sections.append(adf_rule())
        sections.append(adf_heading("Event Description", 3))
        sections.append(adf_paragraph([content.description_text]))

        # 4. Affected Resources
        resource_count = len(content.resources)
        sections.append(adf_rule())
        sections.append(
            adf_heading(f"Affected Resources ({resource_count})", 3),
        )
        if content.csv_needed:
            sections.append(adf_paragraph([
                (f"{resource_count} affected resources", ["strong"]),
                " — see attached CSV file (affected-resources.csv).",
            ]))
        elif content.resources_by_account:
            # Multi-account grouped ticket — render per-account
            for acct_id, acct_name, acct_resources in content.resources_by_account:
                display = f"{acct_name} ({acct_id})" if acct_name != acct_id else acct_id
                sections.append(
                    adf_heading(f"Account: {display} — {len(acct_resources)} resources", 4),
                )
                table = self._build_resource_table_from_list(acct_resources, content.routing_info)
                if table:
                    sections.append(table)
        else:
            table = self._build_resource_table(content)
            if table:
                sections.append(table)

        # 5. Remediation Guidance
        sections.append(adf_rule())
        sections.append(adf_heading("Remediation Guidance", 3))
        sections.append(adf_paragraph([
            "Please address these resources before ",
            (content.guidance_text, ["strong"]),
            ".",
        ]))

        return adf_doc(sections)

    # ------------------------------------------------------------------
    # Template B — Account-Level
    # ------------------------------------------------------------------

    def _format_template_b(self, content: TicketContent) -> dict:
        """Build ADF for Template B (no resource table)."""
        sections: List[dict] = []

        # 1. Event Details heading + metadata
        sections.append(adf_heading("Event Details", 3))
        for label, value in content.metadata_pairs:
            sections.append(adf_bold_value(f"{label}: ", value))

        # 2. Account Tags (conditional)
        if content.account_tags:
            sections.append(adf_rule())
            sections.append(adf_heading("Account Tags", 3))
            for key, value in content.account_tags:
                sections.append(adf_bold_value(f"{key}: ", value))

        # 3. Event Description
        sections.append(adf_rule())
        sections.append(adf_heading("Event Description", 3))
        sections.append(adf_paragraph([content.description_text]))

        # 4. Recommended Action
        sections.append(adf_rule())
        sections.append(adf_heading("Recommended Action", 3))
        sections.append(self._build_recommended_action(content))

        return adf_doc(sections)

    # ------------------------------------------------------------------
    # Shared Helpers
    # ------------------------------------------------------------------

    def _build_resource_table(self, content: TicketContent) -> Optional[dict]:
        """Build ADF resource status table."""
        return self._build_resource_table_from_list(content.resources, content.routing_info)

    def _build_resource_table_from_list(
        self, resources: List[dict], routing: dict,
    ) -> Optional[dict]:
        """Build ADF resource status table from a resource list."""
        if not resources:
            return None

        sorted_resources = sorted(
            resources,
            key=lambda r: _STATUS_ORDER.get(r.get("status", ""), 99),
        )

        has_tag_column = (
            routing.get("resolvedBy") == "tag"
            and routing.get("routingTagKey")
        )
        tag_key = routing.get("routingTagKey", "") if has_tag_column else ""

        if has_tag_column:
            headers = ["Resource ARN", tag_key, "Status", "Last Updated"]
        else:
            headers = ["Resource ARN", "Status", "Last Updated"]

        rows = []
        for r in sorted_resources:
            arn = r.get("arn") or r.get("entityValue", "")
            status = r.get("status", "")
            updated = r.get("lastUpdatedTime", "")

            arn_node = adf_code(arn)
            status_node = {
                "type": "text",
                "text": status,
                "marks": [{"type": "strong"}],
            }

            if has_tag_column:
                tags = r.get("resourceTags", {})
                tag_val = tags.get(tag_key, "") if isinstance(tags, dict) else ""
                rows.append([arn_node, str(tag_val), status_node, updated])
            else:
                rows.append([arn_node, status_node, updated])

        return adf_table_rich(headers, rows)

    def _build_recommended_action(self, content: TicketContent) -> dict:
        """Build recommended action paragraph with deadline variants."""
        # guidance_text holds the startTime value for Template B
        start_time = content.guidance_text
        if not start_time:
            return adf_paragraph([
                "Review this notification and take action as described above.",
            ])

        date_part = start_time[:10] if len(start_time) >= 10 else start_time

        # Check if deadline is in the past
        try:
            from datetime import date as date_cls
            deadline = date_cls.fromisoformat(date_part)
            if deadline < date_cls.today():
                return adf_paragraph([
                    "Review this notification and take action as described above. The original deadline (",
                    (date_part, ["strong"]),
                    ") has passed.",
                ])
        except (ValueError, TypeError):
            pass

        return adf_paragraph([
            "Review this notification and take action as described above before ",
            (date_part, ["strong"]),
            ".",
        ])
