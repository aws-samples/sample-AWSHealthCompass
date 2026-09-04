"""Campaign-level pagination logic for multi-page AWS Health events.

Large PLEs arrive as multiple Amazon EventBridge messages sharing the same
``eventArn`` with ``page``/``totalPages`` fields. This module determines
the correct processing path based on page number and campaign existence,
and provides resource-count helpers for atomic Amazon DynamoDB updates.

Consumers: Processor Lambda.
Dependencies: Python stdlib only (no boto3, no third-party packages).
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, List, NamedTuple, Optional

logger = logging.getLogger("resolve_core")


class PaginationPath(Enum):
    """Processing path for a paginated event.

    Each value maps to a DynamoDB write pattern in the Processor Lambda:

    - ``CREATE``: Page 1, no existing campaign → full create + SNS.
    - ``COMPLETE_STUB``: Page 1, existing PARTIAL campaign → upgrade + SNS.
    - ``MERGE``: Page 1, existing ACTIVE campaign → metadata update, no SNS.
    - ``CREATE_STUB``: Page >1, no existing campaign → PARTIAL stub, no SNS.
    - ``APPEND``: Page >1, existing campaign → append resources, no SNS.
    """

    CREATE = "CREATE"
    COMPLETE_STUB = "COMPLETE_STUB"
    MERGE = "MERGE"
    CREATE_STUB = "CREATE_STUB"
    APPEND = "APPEND"


# Paths that trigger SNS publish for ticket creation.
_SNS_PATHS: frozenset[PaginationPath] = frozenset(
    {PaginationPath.CREATE, PaginationPath.COMPLETE_STUB}
)


class ResourceCounts(NamedTuple):
    """Counts derived from a single page's resource list."""

    total: int
    pending: int
    resolved: int


def determine_pagination_path(
    page: int,
    campaign_exists: bool,
    campaign_status: Optional[str],
) -> PaginationPath:
    """Determine the processing path for a paginated event.

    Args:
        page: Clamped page number from :func:`coerce_page_fields`.
        campaign_exists: Whether a campaign record exists in DynamoDB.
        campaign_status: The ``status`` attribute of the existing campaign,
            or ``None`` if the campaign does not exist.

    Returns:
        The :class:`PaginationPath` that the Processor Lambda should follow.
    """
    if page == 1:
        if not campaign_exists:
            return PaginationPath.CREATE
        if campaign_status == "PARTIAL":
            return PaginationPath.COMPLETE_STUB
        return PaginationPath.MERGE

    # page > 1
    if not campaign_exists:
        return PaginationPath.CREATE_STUB
    return PaginationPath.APPEND


def should_publish_sns(path: PaginationPath, dispatched: bool) -> bool:
    """Return whether the given path should publish to SNS.

    SNS is published only on CREATE or COMPLETE_STUB, and only when the
    dispatch evaluation determined the campaign should create tickets.

    Args:
        path: The pagination path from :func:`determine_pagination_path`.
        dispatched: Whether the dispatch window allows ticket creation.

    Returns:
        ``True`` if the Processor should publish to the SNS Integration
        Topic after this path completes.
    """
    return path in _SNS_PATHS and dispatched


def count_resources(entities: List[dict]) -> ResourceCounts:
    """Count total, pending, and resolved resources from an entity list.

    Used by all five pagination paths to compute the counter deltas for
    DynamoDB ``ADD`` expressions.

    Args:
        entities: List of entity dicts from :func:`extract_entities`.
            Each entity should have a ``status`` key.

    Returns:
        A :class:`ResourceCounts` named tuple.
    """
    total = len(entities)
    pending = 0
    resolved = 0
    for entity in entities:
        status = entity.get("status", "") if isinstance(entity, dict) else ""
        if status == "RESOLVED":
            resolved += 1
        else:
            # All non-RESOLVED statuses (PENDING, IMPAIRED, UNKNOWN, empty,
            # unrecognized) count as pending — fail-safe for future status values.
            pending += 1
    return ResourceCounts(total=total, pending=pending, resolved=resolved)
