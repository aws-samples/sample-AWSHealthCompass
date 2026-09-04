/**
 * STORY-125 (RT-03): Tag value → routing-target mapping editor.
 * STORY-140 (RT-10): platform-aware — persists a ServiceNow assignment-group
 * target (+ record type) for a SNOW-only deployment, hiding the JIRA-only
 * target fields, reusing STORY-139's `snowEnabled`/`jiraEnabled` gating,
 * `RECORD_TYPE_OPTIONS`, and SNOW vocabulary. JIRA-only behavior is unchanged
 * (AC-140.6).
 *
 * A CONTROLLED component (host owns row state — see interface-review GAP-1) so
 * it works identically in the wizard (where the Step-2 content unmounts before
 * `saveAll` fires from Review) and in the RoutingEditModal. The host reads its
 * own state at save time; this component never calls the network itself.
 *
 * Security (Snape SR-125-12 / MUST-140-3 / SR-139-1/2/3): all user-supplied tag
 * values, sys_ids, and all backend `reason`/`code` messages render as inert
 * text via Cloudscape nodes only — no `dangerouslySetInnerHTML`, no HTML
 * interpolation, no href/src/onClick/style sink. Tag values are shown read-only
 * (Box variant="code") once in the list; renaming = remove + re-add.
 */
import React, { useState } from 'react';
import Table from '@cloudscape-design/components/table';
import Input from '@cloudscape-design/components/input';
import Select from '@cloudscape-design/components/select';
import Button from '@cloudscape-design/components/button';
import Box from '@cloudscape-design/components/box';
import Alert from '@cloudscape-design/components/alert';
import Spinner from '@cloudscape-design/components/spinner';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import {
  TagMappingRow,
  validateTagValueClient,
  validateSnowGroupIdClient,
} from './tagMappings';

// STORY-139 vocabulary, reused verbatim (coin no new labels — Harry req 7).
const RECORD_TYPE_OPTIONS = [
  { label: 'Change Request', value: 'change_request' },
  { label: 'Incident', value: 'incident' },
];

/**
 * STORY-140: map STORY-137 structured SNOW error codes to targeted inline
 * messages (reuse STORY-139 error-code map). Falls back to the raw `reason`
 * (JIRA-branch behavior) when the code is unrecognized/absent. All returned
 * text renders as inert Cloudscape text (MUST-140-3).
 */
export function snowErrorMessage(code: string | undefined, reason: string, sysId?: string): string {
  switch (code) {
    case 'CFG_INVALID_SNOW_GROUP_ID':
      return 'Assignment group must be a 32-character lowercase hex sys_id.';
    case 'CFG_INVALID_SNOW_RECORD_TYPE':
      return 'Record type must be Change Request or Incident.';
    case 'CFG_INVALID_SNOW_GROUP_NAME':
      return 'Assignment group name must be 128 characters or fewer.';
    case 'CFG_SNOW_GROUP_NOT_FOUND':
      return `Assignment group '${sysId ?? ''}' was not found in the connected ServiceNow instance.`;
    default:
      return reason;
  }
}

interface TagRoutingMappingsEditorProps {
  /** Gate: renders nothing when tag routing is disabled. */
  enabled: boolean;
  /** The configured tag key (for empty-state copy). */
  tagKey: string;
  /**
   * STORY-140: platform gating (from `resolvePlatformContext`). Default
   * (snowEnabled=false, jiraEnabled=true) preserves pre-epic JIRA-only behavior
   * for any host that does not yet pass these props (AC-140.6).
   */
  snowEnabled?: boolean;
  jiraEnabled?: boolean;
  /** Controlled row list (host state). */
  mappings: TagMappingRow[];
  onMappingsChange: (rows: TagMappingRow[]) => void;
  /** Controlled "removed since load" set (host state) — persisted rows removed this session. */
  removedTagValues: string[];
  onRemovedChange: (values: string[]) => void;
  /** Per-row backend error reasons from the last save, keyed by tagValue (SR-125-4 / NOTE-C). */
  rowErrors?: Record<string, string>;
  /**
   * STORY-140: section-level precondition alert (e.g. CFG_SNOW_NOT_CONFIGURED),
   * rendered inert above the table. Owned/mapped by the host from the top-level
   * error object.
   */
  sectionError?: string | null;
  /** Load-error affordance (NOTE-F): shown instead of the table; blocks save upstream. */
  loadError?: string | null;
  onRetryLoad?: () => void;
  loading?: boolean;
  /** Disable all controls while a save is in flight. */
  disabled?: boolean;
}

export default function TagRoutingMappingsEditor({
  enabled,
  tagKey,
  snowEnabled = false,
  jiraEnabled = true,
  mappings,
  onMappingsChange,
  removedTagValues,
  onRemovedChange,
  rowErrors = {},
  sectionError = null,
  loadError = null,
  onRetryLoad,
  loading = false,
  disabled = false,
}: TagRoutingMappingsEditorProps) {
  const [newTagValue, setNewTagValue] = useState('');
  const [newProject, setNewProject] = useState('');
  const [newIssueType, setNewIssueType] = useState('Task');
  const [newSnowGroupId, setNewSnowGroupId] = useState('');
  const [newSnowRecordType, setNewSnowRecordType] = useState('change_request');
  const [addError, setAddError] = useState('');

  if (!enabled) return null;

  if (loadError) {
    return (
      <Alert
        type="error"
        action={onRetryLoad ? <Button onClick={onRetryLoad}>Retry</Button> : undefined}
      >
        Failed to load tag mappings. Changes can't be saved until they load.
      </Alert>
    );
  }

  if (loading) {
    return <Box textAlign="center" padding="s"><Spinner /> Loading tag mappings…</Box>;
  }

  // SNOW-only iff snowEnabled and NOT jiraEnabled (STORY-136 operative rule
  // consumed via resolvePlatformContext). Dual still shows the JIRA fields.
  const snowOnly = snowEnabled && !jiraEnabled;
  const keyLabel = (tagKey || '').trim() || 'tag';
  const targetLabel = snowOnly ? 'ServiceNow assignment group' : 'JIRA project';

  const handleAdd = () => {
    setAddError('');
    const value = newTagValue.trim();
    // V1/V4/V5 (advisory; backend authoritative). tagValue validation UNCHANGED
    // and platform-independent (MUST-140-1).
    const valueErr = validateTagValueClient(value);
    if (valueErr) { setAddError(valueErr); return; }
    // V2: target requirement is platform-aware.
    if (snowOnly) {
      const idErr = validateSnowGroupIdClient(newSnowGroupId);
      if (idErr) { setAddError(idErr); return; }
    } else if (!newProject.trim()) {
      setAddError('JIRA project is required.'); return;
    }
    // V3: duplicate tag value (case-sensitive, after trim) — both would collide
    // on the TAG_ROUTING#{value} key and the last write would silently win.
    if (mappings.some(m => m.tagValue === value)) {
      setAddError(`A mapping for tag value "${value}" already exists. Edit the existing row instead.`);
      return;
    }
    onMappingsChange([
      ...mappings,
      {
        tagValue: value,
        jiraProject: snowOnly ? '' : newProject.trim(),
        jiraIssueType: snowOnly ? 'Task' : (newIssueType.trim() || 'Task'),
        snowAssignmentGroupId: snowEnabled ? newSnowGroupId.trim() : '',
        snowRecordType: snowEnabled ? newSnowRecordType : 'change_request',
        rowStatus: 'new',
      },
    ]);
    setNewTagValue(''); setNewProject(''); setNewIssueType('Task');
    setNewSnowGroupId(''); setNewSnowRecordType('change_request');
  };

  // Editing a target marks a previously-persisted row 'edited' (so it joins the
  // upsert set); a 'new' row stays 'new'.
  const updateRow = (
    tagValue: string,
    field: 'jiraProject' | 'jiraIssueType' | 'snowAssignmentGroupId' | 'snowRecordType',
    value: string,
  ) => {
    onMappingsChange(mappings.map(m =>
      m.tagValue === tagValue
        ? { ...m, [field]: value, rowStatus: m.rowStatus === 'new' ? 'new' : 'edited' }
        : m,
    ));
  };

  const removeRow = (row: TagMappingRow) => {
    // A row that existed at load (persisted/edited) must be DELETEd on save;
    // a brand-new unsaved row is simply dropped (NOTE-E: no delete-on-disable).
    if (row.rowStatus !== 'new' && !removedTagValues.includes(row.tagValue)) {
      onRemovedChange([...removedTagValues, row.tagValue]);
    }
    onMappingsChange(mappings.filter(m => m.tagValue !== row.tagValue));
  };

  // Column set is platform-conditional (AC-140.3; reuse STORY-139 gating).
  const columnDefinitions: Array<{ id: string; header: string; cell: (m: TagMappingRow) => React.ReactNode; width?: number }> = [
    {
      id: 'tagValue',
      header: 'Tag value',
      // SR-125-12 / MUST-140-3: read-only, text-only rendering. Rename = remove + re-add.
      cell: (m: TagMappingRow) => <Box variant="code">{m.tagValue}</Box>,
      width: 220,
    },
  ];

  if (jiraEnabled) {
    columnDefinitions.push(
      {
        id: 'jiraProject',
        header: 'JIRA project',
        cell: (m: TagMappingRow) => (
          <Input
            value={m.jiraProject}
            onChange={({ detail }) => updateRow(m.tagValue, 'jiraProject', detail.value)}
            placeholder="CLOUDOPS"
            disabled={disabled}
            ariaLabel={`JIRA project for tag value ${m.tagValue}`}
          />
        ),
        width: 180,
      },
      {
        id: 'jiraIssueType',
        header: 'Issue type',
        cell: (m: TagMappingRow) => (
          <Input
            value={m.jiraIssueType}
            onChange={({ detail }) => updateRow(m.tagValue, 'jiraIssueType', detail.value)}
            placeholder="Task"
            disabled={disabled}
            ariaLabel={`JIRA issue type for tag value ${m.tagValue}`}
          />
        ),
        width: 140,
      },
    );
  }

  if (snowEnabled) {
    columnDefinitions.push(
      {
        id: 'snowAssignmentGroupId',
        header: 'ServiceNow Group',
        cell: (m: TagMappingRow) => (
          <Input
            value={m.snowAssignmentGroupId ?? ''}
            onChange={({ detail }) => updateRow(m.tagValue, 'snowAssignmentGroupId', detail.value)}
            placeholder="sys_id"
            disabled={disabled}
            ariaLabel={`ServiceNow group for tag value ${m.tagValue}`}
          />
        ),
        width: 200,
      },
      {
        id: 'snowRecordType',
        header: 'Record Type',
        cell: (m: TagMappingRow) => (
          <Select
            selectedOption={
              RECORD_TYPE_OPTIONS.find(o => o.value === (m.snowRecordType || 'change_request'))
              || RECORD_TYPE_OPTIONS[0]
            }
            onChange={({ detail }) =>
              updateRow(m.tagValue, 'snowRecordType', detail.selectedOption.value || 'change_request')}
            options={RECORD_TYPE_OPTIONS}
            disabled={disabled}
            ariaLabel={`Record type for tag value ${m.tagValue}`}
          />
        ),
        width: 180,
      },
    );
  }

  columnDefinitions.push(
    {
      id: 'status',
      header: 'Status',
      // Per-row backend reason (SR-125-4 / NOTE-C) rendered as inert text.
      cell: (m: TagMappingRow) =>
        rowErrors[m.tagValue]
          ? <StatusIndicator type="error">{rowErrors[m.tagValue]}</StatusIndicator>
          : <Box variant="small" color="text-body-secondary">{m.rowStatus === 'new' ? 'Unsaved' : 'Saved'}</Box>,
    },
    {
      id: 'actions',
      header: '',
      cell: (m: TagMappingRow) => (
        <Button
          variant="icon"
          iconName="remove"
          disabled={disabled}
          ariaLabel={`Remove mapping for tag value ${m.tagValue}`}
          onClick={() => removeRow(m)}
        />
      ),
      width: 60,
    },
  );

  return (
    <SpaceBetween size="s">
      {/* Section-level precondition (e.g. CFG_SNOW_NOT_CONFIGURED) — inert text. */}
      {sectionError && <Alert type="error">{sectionError}</Alert>}
      <Table
        variant="embedded"
        items={mappings}
        columnDefinitions={columnDefinitions}
        empty={
          <Box textAlign="center" color="text-body-secondary" padding="m">
            <b>No tag-value mappings yet.</b>
            <div>
              Add a mapping so events whose <b>{keyLabel}</b> value matches route to a specific {targetLabel}.
              Until at least one mapping exists, tag routing has nothing to match on and every event falls
              through to account → default routing.
            </div>
          </Box>
        }
        ariaLabels={{ tableLabel: 'Tag value routing mappings' }}
      />

      <SpaceBetween size="xs">
        {addError && <Box color="text-status-error" variant="small">{addError}</Box>}
        <SpaceBetween direction="horizontal" size="xs">
          <Input
            value={newTagValue}
            onChange={({ detail }) => setNewTagValue(detail.value)}
            placeholder="platform"
            disabled={disabled}
            ariaLabel="New tag value"
          />
          {jiraEnabled && (
            <>
              <Input
                value={newProject}
                onChange={({ detail }) => setNewProject(detail.value)}
                placeholder="CLOUDOPS"
                disabled={disabled}
                ariaLabel="JIRA project for new tag value"
              />
              <Input
                value={newIssueType}
                onChange={({ detail }) => setNewIssueType(detail.value)}
                placeholder="Task"
                disabled={disabled}
                ariaLabel="JIRA issue type for new tag value"
              />
            </>
          )}
          {snowEnabled && (
            <>
              <Input
                value={newSnowGroupId}
                onChange={({ detail }) => setNewSnowGroupId(detail.value)}
                placeholder="sys_id"
                disabled={disabled}
                ariaLabel="ServiceNow group for new tag value"
              />
              <Select
                selectedOption={
                  RECORD_TYPE_OPTIONS.find(o => o.value === newSnowRecordType) || RECORD_TYPE_OPTIONS[0]
                }
                onChange={({ detail }) => setNewSnowRecordType(detail.selectedOption.value || 'change_request')}
                options={RECORD_TYPE_OPTIONS}
                disabled={disabled}
                ariaLabel="Record type for new tag value"
              />
            </>
          )}
          <Button
            onClick={handleAdd}
            disabled={disabled}
            data-testid="add-tag-mapping-row"
          >
            Add mapping
          </Button>
        </SpaceBetween>
      </SpaceBetween>
    </SpaceBetween>
  );
}
