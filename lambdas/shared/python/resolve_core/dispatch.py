"""Dispatch window evaluation for AWS Health events.

Pure-Python functions that determine whether a processed Health event
should create JIRA tickets or be stored as a filtered campaign only.
All evaluation functions are stateless, side-effect-free, and never
raise exceptions — malformed input returns a documented safe default.

The config loader reads ``DISPATCH_PRESET`` and ``DISPATCH_RULE#*``
items from ConfigTable and returns a ``DispatchConfig`` dict.

Consumers: Processor Lambda (Steps 10–12), Reconciliation Lambda.
Dependencies: Python stdlib only for evaluation; boto3 for config loading.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger("resolve_core")

# --- Constants ---

_PLE_SUFFIX = "_PLANNED_LIFECYCLE_EVENT"
_VALID_MODES = ("all", "ple_only", "custom")
_MAX_DISPATCH_RULES = 100  # / FINDING-IMPL-04

__all__ = [
    "evaluate_dispatch",
    "load_dispatch_config",
    "validate_dispatch_pattern",
    "validate_event_categories",
]

# --- Validation ---

import re

_PATTERN_REGEX = re.compile(r"^AWS_[A-Z0-9_]+\*?$")
_VALID_CATEGORIES = frozenset(("scheduledChange", "accountNotification"))
_RULE_ID_REGEX = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def validate_dispatch_pattern(pattern: str) -> bool:
    """Return True if pattern matches ^AWS_[A-Z0-9_]+\\*?$.

    Enforced server-side before every DynamoDB write.
    """
    return isinstance(pattern, str) and bool(_PATTERN_REGEX.match(pattern))


def validate_event_categories(categories: list) -> bool:
    """Return True if categories is a non-empty list with valid values only."""
    if not isinstance(categories, list) or len(categories) == 0:
        return False
    return all(c in _VALID_CATEGORIES for c in categories)


def validate_rule_id(rule_id: str) -> bool:
    """Return True if ruleId matches ^[a-zA-Z0-9_-]{1,64}$..3: Validated before pk construction.
    """
    return isinstance(rule_id, str) and bool(_RULE_ID_REGEX.match(rule_id))


# ===================================================================
# Pure Evaluation
# ===================================================================


def evaluate_dispatch(
    event_type_code: str,
    event_type_category: str,
    config: dict,
    actionability: str = "",
) -> dict:
    """Evaluate whether an event should create tickets.

    Pure function — no I/O, no side effects, deterministic.

    Args:
        event_type_code: e.g. ``"AWS_EKS_PLANNED_LIFECYCLE_EVENT"``.
        event_type_category: ``"scheduledChange"`` or
            ``"accountNotification"``.
        config: Dict with ``"mode"`` (str) and ``"rules"`` (list[dict]).
            Produced by :func:`load_dispatch_config`.
        actionability: e.g. ``"ACTION_REQUIRED"`` or
            ``"ACTION_MAY_BE_REQUIRED"``. Used by actionability filter.

    Returns:
        Dict with keys ``dispatched`` (bool), ``mode`` (str),
        ``matchedRule`` (str | None).
    """
    mode = config.get("mode", "all") if isinstance(config, dict) else "all"

    # Actionability filter
    actionability_filter = config.get("actionability_filter", "all_actionable") if isinstance(config, dict) else "all_actionable"
    if actionability_filter == "action_required_only":
        if actionability != "ACTION_REQUIRED":
            return {"dispatched": False, "mode": mode, "matchedRule": None, "filteredBy": "actionability"}

    # Unknown mode → safe default (design decision D3: fail-open).
    if mode not in _VALID_MODES:
        return {"dispatched": True, "mode": "default", "matchedRule": None}

    if mode == "all":
        return {"dispatched": True, "mode": "all", "matchedRule": None}

    if mode == "ple_only":
        is_ple = (
            isinstance(event_type_code, str)
            and event_type_code.endswith(_PLE_SUFFIX)
        )
        return {"dispatched": is_ple, "mode": "ple_only", "matchedRule": None}

    # mode == "custom" — first-match-wins, rules pre-sorted by rule_id.
    rules = config.get("rules", [])
    if not isinstance(rules, list):
        return {"dispatched": False, "mode": "custom", "matchedRule": None}

    for rule in rules:
        if not isinstance(rule, dict) or not rule.get("enabled", False):
            continue

        pattern = rule.get("event_type_pattern", "")
        categories = rule.get("event_categories", [])

        if not isinstance(pattern, str) or not isinstance(categories, list):
            continue

        # FINDING-IMPL-01: skip empty/wildcard-only patterns.
        prefix = pattern.rstrip("*")
        if not prefix or not prefix.strip():
            continue

        # Prefix match: "AWS_EKS_*" → startswith("AWS_EKS_").
        # Exact match when pattern has no trailing "*".
        if pattern.endswith("*"):
            pattern_matches = (
                isinstance(event_type_code, str)
                and event_type_code.startswith(prefix)
            )
        else:
            pattern_matches = event_type_code == pattern

        if not pattern_matches:
            continue

        # Category match.
        category_matches = (
            isinstance(event_type_category, str)
            and event_type_category in categories
        )
        if category_matches:
            return {
                "dispatched": True,
                "mode": "custom",
                "matchedRule": rule.get("rule_id"),
            }

    return {"dispatched": False, "mode": "custom", "matchedRule": None}


# ===================================================================
# Config Loading
# ===================================================================


def load_dispatch_config(config_table: Any) -> dict:
    """Load dispatch configuration from ConfigTable.

    Reads ``DISPATCH_PRESET`` via GetItem and ``DISPATCH_RULE#*`` items
    via Scan (only when mode is ``"custom"``). Rules are sorted by
    ``rule_id`` for deterministic first-match-wins evaluation
    (FINDING-IMPL-15). Truncated to 100 rules (FINDING-IMPL-04).

    Args:
        config_table: A boto3 DynamoDB Table resource for ConfigTable.

    Returns:
        Dict with ``"mode"`` (str) and ``"rules"`` (list[dict]).

    Raises:
        ClientError: On DynamoDB failures — caller must handle retry.
    """
    # Read preset.
    resp = config_table.get_item(
        Key={"pk": "DISPATCH_PRESET"}, ConsistentRead=False,
    )
    preset = resp.get("Item")

    if not preset or not isinstance(preset, dict):
        return {"mode": "all", "rules": []}

    mode = preset.get("mode", "all")
    if mode not in _VALID_MODES:
        logger.warning(
            "Invalid dispatch mode — error_code=DISPATCH_INVALID_MODE "
            "value=%s defaulting=all",
            str(mode)[:64],
        )
        return {"mode": "all", "rules": []}

    if mode != "custom":
        return {"mode": mode, "rules": []}

    # Scan for custom rules.
    rules: list[dict] = []
    scan_kwargs: dict[str, Any] = {
        "FilterExpression": "begins_with(pk, :prefix)",
        "ExpressionAttributeValues": {":prefix": "DISPATCH_RULE#"},
        "ConsistentRead": False,
    }
    while True:
        resp = config_table.scan(**scan_kwargs)
        for item in resp.get("Items", []):
            rules.append({
                "rule_id": item.get("rule_id", ""),
                "event_type_pattern": item.get("event_type_pattern", ""),
                "event_categories": item.get("event_categories", []),
                "enabled": bool(item.get("enabled", False)),
            })
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    # FINDING-IMPL-15: sort for deterministic evaluation order.
    rules.sort(key=lambda r: r.get("rule_id", ""))

    # FINDING-IMPL-04: cap at 100 rules.
    if len(rules) > _MAX_DISPATCH_RULES:
        logger.warning(
            "Dispatch rules truncated — error_code=DISPATCH_RULES_TRUNCATED "
            "total=%d limit=%d",
            len(rules), _MAX_DISPATCH_RULES,
        )
        rules = rules[:_MAX_DISPATCH_RULES]

    return {"mode": "custom", "rules": rules}
