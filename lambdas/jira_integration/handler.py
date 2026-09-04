"""JIRA Integration Lambda — entrypoint.

Thin entrypoint delegating to jira_handler.handle().
Proxies module-level attributes to jira_handler for test compatibility.

Trigger: SQS JIRA Queue (batch_size=1, ReportBatchItemFailures=true).
Runtime: Python 3.12, 256 MB, 5 min timeout, reserved concurrency 2.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import jira_handler
from resolve_core.jira_client import JiraClient as _OriginalJiraClient  # noqa: F401


# Attributes that tests read/write/patch via handler module
_PROXIED_ATTRS = frozenset({
    "_jira_client", "_config", "_dynamodb", "_s3_client",
    "_secrets_client", "_campaigns_table", "_resources_table",
    "_config_table", "JiraClient",
})

# Store original values for delattr (mock restore) support
_ORIGINALS = {
    "JiraClient": _OriginalJiraClient,
}


class _HandlerProxy(ModuleType):
    """Module proxy that delegates attribute access to jira_handler.

    Tests set handler._jira_client, handler._campaigns_table, etc.
    Tests patch handler.JiraClient.
    This proxy ensures reads/writes/deletes go to jira_handler's namespace.
    """

    def __getattr__(self, name: str) -> Any:
        if name in _PROXIED_ATTRS:
            return getattr(jira_handler, name)
        raise AttributeError(f"module 'handler' has no attribute {name!r}")

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _PROXIED_ATTRS:
            setattr(jira_handler, name, value)
        else:
            super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if name in _PROXIED_ATTRS:
            # Restore original value (for mock unpatch)
            if name in _ORIGINALS:
                setattr(jira_handler, name, _ORIGINALS[name])
            else:
                setattr(jira_handler, name, None)
        else:
            super().__delattr__(name)


def lambda_handler(event: dict, context: Any) -> dict:
    """SQS ESM handler. Delegates to jira_handler.handle().

    Returns {"batchItemFailures": []} on success, or
    {"batchItemFailures": [{"itemIdentifier": messageId}]} on failure.
    """
    return jira_handler.handle(event, context)


# Install the proxy module so attribute access is forwarded
_proxy = _HandlerProxy(__name__)
_proxy.__dict__.update({
    "__file__": __file__,
    "__loader__": __loader__,
    "__package__": __package__,
    "__spec__": __spec__,
    "__doc__": __doc__,
    "lambda_handler": lambda_handler,
    "jira_handler": jira_handler,
})
sys.modules[__name__] = _proxy
