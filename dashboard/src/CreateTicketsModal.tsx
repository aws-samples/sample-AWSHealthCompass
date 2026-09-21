import React, { useState, useEffect, useMemo } from 'react';
import Modal from '@cloudscape-design/components/modal';
import Box from '@cloudscape-design/components/box';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Button from '@cloudscape-design/components/button';
import Alert from '@cloudscape-design/components/alert';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import RadioGroup from '@cloudscape-design/components/radio-group';
import Select from '@cloudscape-design/components/select';
import Table from '@cloudscape-design/components/table';
import Spinner from '@cloudscape-design/components/spinner';
import type { Campaign } from './types';
import { formatTitle } from './types';
import { apiFetch } from './api';
import { usePlatformLabels } from './PlatformContext';

interface Props { campaign: Campaign; onDismiss: () => void; onCreated: () => void; notify: (t: string, m: string) => void; }

export default function CreateTicketsModal({ campaign, onDismiss, onCreated, notify }: Props) {
  const labels = usePlatformLabels();
  const [creating, setCreating] = useState(false);
  const [strategy, setStrategy] = useState<string>('per-account');
  const [tagKey, setTagKey] = useState<string>('');
  const [preview, setPreview] = useState<any>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);

  // Extract unique tag keys from campaign resources
  const tagKeys = useMemo(() => {
    if (!campaign.resources) return [];
    const keys = new Set<string>();
    campaign.resources.forEach(r => {
      Object.keys(r.tags || {}).forEach(k => keys.add(k));
    });
    return Array.from(keys).sort();
  }, [campaign.resources]);

  // Fetch grouping preview with debounce
  useEffect(() => {
    if (strategy === 'per-tag-value' && !tagKey) return;
    const timer = setTimeout(async () => {
      setPreviewLoading(true);
      try {
        const params = new URLSearchParams({ strategy });
        if (tagKey) params.set('tagKey', tagKey);
        const data = await apiFetch(`/campaigns/${encodeURIComponent(campaign.campaignId)}/group-preview?${params}`);
        setPreview(data);
        setPreviewError(null);
      } catch (e: any) { setPreviewError(e.message); setPreview(null); }
      finally { setPreviewLoading(false); }
    }, 300);
    return () => clearTimeout(timer);
  }, [strategy, tagKey, campaign.campaignId]);

  const create = async () => {
    setCreating(true);
    try {
      const body: any = {};
      if (strategy !== 'per-account' || tagKey) {
        body.grouping = { strategy, ...(tagKey && { tagKey }) };
      }
      const r = await apiFetch(`/campaigns/${encodeURIComponent(campaign.campaignId)}/create-tickets`, {
        method: 'POST',
        body: JSON.stringify(body),
      });
      if (r.ticketsCreated > 0) onCreated();
      else notify('info', r.message || 'No tickets created');
    } catch (e: any) { notify('error', e.message); }
    finally { setCreating(false); }
  };

  const unticketed = campaign.hasResources
    ? campaign.totalResources - (campaign.ticketedResources || 0) - (campaign.resolvedResources || 0)
    : 1;

  const accounts = campaign.resources
    ? new Set(campaign.resources.filter(r => !(r.ticketId || r.jiraTicketKey) && r.healthStatus !== 'RESOLVED').map(r => r.accountId)).size
    : 1;

  return (
    <Modal visible onDismiss={onDismiss} header={`Create Tickets — ${formatTitle(campaign)}`}
      footer={<Box float="right"><SpaceBetween direction="horizontal" size="xs">
        <Button onClick={onDismiss}>Cancel</Button>
        <Button variant="primary" onClick={create} loading={creating}>Create Tickets</Button>
      </SpaceBetween></Box>}>
      <SpaceBetween size="m">
        <Alert type="info">
          {labels.createAlert}
          {' '}{labels.orphanNote}
        </Alert>
        <ColumnLayout columns={2}>
          <div><Box variant="awsui-key-label">Campaign</Box>{formatTitle(campaign)}</div>
          <div><Box variant="awsui-key-label">Service</Box>{campaign.service}</div>
          <div><Box variant="awsui-key-label">Type</Box>{campaign.hasResources ? 'Resource-level' : 'Account-level'}</div>
          <div><Box variant="awsui-key-label">Tickets to create</Box>~{accounts} (one per account)</div>
          <div><Box variant="awsui-key-label">Resources to ticket</Box>{campaign.hasResources ? unticketed : '—'}</div>
          <div><Box variant="awsui-key-label">Deadline</Box>{campaign.deadline || 'None'}</div>
        </ColumnLayout>

        {/* Grouping Strategy */}
        <Box variant="h4">Grouping Strategy</Box>
        <RadioGroup
          value={strategy}
          onChange={({ detail }) => { setStrategy(detail.value); if (detail.value !== 'per-tag-value') setTagKey(''); }}
          items={[
            { value: 'per-account', label: 'One ticket per account' },
            { value: 'per-tag-value', label: 'One ticket per tag value' },
            { value: 'single', label: 'Single ticket for all resources' },
          ]}
        />

        {/* Tag Key Select — visible only for per-tag-value */}
        {strategy === 'per-tag-value' && (
          <Select
            selectedOption={tagKey ? { value: tagKey, label: tagKey } : null}
            onChange={({ detail }) => setTagKey(detail.selectedOption?.value || '')}
            options={tagKeys.map(k => ({ value: k, label: k }))}
            placeholder="Select a tag key"
            empty="No tag keys found in resources"
          />
        )}

        {/* Preview Table */}
        {previewLoading && <Spinner />}
        {previewError && <Alert type="error">{previewError}</Alert>}
        {preview && preview.groups && (
          <>
            <Table
              columnDefinitions={[
                { id: 'label', header: 'Group Label', cell: (item: any) => item.label },
                { id: 'target', header: 'Target', cell: (item: any) => item.target },
                { id: 'count', header: 'Resource Count', cell: (item: any) => item.resourceCount },
              ]}
              items={preview.groups}
              variant="embedded"
            />
            <Box color="text-body-secondary">{preview.groups.length} ticket{preview.groups.length !== 1 ? 's' : ''} will be created</Box>
          </>
        )}
      </SpaceBetween>
    </Modal>
  );
}
