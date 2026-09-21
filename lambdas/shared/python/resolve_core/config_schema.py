"""Config schema constants for the ConfigTable.

Documents partition key values and field names for all configuration items.
Schema-on-read: new fields are optional — items missing them load fine.

Alpha: JIRA only (PK_JIRA_CONNECTION, PK_ROUTING_DEFAULT, ROUTING#, DISPATCH_*).
Beta: Adds ServiceNow (PK_SNOW_CONNECTION) and platform selection (PK_ITSM_PLATFORM).
Adds INTEGRATIONS_ENABLED for multi-platform routing.
"""

from __future__ import annotations

import logging
import re

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# --- Platform selection (Beta) ---
PK_ITSM_PLATFORM = "ITSM_PLATFORM"
DEFAULT_PLATFORM = "jira"
VALID_PLATFORMS = frozenset({"jira", "servicenow"})

# --- Multi-platform integrations ---
PK_INTEGRATIONS_ENABLED = "INTEGRATIONS_ENABLED"

# --- JIRA connection (Alpha) ---
PK_JIRA_CONNECTION = "JIRA_CONNECTION"

# --- ServiceNow connection (Beta) ---
PK_SNOW_CONNECTION = "SNOW_CONNECTION"

# --- Routing ---
PK_ROUTING_DEFAULT = "ROUTING_DEFAULT"
PK_ROUTING_PREFIX = "ROUTING#"

# --- Dispatch ---
PK_DISPATCH_PRESET = "DISPATCH_PRESET"
PK_DISPATCH_RULE_PREFIX = "DISPATCH_RULE#"

# --- Telemetry ---
PK_TELEMETRY = "TELEMETRY"

# --- ServiceNow validation constants (Beta, AD-5) ---
VALID_SNOW_RECORD_TYPES = frozenset({"incident", "change_request"})
SNOW_GROUP_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
_MAX_SNOW_GROUP_NAME_LEN = 128


def get_active_platform(config_cache: dict) -> str:
    """Return active ITSM platform from pre-loaded config cache.

    Returns DEFAULT_PLATFORM ("jira") if ITSM_PLATFORM item is
    absent or malformed. Never raises.
    """
    item = config_cache.get(PK_ITSM_PLATFORM)
    if isinstance(item, dict):
        platform = item.get("platform")
        if platform in VALID_PLATFORMS:
            return platform
    return DEFAULT_PLATFORM


def get_enabled_platforms(config_cache: dict) -> list:
    """Return list of enabled platform IDs from config cache.

    Auto-migration: if INTEGRATIONS_ENABLED is absent,
    falls back to ITSM_PLATFORM for backward compat, defaulting
    to ["jira"].
    """
    item = config_cache.get(PK_INTEGRATIONS_ENABLED)
    if isinstance(item, dict):
        platforms = item.get("platforms")
        if isinstance(platforms, list) and platforms:
            return [p for p in platforms if isinstance(p, str) and p in VALID_PLATFORMS]

    # Backward compat: read legacy ITSM_PLATFORM
    legacy = get_active_platform(config_cache)
    return [legacy]


def _normalize_platform_list(platforms: list) -> list[str]:
    """Normalize a platform list for the resolver contract.

    Enforces:
      - only members of VALID_PLATFORMS (drop unknown/malformed tokens),
      - de-duplicated,
      - deterministic order: JIRA first, then ServiceNow,
      - never empty — if filtering empties the list, fall back to
        [DEFAULT_PLATFORM].

    Never raises.
    """
    # Deterministic canonical order; extend here if VALID_PLATFORMS grows.
    _ORDER = ("jira", "servicenow")
    seen: set[str] = set()
    normalized: list[str] = []
    if isinstance(platforms, list):
        for p in platforms:
            if isinstance(p, str) and p in VALID_PLATFORMS and p not in seen:
                seen.add(p)
                normalized.append(p)
    if not normalized:
        # Never-empty guard: a malformed/empty/all-invalid input
        # must not yield []; fall back to the JIRA default.
        return [DEFAULT_PLATFORM]
    # Reorder deterministically (JIRA first, then ServiceNow).
    return [p for p in _ORDER if p in seen]


def resolve_platforms(config_table) -> list[str]:
    """THE single authoritative platform resolver (live-read variant).

    Reads INTEGRATIONS_ENABLED (source of truth) and, only as a legacy
    fallback, ITSM_PLATFORM, then reuses the existing precedence logic in
    ``get_enabled_platforms`` and normalizes the result.

    Precedence:
        INTEGRATIONS_ENABLED.platforms -> ITSM_PLATFORM.platform -> ["jira"]

    Return shape:
        - non-empty ``list[str]``, elements drawn from VALID_PLATFORMS,
        - de-duplicated, JIRA-first deterministic order — one of
          ["jira"], ["servicenow"], ["jira","servicenow"],
        - never ``[]``, never ``None``.

    Security: resolution routes exclusively through the allow-listed
    ``get_enabled_platforms``/``get_active_platform`` path — no raw
    ``item.get("platform")`` bypass; unknown tokens are dropped, never emitted.

    Fail-safe: any ClientError reading ConfigTable returns
    [DEFAULT_PLATFORM]; the error is logged at WARNING with its error code so
    an AccessDenied/permission failure is distinguishable in CloudWatch from a
    benign JIRA-only deployment. Never raises.
    """
    cache: dict = {}
    try:
        integ = config_table.get_item(Key={"pk": PK_INTEGRATIONS_ENABLED}).get("Item")
        if integ is not None:
            cache[PK_INTEGRATIONS_ENABLED] = integ
        legacy = config_table.get_item(Key={"pk": PK_ITSM_PLATFORM}).get("Item")
        if legacy is not None:
            cache[PK_ITSM_PLATFORM] = legacy
    except ClientError as exc:
        #.2: degrade loudly — log the error code so AccessDenied is not
        # silently masked as a normal JIRA-only deployment.
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        logger.warning(
            "resolve_platforms — ConfigTable read failed, defaulting to "
            "[%s] — error_code=%s",
            DEFAULT_PLATFORM,
            error_code,
        )
        return [DEFAULT_PLATFORM]

    # Reuse the EXISTING precedence logic (do not re-implement —.9).
    platforms = get_enabled_platforms(cache)
    return _normalize_platform_list(platforms)


def operative_platform(platforms: list[str]) -> str:
    """Collapse the resolved platforms array to the single operative platform.

    Defined ONCE here; downstream consumers use this — they MUST NOT re-derive it.

    Rule:
        ["servicenow"]                          -> "servicenow"
        everything else (incl. ["jira"] and
        ["jira","servicenow"])                  -> "jira"

    The asymmetry (ServiceNow requires *sole* enablement; JIRA wins ties) is
    the minimal, JIRA-safe collapse: dual resolves to "jira" so the tested
    JIRA path is preserved and no half-built dual routing/label logic is
    introduced.
    """
    if platforms == ["servicenow"]:
        return "servicenow"
    return DEFAULT_PLATFORM


def extract_routing_target(item: dict, platform: str) -> dict | None:
    """Extract routing target from a config item for the given platform.

    Returns:
        {"routing_target": str, "record_type": str, "routing_target_name": str}
        or None if the item lacks required fields for the platform.
    """
    if platform == "servicenow":
        group_id = item.get("snow_assignment_group_id")
        if not group_id:
            return None
        return {
            "routing_target": group_id,
            "record_type": item.get("snow_record_type", "change_request"),
            "routing_target_name": item.get("snow_assignment_group_name", group_id),
        }
    # jira (default)
    project = item.get("jira_project")
    if not project:
        return None
    return {
        "routing_target": project,
        "record_type": item.get("jira_issue_type", "Task"),
        "routing_target_name": project,
    }
