"""Campaign grouping logic for resource-to-ticket distribution.

Pure-Python module that groups resources by configurable strategies
(per-account, per-tag-value, single) to support Beta campaign grouping
(BRD B-CAMP-1). Each group becomes a single ITSM ticket, reducing
ticket noise for multi-resource campaigns.

Consumers: JIRA Integration Lambda, ServiceNow Integration Lambda.
Dependencies: Python stdlib only (no boto3, no third-party packages).
"""

from __future__ import annotations

__all__ = ["group_resources"]

_MAX_GROUPS = 100  # SEC-073-02: prevent unbounded group explosion

_VALID_STRATEGIES = ("per-account", "per-tag-value", "single")


def group_resources(
    resources: list[dict],
    strategy: str,
    tag_key: str | None = None,
) -> list[dict]:
    """Group resources by strategy.

    Args:
        resources: List of resource dicts with keys: resourceArn, accountId,
                   resourceTags (dict), accountTags (dict), etc.
        strategy: 'per-account' | 'per-tag-value' | 'single'
        tag_key: Tag key name for per-tag-value strategy.

    Returns:
        List of group dicts: [{label: str, resources: list[dict],
        account_ids: set[str]}]

    Raises:
        ValueError: If strategy is invalid or per-tag-value without tag_key,
                    or if group count exceeds 100.
    """
    if strategy not in _VALID_STRATEGIES:
        raise ValueError(
            f"Invalid strategy '{strategy}'. Must be one of: {_VALID_STRATEGIES}"
        )

    if strategy == "per-tag-value" and not tag_key:
        raise ValueError("per-tag-value strategy requires tag_key")

    if strategy == "per-account":
        groups = _group_by_account(resources)
    elif strategy == "per-tag-value":
        groups = _group_by_tag(resources, tag_key)
    else:
        groups = _group_single(resources)

    if len(groups) > _MAX_GROUPS:
        raise ValueError(f"Too many groups (max {_MAX_GROUPS})")

    return groups


def _group_by_account(resources: list[dict]) -> list[dict]:
    """Group resources by accountId field. Label = account ID."""
    buckets: dict[str, list[dict]] = {}
    for r in resources:
        key = r.get("accountId", "unknown")
        buckets.setdefault(key, []).append(r)

    if not buckets:
        return [{"label": "unknown", "resources": [], "account_ids": set()}]

    return [
        {
            "label": label,
            "resources": items,
            "account_ids": {r.get("accountId", "unknown") for r in items},
        }
        for label, items in buckets.items()
    ]


def _group_by_tag(resources: list[dict], tag_key: str) -> list[dict]:
    """Group resources by tag value. Checks resourceTags then accountTags."""
    buckets: dict[str, list[dict]] = {}
    for r in resources:
        value = _resolve_tag_value(r, tag_key)
        buckets.setdefault(value, []).append(r)

    if not buckets:
        return [{"label": "untagged", "resources": [], "account_ids": set()}]

    return [
        {
            "label": label,
            "resources": items,
            "account_ids": {r.get("accountId", "unknown") for r in items},
        }
        for label, items in buckets.items()
    ]


def _group_single(resources: list[dict]) -> list[dict]:
    """All resources in one group. Label = 'all'."""
    return [
        {
            "label": "all",
            "resources": list(resources),
            "account_ids": {r.get("accountId", "unknown") for r in resources},
        }
    ]


def _resolve_tag_value(resource: dict, tag_key: str) -> str:
    """Extract tag value from resourceTags, fallback to accountTags.

    Returns 'untagged' if tag is missing or empty string.
    """
    resource_tags = resource.get("resourceTags") or {}
    value = resource_tags.get(tag_key)

    if not value:
        account_tags = resource.get("accountTags") or {}
        value = account_tags.get(tag_key)

    if not value:
        return "untagged"

    return value
