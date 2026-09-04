export interface Campaign {
  campaignId: string; eventArn: string; title: string; service: string;
  deprecatedVersion?: string; eventTypeCode: string; description: string;
  deadline: string; eventScopeCode?: string; actionability?: string;
  hasResources: boolean; createdAt?: string; status: string;
  totalResources: number; ticketedResources: number;
  resolvedResources: number; ticketsClosedResources?: number;
  affectedAccount?: string; resources?: Resource[];
  groupBreakdown?: Record<string, GroupData>;
}

export interface Resource {
  resourceArn: string; accountId: string; region: string;
  healthStatus: string; ticketStatus: string;
  ticketId?: string; ticketPlatform?: string; ticketUrl?: string;
  jiraStatusName?: string; jiraTicketKey?: string;
  tags: Record<string, string>; lastUpdated?: string;
  tickets?: Record<string, {
    ticketId: string;
    ticketStatus: string;
    ticketUrl: string;
    ticketUpdatedAt?: string;
  }>;
}

export interface GroupData { total: number; resolved: number; pending: number; }

export interface OnboardingConfig {
  /**
   * Legacy scalar platform hint. DO NOT use for platform decisions — a
   * SNOW-only deployment's scalar can still read 'jira'. Retained only as a
   * legacy wire field; consumers must derive platform from `platforms`.
   */
  platform?: 'jira' | 'servicenow';
  /**
   * Authoritative enabled-platform list (source of truth).
   * Always present and non-empty on a live 200. Consume via
   * `resolvePlatformContext(config)` / membership tests — never the scalar.
   */
  platforms?: string[];
  jira?: {
    baseUrl: string;
    email?: string;
    validated: boolean;
    validatedAt?: string;
    validatedUser?: string;
    credentialsConfigured?: boolean;
    hasApiToken?: boolean;
  };
  servicenow?: {
    instanceUrl: string;
    validated: boolean;
    validatedAt?: string;
    authType?: string;
    clientId?: string;
    username?: string;
    credentialsConfigured?: boolean;
    hasClientSecret?: boolean;
    hasPassword?: boolean;
  };
  routing?: {
    defaultProject: string;
    accountMappingCount: number;
    tagRouting?: {
      enabled: boolean;
      tagKey: string;
      tagSource?: string; // resource | account | both (additive)
    };
  };
  dispatch?: {
    mode: string;
    actionabilityFilter?: string;
  };
  setupComplete?: boolean;
}

// Headline orphan signal is TICKET-grained (sync-backed count from
// GET /api/config/routing/orphan-status), gated by the backend threshold — the
// UI must NOT recompute >10. Distinct field names from OrphanResourceBreakdown
// are the type-level guardrail that keeps the resource/ticket units from drifting.
export interface OrphanStatus {
  orphanCount: number;        // TICKETS routed to the default project (orphan-unmapped-account)
  thresholdExceeded: boolean; // backend-computed: orphanCount > ORPHAN_ALERT_THRESHOLD (>10, A-JIRA-10)
  threshold: number;          // 10 (for copy interpolation only; never a client-side gate)
}

// GET /api/routing/orphans is a per-account RESOURCE breakdown for the
// "add mappings" workflow — NOT the headline count. Fields state the resource unit.
export interface OrphanResourceBreakdown {
  defaultRoutedResourceCount: number;             // RESOURCES with routedVia == "default"
  accounts: Array<{
    accountId: string;
    resourceCount: number;                        // RESOURCES (renamed from misleading ticketCount)
    firstSeen: string;                            // ISO-8601
  }>;
  suggestions: Array<{
    accountId: string;
    suggestedTarget: string;
    reason: string;
  }>;
}

export function daysLeft(deadline: string): number {
  if (!deadline) return Infinity;
  const d = Math.ceil((new Date(deadline).getTime() - Date.now()) / 86400000);
  return isNaN(d) ? Infinity : d;
}

export function formatDays(deadline: string): string {
  const d = daysLeft(deadline);
  if (d === Infinity || isNaN(d)) return 'No date';
  if (d < 0) return `${Math.abs(d)}d overdue`;
  return `${d}d`;
}

export function formatTitle(c: Campaign): string {
  if (c.deprecatedVersion) return `${c.service} ${c.deprecatedVersion}`;
  const cleaned = c.title?.replace(/^AWS_/, '').replace(/_PLANNED_LIFECYCLE_EVENT$/, '').replace(/_/g, ' ');
  return cleaned && cleaned !== c.service ? cleaned : c.eventTypeCode?.replace(/^AWS_/, '').replace(/_/g, ' ') || c.service;
}
