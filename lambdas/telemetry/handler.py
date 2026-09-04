"""Resolve Telemetry Lambda — daily aggregation of anonymized metrics.

Collects aggregated counts only (no PII, no ARNs, no account IDs).
Gated by customer consent (TELEMETRY.consent in ConfigTable).
Delivery mechanism TBD (BRD Q-3) — stores payload in ConfigTable for now.

Metrics collected:
  T-B-1: Resource tag routing vs. account tag fallback ratio
  T-B-2: Tag keys used for routing
  T-B-3: ServiceNow vs. JIRA split (platform)
  T-B-4: Dashboard active sessions and unique users per day
  T-B-5: Most common dashboard filters used
  T-B-6: Orphan queue volume
  T-B-7: Time from ticket creation to first status change
  T-B-8: Error log patterns (placeholder — CloudWatch Logs Insights deferred)
"""
import json
import logging
import os
import statistics
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key, Attr

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

CONFIG_TABLE = os.environ.get("CONFIG_TABLE", "compass-config")
RESOURCES_TABLE = os.environ.get("RESOURCES_TABLE", "compass-resources")

_dynamodb = boto3.resource("dynamodb")


def lambda_handler(event, context):
    """Daily telemetry aggregation. Exits immediately if consent not granted."""
    config_table = _dynamodb.Table(CONFIG_TABLE)

    # Consent gate (T-IMP-3)
    consent_item = config_table.get_item(Key={"pk": "TELEMETRY"}).get("Item", {})
    if not consent_item.get("consent"):
        logger.info("Telemetry consent not granted. Skipping.")
        return {"collected": False, "reason": "no_consent"}

    # T-B-3: Active ITSM platform
    platform_item = config_table.get_item(Key={"pk": "ITSM_PLATFORM"}).get("Item", {})
    platform = platform_item.get("platform", "jira")

    # T-B-2: Tag key used for routing
    strategy_item = config_table.get_item(Key={"pk": "ROUTING_STRATEGY"}).get("Item", {})
    tag_key = strategy_item.get("tag_key", None)

    # T-B-1: Routing fallback ratio (scan ResourcesTable, capped at 10 pages)
    routing_counts = _aggregate_routing_counts()

    total = sum(routing_counts.values())
    tag_routed = routing_counts["resourceTag"] + routing_counts["accountTag"]

    # T-B-4: Unique users today (from SESSION# items)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    unique_users = _count_unique_sessions(config_table, today)

    # T-B-5: Top dashboard filters (from TELE_EVENT# items)
    top_filters = _aggregate_top_filters(config_table, today)

    # T-B-6: Orphan queue volume (routedVia=default from T-B-1 scan)
    orphan_volume = routing_counts.get("default", 0)

    # T-B-7: Time from ticket creation to first status change
    median_time_to_first_change = _compute_median_time_to_first_change()

    # T-B-8: Error log patterns (CloudWatch Logs Insights — deferred)
    top_errors = []

    # Build payload (T-IMP-4: versioned schema)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "version": "2.1",
        "collectedAt": now,
        "platform": platform,
        "routingTagKey": tag_key,
        "routingCounts": routing_counts,
        "routingTotal": total,
        "tagRoutedRatio": round(tag_routed / total, 3) if total > 0 else 0,
        "accountFallbackRatio": round(routing_counts["account"] / total, 3) if total > 0 else 0,
        "defaultFallbackRatio": round(routing_counts["default"] / total, 3) if total > 0 else 0,
        # T-B-4
        "uniqueUsersToday": unique_users,
        # T-B-5
        "topFilters": top_filters,
        # T-B-6
        "orphanVolume": orphan_volume,
        # T-B-7
        "medianTimeToFirstChangeHours": median_time_to_first_change,
        # T-B-8
        "topErrors": top_errors,
    }

    # Store latest — delivery mechanism TBD (BRD Q-3)
    config_table.put_item(Item={
        "pk": "TELEMETRY_LATEST",
        **{k: _to_dynamo_safe(v) for k, v in payload.items()},
    })

    logger.info("Telemetry collected: version=%s total=%d platform=%s users=%d",
                payload["version"], total, platform, unique_users)
    return {"collected": True, "payload": payload}


def _aggregate_routing_counts() -> dict:
    """Scan ResourcesTable for routedVia field, capped at 10 pages."""
    counts = {"resourceTag": 0, "accountTag": 0, "account": 0, "default": 0, "error": 0}
    resources_table = _dynamodb.Table(RESOURCES_TABLE)
    scan_kwargs = {"ProjectionExpression": "routedVia"}

    for _ in range(10):
        resp = resources_table.scan(**scan_kwargs)
        for item in resp.get("Items", []):
            rv = item.get("routedVia", "error")
            if rv in counts:
                counts[rv] += 1
            else:
                counts["error"] += 1
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    return counts


def _count_unique_sessions(config_table, today: str) -> int:
    """Count unique hashed user subs from SESSION# items for today (T-B-4)."""
    subs = set()
    scan_kwargs = {
        "FilterExpression": Attr("pk").begins_with(f"SESSION#{today}"),
        "ProjectionExpression": "hashedSub",
    }
    for _ in range(5):
        resp = config_table.scan(**scan_kwargs)
        for item in resp.get("Items", []):
            sub = item.get("hashedSub")
            if sub:
                subs.add(sub)
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return len(subs)


def _aggregate_top_filters(config_table, today: str) -> list:
    """Aggregate top 10 UI events from TELE_EVENT# items for today (T-B-5)."""
    events = []
    scan_kwargs = {
        "FilterExpression": Attr("pk").begins_with(f"TELE_EVENT#{today}"),
        "ProjectionExpression": "pk, #c",
        "ExpressionAttributeNames": {"#c": "count"},
    }
    for _ in range(5):
        resp = config_table.scan(**scan_kwargs)
        for item in resp.get("Items", []):
            pk = item.get("pk", "")
            # pk format: TELE_EVENT#{date}#{event_name}
            parts = pk.split("#", 2)
            name = parts[2] if len(parts) > 2 else pk
            count = int(item.get("count", 0))
            events.append({"event": name, "count": count})
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    events.sort(key=lambda x: x["count"], reverse=True)
    return events[:10]


def _compute_median_time_to_first_change():
    """Compute median hours from ticket creation to first status change (T-B-7).

    Scans ResourcesTable for items with both ticketCreatedAt and firstStatusChangeAt.
    Returns None if insufficient data.
    """
    resources_table = _dynamodb.Table(RESOURCES_TABLE)
    deltas_hours = []
    scan_kwargs = {
        "FilterExpression": Attr("ticketCreatedAt").exists() & Attr("firstStatusChangeAt").exists(),
        "ProjectionExpression": "ticketCreatedAt, firstStatusChangeAt",
    }

    for _ in range(5):
        resp = resources_table.scan(**scan_kwargs)
        for item in resp.get("Items", []):
            created = item.get("ticketCreatedAt", "")
            changed = item.get("firstStatusChangeAt", "")
            delta = _iso_diff_hours(created, changed)
            if delta is not None and delta >= 0:
                deltas_hours.append(delta)
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    if not deltas_hours:
        return None
    return round(statistics.median(deltas_hours), 1)


def _iso_diff_hours(start_iso: str, end_iso: str):
    """Compute difference in hours between two ISO 8601 timestamps."""
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        start = datetime.strptime(start_iso, fmt).replace(tzinfo=timezone.utc)
        end = datetime.strptime(end_iso, fmt).replace(tzinfo=timezone.utc)
        return (end - start).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return None


def _to_dynamo_safe(value):
    """Convert floats to Decimal for DynamoDB compatibility."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_dynamo_safe(v) for v in value]
    return value
