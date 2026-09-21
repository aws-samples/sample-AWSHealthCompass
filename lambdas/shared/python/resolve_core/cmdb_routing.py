"""CMDB-based routing for ServiceNow.

Provides CMDB CI lookup to resolve assignment group from resource ARN.
Only active when platform is ServiceNow and CMDB routing is enabled.

Routing chain (extends existing):
  1. Tag routing (existing)
  2. CMDB routing (this module) — ServiceNow only
  3. Account routing (existing)
  4. Default / orphan queue (existing)

Dependencies: resolve_core.servicenow_client (for get_records).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("resolve_core")


def is_cmdb_routing_enabled(cmdb_config: dict, platform: str) -> bool:
    """Check if CMDB routing is enabled and applicable.

    CMDB routing only works with ServiceNow (requires CMDB Table API).
    Returns False for JIRA or when disabled.
    """
    if platform != "servicenow":
        return False
    return bool(cmdb_config.get("enabled", False))


def resolve_via_cmdb(
    resource_arn: str,
    cmdb_config: dict,
    snow_client: Any,
) -> Optional[str]:
    """Resolve assignment group sys_id via CMDB CI lookup.

    Args:
        resource_arn: The resource ARN to look up in CMDB.
        cmdb_config: CMDB routing configuration dict with 'enabled', 'ci_table', etc.
        snow_client: ServiceNow client instance with get_records method.

    Returns:
        Assignment group sys_id string, or None if not found/disabled/error.
    """
    if not cmdb_config.get("enabled", False):
        return None

    # Only look up valid ARNs
    if not resource_arn or not resource_arn.startswith("arn:"):
        return None

    try:
        ci_table = cmdb_config.get("ci_table", cmdb_config.get("ciTable", "cmdb_ci"))
        lookup_field = cmdb_config.get("lookup_field", cmdb_config.get("lookupField", "object_id"))

        records = snow_client.get_records(
            table=ci_table,
            query=f"{lookup_field}={resource_arn}",
            fields="sys_id,name,support_group",
            limit=1,
        )

        if not records:
            return None

        ci = records[0]
        support_group = ci.get("support_group")

        if not support_group:
            return None

        # Handle reference object format: {"link": "...", "value": "sys_id"}
        if isinstance(support_group, dict):
            return support_group.get("value") or None

        # Handle plain string sys_id
        if isinstance(support_group, str) and support_group.strip():
            return support_group.strip()

        return None

    except Exception:
        logger.exception("CMDB routing lookup failed for %s", resource_arn)
        return None
