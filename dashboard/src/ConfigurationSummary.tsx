import React, { useState, useEffect, useMemo, useCallback } from 'react';
import ContentLayout from '@cloudscape-design/components/content-layout';
import Header from '@cloudscape-design/components/header';
import Container from '@cloudscape-design/components/container';
import SpaceBetween from '@cloudscape-design/components/space-between';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Table from '@cloudscape-design/components/table';
import TextFilter from '@cloudscape-design/components/text-filter';
import Pagination from '@cloudscape-design/components/pagination';
import CollectionPreferences from '@cloudscape-design/components/collection-preferences';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import Alert from '@cloudscape-design/components/alert';
import Spinner from '@cloudscape-design/components/spinner';
import type { TableProps } from '@cloudscape-design/components/table';
import type { OnboardingConfig } from './types';
import { apiFetch } from './api';
import { resolvePlatformContext } from './platformResolver';
import ConnectionEditModal from './modals/ConnectionEditModal';
import DispatchEditModal from './modals/DispatchEditModal';
import RoutingEditModal from './modals/RoutingEditModal';

// --- Types for section data ---

interface AccountMapping {
  account_id: string;
  account_name?: string;
  jira_project: string;
  jira_issue_type?: string;
  snow_assignment_group_id?: string;
}

interface RoutingData {
  default?: {
    jiraProject?: string;
    jiraIssueType?: string;
    snowAssignmentGroupId?: string;
    snowAssignmentGroupName?: string;
    snowRecordType?: string;
    updatedAt?: string;
  };
  accounts: AccountMapping[];
  totalAccounts: number;
  // Legacy flat fields (fallback)
  defaultProject?: string;
  defaultIssueType?: string;
  snowAssignmentGroupId?: string;
  snowRecordType?: string;
  mappings?: AccountMapping[];
}

interface DispatchRule {
  ruleId: string;
  eventTypePattern: string;
  eventCategories: string[];
  enabled: boolean;
}

interface DispatchData {
  mode: 'all' | 'ple_only' | 'custom';
  actionabilityFilter: string;
  rules: DispatchRule[];
  warning?: string | null;
}

interface ServiceNowData {
  instanceUrl: string;
  validated: boolean;
  validatedAt?: string;
  validatedUser?: string;
  recordType?: string;
}

// --- Constants ---

const PAGE_SIZE = 20;

// --- Routing Table Constants ---

/** Default columns shown on initial render. account_id is always visible. */
const DEFAULT_VISIBLE_COLUMNS = ['account_id', 'account_name', 'jira_project'];

/** Page size options available in CollectionPreferences. */
const ROUTING_PAGE_SIZE_OPTIONS = [
  { value: 20, label: '20 mappings' },
  { value: 50, label: '50 mappings' },
  { value: 100, label: '100 mappings' },
];

// --- Props ---

interface Props {
  config: OnboardingConfig;
  onRunWizard: () => void;
  onConfigChanged: () => void;
}

// --- Helper Functions ---

/**
 * Format an ISO timestamp as a relative time string.
 * SEC-095-05: No production console logging.
 */
function formatRelativeTime(isoString: string): string {
  const diff = Date.now() - new Date(isoString).getTime();
  if (diff < 0) return 'Just now';
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return 'Just now';
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(isoString).toLocaleDateString();
}

/**
 * Format the actionability filter value for display.
 */
function formatActionability(filter: string | undefined): string {
  switch (filter) {
    case 'action_required_only':
      return 'ACTION_REQUIRED only';
    case 'all_actionable':
    default:
      return 'ACTION_REQUIRED + ACTION_MAY_BE_REQUIRED';
  }
}

/**
 * Format the dispatch mode for display.
 */
function formatDispatchMode(mode: string | undefined): string {
  switch (mode) {
    case 'ple_only':
      return 'Planned Lifecycle Events only';
    case 'custom':
      return 'Custom rules';
    case 'all':
    default:
      return 'All actionable events';
  }
}

/**
 * Sanitize an error message for display (SEC-095-06).
 * Shows only a brief message, no raw response bodies.
 */
function sanitizeError(err: unknown): string {
  if (err instanceof Error) {
    const msg = err.message;
    // Strip raw response body content after status code
    const match = msg.match(/^API (\d+):/);
    if (match) return `Request failed (HTTP ${match[1]})`;
    return msg.length > 100 ? msg.substring(0, 100) + '…' : msg;
  }
  return 'An unexpected error occurred';
}

/**
 * Case-insensitive substring filter for a single account mapping row.
 * Exported for unit testing without rendering the component.
 *
 * Matches against account_id, account_name, and jira_project fields.
 */
export function matchesRoutingFilter(mapping: AccountMapping, filterText: string): boolean {
  const lower = filterText.toLowerCase();
  return (
    mapping.account_id.toLowerCase().includes(lower) ||
    (mapping.account_name?.toLowerCase().includes(lower) ?? false) ||
    (mapping.jira_project?.toLowerCase().includes(lower) ?? false)
  );
}

/**
 * Build the countText string shown in the TextFilter when a filter is active.
 * Returns undefined when no filter is active (hides the count display).
 */
function getFilterCountText(
  matchCount: number,
  totalCount: number,
  filterActive: boolean
): string | undefined {
  if (!filterActive) return undefined;
  if (matchCount === totalCount) return `${totalCount} match${totalCount !== 1 ? 'es' : ''}`;
  return `${matchCount} of ${totalCount} match${totalCount !== 1 ? 'es' : ''}`;
}

// --- Normalize API response ---

function normalizeResponse<T>(response: any): T {
  return response?.data ?? response;
}

// --- Component ---

export default function ConfigurationSummary({ config, onRunWizard, onConfigChanged }: Props) {
  // STORY-139 (§5.1): drive column gating and label context off the resolved
  // platforms array (not the JIRA-only scalar). snowEnabled/jiraEnabled feed
  // the routing-table column gates; labelPlatform feeds the "Active Platform"
  // display. JIRA-only reduces byte-identical to pre-epic (AC-139.9).
  const { snowEnabled, jiraEnabled, labelPlatform } = resolvePlatformContext(config);

  // Platform-aware default visible columns: a SNOW-only summary defaults to the
  // ServiceNow group column instead of the JIRA project column so a fresh
  // SNOW-only customer sees where to begin mapping (AC-139.6, Luna F-3).
  const defaultVisibleColumns = useMemo(
    () =>
      snowEnabled && !jiraEnabled
        ? ['account_id', 'account_name', 'snow_group']
        : DEFAULT_VISIBLE_COLUMNS,
    [snowEnabled, jiraEnabled]
  );

  // Connection Edit Modal state
  const [editConnectionsVisible, setEditConnectionsVisible] = useState(false);

  // Dispatch Edit Modal state
  const [editDispatchVisible, setEditDispatchVisible] = useState(false);

  // Routing Edit Modal state
  const [editRoutingVisible, setEditRoutingVisible] = useState(false);

  // Routing data state
  const [routingData, setRoutingData] = useState<RoutingData | null>(null);
  const [routingLoading, setRoutingLoading] = useState(true);
  const [routingError, setRoutingError] = useState<string | null>(null);

  // Dispatch data state
  const [dispatchData, setDispatchData] = useState<DispatchData | null>(null);
  const [dispatchLoading, setDispatchLoading] = useState(true);
  const [dispatchError, setDispatchError] = useState<string | null>(null);

  // ServiceNow data state (optional)
  const [snowData, setSnowData] = useState<ServiceNowData | null>(null);
  const [snowLoading, setSnowLoading] = useState(false);

  // Routing table state
  const [filterText, setFilterText] = useState('');
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [sortingColumn, setSortingColumn] = useState<TableProps.SortingColumn<AccountMapping> | null>(null);
  const [sortingDescending, setSortingDescending] = useState(false);
  const [visibleColumns, setVisibleColumns] = useState<string[]>(defaultVisibleColumns);

  // --- Data Fetching ---

  const loadRouting = useCallback(async () => {
    setRoutingLoading(true);
    setRoutingError(null);
    try {
      const response = await apiFetch('/config/routing');
      const data = normalizeResponse<RoutingData>(response);
      // Normalize account mapping keys from camelCase (API) to snake_case (component)
      const rawAccounts = data?.accounts ?? (data as any)?.mappings ?? [];
      const normalizedAccounts: AccountMapping[] = rawAccounts.map((m: any) => ({
        account_id: m.account_id || m.accountId || '',
        account_name: m.account_name || m.accountName || '',
        jira_project: m.jira_project || m.jiraProject || '',
        jira_issue_type: m.jira_issue_type || m.jiraIssueType || '',
        snow_assignment_group_id: m.snow_assignment_group_id || m.snowAssignmentGroupId || '',
      }));
      setRoutingData({ ...data, accounts: normalizedAccounts });
    } catch (err: unknown) {
      setRoutingError(sanitizeError(err));
    } finally {
      setRoutingLoading(false);
    }
  }, []);

  const loadDispatch = useCallback(async () => {
    setDispatchLoading(true);
    setDispatchError(null);
    try {
      const response = await apiFetch('/config/dispatch');
      const data = normalizeResponse<DispatchData>(response);
      setDispatchData(data);
    } catch (err: unknown) {
      setDispatchError(sanitizeError(err));
    } finally {
      setDispatchLoading(false);
    }
  }, []);

  const loadServiceNow = useCallback(async () => {
    setSnowLoading(true);
    try {
      const response = await apiFetch('/config/servicenow');
      const data = normalizeResponse<ServiceNowData>(response);
      // SEC-095-07: Exclude masked credential fields from display
      if (data) {
        const { ...safeData } = data;
        setSnowData(safeData);
      }
    } catch {
      // Silent failure for ServiceNow — non-critical
      setSnowData(null);
    } finally {
      setSnowLoading(false);
    }
  }, []);

  useEffect(() => {
    loadRouting();
    loadDispatch();

    // Only fetch ServiceNow if there's evidence it might be configured
    const shouldFetchSnow =
      config.servicenow?.validated !== undefined ||
      snowEnabled;
    if (shouldFetchSnow) {
      loadServiceNow();
    }
  }, [loadRouting, loadDispatch, loadServiceNow, config]);

  // --- Routing Table Logic ---

  const mappings = routingData?.accounts ?? routingData?.mappings ?? [];

  const filteredItems = useMemo(() => {
    if (!filterText.trim()) return mappings;
    return mappings.filter((m) => matchesRoutingFilter(m, filterText));
  }, [mappings, filterText]);

  const sortedItems = useMemo(() => {
    if (!sortingColumn?.sortingField) return filteredItems;
    return [...filteredItems].sort((a, b) => {
      const field = sortingColumn.sortingField as keyof AccountMapping;
      const aVal = (a[field] || '').toString().toLowerCase();
      const bVal = (b[field] || '').toString().toLowerCase();
      const cmp = aVal.localeCompare(bVal);
      return sortingDescending ? -cmp : cmp;
    });
  }, [filteredItems, sortingColumn, sortingDescending]);

  const paginatedItems = useMemo(() => {
    const start = (currentPage - 1) * pageSize;
    return sortedItems.slice(start, start + pageSize);
  }, [sortedItems, currentPage, pageSize]);

  // Reset to page 1 whenever the filter text changes
  useEffect(() => {
    setCurrentPage(1);
  }, [filterText]);

  // --- Derived Values ---

  const accountMappingCount = mappings.length;

  // Determine which routing-target columns to show. Driven off the resolved
  // platforms array (STORY-139 §5.1) — the mappings.some(...) leg is kept as a
  // harmless extra trigger so a stray SNOW mapping still surfaces the column.
  const showSnowColumn =
    snowEnabled ||
    mappings.some((m) => !!m.snow_assignment_group_id);
  const showJiraColumn = jiraEnabled;

  // --- Status Indicators ---

  const getJiraStatus = (): { type: 'success' | 'error' | 'stopped'; text: string } => {
    if (config.jira?.validated === true) return { type: 'success', text: 'Connected' };
    if (config.jira?.baseUrl) return { type: 'error', text: 'Connection failed' };
    return { type: 'stopped', text: 'Not configured' };
  };

  const getSnowStatus = (): { type: 'success' | 'error' | 'stopped' | 'loading'; text: string } => {
    if (snowLoading) return { type: 'loading', text: 'Loading...' };
    if (snowData?.validated === true) return { type: 'success', text: 'Connected' };
    if (snowData?.instanceUrl) return { type: 'error', text: 'Connection failed' };
    if (config.servicenow?.validated === true) return { type: 'success', text: 'Connected' };
    if (config.servicenow?.instanceUrl) return { type: 'error', text: 'Connection failed' };
    return { type: 'stopped', text: 'Not configured' };
  };

  // --- Routing Column Definitions ---

  /**
   * Full set of possible column definitions for the routing table.
   * The snow_group column is conditionally included based on platform config.
   */
  const allRoutingColumnDefinitions: TableProps.ColumnDefinition<AccountMapping>[] = useMemo(() => {
    const cols: TableProps.ColumnDefinition<AccountMapping>[] = [
      {
        id: 'account_id',
        header: 'Account ID',
        cell: (item) => item.account_id,
        sortingField: 'account_id',
        width: 160,
        minWidth: 140,
      },
      {
        id: 'account_name',
        header: 'Account Name',
        cell: (item) => item.account_name || '—',
        sortingField: 'account_name',
        minWidth: 120,
      },
    ];

    if (showJiraColumn) {
      cols.push({
        id: 'jira_project',
        header: 'JIRA Project',
        cell: (item) => item.jira_project || '—',
        sortingField: 'jira_project',
        width: 140,
        minWidth: 100,
      });
    }

    if (showSnowColumn) {
      cols.push({
        id: 'snow_group',
        header: 'ServiceNow Group',
        cell: (item) => item.snow_assignment_group_id || '—',
        sortingField: 'snow_assignment_group_id',
        width: 180,
        minWidth: 140,
      });
    }

    return cols;
  }, [showSnowColumn, showJiraColumn]);

  /**
   * Only the columns whose id is in visibleColumns are passed to the Table.
   * account_id is always included (editable: false in CollectionPreferences).
   */
  const activeColumnDefinitions = useMemo(() => {
    return allRoutingColumnDefinitions.filter((col) => visibleColumns.includes(col.id!));
  }, [allRoutingColumnDefinitions, visibleColumns]);

  /**
   * CollectionPreferences visibleContentPreference options.
   * account_id is non-editable (always visible); all others are togglable.
   */
  const visibleContentOptions = useMemo(() => {
    const base: Array<{ id: string; label: string; editable?: boolean }> = [
      { id: 'account_id', label: 'Account ID', editable: false },
      { id: 'account_name', label: 'Account Name' },
    ];
    if (showJiraColumn) {
      base.push({ id: 'jira_project', label: 'JIRA Project' });
    }
    if (showSnowColumn) {
      base.push({ id: 'snow_group', label: 'ServiceNow Group' });
    }
    return base;
  }, [showSnowColumn, showJiraColumn]);

  // --- Dispatch Rules Column Definitions ---

  const dispatchColumnDefs = [
    {
      id: 'pattern',
      header: 'Event Type Pattern',
      cell: (item: DispatchRule) => (
        <Box variant="code">{item.eventTypePattern}</Box>
      ),
      width: 250,
    },
    {
      id: 'categories',
      header: 'Categories',
      cell: (item: DispatchRule) => item.eventCategories?.join(', ') || '—',
      width: 200,
    },
    {
      id: 'status',
      header: 'Status',
      cell: (item: DispatchRule) =>
        item.enabled ? (
          <StatusIndicator type="success">Enabled</StatusIndicator>
        ) : (
          <StatusIndicator type="stopped">Disabled</StatusIndicator>
        ),
      width: 120,
    },
  ];

  // --- Render ---

  const jiraStatus = getJiraStatus();
  const snowStatus = getSnowStatus();

  return (
    <>
    <ContentLayout
      header={
        <Header
          variant="h1"
          description="Manage ITSM platform connections, routing rules, and dispatch window settings."
          actions={
            <Button
              variant="normal"
              onClick={onRunWizard}
              data-testid="run-setup-wizard"
            >
              Run Setup Wizard
            </Button>
          }
        >
          Configuration
        </Header>
      }
    >
      <SpaceBetween size="l">

        {/* Section 1: ITSM Connections */}
        <Container
          header={
            <Header
              variant="h2"
              actions={
                <Button
                  onClick={() => setEditConnectionsVisible(true)}
                  data-testid="edit-connections"
                  ariaLabel="Edit ITSM connections"
                >
                  Edit
                </Button>
              }
            >
              ITSM Connections
            </Header>
          }
        >
          <ColumnLayout columns={2} variant="text-grid">
            {/* JIRA Card */}
            <SpaceBetween size="xs">
              <Box variant="h3">JIRA Cloud</Box>
              <StatusIndicator type={jiraStatus.type}>
                {jiraStatus.text}
              </StatusIndicator>
              {config.jira?.baseUrl && (
                <SpaceBetween size="xxxs">
                  {/* SEC-095-03: URL as plain text, not a clickable link */}
                  <Box variant="small" color="text-body-secondary">
                    {config.jira.baseUrl}
                  </Box>
                  {config.jira.validatedUser && (
                    <Box variant="small" color="text-body-secondary">
                      User: {config.jira.validatedUser}
                    </Box>
                  )}
                  {config.jira.validatedAt && (
                    <Box variant="small" color="text-body-secondary">
                      Last validated: {formatRelativeTime(config.jira.validatedAt)}
                    </Box>
                  )}
                </SpaceBetween>
              )}
            </SpaceBetween>

            {/* ServiceNow Card */}
            <SpaceBetween size="xs">
              <Box variant="h3">ServiceNow</Box>
              <StatusIndicator type={snowStatus.type}>
                {snowStatus.text}
              </StatusIndicator>
              {(snowData?.instanceUrl || config.servicenow?.instanceUrl) && (
                <SpaceBetween size="xxxs">
                  {/* SEC-095-03: URL as plain text */}
                  <Box variant="small" color="text-body-secondary">
                    {snowData?.instanceUrl || config.servicenow?.instanceUrl}
                  </Box>
                  {snowData?.validatedUser && (
                    <Box variant="small" color="text-body-secondary">
                      User: {snowData.validatedUser}
                    </Box>
                  )}
                  {(snowData?.validatedAt || config.servicenow?.validatedAt) && (
                    <Box variant="small" color="text-body-secondary">
                      Last validated: {formatRelativeTime(
                        snowData?.validatedAt || config.servicenow?.validatedAt || ''
                      )}
                    </Box>
                  )}
                </SpaceBetween>
              )}
            </SpaceBetween>
          </ColumnLayout>
        </Container>

        {/* Section 2: Routing Rules */}
        <Container
          header={
            <Header
              variant="h2"
              counter={routingLoading ? undefined : `(${accountMappingCount})`}
              actions={
                <Button
                  onClick={() => setEditRoutingVisible(true)}
                  data-testid="edit-routing"
                  ariaLabel="Edit routing rules"
                >
                  Edit Routing
                </Button>
              }
            >
              Routing Rules
            </Header>
          }
        >
          {routingLoading ? (
            <Box textAlign="center" padding="l">
              <Spinner />
            </Box>
          ) : routingError ? (
            <Alert
              type="error"
              action={
                <Button onClick={loadRouting} variant="normal">
                  Retry
                </Button>
              }
            >
              {routingError}
            </Alert>
          ) : (
            <SpaceBetween size="m">
              {/* Summary Metrics Row */}
              <ColumnLayout columns={4} variant="text-grid">
                <div>
                  <Box variant="awsui-key-label">Default Project</Box>
                  <Box variant="p">
                    {routingData?.default?.jiraProject || routingData?.defaultProject || config.routing?.defaultProject || '—'}
                  </Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Account Mappings</Box>
                  <Box variant="p">{accountMappingCount} configured</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Tag Routing</Box>
                  <Box variant="p">
                    {config.routing?.tagRouting?.enabled
                      ? `Enabled (Key: ${config.routing?.tagRouting?.tagKey || '—'})`
                      : 'Disabled'}
                  </Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Issue Type</Box>
                  <Box variant="p">{routingData?.default?.jiraIssueType || routingData?.defaultIssueType || 'Task'}</Box>
                </div>
              </ColumnLayout>

              {/* Routing Table */}
              {(() => {
                // Controls are only shown when there is data to interact with
                const hasData = mappings.length > 0;
                const isFiltered = filterText.trim().length > 0;
                const totalPages = Math.ceil(filteredItems.length / pageSize);

                return (
                  <Table
                    variant="embedded"
                    stickyHeader
                    columnDefinitions={activeColumnDefinitions}
                    items={paginatedItems}
                    loading={routingLoading}
                    loadingText="Loading account mappings..."
                    sortingColumn={sortingColumn ?? undefined}
                    sortingDescending={sortingDescending}
                    onSortingChange={({ detail }) => {
                      setSortingColumn(detail.sortingColumn);
                      setSortingDescending(detail.isDescending ?? false);
                    }}
                    empty={
                      isFiltered ? (
                        /* State B: filter is active but yields no results */
                        <Box textAlign="center" padding="l">
                          <SpaceBetween size="xs">
                            <Box variant="strong">No account mappings match the filter</Box>
                            <Box variant="p" color="text-body-secondary">
                              Try a different search term or clear the filter.
                            </Box>
                            <Button
                              variant="link"
                              onClick={() => {
                                setFilterText('');
                                setCurrentPage(1);
                              }}
                            >
                              Clear filter
                            </Button>
                          </SpaceBetween>
                        </Box>
                      ) : (
                        /* State A: no mappings exist at all */
                        <Box textAlign="center" padding="l">
                          <SpaceBetween size="xs">
                            <Box variant="strong">No account mappings configured</Box>
                            <Box variant="p" color="text-body-secondary">
                              All events route to the default project (
                              {routingData?.default?.jiraProject || routingData?.defaultProject || config.routing?.defaultProject || 'none'}).
                            </Box>
                          </SpaceBetween>
                        </Box>
                      )
                    }
                    filter={
                      hasData ? (
                        <TextFilter
                          filteringText={filterText}
                          filteringPlaceholder="Search by account ID, name, or project"
                          filteringAriaLabel="Filter account mappings"
                          onChange={({ detail }) => {
                            setFilterText(detail.filteringText);
                            setCurrentPage(1);
                          }}
                          countText={getFilterCountText(
                            filteredItems.length,
                            mappings.length,
                            isFiltered
                          )}
                        />
                      ) : undefined
                    }
                    pagination={
                      hasData && filteredItems.length > 0 ? (
                        <Pagination
                          currentPageIndex={currentPage}
                          pagesCount={totalPages}
                          onChange={({ detail }) => setCurrentPage(detail.currentPageIndex)}
                          ariaLabels={{
                            nextPageLabel: 'Next page',
                            previousPageLabel: 'Previous page',
                            pageLabel: (n) => `Page ${n} of ${totalPages}`,
                          }}
                        />
                      ) : undefined
                    }
                    preferences={
                      hasData ? (
                        <CollectionPreferences
                          title="Preferences"
                          confirmLabel="Confirm"
                          cancelLabel="Cancel"
                          preferences={{
                            pageSize,
                            visibleContent: visibleColumns,
                          }}
                          onConfirm={({ detail }) => {
                            setPageSize(detail.pageSize ?? 20);
                            setVisibleColumns(detail.visibleContent ? [...detail.visibleContent] : defaultVisibleColumns);
                            setCurrentPage(1);
                          }}
                          pageSizePreference={{
                            title: 'Page size',
                            options: ROUTING_PAGE_SIZE_OPTIONS,
                          }}
                          visibleContentPreference={{
                            title: 'Visible columns',
                            options: [
                              {
                                label: 'Account mapping properties',
                                options: visibleContentOptions,
                              },
                            ],
                          }}
                        />
                      ) : undefined
                    }
                    ariaLabels={{
                      tableLabel: 'Account routing mappings',
                    }}
                  />
                );
              })()}
            </SpaceBetween>
          )}
        </Container>

        {/* Section 3: Dispatch Window */}
        <Container
          header={
            <Header
              variant="h2"
              actions={
                <Button
                  onClick={() => setEditDispatchVisible(true)}
                  data-testid="edit-dispatch"
                  ariaLabel="Edit dispatch window settings"
                >
                  Edit
                </Button>
              }
            >
              Dispatch Window
            </Header>
          }
        >
          {dispatchLoading ? (
            <Box textAlign="center" padding="l">
              <Spinner />
            </Box>
          ) : dispatchError ? (
            <Alert
              type="error"
              action={
                <Button onClick={loadDispatch} variant="normal">
                  Retry
                </Button>
              }
            >
              {dispatchError}
            </Alert>
          ) : (
            <SpaceBetween size="s">
              {dispatchData?.warning && (
                <Alert type="info">{dispatchData.warning}</Alert>
              )}

              <ColumnLayout
                columns={dispatchData?.mode === 'custom' ? 3 : 2}
                variant="text-grid"
              >
                <div>
                  <Box variant="awsui-key-label">Mode</Box>
                  <Box variant="p">
                    {formatDispatchMode(dispatchData?.mode || config.dispatch?.mode)}
                  </Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Actionability Filter</Box>
                  <Box variant="p">
                    {formatActionability(
                      dispatchData?.actionabilityFilter || config.dispatch?.actionabilityFilter
                    )}
                  </Box>
                </div>
                {dispatchData?.mode === 'custom' && (
                  <div>
                    <Box variant="awsui-key-label">Rules</Box>
                    <Box variant="p">
                      {dispatchData.rules?.filter((r) => r.enabled).length || 0} active,{' '}
                      {dispatchData.rules?.filter((r) => !r.enabled).length || 0} disabled
                    </Box>
                  </div>
                )}
              </ColumnLayout>

              {/* Mode description */}
              {(dispatchData?.mode || config.dispatch?.mode) !== 'custom' && (
                <Box variant="small" color="text-body-secondary">
                  {(dispatchData?.mode || config.dispatch?.mode) === 'ple_only'
                    ? 'Only events with type codes ending in _PLANNED_LIFECYCLE_EVENT create tickets.'
                    : 'Tickets are created for all scheduledChange and accountNotification events matching the actionability filter.'}
                </Box>
              )}

              {/* Custom rules table */}
              {dispatchData?.mode === 'custom' && (
                <>
                  {dispatchData.rules && dispatchData.rules.length > 0 ? (
                    <Table
                      variant="embedded"
                      items={dispatchData.rules}
                      columnDefinitions={dispatchColumnDefs}
                      ariaLabels={{ tableLabel: 'Dispatch window rules' }}
                    />
                  ) : (
                    <Box textAlign="center" padding="s" color="text-body-secondary">
                      No custom rules defined. No events will create tickets.
                    </Box>
                  )}
                </>
              )}
            </SpaceBetween>
          )}
        </Container>

        {/* Section 4: System Information */}
        <ExpandableSection
          variant="container"
          headerText="System Information"
          defaultExpanded={false}
        >
          <ColumnLayout columns={2} variant="text-grid">
            <div>
              <Box variant="awsui-key-label">Telemetry</Box>
              <Box variant="p">
                {(config as any).telemetryConsent !== undefined
                  ? (config as any).telemetryConsent
                    ? 'Enabled'
                    : 'Disabled'
                  : 'Unknown'}
              </Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Integration Status</Box>
              <Box variant="p">
                <StatusIndicator
                  type={config.jira?.validated || config.servicenow?.validated ? 'success' : 'warning'}
                >
                  {config.jira?.validated || config.servicenow?.validated
                    ? 'Active'
                    : 'Incomplete setup'}
                </StatusIndicator>
              </Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Active Platform</Box>
              <Box variant="p">
                {labelPlatform === 'servicenow' ? 'ServiceNow' : 'JIRA Cloud'}
              </Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Last Modified</Box>
              <Box variant="p">
                {(config as any).lastModified
                  ? new Date((config as any).lastModified).toLocaleString()
                  : '—'}
              </Box>
            </div>
          </ColumnLayout>
        </ExpandableSection>

      </SpaceBetween>
    </ContentLayout>

    {/* Connection Edit Modal */}
    <ConnectionEditModal
      visible={editConnectionsVisible}
      config={config}
      onDismiss={() => setEditConnectionsVisible(false)}
      onSave={() => {
        setEditConnectionsVisible(false);
        onConfigChanged();
      }}
    />

    {/* Dispatch Edit Modal */}
    <DispatchEditModal
      visible={editDispatchVisible}
      initialConfig={{
        mode: (dispatchData?.mode || config.dispatch?.mode || 'all') as 'all' | 'ple_only' | 'custom',
        actionabilityFilter: (dispatchData?.actionabilityFilter || config.dispatch?.actionabilityFilter || 'all_actionable') as 'all_actionable' | 'action_required_only',
        rules: dispatchData?.rules || [],
      }}
      onDismiss={() => setEditDispatchVisible(false)}
      onSave={() => {
        setEditDispatchVisible(false);
        loadDispatch();
        onConfigChanged();
      }}
    />

    {/* Routing Edit Modal */}
    <RoutingEditModal
      visible={editRoutingVisible}
      onDismiss={() => setEditRoutingVisible(false)}
      onSave={() => {
        setEditRoutingVisible(false);
        loadRouting();
        onConfigChanged();
      }}
    />
    </>
  );
}
