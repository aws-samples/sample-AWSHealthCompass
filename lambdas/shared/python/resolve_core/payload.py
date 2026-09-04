"""Amazon S3 offload for large Amazon SNS payloads (Claim-Check pattern).

Provides two functions:
- ``publish_or_offload`` — producer side: publishes inline or via S3.
- ``resolve_payload`` — consumer side: fetches from S3 if needed.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from botocore.exceptions import ClientError

logger = logging.getLogger("resolve")

# --- Constants ---

_SNS_SOFT_THRESHOLD_BYTES = 200 * 1024  # 200 KB — offload above this
_SNS_HARD_LIMIT_BYTES = 256 * 1024      # 256 KB — SNS hard limit
_CAMPAIGN_ID_SAFE = re.compile(r"[^a-zA-Z0-9:_\-.]")
_S3_KEY_SAFE = re.compile(r"^payloads/[a-zA-Z0-9\-_.:]+/[a-zA-Z0-9T]+Z\.json$")


# ===================================================================
# Producer — publish_or_offload
# ===================================================================


def publish_or_offload(
    sns_client: Any,
    s3_client: Any,
    topic_arn: str,
    bucket: str,
    event_dict: dict,
    message_attributes: Optional[Dict[str, dict]] = None,
    threshold_kb: int = 0,
) -> dict:
    """Publish a standardized event to SNS, offloading to S3 if large.

    Args:
        sns_client: boto3 SNS client.
        s3_client: boto3 S3 client.
        topic_arn: SNS topic ARN.
        bucket: S3 bucket for offloaded payloads.
        event_dict: The standardized event payload dict.
        message_attributes: SNS MessageAttributes dict (optional).
        threshold_kb: Size threshold in KB. 0 = use module default (200KB).

    Returns:
        dict with keys: method ("inline"|"s3"|"truncated"), size (int),
        and optionally bucket, key for S3 offloads.
    """
    threshold_bytes = (threshold_kb * 1024) if threshold_kb > 0 else _SNS_SOFT_THRESHOLD_BYTES
    payload_json = json.dumps(event_dict, separators=(",", ":"), default=str)
    payload_bytes = len(payload_json.encode("utf-8"))
    attrs = message_attributes or {}

    # --- Inline path ---
    if payload_bytes <= threshold_bytes:
        sns_client.publish(
            TopicArn=topic_arn,
            Message=payload_json,
            MessageAttributes=attrs,
        )
        logger.info(
            "SNS published inline — payload_bytes=%d", payload_bytes,
        )
        return {"method": "inline", "size": payload_bytes}

    # --- S3 offload path ---
    campaign_id = event_dict.get("event", {}).get("campaignId", "unknown")
    safe_id = _CAMPAIGN_ID_SAFE.sub("", campaign_id)[:256]
    now_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    s3_key = f"payloads/{safe_id}/{now_ts}.json"

    try:
        s3_client.put_object(
            Bucket=bucket,
            Key=s3_key,
            Body=payload_json.encode("utf-8"),
            ContentType="application/json",
        )
    except ClientError:
        logger.warning(
            "S3 offload failed — campaign_id=%s payload_bytes=%d",
            campaign_id[:256], payload_bytes,
        )
        # Fallback: inline if under hard limit
        if payload_bytes < _SNS_HARD_LIMIT_BYTES:
            sns_client.publish(
                TopicArn=topic_arn,
                Message=payload_json,
                MessageAttributes=attrs,
            )
            return {"method": "truncated", "size": payload_bytes}
        # Cannot publish — raise so SQS retries
        raise

    # F-IMPL-2: Include campaignId and action for DLQ triage
    action = event_dict.get("event", {}).get("action", "CREATE")
    reference = json.dumps({
        "_s3Ref": True,
        "s3_bucket": bucket,
        "s3_key": s3_key,
        "campaignId": campaign_id,
        "action": action,
        "payload_bytes": payload_bytes,
    })

    attrs["payloadLocation"] = {
        "DataType": "String",
        "StringValue": "s3",
    }
    sns_client.publish(
        TopicArn=topic_arn,
        Message=reference,
        MessageAttributes=attrs,
    )
    logger.info(
        "SNS published via S3 offload — s3_key=%s payload_bytes=%d",
        s3_key, payload_bytes,
    )
    return {"method": "s3", "size": payload_bytes, "bucket": bucket, "key": s3_key}


# ===================================================================
# Consumer — resolve_payload
# ===================================================================


def resolve_payload(
    s3_client: Any,
    message_body: dict,
    expected_bucket: str = "",
) -> dict:
    """Resolve a message body, fetching from S3 if it's an offload reference.

    Args:
        s3_client: boto3 S3 client.
        message_body: Parsed JSON message (may be inline or S3 reference).
        expected_bucket: Expected S3 bucket name for validation.

    Returns:
        The full event payload dict.

    Raises:
        ValueError: On validation failure (non-retryable).
        ClientError: On transient S3 errors (retryable) or permanent
            S3 errors (NoSuchKey, AccessDenied — non-retryable).
    """
    # Detect S3 reference via _s3Ref discriminator or s3_bucket+s3_key presence
    is_ref = (
        message_body.get("_s3Ref") is True
        or (message_body.get("s3_bucket") and message_body.get("s3_key"))
    )
    if not is_ref:
        return message_body

    bucket = message_body.get("s3_bucket", "")
    key = message_body.get("s3_key", "")

    # Bucket must match expected
    if expected_bucket and bucket != expected_bucket:
        raise ValueError(
            f"S3 bucket mismatch: got {bucket!r}, expected {expected_bucket!r}"
        )

    # Key character + path traversal validation
    if not key or ".." in key:
        raise ValueError(f"Invalid S3 key: {key!r}")

    # F-IMPL-1: Explicit payloads/ prefix check
    if not key.startswith("payloads/"):
        raise ValueError(f"S3 key missing payloads/ prefix: {key!r}")

    # Character safety
    if not _S3_KEY_SAFE.match(key):
        raise ValueError(f"S3 key contains unsafe characters: {key!r}")

    resp = s3_client.get_object(Bucket=bucket, Key=key)
    body = resp["Body"].read()
    return json.loads(body)
