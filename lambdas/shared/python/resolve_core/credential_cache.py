"""TTL-based credential cache with automatic refresh.

Provides a module-level cache that persists across warm Lambda invocations,
reducing Secrets Manager API calls. Designed for OAuth platforms (ServiceNow,
Azure DevOps) where tokens expire and need refresh. Static-token platforms
(JIRA) can use this with infinite TTL or bypass it entirely.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Callable, Dict, Optional

import boto3

logger = logging.getLogger("resolve_core")

# Module-level cache persists across warm Lambda invocations.
_cache: Dict[str, "CachedCredentialProvider"] = {}

_AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Buffer before actual TTL expiry to trigger proactive refresh.
_EXPIRY_BUFFER_SECONDS = 60


class CredentialRefreshError(Exception):
    """Raised when a refresh callback fails with an auth error."""

    pass


class CachedCredentialProvider:
    """TTL-based credential cache with automatic refresh.

    Designed for OAuth platforms (ServiceNow, Azure DevOps) where tokens
    expire and need refresh. Static-token platforms (JIRA) can use this
    with infinite TTL or bypass it entirely.
    """

    def __init__(
        self,
        secret_arn: str,
        refresh_callback: Optional[Callable[[dict], dict]] = None,
        ttl_seconds: int = 1800,
        secrets_client=None,
    ):
        """Initialize the credential provider.

        Args:
            secret_arn: ARN of the Secrets Manager secret.
            refresh_callback: Optional callable that accepts current credentials
                and returns refreshed credentials. If None, credentials are
                re-read from Secrets Manager on expiry.
            ttl_seconds: Cache TTL in seconds (default 30 minutes).
            secrets_client: Optional boto3 secretsmanager client (for testing).
        """
        self._secret_arn = secret_arn
        self._refresh_callback = refresh_callback
        self._ttl_seconds = ttl_seconds
        self._secrets_client = secrets_client
        self._credentials: Optional[dict] = None
        self._cached_at: float = 0.0

    @property
    def _client(self):
        """Lazy-init Secrets Manager client."""
        if self._secrets_client is None:
            self._secrets_client = boto3.client(
                "secretsmanager", region_name=_AWS_REGION,
            )
        return self._secrets_client

    def _is_expired(self) -> bool:
        """Check if cached credentials are past TTL (with buffer).

        Also enforces a maximum absolute age of 2×TTL to bound credential
        memory lifetime on idle containers (security condition C1).
        """
        if self._credentials is None:
            return True
        elapsed = time.time() - self._cached_at
        # Hard eviction at 2×TTL regardless of access pattern.
        if elapsed >= (self._ttl_seconds * 2):
            self._credentials = None
            return True
        return elapsed >= (self._ttl_seconds - _EXPIRY_BUFFER_SECONDS)

    def _read_from_secrets_manager(self) -> dict:
        """Read credentials directly from Secrets Manager."""
        logger.debug("Reading credentials from Secrets Manager: %s", self._secret_arn)
        resp = self._client.get_secret_value(SecretId=self._secret_arn)
        credentials = json.loads(resp["SecretString"])
        self._credentials = credentials
        self._cached_at = time.time()
        return credentials

    def _write_to_secrets_manager(self, credentials: dict) -> None:
        """Update Secrets Manager with refreshed credentials."""
        logger.debug("Updating Secrets Manager with refreshed credentials")
        self._client.put_secret_value(
            SecretId=self._secret_arn,
            SecretString=json.dumps(credentials),
        )

    def get_credentials(self) -> dict:
        """Return cached credentials if valid, refresh if expired.

        Flow:
        1. If cached and within TTL → return cached.
        2. If never loaded (first call) → read from Secrets Manager.
        3. If expired and refresh_callback exists → call it.
           a. On success → update cache + Secrets Manager → return new creds.
           b. On auth error → fall back to full Secrets Manager re-read.
        4. If no refresh_callback → re-read from Secrets Manager.
        """
        if not self._is_expired():
            return self._credentials

        # First call — always read from Secrets Manager
        if self._credentials is None:
            return self._read_from_secrets_manager()

        # No refresh callback — just re-read from source
        if self._refresh_callback is None:
            return self._read_from_secrets_manager()

        # Try refresh callback with current credentials
        try:
            new_credentials = self._refresh_callback(self._credentials)
            self._credentials = new_credentials
            self._cached_at = time.time()
            try:
                self._write_to_secrets_manager(new_credentials)
            except Exception:
                # Write-back failure is non-critical. Log sanitized message
                # without credential values to prevent secret leakage (C2).
                logger.warning(
                    "Failed to write refreshed credentials to Secrets Manager: %s",
                    self._secret_arn,
                )
            return new_credentials
        except CredentialRefreshError:
            # Auth error during refresh — credentials may have been rotated
            # externally. Fall back to full re-read from Secrets Manager.
            logger.warning(
                "Refresh callback failed with auth error, "
                "falling back to Secrets Manager re-read",
            )
            return self._read_from_secrets_manager()

    def update(self, credentials: dict) -> None:
        """Update cache and persist refreshed credentials to Secrets Manager.

        Used when the caller has already performed a token refresh (e.g.,
        password grant) and needs to store the result without going through
        the normal TTL-based refresh flow.
        """
        self._credentials = credentials
        self._cached_at = time.time()
        try:
            self._write_to_secrets_manager(credentials)
        except Exception:
            logger.warning(
                "Failed to write updated credentials to Secrets Manager: %s",
                self._secret_arn,
            )

    def invalidate(self) -> None:
        """Force next call to refresh credentials."""
        self._credentials = None
        self._cached_at = 0.0


def get_credential_provider(secret_arn: str, **kwargs) -> CachedCredentialProvider:
    """Get or create a cached provider for this secret ARN.

    Uses the module-level _cache dict so providers persist across
    warm Lambda invocations.

    Args:
        secret_arn: ARN of the Secrets Manager secret.
        **kwargs: Passed to CachedCredentialProvider constructor
            (refresh_callback, ttl_seconds, secrets_client).

    Returns:
        Existing or newly created CachedCredentialProvider.
    """
    if secret_arn not in _cache:
        _cache[secret_arn] = CachedCredentialProvider(secret_arn, **kwargs)
    return _cache[secret_arn]
