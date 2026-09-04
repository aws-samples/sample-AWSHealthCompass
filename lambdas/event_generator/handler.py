"""Event Generator Lambda — publishes synthetic AWS Health events to SQS.

TEST ONLY. Invoked via ``aws lambda invoke`` or SDK. Accepts ``count`` and
optional ``scenario`` parameters. Delegates all event construction to
``event_factory.py`` (STORY-033). Sends events directly to the SQS
Ingestion Queue (bypasses EventBridge to avoid reserved source rejection).

Environment variables:
    INGESTION_QUEUE_URL — SQS queue URL for the ingestion queue.
"""
import json
import logging
import os
import time
import random

import boto3

from event_factory import make_health_event, make_resources

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_INGESTION_QUEUE_URL = os.environ.get("INGESTION_QUEUE_URL", "")

VALID_SCENARIOS = frozenset({
    "basic", "paginated", "zero_resources", "bare_instance_ids",
    "backup_event", "informational", "large_campaign",
    "account_notification", "mixed",
})

DEFAULT_ACCOUNTS = ["111111111111", "222222222222", "333333333333"]
DEFAULT_SERVICES = ["EKS", "RDS", "EC2", "LAMBDA", "ELASTICACHE", "S3"]


def lambda_handler(event: dict, context) -> dict:
    """Entry point. Validates input, generates events, sends to SQS."""
    # CTRL-07: Audit log before processing.
    logger.info(json.dumps({
        "action": "event_generator_invoked",
        "request_id": getattr(context, "aws_request_id", "local"),
        "payload": event,
    }))

    # --- Single event mode (STORY-078: Route Test / Full Pipeline) ---
    single_event = event.get("singleEvent")
    if single_event:
        return _handle_single_event(single_event, context)

    # --- Validation (CTRL-08) ---
    if "count" not in event or not isinstance(event.get("count"), int):
        return _error(400, "MISSING_COUNT", "count (integer) is required")

    count = event["count"]
    if count < 0 or count > 1000:
        return _error(400, "COUNT_OUT_OF_RANGE", "count must be 0–1000")

    scenario = event.get("scenario")
    if scenario is not None and scenario not in VALID_SCENARIOS:
        return _error(400, "UNKNOWN_SCENARIO", f"Unknown scenario: {scenario}. Valid: {sorted(VALID_SCENARIOS)}")

    accounts = event.get("accounts", DEFAULT_ACCOUNTS)
    if not isinstance(accounts, list) or not all(
        isinstance(a, str) and len(a) == 12 and a.isdigit() for a in accounts
    ):
        return _error(400, "INVALID_ACCOUNT_ID", "accounts must be a list of 12-digit strings")

    services = event.get("services", DEFAULT_SERVICES)

    # --- Generate events ---
    if count == 0:
        return _success([], scenario)

    # Generate a unique run ID so each invocation creates distinct campaigns.
    # Campaign IDs for PLEs are derived from service:eventTypeCode, so we
    # append a run-unique suffix to eventTypeCode to prevent merging.
    run_id = f"{int(time.time())}_{random.randint(1000, 9999)}"

    events_to_publish = _generate(count, scenario, accounts, services, run_id)

    # --- Send to SQS ingestion queue ---
    queue_url = _INGESTION_QUEUE_URL
    if not queue_url:
        return _error(500, "MISSING_QUEUE_URL", "INGESTION_QUEUE_URL environment variable not set")

    sqs_client = boto3.client("sqs")
    event_arns = []
    failed = []

    for i, health_event in enumerate(events_to_publish):
        try:
            sqs_client.send_message(
                QueueUrl=queue_url,
                MessageBody=json.dumps(health_event),
            )
            event_arns.append(health_event["detail"]["eventArn"])
        except Exception as exc:
            logger.error(json.dumps({
                "action": "sqs_send_failed",
                "index": i,
                "error": str(exc),
            }))
            failed.append({"index": i, "errorCode": "SQS_ERROR", "errorMessage": str(exc)})

    result = _success(event_arns, scenario)
    if failed:
        result["statusCode"] = 207
        result["failedEvents"] = failed
    # CTRL-08: Warn on high count.
    if count > 100:
        result["warning"] = "High event count may trigger significant JIRA ticket creation if routing is configured."
    return result


# ---------------------------------------------------------------------------
# Single event mode (STORY-078)
# ---------------------------------------------------------------------------

def _handle_single_event(spec: dict, context) -> dict:
    """Generate exactly 1 synthetic event using caller-specified fields."""
    queue_url = _INGESTION_QUEUE_URL
    if not queue_url:
        return _error(500, "MISSING_QUEUE_URL", "INGESTION_QUEUE_URL environment variable not set")

    service = spec.get("service", "EKS")
    account_id = spec.get("accountId", "111111111111")
    resource_tags = spec.get("resourceTags", {})
    account_tags = spec.get("accountTags", {})
    event_type_code = spec.get("eventTypeCode") or f"AWS_{service}_PLANNED_LIFECYCLE_EVENT"

    run_id = f"{int(time.time())}_{random.randint(1000, 9999)}"
    etcode = event_type_code

    resources = make_resources(
        count=3, service=service, account=account_id, tags=resource_tags if resource_tags else None,
    )

    health_event = make_health_event(
        service=service,
        affected_account=account_id,
        event_type_code=etcode,
        account_tags=account_tags if account_tags else {"Team": "platform", "Environment": "production"},
        resources=resources,
    )

    sqs_client = boto3.client("sqs")
    try:
        sqs_client.send_message(QueueUrl=queue_url, MessageBody=json.dumps(health_event))
    except Exception as exc:
        logger.error(json.dumps({"action": "sqs_send_failed", "error": str(exc)}))
        return _error(500, "SQS_ERROR", str(exc))

    return _success([health_event["detail"]["eventArn"]], None)


# ---------------------------------------------------------------------------
# Scenario dispatch
# ---------------------------------------------------------------------------

def _generate(count, scenario, accounts, services, run_id):
    """Generate events with a unique run_id suffix in eventTypeCode to prevent campaign merging."""
    if scenario == "basic":
        return [make_health_event(affected_account=accounts[i % len(accounts)],
                                  service=services[i % len(services)],
                                  event_type_code=f"AWS_{services[i % len(services)]}_PLANNED_LIFECYCLE_EVENT") for i in range(count)]
    if scenario == "paginated":
        return _scenario_paginated(accounts, run_id)
    if scenario == "large_campaign":
        return _scenario_large_campaign(accounts, run_id)
    if scenario == "zero_resources":
        return [make_health_event(resource_count=0, affected_account=accounts[i % len(accounts)],
                                  service=services[i % len(services)],
                                  event_type_code=f"AWS_{services[i % len(services)]}_PLANNED_LIFECYCLE_EVENT") for i in range(count)]
    if scenario == "bare_instance_ids":
        return [make_health_event(service="EC2", affected_account=accounts[i % len(accounts)],
                                  resources=make_resources(3, service="EC2", bare_ids=True),
                                  event_type_code=f"AWS_EC2_PLANNED_LIFECYCLE_EVENT") for i in range(count)]
    if scenario == "backup_event":
        return [make_health_event(backup_event=True, affected_account=accounts[i % len(accounts)],
                                  service=services[i % len(services)],
                                  event_type_code=f"AWS_{services[i % len(services)]}_PLANNED_LIFECYCLE_EVENT") for i in range(count)]
    if scenario == "informational":
        return [make_health_event(actionability="INFORMATIONAL", affected_account=accounts[i % len(accounts)],
                                  service=services[i % len(services)],
                                  event_type_code=f"AWS_{services[i % len(services)]}_PLANNED_LIFECYCLE_EVENT") for i in range(count)]
    if scenario == "account_notification":
        return [make_health_event(event_type_category="accountNotification", resource_count=0,
                                  affected_account=accounts[i % len(accounts)],
                                  service=services[i % len(services)],
                                  event_type_code=f"AWS_{services[i % len(services)]}_ACCOUNT_NOTIFICATION_{run_id}") for i in range(count)]
    if scenario == "mixed":
        return _scenario_mixed(count, accounts, services, run_id)
    # Default: random distribution with unique eventTypeCode per run
    return [make_health_event(affected_account=accounts[i % len(accounts)],
                              service=services[i % len(services)],
                              event_type_code=f"AWS_{services[i % len(services)]}_PLANNED_LIFECYCLE_EVENT") for i in range(count)]


def _scenario_paginated(accounts, run_id):
    """3 events sharing one eventArn, pages 1/3, 2/3, 3/3."""
    base = make_health_event(affected_account=accounts[0], page="1", total_pages="3",
                             event_type_code=f"AWS_EKS_PLANNED_LIFECYCLE_EVENT")
    shared_arn = base["detail"]["eventArn"]
    page2 = make_health_event(affected_account=accounts[0], page="2", total_pages="3",
                              event_type_code=f"AWS_EKS_PLANNED_LIFECYCLE_EVENT",
                              detail={"eventArn": shared_arn})
    page3 = make_health_event(affected_account=accounts[0], page="3", total_pages="3",
                              event_type_code=f"AWS_EKS_PLANNED_LIFECYCLE_EVENT",
                              detail={"eventArn": shared_arn})
    return [base, page2, page3]


def _scenario_large_campaign(accounts, run_id):
    """Single event with 500 resources."""
    return [make_health_event(resource_count=500, affected_account=accounts[0],
                              event_type_code=f"AWS_EKS_PLANNED_LIFECYCLE_EVENT")]


def _scenario_mixed(count, accounts, services, run_id):
    """Weighted distribution across scenario types."""
    result = []
    for i in range(count):
        acct = accounts[i % len(accounts)]
        svc = services[i % len(services)]
        mod = i % 10
        if mod < 6:  # 60% normal PLE
            result.append(make_health_event(affected_account=acct, service=svc,
                                            event_type_code=f"AWS_{svc}_PLANNED_LIFECYCLE_EVENT"))
        elif mod < 8:  # 20% account notification
            result.append(make_health_event(event_type_category="accountNotification",
                                            resource_count=0, affected_account=acct, service=svc,
                                            event_type_code=f"AWS_{svc}_ACCOUNT_NOTIFICATION_{run_id}"))
        elif mod == 8:  # 10% zero resources
            result.append(make_health_event(resource_count=0, affected_account=acct, service=svc,
                                            event_type_code=f"AWS_{svc}_PLANNED_LIFECYCLE_EVENT"))
        else:  # 10% informational
            result.append(make_health_event(actionability="INFORMATIONAL", affected_account=acct, service=svc,
                                            event_type_code=f"AWS_{svc}_PLANNED_LIFECYCLE_EVENT"))
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error(status_code, code, message):
    return {"statusCode": status_code, "errorCode": code, "errorMessage": message}


def _success(event_arns, scenario):
    return {"statusCode": 200, "published": len(event_arns), "eventArns": event_arns,
            "scenario": scenario}
