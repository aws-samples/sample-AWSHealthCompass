"""Routing resolution for AWS Health events.

Pure-Python functions that resolve a JIRA project for a Health event
via a 4-step failover chain:

    1. Tag routing   — TAG_ROUTING#{tagValue} from inline tags
    2. Account routing — ROUTING#{affectedAccount}
    3. Default        — ROUTING_DEFAULT
    4. Error          — no routing found

All evaluation functions are stateless, side-effect-free, and never
raise exceptions — malformed input returns a documented safe default
with ``resolvedBy="error"``.

Consumers: Processor Lambda (step k), Reconciliation Lambda.
Dependencies: Python stdlib only (no boto3, no third-party packages).

Design reference: STORY-018 / 03_dumbledore_design.md §3.
BRD reference: A-JIRA-2, A-JIRA-3, B-ROUTE-1, B-ROUTE-2, BRD §14.3, C-9.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger("resolve_core")

# --- Constants ---

_ACCOUNT_ID_PATTERN = re.compile(r"\d{12}")

_VALID_MODES = frozenset({"account", "tag"})
_VALID_TAG_SOURCES = frozenset({"resource", "account", "both"})

__all__ = [
    "extract_affected_account",
    "extract_tag_value",
    "resolve_routing",
    "resolve_account_routing",
    "derive_routed_via",
]


# ===================================================================
# Account Extraction (C-9)
# ===================================================================


def extract_affected_account(detail: dict, envelope: dict) -> Optional[str]:
    """Extract the affected account ID from a Health event.

    Resolution order per BRD C-9:
    1. ``detail.affectedAccount`` — preferred for org-level events.
    2. ``envelope.account`` — fallback for account-level events.
    3. ``None`` — triggers error routing.

    Args:
        detail: The ``detail`` object from the Health event.
        envelope: The top-level EventBridge envelope.

    Returns:
        12-digit account ID string, or ``None`` if neither source
        has a valid value.
    """
    if isinstance(detail, dict):
        val = detail.get("affectedAccount")
        if isinstance(val, str) and val.strip():
            return val.strip()

    if isinstance(envelope, dict):
        val = envelope.get("account")
        if isinstance(val, str) and val.strip():
            return val.strip()

    return None


# ===================================================================
# Tag Value Extraction (STORY-018)
# ===================================================================


def extract_tag_value(
    strategy: dict,
    entities: list,
    account_tags: dict,
) -> Optional[str]:
    """Extract the routing tag value from inline tags.

    Reads ``tag_key`` and ``tag_source`` from the strategy dict, then
    looks up the tag value from the appropriate source. Only
    ``entities[0].resourceTags`` is checked for resource tags — per-
    resource routing is deferred to Beta (AD-2).

    Args:
        strategy: ``ROUTING_STRATEGY`` config item with ``tag_key``
            and ``tag_source`` fields.
        entities: List of affected entity dicts (may have
            ``resourceTags``).
        account_tags: Normalized account-level tags dict.

    Returns:
        Tag value string, or ``None`` if the tag is absent, empty,
        or not a string (SR-018-04 defense-in-depth).
    """
    tag_key = strategy.get("tag_key")
    if not isinstance(tag_key, str) or not tag_key.strip():
        return None

    tag_source = strategy.get("tag_source", "account")
    if tag_source not in _VALID_TAG_SOURCES:
        # SR-018-06: unknown tag_source treated as "account" with warning
        logger.warning(
            "Unknown tag_source in ROUTING_STRATEGY — "
            "error_code=ROUTING_STRATEGY_INVALID_TAG_SOURCE "
            "value=%s defaulting=account",
            str(tag_source)[:64],
        )
        tag_source = "account"

    value = None

    if tag_source in ("resource", "both"):
        value = _extract_from_resource(tag_key, entities)

    if value is None and tag_source in ("account", "both"):
        value = _extract_from_account(tag_key, account_tags)

    return value


def _extract_from_resource(tag_key: str, entities: list) -> Optional[str]:
    """Extract tag value from the first entity's resourceTags."""
    if not entities or not isinstance(entities, list):
        return None
    first = entities[0] if entities else None
    if not isinstance(first, dict):
        return None
    tags = first.get("resourceTags")
    if not isinstance(tags, dict):
        return None
    return _validate_tag_value(tags.get(tag_key))


def _extract_from_account(tag_key: str, account_tags: dict) -> Optional[str]:
    """Extract tag value from account-level tags."""
    if not isinstance(account_tags, dict):
        return None
    return _validate_tag_value(account_tags.get(tag_key))


def _validate_tag_value(value: Any) -> Optional[str]:
    """Validate and normalize a raw tag value.

    SR-018-04: defense-in-depth type check.
    AD-3: empty/whitespace-only values treated as missing.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped if stripped else None


# ===================================================================
# Platform Target Extraction (STORY-093)
# ===================================================================


def _extract_platform_targets(item: dict) -> dict:
    """Extract all platform-specific routing targets from a config item.

    Returns a dict keyed by platform ID. Only platforms whose required
    fields are present and non-empty are included.

    Args:
        item: A DynamoDB config item (ROUTING#, ROUTING_DEFAULT,
              TAG_ROUTING#, SERVICE_ROUTING#).

    Returns:
        Dict like:
          {"jira": {"project": "X", "issueType": "Task"},
           "servicenow": {"assignmentGroupId": "abc", ...}}
        or {} if no platform fields present.
    """
    platforms = {}

    # JIRA
    jira_project = item.get("jira_project")
    if isinstance(jira_project, str) and jira_project.strip():
        platforms["jira"] = {
            "project": jira_project.strip(),
            "issueType": item.get("jira_issue_type", "Task"),
        }

    # ServiceNow
    snow_group_id = item.get("snow_assignment_group_id")
    if isinstance(snow_group_id, str) and snow_group_id.strip():
        platforms["servicenow"] = {
            "assignmentGroupId": snow_group_id.strip(),
            "assignmentGroupName": item.get("snow_assignment_group_name", ""),
            "recordType": item.get("snow_record_type", "change_request"),
        }

    return platforms


def _build_routing_result(
    platforms: dict,
    resolved_by: str,
    routing_config_key: Optional[str],
    routing_tag_key: Optional[str] = None,
    routing_tag_value: Optional[str] = None,
    fallback_used: bool = False,
) -> dict:
    """Build a routing result dict with backward-compat fields."""
    jira_target = platforms.get("jira")
    return {
        "resolvedProject": jira_target["project"] if jira_target else None,
        "resolvedBy": resolved_by,
        "routingConfigKey": routing_config_key,
        "routingTagKey": routing_tag_key,
        "routingTagValue": routing_tag_value,
        "issueType": jira_target["issueType"] if jira_target else "Task",
        "fallbackUsed": fallback_used,
        "platforms": platforms,
    }


# ===================================================================
# Account Routing (Steps 2–3 of failover chain)
# ===================================================================


def resolve_account_routing(
    affected_account: Optional[str],
    config_cache: dict,
    service: str = "",
) -> dict:
    """Resolve routing targets via account mapping → service → default fallback.

    Pure function — no I/O, no DynamoDB calls. Reads from the
    pre-loaded ``config_cache`` dict.

    Args:
        affected_account: 12-digit account ID from
            :func:`extract_affected_account`, or ``None``.
        config_cache: Pre-loaded ConfigTable items keyed by ``pk``.
        service: AWS service name from the Health event detail
            (used for service-based routing fallback, STORY-088/S-9).

    Returns:
        Routing result dict with keys: ``resolvedProject``,
        ``resolvedBy``, ``routingConfigKey``, ``routingTagKey``,
        ``routingTagValue``, ``issueType``, ``fallbackUsed``,
        ``platforms``.
    """
    # Step 2 — Account lookup
    if (
        isinstance(affected_account, str)
        and _ACCOUNT_ID_PATTERN.fullmatch(affected_account)
    ):
        key = f"ROUTING#{affected_account}"
        item = config_cache.get(key)
        if isinstance(item, dict):
            platforms = _extract_platform_targets(item)
            if platforms:
                return _build_routing_result(
                    platforms, "account", key, fallback_used=False,
                )
            # Item exists but no platform fields
            logger.warning(
                "Account routing item has no platform targets — "
                "error_code=CONFIG_VALIDATION_FAILED "
                "account_id=%s",
                affected_account,
            )

    # Step 2.5 — Service-based routing (STORY-088, S-9)
    if isinstance(service, str) and service.strip():
        service_key = f"SERVICE_ROUTING#{service.upper()}"
        service_item = config_cache.get(service_key)
        if isinstance(service_item, dict):
            platforms = _extract_platform_targets(service_item)
            if platforms:
                return _build_routing_result(
                    platforms, "service", service_key, fallback_used=True,
                )

    # Step 3 — Default fallback
    default = config_cache.get("ROUTING_DEFAULT")
    if isinstance(default, dict):
        platforms = _extract_platform_targets(default)
        if platforms:
            if affected_account:
                jira_target = platforms.get("jira")
                logger.warning(
                    "Account '%s' has no routing mapping — "
                    "using default routing",
                    affected_account,
                )
            return _build_routing_result(
                platforms, "default", "ROUTING_DEFAULT", fallback_used=True,
            )

    # Step 4 — Error (no default configured)
    logger.error(
        "Routing failed — error_code=CFG_ROUTING_NOT_FOUND "
        "affected_account=%s reason=no_routing_targets",
        affected_account or "NONE",
    )
    return {
        "resolvedProject": None,
        "resolvedBy": "error",
        "routingConfigKey": None,
        "routingTagKey": None,
        "routingTagValue": None,
        "issueType": "Task",
        "fallbackUsed": True,
        "platforms": {},
    }


# ===================================================================
# Tag Routing (Step 1 of failover chain — STORY-018)
# ===================================================================


def _resolve_tag_routing(
    strategy: dict,
    account_tags: dict,
    entities: list,
    config_cache: dict,
) -> Optional[dict]:
    """Resolve routing via tag value → TAG_ROUTING# lookup.

    Returns a complete routing result dict on success, or ``None``
    to signal the orchestrator to fall through to account routing.

    The lookup is a Python dict key access on pre-loaded cache data —
    O(1), no DynamoDB call at routing time (AD-5, SEC-21).
    """
    tag_value = extract_tag_value(strategy, entities, account_tags)
    if tag_value is None:
        return None

    # Exact-match lookup in pre-loaded cache (AC-7 / SEC-21)
    tag_key = f"TAG_ROUTING#{tag_value}"
    mapping = config_cache.get(tag_key)
    if not isinstance(mapping, dict):
        return None

    platforms = _extract_platform_targets(mapping)
    if not platforms:
        return None

    return _build_routing_result(
        platforms, "tag", tag_key,
        routing_tag_key=strategy.get("tag_key"),
        routing_tag_value=tag_value,
        fallback_used=False,
    )


# ===================================================================
# Orchestrator
# ===================================================================


def resolve_routing(
    detail: dict,
    envelope: dict,
    account_tags: dict,
    entities: list,
    config_cache: dict,
) -> dict:
    """Resolve JIRA project via the full 4-step failover chain.

    Chain: tag routing → account mapping → default → error.

    This is the single entry point called by the Processor Lambda.

    Args:
        detail: The ``detail`` dict from the Health event.
        envelope: The top-level EventBridge envelope.
        account_tags: Normalized account-level tags.
        entities: List of affected entity dicts.
        config_cache: Pre-loaded ConfigTable items keyed by ``pk``.

    Returns:
        Routing result dict with keys: ``resolvedProject``,
        ``resolvedBy``, ``routingTagKey``, ``routingTagValue``,
        ``issueType``, ``fallbackUsed``.
    """
    affected_account = extract_affected_account(detail, envelope)
    tag_attempted = False

    # Step 1: Tag routing
    strategy = config_cache.get("ROUTING_STRATEGY")
    if isinstance(strategy, dict):
        mode = strategy.get("mode")
        if mode == "tag":
            tag_attempted = True
            tag_result = _resolve_tag_routing(
                strategy, account_tags, entities, config_cache,
            )
            if tag_result is not None:
                return tag_result
        elif mode not in _VALID_MODES:
            # FINDING-IMPL-018-02: log warning for unexpected mode values
            logger.warning(
                "Unknown mode in ROUTING_STRATEGY — "
                "error_code=ROUTING_STRATEGY_INVALID_MODE "
                "value=%s defaulting=account",
                str(mode)[:64],
            )

    # Steps 2–4: Account mapping → service → default → error
    # FINDING-IMPL-018-03: construct clean result — no post-hoc mutation
    service = detail.get("service", "") if isinstance(detail, dict) else ""
    result = resolve_account_routing(affected_account, config_cache, service=service)

    if tag_attempted and result["resolvedBy"] != "error":
        # Tag routing was attempted but failed; account/default is a fallback
        return {
            "resolvedProject": result["resolvedProject"],
            "resolvedBy": result["resolvedBy"],
            "routingConfigKey": result.get("routingConfigKey"),
            "routingTagKey": None,
            "routingTagValue": None,
            "issueType": result["issueType"],
            "fallbackUsed": True,
            "platforms": result.get("platforms", {}),
        }

    return result


# ===================================================================
# Routing Attribution — resolvedBy → routedVia mapping (STORY-071/126)
# ===================================================================


def derive_routed_via(
    routing_result: dict,
    strategy: Optional[dict],
) -> str:
    """Map a routing decision (``resolvedBy``) to the persisted ``routedVia``.

    ``resolvedBy`` is the engine's in-memory decision vocabulary
    (``tag`` / ``account`` / ``service`` / ``default`` / ``error``).
    ``routedVia`` is the granular value persisted on each ResourcesTable
    resource row and read by the B-ROUTE-3 coverage metric
    (``lambdas/api/coverage_handlers.py``), whose recognized set is
    ``{resourceTag, accountTag, account, service, default}`` (∪ ``error``).

    The critical bridge (STORY-117-class drift guard): the engine emits the
    coarse value ``"tag"``, which is **not** in the coverage reader whitelist.
    This function maps ``"tag"`` to the granular ``resourceTag`` / ``accountTag``
    per ``ROUTING_STRATEGY.tag_source`` so the persisted vocabulary matches the
    reader exactly. Unknown / absent ``resolvedBy`` fails closed to ``"error"``.

    This is the single source of truth for the write-side vocabulary, shared by
    the Processor Lambda (real-time) and the Reconciliation Lambda (daily
    Health-API catch-up) so the two paths cannot drift.

    Args:
        routing_result: Result dict from :func:`resolve_routing` /
            :func:`resolve_account_routing` (must carry ``resolvedBy``).
        strategy: The ``ROUTING_STRATEGY`` config item
            (``config_cache.get("ROUTING_STRATEGY")``), or ``None``. Used
            only to disambiguate ``tag`` → ``resourceTag`` / ``accountTag``.

    Returns:
        A value in ``{resourceTag, accountTag, account, service, default,
        error}`` — never the raw engine value ``"tag"``, never empty/None.
    """
    resolved_by = routing_result.get("resolvedBy", "error")
    if resolved_by == "tag":
        # Disambiguate resource vs. account tag from the strategy config.
        if isinstance(strategy, dict):
            tag_source = strategy.get("tag_source", "account")
            if tag_source == "resource":
                return "resourceTag"
            elif tag_source == "both":
                # source=both checks resource tags first, so a matched tag
                # value is attributed to the resource tag.
                return "resourceTag"
            else:
                return "accountTag"
        return "accountTag"
    elif resolved_by in ("account", "default", "error", "service"):
        return resolved_by
    return "error"
