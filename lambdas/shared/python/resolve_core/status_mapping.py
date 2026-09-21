"""Platform-agnostic status normalization for ITSM bidirectional sync.

Maps platform-specific status values to normalized states:
- Created: work not started
- In Progress: work underway
- Closed: work completed (or cancelled)

Usage:
    from resolve_core.status_mapping import normalize_status
    normalized = normalize_status("jira", "indeterminate")  # "In Progress"
    normalized = normalize_status("servicenow_incident", "6")  # "Closed"
"""

# Normalized states
STATUS_CREATED = "Created"
STATUS_IN_PROGRESS = "In Progress"
STATUS_CLOSED = "Closed"

# JIRA: uses statusCategory.key
JIRA_STATUS_MAP = {
    "new": STATUS_CREATED,
    "undefined": STATUS_CREATED,
    "indeterminate": STATUS_IN_PROGRESS,
    "done": STATUS_CLOSED,
}

# ServiceNow: incident states (numeric as string)
SNOW_INCIDENT_STATE_MAP = {
    "1": STATUS_CREATED,      # New
    "2": STATUS_IN_PROGRESS,  # In Progress
    "3": STATUS_IN_PROGRESS,  # On Hold
    "6": STATUS_CLOSED,       # Resolved
    "7": STATUS_CLOSED,       # Closed
    "8": STATUS_CLOSED,       # Canceled
}

# ServiceNow: change_request states
SNOW_CHANGE_STATE_MAP = {
    "-5": STATUS_CREATED,      # New
    "-4": STATUS_CREATED,      # Assess
    "-3": STATUS_CREATED,      # Authorize
    "-2": STATUS_IN_PROGRESS,  # Scheduled
    "-1": STATUS_IN_PROGRESS,  # Implement
    "0": STATUS_IN_PROGRESS,   # Review
    "3": STATUS_CLOSED,        # Closed
    "4": STATUS_CLOSED,        # Canceled
}

# GitHub: issue state
GITHUB_STATE_MAP = {
    "open": STATUS_CREATED,
    "closed": STATUS_CLOSED,
}

# Azure DevOps: work item states
AZDO_STATE_MAP = {
    "New": STATUS_CREATED,
    "Active": STATUS_IN_PROGRESS,
    "Resolved": STATUS_CLOSED,
    "Closed": STATUS_CLOSED,
}

# Platform → map registry
_PLATFORM_MAPS = {
    "jira": JIRA_STATUS_MAP,
    "servicenow_incident": SNOW_INCIDENT_STATE_MAP,
    "servicenow_change": SNOW_CHANGE_STATE_MAP,
    "github": GITHUB_STATE_MAP,
    "azdo": AZDO_STATE_MAP,
}


def normalize_status(platform: str, raw_status: str) -> str:
    """Map platform-specific status to normalized state.

    Args:
        platform: Platform identifier. One of: "jira",
            "servicenow_incident", "servicenow_change", "github", "azdo".
        raw_status: Platform-native status value (e.g. statusCategory.key
            for JIRA, numeric state string for ServiceNow).

    Returns:
        Normalized status: "Created", "In Progress", or "Closed".
        Returns "Created" as safe default for unknown status values.

    Raises:
        ValueError: If platform is not recognized.
    """
    status_map = _PLATFORM_MAPS.get(platform)
    if status_map is None:
        raise ValueError(f"Unknown ITSM platform: {platform!r}")
    return status_map.get(raw_status, STATUS_CREATED)
