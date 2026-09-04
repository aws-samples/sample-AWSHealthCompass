import React, { useState, useEffect } from 'react';
import Table from '@cloudscape-design/components/table';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Box from '@cloudscape-design/components/box';
import TextFilter from '@cloudscape-design/components/text-filter';
import Pagination from '@cloudscape-design/components/pagination';
import SplitPanel from '@cloudscape-design/components/split-panel';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Container from '@cloudscape-design/components/container';
import Tabs from '@cloudscape-design/components/tabs';
import Badge from '@cloudscape-design/components/badge';
import Button from '@cloudscape-design/components/button';
import ProgressBar from '@cloudscape-design/components/progress-bar';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Alert from '@cloudscape-design/components/alert';
import Popover from '@cloudscape-design/components/popover';
import type { Campaign, Resource, GroupData, OnboardingConfig, OrphanStatus } from './types';
import { formatTitle, formatDays, daysLeft } from './types';
import { apiFetch } from './api';
import { usePlatformLabels } from './PlatformContext';
import { resolvePlatformContext } from './platformResolver';
import CreateTicketsModal from './CreateTicketsModal';

interface Props { campaigns: Campaign[]; config: OnboardingConfig | null; onRefresh: () => void; notify: (t: string, m: string) => void; onNavigate: (page: string) => void; onSync: () => void; onSplitPanelChange: (panel: React.ReactNode) => void; }

export default function Dashboard({ campaigns, config, onRefresh, notify, onNavigate, onSync, onSplitPanelChange }: Props) {
  const labels = usePlatformLabels();
  const [filter, setFilter] = useState('');
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<Campaign | null>(null);
  const [detail, setDetail] = useState<Campaign | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [resources, setResources] = useState<Resource[] | null>(null);
  const [breakdown, setBreakdown] = useState<Record<string, GroupData> | null>(null);
  const [resourcesLoading, setResourcesLoading] = useState(false);
  const [breakdownLoading, setBreakdownLoading] = useState(false);
  const [resourcesError, setResourcesError] = useState<string | null>(null);
  const [breakdownError, setBreakdownError] = useState<string | null>(null);
  // STORY-133: headline orphan signal is the TICKET count from the sync-backed
  // orphan-status contract, gated by the backend-owned thresholdExceeded boolean.
  const [orphanStatus, setOrphanStatus] = useState<OrphanStatus>({
    orphanCount: 0,
    thresholdExceeded: false,
    threshold: 10,
  });
  const PAGE_SIZE = 10;

  useEffect(() => {
    // STORY-133 (Q1/AC-6): read the sync-backed TICKET count (O(1) ConfigTable
    // get_item) — not the ResourcesTable /routing/orphans resource scan. The
    // banner/card gate on the backend thresholdExceeded (>10 tickets, A-JIRA-10),
    // never a hardcoded frontend threshold.
    apiFetch('/config/routing/orphan-status')
      .then((data: OrphanStatus) => setOrphanStatus({
        orphanCount: data.orphanCount ?? 0,
        thresholdExceeded: data.thresholdExceeded ?? false,
        threshold: data.threshold ?? 10,
      }))
      .catch(() => {});
  }, [campaigns]);

  useEffect(() => {
    if (selected && detail) {
      const resourcesTab = (() => {
        if (resourcesLoading) return <StatusIndicator type="loading">Loading resources…</StatusIndicator>;
        if (resourcesError) return <Alert type="error">Failed to load resources. {resourcesError}</Alert>;
        if (!resources || resources.length === 0) return <Box textAlign="center">No resources — this is an account-level event.</Box>;
        return (
          <Table items={resources} columnDefinitions={[
            { id: 'arn', header: 'Resource ARN', cell: (r: Resource) => <Box variant="code">{r.resourceArn}</Box> },
            { id: 'account', header: 'Account', cell: (r: Resource) => r.accountId },
            { id: 'health', header: 'Health Status', cell: (r: Resource) =>
              r.healthStatus === 'RESOLVED' ? <StatusIndicator type="success">Resolved</StatusIndicator>
              : <StatusIndicator type="pending">Pending</StatusIndicator> },
            { id: 'ticket', header: labels.ticketColumn, cell: (r: Resource) => r.ticketId || r.jiraTicketKey || '—' },
            { id: 'ticketStatus', header: labels.statusColumn, cell: (r: Resource) => r.ticketStatus === 'none' ? '—' : r.jiraStatusName || r.ticketStatus },
          ]} empty={<Box>No resources</Box>} />
        );
      })();

      const breakdownTab = (() => {
        if (breakdownLoading) return <StatusIndicator type="loading">Loading breakdown…</StatusIndicator>;
        if (breakdownError) return <Alert type="error">Failed to load breakdown. {breakdownError}</Alert>;
        if (!breakdown || Object.keys(breakdown).length === 0) return <Box textAlign="center">No breakdown data available.</Box>;
        return (
          <SpaceBetween size="s">
            {Object.entries(breakdown).map(([acct, data]) => (
              <div key={acct}>
                <Box variant="awsui-key-label">{acct}</Box>
                <ProgressBar value={data.total > 0 ? (data.resolved / data.total) * 100 : 0}
                  additionalInfo={`${data.resolved}/${data.total} resolved`} />
              </div>
            ))}
          </SpaceBetween>
        );
      })();

      onSplitPanelChange(
        <SplitPanel header={formatTitle(detail)} closeBehavior="hide">
          <Tabs tabs={[
            { label: 'Event Details', id: 'details', content: (
              <SpaceBetween size="s">
                <ColumnLayout columns={2}>
                  <div><Box variant="awsui-key-label">Service</Box>{detail.service}</div>
                  <div><Box variant="awsui-key-label">Deadline</Box>{detail.deadline || 'None'}</div>
                  <div><Box variant="awsui-key-label">Actionability</Box>{detail.actionability}</div>
                  <div><Box variant="awsui-key-label">Scope</Box>{detail.hasResources ? 'Resource-level' : 'Account-level'}</div>
                  <div><Box variant="awsui-key-label">Event ARN</Box><Box variant="code">{detail.eventArn}</Box></div>
                  <div><Box variant="awsui-key-label">Account</Box>{detail.affectedAccount}</div>
                </ColumnLayout>
                <Box variant="awsui-key-label">Description</Box>
                <Box variant="p">{detail.description}</Box>
                {detail.ticketedResources === 0 && (
                  <Button variant="primary" onClick={() => setShowCreate(true)}>Create Tickets</Button>
                )}
              </SpaceBetween>
            )},
            { label: `Resources${resources ? ` (${resources.length})` : ''}`, id: 'resources', content: resourcesTab },
            { label: 'Account Breakdown', id: 'breakdown', content: breakdownTab },
          ]} />
        </SplitPanel>
      );
    } else {
      onSplitPanelChange(null);
    }
  }, [selected, detail, resources, breakdown, resourcesLoading, breakdownLoading, resourcesError, breakdownError]);

  const filtered = campaigns.filter(c => {
    const q = filter.toLowerCase();
    return !q || c.title?.toLowerCase().includes(q) || c.service?.toLowerCase().includes(q) || c.eventTypeCode?.toLowerCase().includes(q);
  });
  const paged = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  const loadDetail = async (c: Campaign) => {
    setSelected(c);
    setResources(null); setBreakdown(null);
    setResourcesError(null); setBreakdownError(null);
    setResourcesLoading(true); setBreakdownLoading(true);

    const id = encodeURIComponent(c.campaignId);

    // Fetch campaign detail
    try { setDetail(await apiFetch(`/campaigns/${id}`)); }
    catch { setDetail(c); }

    // Fetch resources (parallel)
    apiFetch(`/campaigns/${id}/resources`)
      .then(data => setResources(data.resources || []))
      .catch(e => setResourcesError(e.message))
      .finally(() => setResourcesLoading(false));

    // Fetch breakdown (parallel)
    apiFetch(`/campaigns/${id}/breakdown`)
      .then(data => setBreakdown(data.breakdown || {}))
      .catch(e => setBreakdownError(e.message))
      .finally(() => setBreakdownLoading(false));
  };

  const active = campaigns.filter(c => c.status === 'active').length;
  const critical = campaigns.filter(c => daysLeft(c.deadline) <= 30 && daysLeft(c.deadline) >= 0).length;
  const pendingRes = campaigns.reduce((s, c) => s + c.totalResources - c.resolvedResources, 0);
  const activeCamp = campaigns.filter(c => c.ticketedResources > 0).length;

  // STORY-139 (§4): platform-aware setup-alert readiness, driven by the resolved
  // platforms array (not the JIRA-only scalar). For a SNOW-only deployment,
  // readiness keys off the ServiceNow connection/routing signals so a fully
  // configured SNOW-only customer is not perpetually told to "configure JIRA"
  // (AC-139.5). For JIRA/dual, the legs reduce byte-identical to pre-epic
  // (AC-139.9 no-regression).
  const { labelPlatform } = resolvePlatformContext(config);
  const connectionReady =
    labelPlatform === 'servicenow'
      ? config?.servicenow?.validated === true
      : config?.jira?.credentialsConfigured === true;
  const routingReady =
    labelPlatform === 'servicenow'
      // Prefer a discrete SNOW default-group signal if the summary exposes it;
      // otherwise use the platform-neutral accountMappingCount fallback (§4.1).
      ? !!(config?.routing as any)?.snowAssignmentGroupId ||
        (config?.routing?.accountMappingCount ?? 0) > 0
      : !!config?.routing?.defaultProject;

  return (
    <SpaceBetween size="l">
      <Header variant="h1" actions={<Button onClick={onSync} iconName="refresh">Sync</Button>}>Health Events Dashboard</Header>

      {config && !connectionReady && (
        <Alert type="warning" action={<Button onClick={() => onNavigate('configuration')}>Go to Configuration</Button>}>
          Setup incomplete — {labels.setupAlert}.
        </Alert>
      )}
      {config && connectionReady && !routingReady && (
        <Alert type="info" action={<Button onClick={() => onNavigate('configuration')}>Configure Routing</Button>}>
          {labels.connectedAlert}
        </Alert>
      )}
      {orphanStatus.thresholdExceeded && (
        <Alert
          type="warning"
          header={`More than ${orphanStatus.threshold} tickets in the default project`}
          action={<Button onClick={() => onNavigate('configuration')}>Add Mappings</Button>}
        >
          {orphanStatus.orphanCount} tickets have been routed to the default project because their AWS accounts aren't mapped to a specific project yet. These events are still routed (they count toward Routing Coverage) — add account mappings to route them to the right teams.
        </Alert>
      )}

      <ColumnLayout columns={4} variant="text-grid">
        <Container><Box variant="awsui-key-label">Active Events</Box><Box variant="awsui-value-large">{active}</Box></Container>
        <Container><Box variant="awsui-key-label">Critical (&lt;30d)</Box><Box variant="awsui-value-large">{critical}</Box></Container>
        <Container><Box variant="awsui-key-label">Pending Resources</Box><Box variant="awsui-value-large">{pendingRes}</Box></Container>
        <Container><Box variant="awsui-key-label">Active Campaigns</Box><Box variant="awsui-value-large">{activeCamp}</Box></Container>
        {orphanStatus.orphanCount > 0 && (
          <Container>
            <Box variant="awsui-key-label" color={orphanStatus.thresholdExceeded ? 'text-status-error' : undefined}>
              <Popover
                dismissButton={false}
                position="top"
                size="medium"
                triggerType="text"
                content="Tickets routed to the default project because their account has no specific mapping. This is not a routing failure — these events are counted as routed in Routing Coverage. Reduce this number by adding account → project mappings in Configuration."
              >
                Default-Project Tickets
              </Popover>
            </Box>
            <Box variant="awsui-value-large">{orphanStatus.orphanCount}</Box>
            <Box fontSize="body-s" color="text-body-secondary">Routed to default — accounts not yet mapped</Box>
          </Container>
        )}
      </ColumnLayout>

      <Table
        header={<Header counter={`(${filtered.length})`}>Health Events</Header>}
        items={paged}
        onSelectionChange={({ detail: d }) => d.selectedItems[0] && loadDetail(d.selectedItems[0])}
        selectionType="single"
        selectedItems={selected ? [selected] : []}
        filter={<TextFilter filteringText={filter} onChange={({ detail: d }) => { setFilter(d.filteringText); setPage(1); }} />}
        pagination={<Pagination currentPageIndex={page} pagesCount={Math.ceil(filtered.length / PAGE_SIZE)} onChange={({ detail: d }) => setPage(d.currentPageIndex)} />}
        columnDefinitions={[
          { id: 'title', header: 'Event', cell: c => <>{formatTitle(c)} {c.ticketedResources > 0 && <Badge color="blue">Campaign</Badge>}</>, sortingField: 'title' },
          { id: 'service', header: 'Service', cell: c => c.service, sortingField: 'service' },
          { id: 'type', header: 'Type', cell: c => c.hasResources ? 'Resource-level' : 'Account-level' },
          { id: 'resources', header: 'Resources', cell: c => c.hasResources ? `${c.resolvedResources}/${c.totalResources}` : '—' },
          { id: 'deadline', header: 'Deadline', cell: c => formatDays(c.deadline), sortingField: 'deadline' },
          { id: 'actionability', header: 'Action', cell: c => c.actionability === 'ACTION_REQUIRED'
            ? <StatusIndicator type="warning">Required</StatusIndicator>
            : <StatusIndicator type="info">May be required</StatusIndicator> },
        ]}
        empty={<Box textAlign="center" color="inherit"><b>No events</b><Box padding={{bottom:'s'}}>No Health events ingested yet.</Box><Button onClick={onSync}>Sync Now</Button></Box>}
      />

      {showCreate && detail && (
        <CreateTicketsModal campaign={detail} onDismiss={() => setShowCreate(false)}
          onCreated={() => { setShowCreate(false); onRefresh(); notify('success', 'Tickets created'); }} notify={notify} />
      )}
    </SpaceBetween>
  );
}
