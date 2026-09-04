import React, { useState, useEffect } from 'react';
import Table from '@cloudscape-design/components/table';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Box from '@cloudscape-design/components/box';
import Container from '@cloudscape-design/components/container';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import ProgressBar from '@cloudscape-design/components/progress-bar';
import SplitPanel from '@cloudscape-design/components/split-panel';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import SegmentedControl from '@cloudscape-design/components/segmented-control';
import Button from '@cloudscape-design/components/button';
import type { Campaign, OnboardingConfig } from './types';
import { formatTitle, formatDays, daysLeft } from './types';
import { apiFetch } from './api';
import { usePlatformLabels } from './PlatformContext';

interface Props { campaigns: Campaign[]; config: OnboardingConfig | null; onRefresh: () => void; notify: (t: string, m: string) => void; onSync?: () => void; onSplitPanelChange: (panel: React.ReactNode) => void; }

export default function Campaigns({ campaigns, config, onRefresh, notify, onSync, onSplitPanelChange }: Props) {
  const labels = usePlatformLabels();
  const [view, setView] = useState('campaigns');
  const [selected, setSelected] = useState<Campaign | null>(null);
  const [detail, setDetail] = useState<Campaign | null>(null);

  const loadDetail = async (c: Campaign) => {
    setSelected(c);
    try { setDetail(await apiFetch(`/campaigns/${encodeURIComponent(c.campaignId)}`)); }
    catch { setDetail(c); }
  };

  useEffect(() => {
    if (selected && detail) {
      onSplitPanelChange(
        <SplitPanel header={formatTitle(detail)} closeBehavior="hide">
          <SpaceBetween size="m">
            <ColumnLayout columns={3}>
              <div><Box variant="awsui-key-label">Service</Box>{detail.service}</div>
              <div><Box variant="awsui-key-label">Deadline</Box>{detail.deadline || 'None'}</div>
              <div><Box variant="awsui-key-label">Completion</Box>{detail.totalResources > 0 ? `${Math.round((detail.resolvedResources / detail.totalResources) * 100)}%` : '—'}</div>
            </ColumnLayout>
            {detail.resources && detail.resources.length > 0 && (
              <Table items={detail.resources} columnDefinitions={[
                { id: 'arn', header: 'Resource', cell: (r: any) => r.resourceArn },
                { id: 'account', header: 'Account', cell: (r: any) => r.accountId },
                { id: 'health', header: 'Health', cell: (r: any) =>
                  r.healthStatus === 'RESOLVED' ? <StatusIndicator type="success">Resolved</StatusIndicator>
                  : <StatusIndicator type="pending">Pending</StatusIndicator> },
                { id: 'ticket', header: labels.ticketColumn, cell: (r: any) => r.ticketId || r.jiraTicketKey || '—' },
                { id: 'status', header: labels.statusColumn, cell: (r: any) => r.jiraStatusName || r.ticketStatus || '—' },
              ]} />
            )}
          </SpaceBetween>
        </SplitPanel>
      );
    } else {
      onSplitPanelChange(null);
    }
  }, [selected, detail]);

  const totalTicketed = campaigns.reduce((s, c) => s + c.ticketedResources, 0);
  const totalClosed = campaigns.reduce((s, c) => s + (c.ticketsClosedResources || 0), 0);
  const atRisk = campaigns.filter(c => daysLeft(c.deadline) <= 30 && daysLeft(c.deadline) >= 0).length;

  // Aggregate by account across all campaigns
  const byAccount = new Map<string, { total: number; resolved: number; campaigns: number }>();
  for (const c of campaigns) {
    if (c.groupBreakdown) {
      for (const [acct, data] of Object.entries(c.groupBreakdown)) {
        const prev = byAccount.get(acct) || { total: 0, resolved: 0, campaigns: 0 };
        byAccount.set(acct, { total: prev.total + data.total, resolved: prev.resolved + data.resolved, campaigns: prev.campaigns + 1 });
      }
    } else if (c.affectedAccount) {
      const prev = byAccount.get(c.affectedAccount) || { total: 0, resolved: 0, campaigns: 0 };
      byAccount.set(c.affectedAccount, { total: prev.total + c.totalResources, resolved: prev.resolved + c.resolvedResources, campaigns: prev.campaigns + 1 });
    }
  }

  return (
    <SpaceBetween size="l">
      <Header variant="h1" actions={onSync && <Button onClick={onSync} iconName="refresh">Sync</Button>}>Campaigns</Header>

      <ColumnLayout columns={4} variant="text-grid">
        <Container><Box variant="awsui-key-label">Active Campaigns</Box><Box variant="awsui-value-large">{campaigns.length}</Box></Container>
        <Container><Box variant="awsui-key-label">Ticketed Resources</Box><Box variant="awsui-value-large">{totalTicketed}</Box></Container>
        <Container><Box variant="awsui-key-label">Tickets Closed</Box><Box variant="awsui-value-large">{totalClosed}</Box></Container>
        <Container><Box variant="awsui-key-label">At Risk (&lt;30d)</Box><Box variant="awsui-value-large">{atRisk}</Box></Container>
      </ColumnLayout>

      <SegmentedControl
        selectedId={view}
        onChange={({ detail: d }) => { setView(d.selectedId); setSelected(null); setDetail(null); }}
        options={[
          { text: 'Campaigns', id: 'campaigns' },
          { text: 'By Account', id: 'account' },
        ]}
      />

      {view === 'campaigns' && (
        <Table
          items={campaigns}
          onSelectionChange={({ detail: d }) => d.selectedItems[0] && loadDetail(d.selectedItems[0])}
          selectionType="single" selectedItems={selected ? [selected] : []}
          columnDefinitions={[
            { id: 'title', header: 'Campaign', cell: c => formatTitle(c), sortingField: 'title' },
            { id: 'service', header: 'Service', cell: c => c.service },
            { id: 'progress', header: 'Progress', cell: c => c.hasResources
              ? <ProgressBar value={c.totalResources > 0 ? (c.resolvedResources / c.totalResources) * 100 : 0}
                  additionalInfo={`${c.resolvedResources}/${c.totalResources}`} />
              : <StatusIndicator type={c.ticketedResources > 0 ? 'success' : 'pending'}>{c.ticketedResources > 0 ? 'Ticketed' : 'Pending'}</StatusIndicator>
            },
            { id: 'ticketed', header: 'Ticketed', cell: c => c.ticketedResources },
            { id: 'closed', header: 'Closed', cell: c => c.ticketsClosedResources || 0 },
            { id: 'deadline', header: 'Deadline', cell: c => formatDays(c.deadline), sortingField: 'deadline' },
          ]}
          empty={<Box textAlign="center"><b>No campaigns</b><Box>Create tickets from the Health Events dashboard</Box></Box>}
        />
      )}

      {view === 'account' && (
        <Table
          items={[...byAccount.entries()].map(([acct, data]) => ({ acct, ...data }))}
          columnDefinitions={[
            { id: 'acct', header: 'Account', cell: (r: any) => r.acct },
            { id: 'campaigns', header: 'Campaigns', cell: (r: any) => r.campaigns },
            { id: 'progress', header: 'Progress', cell: (r: any) =>
              <ProgressBar value={r.total > 0 ? (r.resolved / r.total) * 100 : 0} additionalInfo={`${r.resolved}/${r.total}`} /> },
            { id: 'total', header: 'Total Resources', cell: (r: any) => r.total },
          ]}
          empty={<Box textAlign="center"><b>No account data</b></Box>}
        />
      )}

    </SpaceBetween>
  );
}
