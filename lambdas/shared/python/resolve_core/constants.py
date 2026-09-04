"""Cross-cutting constants shared across Resolve Lambdas.

These are integration contract values — changing them has
cross-module impact and must be coordinated.

Extracted to prevent drift between ticket creation
(write-side) and sync retrieval (read-side).
"""

# JIRA label applied to all Compass-created tickets.
# Used by ticket_builder (write) and sync handler (read/query).
#
# This label value is a DATA-CONTRACT identifier: the write side
# (ticket_builder._build_labels) and the read side (sync handler JQL —
# _build_sync_jql / _build_orphan_jql) MUST agree on the exact literal, or the
# bidirectional sync silently fails to find its own tickets. To guarantee they
# can never drift, both sides reference this single constant — never a literal.
#
# NOTE (Session 41 rename, 2026-09-04): renamed value "resolve-campaign" ->
# "compass-campaign" for the public Compass release. This is SAFE because the
# deployment is fully torn down (no live tickets), so there are no existing
# tickets carrying the old label to orphan from the sync query. Had tickets
# been live, this rename would have required a JIRA-side relabel migration.
COMPASS_LABEL = "compass-campaign"

# --- Product branding (Session 41: Resolve → Compass) ---
# Single source of truth for the user-facing product NAME on the Python side,
# mirroring dashboard/src/branding.ts (APP_NAME). Use this instead of scattering
# the literal "Compass" / "[Compass]" across Lambdas.
#
# This is BRANDING ONLY. It is intentionally unrelated to the `resolve_core`
# module name and the standardized-event `source` schema value ("compass"),
# both of which are internal / data-contract identifiers. (The `resolve_core`
# module name is kept as-is; the `source` value was rebranded to "compass" in
# Session 41 with the deployment torn down.)
# (The JIRA label WAS rebranded to COMPASS_LABEL in Session 41 — see above —
# because it was safe to do so with the deployment torn down.)
APP_NAME = "Compass"

# Prefix on every ITSM ticket summary/short_description the user sees, e.g.
# "[Compass] EC2 AWS_EC2_PLANNED_LIFECYCLE_EVENT — 123456789012 (5 resources)".
# Sync matches tickets by COMPASS_LABEL (the JIRA label), NOT by this summary
# text, so this prefix is safe to rebrand.
TICKET_SUMMARY_PREFIX = f"[{APP_NAME}]"

# Secret-description text shown in the AWS console for credential secrets created
# by the API Lambda's create_secret fallback (CDK sets its own description).
JIRA_SECRET_DESCRIPTION = f"JIRA API credentials for {APP_NAME} integration"
SERVICENOW_SECRET_DESCRIPTION = f"ServiceNow OAuth credentials for {APP_NAME} integration"

# JIRA label for tickets routed to the default/orphan queue.
# Used by ticket_builder (write) and sync handler (orphan count query).
ORPHAN_LABEL = "orphan-unmapped-account"

# --- Orphan queue count/threshold-alert ---
# Extracted to prevent drift between the sync Lambda (write-side,
# lambdas/sync/handler.py::_write_orphan_count) and the config API's
# orphan-status endpoint (read-side, lambdas/api/orphan_handlers.py).
# Previously the reader used a different pk ("ORPHAN_STATUS") and a
# different field name ("orphan_count") than what the writer actually
# persisted ("ORPHAN_COUNT" / "count"), so the API always returned 0.

# ConfigTable partition key the orphan count record is stored under.
ORPHAN_STATUS_KEY = "ORPHAN_COUNT"

# Field on the ORPHAN_STATUS_KEY item holding the current orphan ticket count.
ORPHAN_COUNT_FIELD = "count"

# Orphan ticket count above which the "default project exceeds N tickets"
# alert is raised.
ORPHAN_ALERT_THRESHOLD = 10

# --- Campaign state vs. ticketing lock (CampaignsTable item) ---
# NOTE (ACCEPT AS DEBT): a single CampaignsTable item
# carries TWO independent status-like attributes that read as related but
# are NOT. This was reviewed and accepted as debt:
# renaming or merging either attribute would touch the
# ticketing-lock compare-and-swap logic in
# lambdas/api/dashboard_handlers.py::handle_create_tickets and requires a
# CampaignsTable migration. Both are deferred to a future date — do NOT
# rename, merge, or refactor either attribute as part of clarity work.
#
# `status` (CAMPAIGN_STATE_FIELD) — the CAMPAIGN STATE MACHINE.
#   Values: ACTIVE / COMPLETED / PARTIAL / FILTERED. See
#   resolve_core.campaign.VALID_CAMPAIGN_STATUSES for the authoritative
#   transition table. Tracks whether the Health event's affected resources
#   are still pending remediation.
#   Written by: Processor Lambda, Reconciliation Lambda (via
#   resolve_core.campaign.create_or_update_campaign /
#   recalculate_completion / update_campaign_status).
#   Read by: dashboard API (lambdas/api/dashboard_handlers.py::_format_campaign
#   exposes it as the "status" field in the campaign response).
#
# `campaignStatus` (TICKETING_LOCK_FIELD) — the TICKETING LOCK.
#   Values: TICKETING_IN_PROGRESS / TICKETED / TICKETING_FAILED. A
#   compare-and-swap mutex that prevents duplicate concurrent
#   "Create Tickets" requests for the same campaign. It is UNRELATED to
#   Health resource remediation state.
#   Written & read exclusively by:
#   lambdas/api/dashboard_handlers.py::handle_create_tickets (acquire on
#   entry, release in the try/finally at the end).
#   Exposed to the dashboard as "ticketingStatus" (deliberately NOT
#   "status") to avoid this exact naming collision leaking into the API
#   contract — see _format_campaign.
#
# If/when a future CampaignsTable migration is scheduled, consider renaming
# `campaignStatus` to something like `ticketingLockState` to remove the
# "status" / "Status" visual collision. Not in scope for Alpha/Beta.
CAMPAIGN_STATE_FIELD = "status"
TICKETING_LOCK_FIELD = "campaignStatus"
