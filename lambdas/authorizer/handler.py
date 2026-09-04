"""Resolve Lambda Authorizer — dual-auth (Cognito JWT + API key).

REQUEST-type Lambda authorizer for API Gateway.
Accepts EITHER:
  - Authorization: Bearer <jwt> — validates Cognito JWT
  - x-api-key: <key> — validates against configured API key value

Returns IAM policy document with context: userId, groups, authMethod.
Fail-closed: any validation failure = Deny.

Security controls applied.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from base64 import urlsafe_b64decode


logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
APP_CLIENT_ID = os.environ.get("APP_CLIENT_ID", "")
REGION = os.environ.get("REGION", os.environ.get("AWS_REGION", "us-east-1"))
API_KEY_VALUE = os.environ.get("API_KEY_VALUE", "")

ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"

# Module-level JWKS cache — persists across warm Lambda invocations
_jwks_cache: dict | None = None


def lambda_handler(event, context):
    """Authorizer entry point."""
    headers = event.get("headers") or {}
    # API Gateway normalizes header names to lowercase
    auth_header = headers.get("authorization", headers.get("Authorization", ""))
    api_key = headers.get("x-api-key", headers.get("X-Api-Key", ""))
    method_arn = event.get("methodArn", "")

    has_bearer = auth_header.lower().startswith("bearer ") if auth_header else False
    has_api_key = bool(api_key)

    # Reject ambiguous authentication
    if has_bearer and has_api_key:
        logger.warning("Ambiguous auth: both Bearer and x-api-key present")
        return _deny(method_arn)

    try:
        if has_bearer:
            token = auth_header[7:]  # Strip "Bearer "
            claims = _validate_jwt(token)
            user_id = claims.get("email", claims.get("sub", "unknown"))
            groups = claims.get("cognito:groups", [])
            return _allow(method_arn, user_id, groups, "cognito")

        if has_api_key:
            if _validate_api_key(api_key):
                return _allow(method_arn, "api-key-user", ["Admins"], "api_key")
            logger.warning("Invalid API key presented")
            return _deny(method_arn)

    except Exception as e:
        # Fail closed on any error
        logger.error("Auth validation failed: %s", str(e))
        return _deny(method_arn)

    # No credentials provided
    logger.info("No credentials provided")
    return _deny(method_arn)


# ===================================================================
# JWT Validation
# ===================================================================

def _validate_jwt(token: str) -> dict:
    """Validate Cognito JWT. Returns claims dict. Raises on failure."""
    # Decode header without verification to get kid
    header = _decode_jwt_part(token, 0)
    kid = header.get("kid")
    if not kid:
        raise ValueError("JWT missing kid in header")

    # Get signing key
    key = _get_jwk(kid)
    if not key:
        raise ValueError(f"No JWK found for kid: {kid}")

    # Decode and verify
    payload = _decode_jwt_part(token, 1)

    # Verify claims
    if payload.get("iss") != ISSUER:
        raise ValueError(f"Invalid issuer: {payload.get('iss')}")

    # For ID tokens, audience is in 'aud'; for access tokens it's in 'client_id'
    token_aud = payload.get("aud", payload.get("client_id", ""))
    if token_aud != APP_CLIENT_ID:
        raise ValueError(f"Invalid audience: {token_aud}")

    now = int(time.time())
    if payload.get("exp", 0) < now:
        raise ValueError("Token expired")

    # Verify RSA signature
    _verify_signature(token, key)

    return payload


def _decode_jwt_part(token: str, part_index: int) -> dict:
    """Decode a JWT part (header=0, payload=1) without signature verification."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Malformed JWT: expected 3 parts")
    segment = parts[part_index]
    # Add padding
    padding = 4 - len(segment) % 4
    if padding != 4:
        segment += "=" * padding
    decoded = urlsafe_b64decode(segment)
    return json.loads(decoded)


def _verify_signature(token: str, jwk: dict) -> None:
    """Verify RSA signature of JWT using JWK public key.

    Pure stdlib implementation — no third-party dependencies required.
    Uses PKCS#1 v1.5 (RS256) verification via modular exponentiation.
    """
    import hashlib

    def _b64url_to_int(val: str) -> int:
        padded = val + "=" * (4 - len(val) % 4) if len(val) % 4 else val
        decoded = urlsafe_b64decode(padded)
        return int.from_bytes(decoded, "big")

    # Extract RSA public key components from JWK
    n = _b64url_to_int(jwk["n"])
    e = _b64url_to_int(jwk["e"])

    # Split token and decode signature
    parts = token.split(".")
    message = f"{parts[0]}.{parts[1]}".encode("utf-8")
    sig_segment = parts[2]
    padding_needed = 4 - len(sig_segment) % 4
    if padding_needed != 4:
        sig_segment += "=" * padding_needed
    signature = urlsafe_b64decode(sig_segment)

    # RSA verify: decrypt signature with public key (modular exponentiation)
    sig_int = int.from_bytes(signature, "big")
    decrypted_int = pow(sig_int, e, n)

    # Convert back to bytes (key length)
    key_size = (n.bit_length() + 7) // 8
    decrypted_bytes = decrypted_int.to_bytes(key_size, "big")

    # PKCS#1 v1.5 signature format: 0x00 0x01 [padding 0xFF...] 0x00 [DigestInfo]
    # DigestInfo for SHA-256: fixed prefix + 32-byte hash
    sha256_digest_info_prefix = bytes.fromhex(
        "3031300d060960864801650304020105000420"
    )

    # Compute expected hash
    expected_hash = hashlib.sha256(message).digest()
    expected_suffix = sha256_digest_info_prefix + expected_hash

    # Verify PKCS#1 v1.5 padding structure
    if decrypted_bytes[0:2] != b"\x00\x01":
        raise ValueError("Invalid signature: bad PKCS#1 prefix")

    # Find the 0x00 separator after FF padding
    separator_idx = decrypted_bytes.index(b"\x00", 2)
    # All bytes between prefix and separator must be 0xFF
    padding_bytes = decrypted_bytes[2:separator_idx]
    if not all(b == 0xFF for b in padding_bytes) or len(padding_bytes) < 8:
        raise ValueError("Invalid signature: bad PKCS#1 padding")

    # Compare DigestInfo + hash
    actual_suffix = decrypted_bytes[separator_idx + 1:]
    if actual_suffix != expected_suffix:
        raise ValueError("Invalid signature: hash mismatch")


# ===================================================================
# JWKS Key Management
# ===================================================================

def _get_jwk(kid: str) -> dict | None:
    """Get JWK by kid. Uses module-level cache, refreshes on miss."""
    global _jwks_cache

    # Try cache first
    if _jwks_cache:
        key = _find_key(_jwks_cache, kid)
        if key:
            return key

    # Cache miss or kid not found — refresh
    _jwks_cache = _fetch_jwks()
    return _find_key(_jwks_cache, kid)


def _find_key(jwks: dict, kid: str) -> dict | None:
    """Find a key by kid in JWKS."""
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return key
    return None


def _fetch_jwks() -> dict:
    """Fetch JWKS from Cognito endpoint."""
    if not JWKS_URL.startswith("https://"):
        raise ValueError(f"JWKS_URL must use HTTPS scheme, got: {JWKS_URL}")
    logger.info("Fetching JWKS from %s", JWKS_URL)
    req = urllib.request.Request(JWKS_URL)
    with urllib.request.urlopen(req, timeout=5) as resp:  # nosec B310 — URL scheme validated above
        return json.loads(resp.read().decode("utf-8"))


# ===================================================================
# API Key Validation
# ===================================================================

def _validate_api_key(api_key: str) -> bool:
    """Validate API key against configured value."""
    if not API_KEY_VALUE:
        logger.error("API_KEY_VALUE not configured")
        return False
    # Constant-time comparison to prevent timing attacks
    import hmac
    return hmac.compare_digest(api_key, API_KEY_VALUE)


# ===================================================================
# Policy Document Builders
# ===================================================================

def _allow(method_arn: str, user_id: str, groups: list, auth_method: str) -> dict:
    """Build Allow policy with context."""
    resource_arn = _build_resource_arn(method_arn)
    return {
        "principalId": user_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [{
                "Action": "execute-api:Invoke",
                "Effect": "Allow",
                "Resource": resource_arn,
            }],
        },
        "context": {
            "userId": user_id,
            "groups": ",".join(groups) if isinstance(groups, list) else str(groups),
            "authMethod": auth_method,
        },
    }


def _deny(method_arn: str) -> dict:
    """Build Deny policy."""
    resource_arn = _build_resource_arn(method_arn)
    return {
        "principalId": "anonymous",
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [{
                "Action": "execute-api:Invoke",
                "Effect": "Deny",
                "Resource": resource_arn,
            }],
        },
    }


def _build_resource_arn(method_arn: str) -> str:
    """Build wildcard resource ARN from method ARN for caching."""
    if not method_arn:
        return "arn:aws:execute-api:*:*:*"
    # method_arn format: arn:aws:execute-api:region:acct:api-id/stage/METHOD/resource
    parts = method_arn.split(":")
    if len(parts) >= 6:
        api_gw_parts = parts[5].split("/")
        if len(api_gw_parts) >= 2:
            return f"{':'.join(parts[:5])}:{api_gw_parts[0]}/{api_gw_parts[1]}/*"
    return method_arn
