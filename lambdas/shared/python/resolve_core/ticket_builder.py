"""JIRA ticket builder for Template A (events WITH resources).

Builds summary, labels, due date, and ADF description from a
standardized v2.0 event. Pure-Python functions — no I/O except
the public build_template_a() which is called by the handler.

Summary truncated to 255 chars.
Account ID validated as 12 digits.
Resource count validated as non-negative int.
Tag values placed in text nodes only.

Provides build_ticket_content() and build_jira_ticket() for
platform-agnostic content model extraction.

Consumers: handler.py (JIRA Integration Lambda).
Dependencies: resolve_core.adf_builder, resolve_core.tags,
    resolve_core.ticket_content, resolve_core.jira_formatter.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

from resolve_core.adf_builder import (
    adf_bold_value,
    adf_bullet_list,
    adf_code,
    adf_doc,
    adf_heading,
    adf_paragraph,
    adf_rule,
    adf_table_rich,
)
from resolve_core.tags import sanitize_for_label
from resolve_core.constants import COMPASS_LABEL, ORPHAN_LABEL, TICKET_SUMMARY_PREFIX

# --- Constants ---

_MAX_SUMMARY_LEN = 255
_MAX_INLINE_RESOURCES = 100
_MAX_DESCRIPTION_LEN = 30_000
_MAX_DISPLAY_LEN = 50
_ACCOUNT_ID_RE = re.compile(r"^\d{12}$")
_DEFAULT_TAG_DISPLAY_KEYS = ["Owner", "Team", "Environment"]

# Resource status sort order: PENDING first (most urgent)
_STATUS_ORDER = {"PENDING": 0, "IMPAIRED": 1, "UNKNOWN": 2, "RESOLVED": 3}


def build_template_a(
    event: dict,
    resources: List[dict],
    routing: dict,
    account_tags: dict,
    tag_display_keys: Optional[List[str]] = None,
) -> dict:
    """Build a JIRA ticket payload for Template A (with resources).

    Args:
        event: The ``event`` sub-dict from the standardized v2.0 payload.
        resources: The ``resources`` list from the standardized payload.
        routing: The ``routing`` dict from the standardized payload.
        account_tags: The ``accountTags`` dict from the standardized payload.
        tag_display_keys: Config-driven list of tag keys to display.

    Returns:
        Dict with keys: summary, description_adf, labels, due_date.
    """
    display_keys = tag_display_keys or _DEFAULT_TAG_DISPLAY_KEYS
    resource_count = len(resources) if isinstance(resources, list) else 0
    # Ensure non-negative
    resource_count = max(0, resource_count)

    return {
        "summary": _build_summary(event, routing, resource_count),
        "description_adf": _build_description(
            event, resources, routing, account_tags, display_keys,
            resource_count,
        ),
        "labels": _build_labels(event, routing),
        "due_date": _compute_due_date(event.get("startTime")),
    }


# ------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------


def _build_summary(event: dict, routing: dict, resource_count: int) -> str:
    """Build the JIRA summary line.

    Format: [Compass] {service} {eventTypeCode} — {account} ({N} resources)
    Truncated to 255 chars.
    """
    service = event.get("service", "Unknown")
    event_type = event.get("eventTypeCode", "")
    account = event.get("affectedAccount", "")

    summary = (
        f"{TICKET_SUMMARY_PREFIX} {service} {event_type} — "
        f"{account} ({resource_count} resources)"
    )
    # JIRA rejects >255 chars
    return summary[:_MAX_SUMMARY_LEN]


# ------------------------------------------------------------------
# Labels
# ------------------------------------------------------------------


def _build_labels(event: dict, routing: dict) -> List[str]:
    """Build the JIRA labels array.

    Labels: compass-campaign, {service}, campaign-{campaignId},
    {accountId}, tag-derived, orphan-unmapped-account if applicable.
    """
    labels = [COMPASS_LABEL]

    service = event.get("service", "")
    if service:
        labels.append(sanitize_for_label(service))

    campaign_id = event.get("campaignId", "")
    if campaign_id:
        labels.append(sanitize_for_label(f"campaign-{campaign_id}"))

    account = event.get("affectedAccount", "")
    # Only add if valid 12-digit account ID
    if isinstance(account, str) and _ACCOUNT_ID_RE.match(account):
        labels.append(account)

    # Tag-derived label from routing
    tag_value = routing.get("routingTagValue")
    if tag_value:
        labels.append(sanitize_for_label(tag_value))

    # Orphan label if routed to default
    if routing.get("fallbackUsed") and routing.get("resolvedBy") == "default":
        labels.append(ORPHAN_LABEL)

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for label in labels:
        if label and label not in seen:
            seen.add(label)
            unique.append(label)
    return unique


# ------------------------------------------------------------------
# Due Date
# ------------------------------------------------------------------


def _compute_due_date(start_time: Optional[str]) -> Optional[str]:
    """Extract YYYY-MM-DD from ISO 8601 startTime, if present."""
    if not isinstance(start_time, str) or not start_time:
        return None
    # ISO 8601: "2026-08-27T00:00:00Z" → "2026-08-27"
    if len(start_time) >= 10:
        date_part = start_time[:10]
        # Basic validation: YYYY-MM-DD
        if len(date_part) == 10 and date_part[4] == "-" and date_part[7] == "-":
            return date_part
    return None


# ------------------------------------------------------------------
# ADF Description
# ------------------------------------------------------------------


def _build_description(
    event: dict,
    resources: List[dict],
    routing: dict,
    account_tags: dict,
    display_keys: List[str],
    resource_count: int,
) -> dict:
    """Build the full ADF description document for Template A."""
    sections: List[dict] = []

    # 1. Campaign Details heading + metadata
    sections.append(adf_heading("Campaign Details", 3))
    sections.extend(_build_metadata(event, routing))

    # 2. Account Tags (conditional — omit if empty)
    tag_section = _build_account_tags_section(account_tags, display_keys)
    if tag_section:
        sections.extend(tag_section)

    # 3. Event Description
    sections.append(adf_rule())
    sections.append(adf_heading("Event Description", 3))
    description = sanitize_description(event.get("description"))
    sections.append(adf_paragraph([description]))

    # 4. Affected Resources
    sections.append(adf_rule())
    sections.append(
        adf_heading(f"Affected Resources ({resource_count})", 3),
    )
    if resource_count <= _MAX_INLINE_RESOURCES:
        table = _build_resource_table(resources, routing)
        if table:
            sections.append(table)
    else:
        sections.append(adf_paragraph([
            (f"{resource_count} affected resources", ["strong"]),
            " — see attached CSV file (affected-resources.csv).",
        ]))

    # 5. Remediation Guidance
    sections.append(adf_rule())
    sections.append(adf_heading("Remediation Guidance", 3))
    start_time = event.get("startTime", "the deadline")
    sections.append(adf_paragraph([
        "Please address these resources before ",
        (str(start_time), ["strong"]),
        ".",
    ]))

    return adf_doc(sections)


def _build_metadata(event: dict, routing: dict) -> List[dict]:
    """Build metadata paragraphs (bold label + value)."""
    paragraphs = [
        adf_bold_value("Service: ", event.get("service", "")),
        adf_bold_value("Deadline: ", event.get("startTime", "")),
        adf_bold_value("Account: ", event.get("affectedAccount", "")),
        adf_bold_value("Region: ", event.get("region", "")),
        adf_bold_value("Actionability: ", event.get("actionability", "")),
    ]
    if routing.get("resolvedBy"):
        paragraphs.append(
            adf_bold_value("Routed by: ", routing["resolvedBy"]),
        )
    return paragraphs


def _build_account_tags_section(
    account_tags: dict,
    display_keys: List[str],
) -> Optional[List[dict]]:
    """Build Account Tags section. Returns None if no tags to display."""
    if not isinstance(account_tags, dict) or not account_tags:
        return None

    tag_paragraphs = []
    for key in display_keys:
        value = account_tags.get(key)
        if value:
            # Tag values in text nodes only
            tag_paragraphs.append(adf_bold_value(f"{key}: ", str(value)))

    if not tag_paragraphs:
        return None

    return [
        adf_rule(),
        adf_heading("Account Tags", 3),
        *tag_paragraphs,
    ]


def _build_resource_table(
    resources: List[dict],
    routing: dict,
) -> Optional[dict]:
    """Build the ADF resource status table.

    Columns vary by routing mode:
    - Tag routing: Resource ARN, {routingTagKey}, Status, Last Updated
    - Other: Resource ARN, Status, Last Updated

    Rows sorted: PENDING first, then RESOLVED, then others.
    """
    if not resources:
        return None

    # Sort: PENDING first
    sorted_resources = sorted(
        resources,
        key=lambda r: _STATUS_ORDER.get(
            r.get("status", ""), 99,
        ),
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

        # ARN as inline code, status as bold
        arn_node = adf_code(arn)
        status_node = {
            "type": "text",
            "text": status,
            "marks": [{"type": "strong"}],
        }

        if has_tag_column:
            # Tag value in text node only
            tags = r.get("resourceTags", {})
            tag_val = tags.get(tag_key, "") if isinstance(tags, dict) else ""
            rows.append([arn_node, str(tag_val), status_node, updated])
        else:
            rows.append([arn_node, status_node, updated])

    return adf_table_rich(headers, rows)


# ------------------------------------------------------------------
# Shared Helpers
# ------------------------------------------------------------------


def sanitize_description(description: Any) -> str:
    """Sanitize event description for ADF insertion.

    Truncate to 30,000 chars.
    Strip null bytes.
    Fallback to default if None/empty/non-string.
    """
    if not description or not isinstance(description, str):
        return "No description provided."
    cleaned = description.replace("\x00", "")
    if not cleaned.strip():
        return "No description provided."
    return cleaned[:_MAX_DESCRIPTION_LEN]


def _format_account_display(event: dict) -> str:
    """Format account display string, truncated to 50 chars."""
    account_tags = event.get("accountTags") or {}
    name = account_tags.get("Name", "")
    account_id = event.get("affectedAccount", "")
    if name:
        display = f"{account_id} ({name})"
    else:
        display = account_id
    return display[:_MAX_DISPLAY_LEN]


# ------------------------------------------------------------------
# Template B — Account-Level Tickets (No Resources)
# ------------------------------------------------------------------


def build_template_b(
    event: dict,
    routing: dict,
    account_tags: Optional[Dict[str, Any]] = None,
    tag_display_keys: Optional[List[str]] = None,
) -> dict:
    """Build a JIRA ticket payload for Template B (without resources).

    Template B is for account-level campaigns: API/SDK deprecations,
    account notifications, and PLEs without specific resources.
    No resource table, no burndown, no CSV attachment.

    Args:
        event: The ``event`` sub-dict from the standardized v2.0 payload.
        routing: The ``routing`` dict from the standardized payload.
        account_tags: The ``accountTags`` dict from the standardized payload.
        tag_display_keys: Config-driven list of tag keys to display.

    Returns:
        Dict with keys: summary, description_adf, labels, due_date.
    """
    display_keys = tag_display_keys or _DEFAULT_TAG_DISPLAY_KEYS
    tags = account_tags if isinstance(account_tags, dict) else {}

    return {
        "summary": _build_summary_b(event, routing),
        "description_adf": _build_description_b(event, tags, display_keys),
        "labels": _build_labels_b(event, routing),
        "due_date": _compute_due_date(event.get("startTime")),
    }


def _build_summary_b(event: dict, routing: dict) -> str:
    """Build summary line for Template B.

    Format: {service}: {eventTypeCode} — {accountDisplay} / {routingTagValue}
    routingTagValue truncated to 50 chars.
    accountDisplay truncated to 50 chars.
    """
    service = event.get("service", "Unknown")
    code = event.get("eventTypeCode", "")
    account = _format_account_display(event)
    tag_val = routing.get("routingTagValue", "")
    if tag_val:
        tag_val = str(tag_val)[:_MAX_DISPLAY_LEN]

    summary = f"{service}: {code} — {account}"
    if tag_val:
        summary += f" / {tag_val}"

    return summary[:_MAX_SUMMARY_LEN]


def _build_labels_b(event: dict, routing: dict) -> List[str]:
    """Build labels for Template B. Includes ``account-level`` discriminator."""
    labels = [COMPASS_LABEL]

    service = event.get("service", "")
    if service:
        labels.append(sanitize_for_label(service))

    campaign_id = event.get("campaignId", "")
    if campaign_id:
        labels.append(sanitize_for_label(f"campaign-{campaign_id}"))

    account = event.get("affectedAccount", "")
    if isinstance(account, str) and _ACCOUNT_ID_RE.match(account):
        labels.append(account)

    actionability = event.get("actionability", "")
    if actionability:
        labels.append(
            sanitize_for_label(f"act-{actionability.lower().replace('action_', '')}"),
        )

    tag_value = routing.get("routingTagValue")
    if tag_value:
        tag_key = routing.get("routingTagKey", "tag").lower()
        labels.append(sanitize_for_label(f"{tag_key}-{tag_value}"))

    if routing.get("resolvedBy") == "default":
        labels.append(ORPHAN_LABEL)

    # Template B discriminator
    labels.append("account-level")

    # Deduplicate preserving order
    seen: set = set()
    unique: List[str] = []
    for label in labels:
        if label and label not in seen:
            seen.add(label)
            unique.append(label)
    return unique


def _build_description_b(
    event: dict,
    account_tags: dict,
    display_keys: List[str],
) -> dict:
    """Build ADF description for Template B (no resource table)."""
    sections: List[dict] = []

    # 1. Event Details heading + metadata
    sections.append(adf_heading("Event Details", 3))
    sections.extend(_build_metadata_b(event))

    # 2. Account Tags (conditional)
    tag_section = _build_account_tags_section(account_tags, display_keys)
    if tag_section:
        sections.extend(tag_section)

    # 3. Event Description
    sections.append(adf_rule())
    sections.append(adf_heading("Event Description", 3))
    description = sanitize_description(event.get("description"))
    sections.append(adf_paragraph([description]))

    # 4. Recommended Action
    sections.append(adf_rule())
    sections.append(adf_heading("Recommended Action", 3))
    sections.append(_build_recommended_action(event.get("startTime")))

    return adf_doc(sections)


def _build_metadata_b(event: dict) -> List[dict]:
    """Build metadata paragraphs for Template B, including Scope line."""
    return [
        adf_bold_value("Service: ", event.get("service", "")),
        adf_bold_value("Deadline: ", event.get("startTime") or "Not specified"),
        adf_bold_value("Account: ", _format_account_display(event)),
        adf_bold_value("Region: ", event.get("region", "")),
        adf_bold_value("Actionability: ", event.get("actionability", "")),
        adf_bold_value(
            "Scope: ",
            "Account-level notification (no specific resources identified)",
        ),
    ]


def _build_recommended_action(start_time: Optional[str]) -> dict:
    """Build recommended action paragraph with deadline variants."""
    if not start_time or not isinstance(start_time, str):
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


# ------------------------------------------------------------------
# Resource Update — Summary + Burndown Comment
# ------------------------------------------------------------------

# ISEC-05: Control character and angle bracket stripping
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_MAX_ARN_LEN = 2048
_MAX_COMMENT_BODY_BYTES = 32_768
_MAX_NEWLY_RESOLVED_DISPLAY = 50


def _sanitize_summary_component(value: str) -> str:
    """Strip control characters and HTML angle brackets from a summary part.

    ISEC-05a/b: Prevents injection of control chars or HTML-like content
    into the JIRA summary field.
    """
    cleaned = _CONTROL_CHARS_RE.sub("", value)
    return cleaned.replace("<", "").replace(">", "")


def build_update_summary(
    event: dict,
    pending_count: int,
    resolved_count: int,
) -> str:
    """Build an updated JIRA summary line for a RESOURCE_UPDATE.

    Uses campaign-level counts per design

    ISEC-04: Type validation on counts.
    ISEC-05: Summary component sanitization.

    Args:
        event: The ``event`` sub-dict from the standardized payload.
        pending_count: Campaign-level pending resource count.
        resolved_count: Campaign-level resolved resource count.

    Returns:
        Sanitized summary string, truncated to 255 chars.
    """
    # ISEC-04: Ensure counts are non-negative ints
    pending = max(0, int(pending_count)) if isinstance(pending_count, (int, float)) else 0
    resolved = max(0, int(resolved_count)) if isinstance(resolved_count, (int, float)) else 0

    service = _sanitize_summary_component(str(event.get("service", "Unknown")))
    event_type = _sanitize_summary_component(str(event.get("eventTypeCode", "")))
    account = _sanitize_summary_component(str(event.get("affectedAccount", "")))

    summary = (
        f"{TICKET_SUMMARY_PREFIX} {service} {event_type} — "
        f"{account} ({pending} pending, {resolved} resolved)"
    )
    return summary[:_MAX_SUMMARY_LEN]


def build_burndown_comment(
    campaign: dict,
    pending_count: int,
    resolved_count: int,
    newly_resolved_arns: Optional[List[str]] = None,
) -> Optional[dict]:
    """Build an ADF burndown comment for a PLE campaign ticket.

    Daily burndown update showing progress and newly resolved
    resources.

    ISEC-04b: Division-by-zero guard on completion %.
    ISEC-04c: Comment body size check (< 32KB).
    ISEC-06: newly_resolved_arns rendered via adf_bullet_list (strings only).

    Args:
        campaign: Campaign dict with ``campaignId``, ``totalResourceCount``.
        pending_count: Current pending count.
        resolved_count: Current resolved count.
        newly_resolved_arns: List of ARN strings that newly resolved.

    Returns:
        ADF document dict for the comment body, or None if the
        serialized body exceeds 32KB.
    """
    import json as _json
    from datetime import datetime as _dt, timezone as _tz

    pending = max(0, int(pending_count)) if isinstance(pending_count, (int, float)) else 0
    resolved = max(0, int(resolved_count)) if isinstance(resolved_count, (int, float)) else 0
    total = campaign.get("totalResourceCount", 0)
    total = max(0, int(total)) if isinstance(total, (int, float)) else 0

    # ISEC-04b: Division-by-zero guard
    if total > 0:
        pct = round(resolved / total * 100, 1)
    else:
        pct = 0.0

    date_str = _dt.now(_tz.utc).strftime("%Y-%m-%d")
    is_complete = total > 0 and resolved >= total

    sections: List[dict] = [
        adf_heading(f"Burndown Update — {date_str}", 3),
        adf_paragraph([
            ("Pending: ", ["strong"]), str(pending),
            " | ",
            ("Resolved: ", ["strong"]), str(resolved),
            " | ",
            ("Total: ", ["strong"]), str(total),
        ]),
        adf_paragraph([
            f"Resolution progress: {pct}%",
        ]),
    ]

    if is_complete:
        sections.append(adf_paragraph([
            ("✅ All resources resolved.", ["strong"]),
        ]))

    # Newly resolved list
    arns = newly_resolved_arns or []
    if arns:
        # ISEC-04c: Truncate each ARN
        safe_arns = [str(a)[:_MAX_ARN_LEN] for a in arns[:_MAX_NEWLY_RESOLVED_DISPLAY]]
        truncated = len(arns) - len(safe_arns)

        sections.append(adf_heading("Newly Resolved", 4))
        sections.append(adf_bullet_list(safe_arns))

        if truncated > 0:
            sections.append(adf_paragraph([
                f"...and {truncated} more.",
            ]))

    doc = adf_doc(sections)

    # ISEC-04c: Size check
    serialized = _json.dumps(doc, separators=(",", ":"), default=str)
    if len(serialized) > _MAX_COMMENT_BODY_BYTES:
        return None

    return doc


# ------------------------------------------------------------------
# Platform-Agnostic Content Model
# ------------------------------------------------------------------

from resolve_core.jira_formatter import JiraFormatter
from resolve_core.ticket_content import TicketContent

_jira_formatter = JiraFormatter()


def _build_resources_by_account(
    resources: List[dict],
) -> Optional[List[tuple]]:
    """Group resources by accountId for multi-account ticket rendering.

    When a grouped ticket spans multiple accounts, returns
    a list of (account_id, account_name, resources) tuples sorted by
    account_id. Returns None if all resources belong to a single account.
    """
    if not resources:
        return None

    account_ids = {r.get("accountId", "") for r in resources}
    if len(account_ids) <= 1:
        return None

    # Group resources by account
    buckets: Dict[str, List[dict]] = {}
    for r in resources:
        aid = r.get("accountId", "unknown")
        buckets.setdefault(aid, []).append(r)

    result = []
    for aid in sorted(buckets):
        acct_resources = buckets[aid]
        # Derive account name from accountName field or tags, fallback to ID
        acct_name = aid
        for r in acct_resources:
            name = r.get("accountName") or ""
            if not name:
                tags = r.get("accountTags") or {}
                name = tags.get("Name", "")
            if name:
                acct_name = name
                break
        result.append((aid, acct_name, acct_resources))

    return result


def build_ticket_content(
    event: dict,
    resources: List[dict],
    routing: dict,
    account_tags: Optional[Dict[str, Any]] = None,
    tag_display_keys: Optional[List[str]] = None,
) -> TicketContent:
    """Build a platform-agnostic TicketContent from event data.

    Extracts all data needed for ticket rendering into a TicketContent
    dataclass, independent of any ITSM platform format.

    Args:
        event: The ``event`` sub-dict from the standardized v2.0 payload.
        resources: The ``resources`` list from the standardized payload.
        routing: The ``routing`` dict from the standardized payload.
        account_tags: The ``accountTags`` dict from the standardized payload.
        tag_display_keys: Config-driven list of tag keys to display.

    Returns:
        TicketContent dataclass with all extracted data.
    """
    display_keys = tag_display_keys or _DEFAULT_TAG_DISPLAY_KEYS
    tags = account_tags if isinstance(account_tags, dict) else {}
    resource_list = resources if isinstance(resources, list) else []
    resource_count = len(resource_list)
    has_resources = resource_count > 0

    campaign_type = "resource-level" if has_resources else "account-level"
    csv_needed = resource_count > _MAX_INLINE_RESOURCES

    # Build summary based on campaign type
    if has_resources:
        summary = _build_summary(event, routing, resource_count)
    else:
        summary = _build_summary_b(event, routing)

    # Build labels based on campaign type
    if has_resources:
        labels = _build_labels(event, routing)
    else:
        labels = _build_labels_b(event, routing)

    due_date = _compute_due_date(event.get("startTime"))

    # Metadata pairs
    if has_resources:
        metadata_pairs = [
            ("Service", event.get("service", "")),
            ("Deadline", event.get("startTime", "")),
            ("Account", event.get("affectedAccount", "")),
            ("Region", event.get("region", "")),
            ("Actionability", event.get("actionability", "")),
        ]
        if routing.get("resolvedBy"):
            metadata_pairs.append(("Routed by", routing["resolvedBy"]))
    else:
        metadata_pairs = [
            ("Service", event.get("service", "")),
            ("Deadline", event.get("startTime") or "Not specified"),
            ("Account", _format_account_display(event)),
            ("Region", event.get("region", "")),
            ("Actionability", event.get("actionability", "")),
            ("Scope", "Account-level notification (no specific resources identified)"),
        ]

    # Description text
    description_text = sanitize_description(event.get("description"))

    # Guidance text: for Template A it's the deadline string, for Template B
    # it's the startTime (used for recommended action rendering)
    if has_resources:
        guidance_text = str(event.get("startTime", "the deadline"))
    else:
        guidance_text = event.get("startTime") or ""

    # Account tags for display
    account_tag_pairs = []
    for key in display_keys:
        value = tags.get(key)
        if value:
            account_tag_pairs.append((key, str(value)))

    return TicketContent(
        summary=summary,
        metadata_pairs=metadata_pairs,
        description_text=description_text,
        resources=resource_list,
        guidance_text=guidance_text,
        labels=labels,
        due_date=due_date,
        campaign_type=campaign_type,
        csv_needed=csv_needed,
        account_tags=account_tag_pairs,
        routing_info=routing if isinstance(routing, dict) else {},
        resources_by_account=_build_resources_by_account(resource_list),
    )


def build_jira_ticket(content: TicketContent) -> dict:
    """Build a full JIRA API ticket payload from TicketContent.

    Uses JiraFormatter to render the description as ADF, then assembles
    the complete payload dict matching the existing public API.

    Args:
        content: Platform-agnostic TicketContent.

    Returns:
        Dict with keys: summary, description_adf, labels, due_date.
    """
    return {
        "summary": content.summary,
        "description_adf": _jira_formatter.format_description(content),
        "labels": content.labels,
        "due_date": content.due_date,
    }
