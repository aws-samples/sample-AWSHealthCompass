"""Tag normalization, sanitization, and cross-region enrichment utilities.

Pure-Python functions for normalizing tag dicts from Health event payloads
and sanitizing tag values for use as JIRA labels. Also provides cross-region
tag fetching via Resource Groups Tagging API for cases where inline tags are
absent (older events, non-org accounts).

Primary tag source: Inline ``resourceTags``/``accountTags`` from the Health
event payload (zero-latency, no API calls). Cross-region fetching is a
fallback only.

Consumers: Processor Lambda (normalize_tags, fetch_resource_tags),
JIRA Integration Lambda (sanitize_for_label),
Reconciliation Lambda (normalize_tags, fetch_resource_tags).
Dependencies: Python stdlib (normalize/sanitize), boto3 (fetch only).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

logger = logging.getLogger("resolve_core")

# --- Module-level constants (immutable) ---

_MAX_TAG_KEY_LEN = 255     # IC-7
_MAX_TAG_VALUE_LEN = 1024  # IC-7
_MAX_TAG_ENTRIES = 100     # IC-7

_LABEL_INVALID_CHARS = re.compile(r"[^a-z0-9\-_.]")  # IC-9
_LABEL_MULTI_HYPHEN = re.compile(r"-{2,}")            # IC-10

# --- Public API ---

__all__ = [
    "normalize_tags",
    "sanitize_for_label",
    "extract_region_from_arn",
    "fetch_resource_tags",
]

# --- Cross-region tag fetching ---

_DEFAULT_REGION = os.environ.get("AWS_REGION", "us-east-1")
_regional_clients: dict[str, Any] = {}


def extract_region_from_arn(arn: str) -> Optional[str]:
    """Extract region from an ARN string.

    Returns None for global services (S3, IAM) where the region
    component is empty, or for invalid/non-ARN values.

    Args:
        arn: A resource ARN string.

    Returns:
        Region string (e.g., ``"us-east-1"``) or ``None``.
    """
    if not arn or not arn.startswith("arn:"):
        return None
    parts = arn.split(":")
    if len(parts) >= 4 and parts[3]:
        return parts[3]
    return None


def _get_tagging_client(region: str):
    """Get or create a cached regional Resource Groups Tagging API client."""
    if region not in _regional_clients:
        import boto3

        _regional_clients[region] = boto3.client(
            "resourcegroupstaggingapi", region_name=region
        )
    return _regional_clients[region]


def fetch_resource_tags(arn: str) -> dict[str, str]:
    """Fetch tags for a resource ARN using the correct regional endpoint.

    Uses the Resource Groups Tagging API in the region extracted from
    the ARN. Falls back to the Lambda's deployment region for global
    services (S3, IAM) where the ARN has no region component.

    This is a fallback for when inline tags (``resourceTags`` in the
    Health event payload) are absent. Failures return ``{}`` — tag
    enrichment is best-effort and must never block processing.

    Args:
        arn: A valid AWS resource ARN.

    Returns:
        A ``dict[str, str]`` of tag key-value pairs, or ``{}`` on
        any failure (invalid ARN, API error, access denied, etc.).
    """
    if not arn or not arn.startswith("arn:"):
        return {}

    region = extract_region_from_arn(arn) or _DEFAULT_REGION

    try:
        client = _get_tagging_client(region)
        response = client.get_resources(
            ResourceARNList=[arn],
            ResourcesPerPage=1,
        )
        resource_list = response.get("ResourceTagMappingList", [])
        if not resource_list:
            return {}

        tags = resource_list[0].get("Tags", [])
        return {tag["Key"]: tag["Value"] for tag in tags}

    except Exception:
        logger.warning(
            "Cross-region tag fetch failed — "
            "arn=%s region=%s",
            arn,
            region,
            exc_info=True,
        )
        return {}


def normalize_tags(tags: Any) -> dict[str, str]:
    """Normalize a tag dict to consistent ``dict[str, str]`` format.

    Converts ``None``, missing, or non-dict values to ``{}``. Coerces
    all keys and values to strings, strips whitespace-only keys, and
    truncates keys to 255 chars and values to 1024 chars. Caps entries
    at 100.

    Args:
        tags: Tag dict from a Health event (``accountTags`` or
            ``resourceTags``). Any type accepted.

    Returns:
        A normalized ``dict[str, str]``. Never ``None``, never raises.
    """
    if not isinstance(tags, dict):  # IC-1
        return {}

    result: dict[str, str] = {}
    for key, value in tags.items():
        if len(result) >= _MAX_TAG_ENTRIES:  # IC-4
            logger.warning(
                "Tag entries exceed maximum — "
                "error_code=TAG_ENTRIES_EXCEEDED max=%d total=%d",
                _MAX_TAG_ENTRIES,
                len(tags),
            )  # IC-8: log count only, no keys/values
            break

        str_key = str(key).strip()  # IC-2
        if not str_key:
            continue

        str_key = str_key[:_MAX_TAG_KEY_LEN]  # IC-3

        if value is None:  # IC-5
            str_value = ""
        else:
            str_value = str(value)

        str_value = str_value[:_MAX_TAG_VALUE_LEN]  # IC-3
        result[str_key] = str_value

    return result


def sanitize_for_label(value: Any) -> str:
    """Convert a value to a JIRA-safe label string.

    Lowercases, replaces spaces with hyphens, strips invalid characters
    (keeping only ``[a-z0-9\\-_.]``), collapses consecutive hyphens,
    strips leading/trailing hyphens, and truncates to 255 chars.

    Args:
        value: Tag value or arbitrary string. Any type accepted.

    Returns:
        A sanitized label string. May be ``""`` if the input contains
        no valid label characters. Never ``None``, never raises.
    """
    s = str(value).strip().lower()  # IC-12
    s = s.replace(" ", "-")
    s = _LABEL_INVALID_CHARS.sub("", s)  # IC-9
    s = _LABEL_MULTI_HYPHEN.sub("-", s)  # IC-10
    s = s.strip("-")                      # IC-10
    return s[:_MAX_TAG_KEY_LEN]           # IC-11: 255 char max
