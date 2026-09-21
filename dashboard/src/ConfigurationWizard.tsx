import React, { useState, useEffect } from 'react';
import Wizard from '@cloudscape-design/components/wizard';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import Button from '@cloudscape-design/components/button';
import Alert from '@cloudscape-design/components/alert';
import Table from '@cloudscape-design/components/table';
import RadioGroup from '@cloudscape-design/components/radio-group';
import Textarea from '@cloudscape-design/components/textarea';
import TokenGroup from '@cloudscape-design/components/token-group';
import Box from '@cloudscape-design/components/box';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Toggle from '@cloudscape-design/components/toggle';
import Checkbox from '@cloudscape-design/components/checkbox';
import Select from '@cloudscape-design/components/select';
import type { OnboardingConfig } from './types';
import { DispatchRule, DispatchMode, ActionabilityFilter, buildDispatchBody } from './types/dispatch';
import { apiFetch } from './api';
import { APP_NAME } from './branding';
import { parseApiError } from './errors';
import { usePlatformLabels } from './PlatformContext';
import TagRoutingMappingsEditor from './components/TagRoutingMappingsEditor';
import { TagMappingRow, TagMappingsSaveResult, getUpsertRows, hasTagMappingClientErrors, persistTagMappings } from './components/tagMappings';

interface Props { config: OnboardingConfig | null; onSave: () => void; }

// Dispatch custom-rule category options (mirrors DispatchEditModal's Select pattern
// so the wizard cannot submit an arbitrary, API-rejected category string).
const DISPATCH_CATEGORY_OPTIONS = [
  { label: 'Scheduled Change', value: 'scheduledChange' },
  { label: 'Account Notification', value: 'accountNotification' },
  { label: 'Both', value: 'both' },
];

/** Map a category Select value to the API eventCategories array. */
function selectValueToCategories(val: string): string[] {
  if (val === 'both') return ['scheduledChange', 'accountNotification'];
  return [val];
}

// Tag-source selector. Values are sent verbatim as `tagSource`
// and match the backend _VALID_TAG_SOURCES exactly — no transform layer. Resource
// is listed FIRST for discoverability (the marquee capability), while the default
// selection stays 'account' (engine-aligned; zero behavior change on re-save).
const TAG_SOURCE_ITEMS = [
  { value: 'resource', label: 'Resource tags', description: "Read the routing tag from each affected resource's own tags (resourceTags). Best for per-workload / per-team ownership. If a resource has no value for this tag, routing falls back to the account-ID mapping — the event is never dropped." },
  { value: 'account', label: 'Account tags', description: "Read the routing tag from the AWS account's tags (accountTags, from AWS Organizations). If the account has no value for this tag, routing falls back to the account-ID mapping." },
  { value: 'both', label: 'Both (resource, then account)', description: 'Prefer the resource tag; if the resource has no value, use the account tag. If neither is present, routing falls back to the account-ID mapping.' },
];

const TAG_SOURCE_LABEL: Record<string, string> = {
  resource: 'Resource tags',
  account: 'Account tags',
  both: 'Both (resource, then account)',
};

const TAG_SOURCE_GROUP_DESCRIPTION =
  `Choose where ${APP_NAME} reads the routing tag. If the selected source has no value for an event, routing automatically falls back to the account-ID mapping, then the default project — no event is dropped.`;

/** Normalize a stored tag source to a valid selector value (unknown -> account). */
function normalizeTagSource(raw: unknown): 'resource' | 'account' | 'both' {
  return raw === 'resource' || raw === 'both' ? raw : 'account';
}

export default function ConfigurationWizard({ config, onSave }: Props) {
  const labels = usePlatformLabels();
  const [activeStep, setActiveStep] = useState(0);

  // Platform selection (Step 0) — multi-platform checkboxes
  const [enabledPlatforms, setEnabledPlatforms] = useState<string[]>(['jira']);
  // Legacy single-platform field kept for label context only
  const platform = enabledPlatforms.includes('servicenow') && !enabledPlatforms.includes('jira') ? 'servicenow' : 'jira';

  // Step 1: JIRA connection
  const [jiraBaseUrl, setJiraBaseUrl] = useState('');
  const [jiraEmail, setJiraEmail] = useState('');
  const [jiraApiToken, setJiraApiToken] = useState('');
  const [jiraTestResult, setJiraTestResult] = useState<{ ok: boolean; msg: string } | null>(null);
  const [jiraTesting, setJiraTesting] = useState(false);

  // Step 1: ServiceNow connection
  const [snowInstanceUrl, setSnowInstanceUrl] = useState('');
  const [snowClientId, setSnowClientId] = useState('');
  const [snowClientSecret, setSnowClientSecret] = useState('');
  const [snowUsername, setSnowUsername] = useState('');
  const [snowPassword, setSnowPassword] = useState('');
  const [snowTesting, setSnowTesting] = useState(false);
  const [snowTestResult, setSnowTestResult] = useState<{ ok: boolean; msg: string } | null>(null);
  const [snowShowForm, setSnowShowForm] = useState(false);
  const [connectionValidated, setConnectionValidated] = useState(false);

  // Step 2: Routing
  const [defaultProject, setDefaultProject] = useState('');
  const [defaultSnowGroupId, setDefaultSnowGroupId] = useState('');
  const [defaultSnowRecordType, setDefaultSnowRecordType] = useState('change_request');
  const [accountMappings, setAccountMappings] = useState<{ account_id: string; jira_project: string; snow_assignment_group_id?: string; account_name?: string }[]>([]);
  const [newAcct, setNewAcct] = useState('');
  const [newProj, setNewProj] = useState('');
  const [newSnowGroup, setNewSnowGroup] = useState('');
  const [bulkText, setBulkText] = useState('');
  const [bulkMode, setBulkMode] = useState(false);
  const [loadingOrg, setLoadingOrg] = useState(false);
  const [tagRoutingEnabled, setTagRoutingEnabled] = useState(false);
  const [tagRoutingKey, setTagRoutingKey] = useState('');
  // Which source the routing tag is read from. Default
  // 'account' matches the engine/handler default so re-saving an existing
  // config changes nothing; the selector is always visible when tag routing is
  // enabled so the value is never silently persisted.
  const [tagSource, setTagSource] = useState<'resource' | 'account' | 'both'>('account');
  // Persistence result of the tag-routing strategy, distinct
  // from the local toggle intent. Mutated ONLY inside saveAll; reset to 'unsaved'
  // whenever the toggle/key is edited. The Review step derives its "Enabled"
  // confirmation from this — an affirmative "Enabled" is shown ONLY on 'saved'
  // (a successful HTTP 200 persist), never from toggle intent alone.
  const [tagRoutingSaveState, setTagRoutingSaveState] = useState<'unsaved' | 'saving' | 'saved' | 'error'>('unsaved');
  // Tag value → routing-target mapping editor state. Lifted
  // to the wizard parent because the Step-2 content
  // unmounts before saveAll fires from the Review step — saveAll MUST read this
  // parent state, never a ref to the unmounted editor. Mirrors accountMappings.
  const [tagMappings, setTagMappings] = useState<TagMappingRow[]>([]);
  const [removedTagValues, setRemovedTagValues] = useState<string[]>([]);
  const [tagMappingsLoadError, setTagMappingsLoadError] = useState<string | null>(null);
  const [tagMappingsLoading, setTagMappingsLoading] = useState(false);
  // Per-row backend reasons keyed by tagValue, surfaced after a partial save.
  const [tagMappingRowErrors, setTagMappingRowErrors] = useState<Record<string, string>>({});
  const [routingValidation, setRoutingValidation] = useState<Record<string, { valid: boolean; displayName?: string; error?: string }>>({});
  const [routingValidating, setRoutingValidating] = useState(false);
  const [routingValidationError, setRoutingValidationError] = useState('');

  // Step 3: Dispatch
  const [dispatchMode, setDispatchMode] = useState<DispatchMode>('all');
  const [actionabilityFilter, setActionabilityFilter] = useState<ActionabilityFilter>('all_actionable');
  const [customRules, setCustomRules] = useState<DispatchRule[]>([]);
  const [newPattern, setNewPattern] = useState('');
  const [newCategory, setNewCategory] = useState('scheduledChange');

  // Step 4: Review & Activate
  const [saving, setSaving] = useState(false);
  const [saveErrors, setSaveErrors] = useState<string[]>([]);

  // Setup timer (B-CFG-2)
  const [timer, setTimer] = useState<{ started: boolean; completed: boolean; durationMinutes?: number | null } | null>(null);

  useEffect(() => {
    apiFetch('/config/setup-timer').then(setTimer).catch(() => {});
    apiFetch('/config/setup-timer/start', { method: 'POST' }).catch(() => {});
  }, []);

  // Load existing config on mount
  useEffect(() => {
    if (!config) return;
    if (config.jira?.baseUrl) setJiraBaseUrl(config.jira.baseUrl);
    if (config.jira?.validated || config.servicenow?.validated) setConnectionValidated(true);
    if (config.routing?.defaultProject) setDefaultProject(config.routing.defaultProject);
    // Reflect the persisted tag source on the selector so a
    // resource/both config round-trips truthfully; unknown/legacy -> 'account'.
    if (config.routing?.tagRouting?.tagSource) setTagSource(normalizeTagSource(config.routing.tagRouting.tagSource));
    if ((config.routing as any)?.snowAssignmentGroupId) setDefaultSnowGroupId((config.routing as any).snowAssignmentGroupId);
    if ((config.routing as any)?.snowRecordType) setDefaultSnowRecordType((config.routing as any).snowRecordType);
    if (config.dispatch?.mode) setDispatchMode(config.dispatch.mode as DispatchMode);
    if (config.dispatch?.actionabilityFilter) setActionabilityFilter(config.dispatch.actionabilityFilter as ActionabilityFilter);
    if (config.servicenow?.validated) setSnowShowForm(false);

    // Infer enabled platforms from validated connections (fallback if /config/integrations fails)
    const inferred: string[] = [];
    if (config.jira?.validated || config.jira?.baseUrl) inferred.push('jira');
    if (config.servicenow?.validated || config.servicenow?.instanceUrl) inferred.push('servicenow');
    if (inferred.length > 0) setEnabledPlatforms(inferred);
  }, [config]);

  // Load enabled platforms from new integrations API
  useEffect(() => {
    apiFetch('/config/integrations')
      .then((data: any) => {
        if (data?.platforms && Array.isArray(data.platforms) && data.platforms.length > 0) {
          setEnabledPlatforms(data.platforms);
        }
      })
      .catch(() => { /* Keep default ['jira'] on error */ });
  }, []);

  // Load persisted tag→target mappings for round-trip
  // On failure show an error affordance rather than a silent
  // empty editor that could invite destructive re-entry.
  const loadTagMappings = async () => {
    setTagMappingsLoading(true);
    setTagMappingsLoadError(null);
    try {
      const resp = await apiFetch('/config/routing/tags');
      const rows: TagMappingRow[] = (resp?.mappings ?? []).map((m: any) => ({
        tagValue: m.tagValue ?? '',
        jiraProject: m.jiraProject ?? '',
        jiraIssueType: m.jiraIssueType ?? 'Task',
        rowStatus: 'persisted' as const,
      }));
      setTagMappings(rows);
      setRemovedTagValues([]);
      setTagMappingRowErrors({});
    } catch (e: unknown) {
      setTagMappingsLoadError(parseApiError(e));
    } finally {
      setTagMappingsLoading(false);
    }
  };

  useEffect(() => {
    loadTagMappings();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Platform toggle handler — enables/disables platforms (both can be active)
  const togglePlatform = (platformId: string, checked: boolean) => {
    setEnabledPlatforms(prev => {
      if (checked) return [...prev, platformId].filter((v, i, a) => a.indexOf(v) === i);
      const next = prev.filter(p => p !== platformId);
      // Ensure at least one platform stays enabled
      return next.length > 0 ? next : prev;
    });
  };

  // Test JIRA connection
  const testJira = async () => {
    setJiraTesting(true); setJiraTestResult(null);
    try {
      const r = await apiFetch('/config/jira', {
        method: 'POST', body: JSON.stringify({ baseUrl: jiraBaseUrl, email: jiraEmail, apiToken: jiraApiToken }),
      });
      setJiraTestResult({ ok: true, msg: `Connected as ${r.validatedUser}` });
      setConnectionValidated(true);
    } catch (e: any) { setJiraTestResult({ ok: false, msg: e.message }); }
    finally { setJiraTesting(false); }
  };

  // Test ServiceNow connection
  const testServiceNow = async () => {
    setSnowTesting(true); setSnowTestResult(null);
    try {
      const r = await apiFetch('/config/servicenow/test', {
        method: 'POST',
        body: JSON.stringify({ instanceUrl: snowInstanceUrl, clientId: snowClientId, clientSecret: snowClientSecret, username: snowUsername, password: snowPassword }),
      });
      if (r.valid) {
        const roles = (r.roles || []).join(', ');
        setSnowTestResult({ ok: true, msg: `Connected as: ${r.displayName || snowUsername}${roles ? ` | Roles: ${roles}` : ''}` });
        setConnectionValidated(true);
      } else {
        setSnowTestResult({ ok: false, msg: (r.errors || ['Validation failed']).join('; ') });
      }
    } catch (e: any) { setSnowTestResult({ ok: false, msg: e.message }); }
    finally { setSnowTesting(false); }
  };

  const snowAllFieldsFilled = !!(snowInstanceUrl && snowClientId && snowClientSecret && snowUsername && snowPassword);

  // Org accounts discovery
  const loadOrgAccounts = async () => {
    setLoadingOrg(true);
    try {
      const r = await apiFetch('/config/routing/discover', { method: 'POST' });
      const accts = (r.accounts || []).map((a: any) => ({ account_id: a.accountId, account_name: a.accountName, jira_project: '', snow_assignment_group_id: '' }));
      setAccountMappings(accts);
    } catch (e: any) { alert(`Failed: ${e.message}`); }
    finally { setLoadingOrg(false); }
  };

  // Validate routing targets against ITSM API
  const validateRoutingTargets = async (): Promise<boolean> => {
    const allTargets = [defaultProject, ...accountMappings.map(m => m.jira_project)].filter(Boolean);
    const unique = [...new Set(allTargets)];
    if (unique.length === 0) return true;

    setRoutingValidating(true);
    setRoutingValidationError('');
    try {
      const resp = await apiFetch('/config/routing/validate', {
        method: 'POST',
        body: JSON.stringify({ platform, targets: unique }),
      });
      const map: Record<string, { valid: boolean; displayName?: string; error?: string }> = {};
      for (const r of resp.results || []) {
        map[r.target] = { valid: r.valid, displayName: r.displayName, error: r.error };
      }
      setRoutingValidation(map);
      return (resp.results || []).every((r: any) => r.valid);
    } catch (e: any) {
      setRoutingValidationError(e.message || 'Validation failed');
      return false;
    } finally {
      setRoutingValidating(false);
    }
  };

  // Bulk CSV parse
  const parseBulk = () => {
    const lines = bulkText.trim().split('\n').filter(l => l.trim());
    const parsed = lines.map(l => {
      const [account_id, jira_project, snow_assignment_group_id] = l.split(',').map(s => s.trim());
      return { account_id, jira_project, snow_assignment_group_id: snow_assignment_group_id || undefined, account_name: '' };
    }).filter(m => m.account_id && (m.jira_project || m.snow_assignment_group_id));
    setAccountMappings(parsed);
    setBulkMode(false);
    setBulkText('');
  };

  // Save all config on final step
  const saveAll = async () => {
    setSaving(true);
    setSaveErrors([]);
    const errors: string[] = [];
    // SECURITY INVARIANT: if the dispatch save fails we MUST NOT
    // proceed to /config/activate (which would auto-write DISPATCH_PRESET={mode:'all'},
    // silently widening dispatch to the broadest setting).
    let dispatchFailed = false;

    // 1. Save connections for enabled platforms
    if (enabledPlatforms.includes('jira') && jiraBaseUrl && jiraEmail && jiraApiToken) {
      try {
        await apiFetch('/config/jira', { method: 'POST', body: JSON.stringify({ baseUrl: jiraBaseUrl, email: jiraEmail, apiToken: jiraApiToken }) });
      } catch (e: any) { errors.push(`JIRA connection: ${e.message}`); }
    }
    if (enabledPlatforms.includes('servicenow') && snowAllFieldsFilled) {
      try {
        await apiFetch('/config/servicenow', { method: 'POST', body: JSON.stringify({ instanceUrl: snowInstanceUrl, clientId: snowClientId, clientSecret: snowClientSecret, username: snowUsername, password: snowPassword }) });
        setSnowClientSecret(''); setSnowPassword('');
      } catch (e: any) { errors.push(`ServiceNow connection: ${e.message}`); }
    }

    // 2. Save enabled platforms via new integrations endpoint
    try {
      await apiFetch('/config/integrations', { method: 'PUT', body: JSON.stringify({ platforms: enabledPlatforms }) });
    } catch (e: any) { errors.push(`Platform selection: ${e.message}`); }

    // 3. Save routing — include fields for ALL enabled platforms in one call
    const routingBody: any = {};
    if (enabledPlatforms.includes('jira') && defaultProject) {
      routingBody.jiraProject = defaultProject;
      routingBody.jiraIssueType = 'Task';
    }
    if (enabledPlatforms.includes('servicenow') && defaultSnowGroupId) {
      routingBody.snowAssignmentGroupId = defaultSnowGroupId;
      routingBody.snowRecordType = defaultSnowRecordType;
    }
    if (Object.keys(routingBody).length > 0) {
      try {
        await apiFetch('/config/routing/default', { method: 'POST', body: JSON.stringify(routingBody) });
      } catch (e: any) { errors.push(`Default routing: ${e.message}`); }
    }

    const validMappings = accountMappings.filter(m => m.account_id && (m.jira_project || m.snow_assignment_group_id));
    if (validMappings.length > 0) {
      try {
        const importPayload = {
          format: 'json',
          data: JSON.stringify(validMappings.map(m => ({
            accountId: m.account_id,
            jiraProject: m.jira_project,
            snowAssignmentGroupId: m.snow_assignment_group_id || undefined,
          }))),
        };
        const preview = await apiFetch('/config/routing/import', { method: 'POST', body: JSON.stringify(importPayload) });
        if (preview?.importId) {
          await apiFetch('/config/routing/import/confirm', { method: 'POST', body: JSON.stringify({ importId: preview.importId }) });
        }
      } catch (e: any) { errors.push(`Account mappings: ${e.message}`); }
    }

    // 3.5 Save tag-routing strategy — sequenced BEFORE
    // /config/activate. Clones the working RoutingEditModal.handleSave strategy
    // write path so the single ROUTING_STRATEGY item (surfaced as the
    // nested routing.tagRouting.enabled/.tagKey shape) stays the sole source of
    // truth — no parallel/flat field, no second endpoint.
    //
    // Always POST: enabled -> {mode:'tag', tagKey}; disabled -> {mode:'account'}.
    // The account-mode write actively clears any previously-persisted tag
    // strategy (handler blanks tag_key/tag_source when mode!='tag'), so re-running
    // onboarding with the toggle off cannot leave a stale tag strategy behind.
    // This is independent of the dispatch gate: a tag-strategy failure
    // is surfaced and blocks a false "Enabled", but does NOT set dispatchFailed
    // and does NOT gate /config/activate (routing safely falls back to
    // account/default; the write is idempotent and retryable).
    if (tagRoutingEnabled && !tagRoutingKey.trim()) {
      // Defense in depth: the onNavigate submit guard normally prevents reaching
      // saveAll in this state. Never POST mode:'tag' without a key (a guaranteed
      // 400) — surface the error and do not claim "Enabled".
      setTagRoutingSaveState('error');
      errors.push('Tag routing: Tag key is required when tag routing is enabled.');
    } else {
      setTagRoutingSaveState('saving');
      try {
        await apiFetch('/config/routing/strategy', {
          method: 'POST',
          body: JSON.stringify({ mode: tagRoutingEnabled ? 'tag' : 'account', tagKey: tagRoutingEnabled ? tagRoutingKey.trim() : undefined, tagSource: tagRoutingEnabled ? tagSource : undefined }),
        });
        // Only the enabled path shows an affirmative "Enabled" on Review; the
        // disabled path is a clean account-mode reset (Review reads "Disabled").
        setTagRoutingSaveState(tagRoutingEnabled ? 'saved' : 'unsaved');
      } catch (e: unknown) {
        setTagRoutingSaveState('error');
        errors.push(`Tag routing: ${parseApiError(e)}`);
      }
    }

    // 3.6 Save tag→target mappings — sequenced AFTER the
    // strategy POST (so ROUTING_STRATEGY.mode='tag' is persisted and the engine
    // will consult TAG_ROUTING#) and BEFORE /config/activate. persistTagMappings
    // runs DELETEs first, then one upsert POST. Only runs when tag routing is
    // enabled; disabling never deletes existing mappings. A
    // partial/failed result surfaces per-row and pushes to errors[] (blocking a
    // false "all done"), but — like the strategy step — does NOT set
    // dispatchFailed and does NOT gate /config/activate (routing safely falls
    // back to account/default; the writes are idempotent and retryable).
    if (tagRoutingEnabled && !tagMappingsLoadError) {
      const upsertRows = getUpsertRows(tagMappings);
      if (upsertRows.length > 0 || removedTagValues.length > 0) {
        const result: TagMappingsSaveResult = await persistTagMappings(upsertRows, removedTagValues);
        if (result.transportError) {
          errors.push(`Tag mappings: ${result.transportError}`);
        } else if (result.validationErrors.length > 0) {
          const rowErr: Record<string, string> = {};
          for (const ve of result.validationErrors) rowErr[ve.tagValue] = ve.reason;
          setTagMappingRowErrors(rowErr);
          errors.push(`Tag mappings: ${result.validationErrors.length} of ${upsertRows.length} row(s) rejected — fix the flagged rows and save again.`);
        } else {
          // Fully successful — reconcile local rows to persisted, clear removals.
          setTagMappingRowErrors({});
          setTagMappings(prev => prev.map(r => ({ ...r, rowStatus: 'persisted' as const })));
          setRemovedTagValues([]);
        }
      }
    }

    // 4. Save dispatch
    try {
      await apiFetch('/config/dispatch', { method: 'POST', body: JSON.stringify(buildDispatchBody(dispatchMode, actionabilityFilter, customRules)) });
    } catch (e: any) { dispatchFailed = true; errors.push(`Dispatch window: ${parseApiError(e)}`); }

    // 5. Activate — SECURITY INVARIANT: skip activation if the
    // dispatch save failed, so we never silently activate under the wrong
    // (widest) dispatch mode.
    if (!dispatchFailed) {
      try {
        await apiFetch('/config/activate', { method: 'POST' });
      } catch (e: any) { /* activate may not exist yet — non-fatal */ }
    }

    // 6. Complete setup timer
    try {
      await apiFetch('/config/setup-timer/complete', { method: 'POST' });
      const t = await apiFetch('/config/setup-timer');
      setTimer(t);
    } catch (e: any) { /* non-fatal */ }

    setSaving(false);
    if (errors.length > 0) {
      setSaveErrors(errors);
    } else {
      onSave();
    }
  };

  // ServiceNow configured status
  const snowConfigured = config?.servicenow?.validated && !snowShowForm;

  // Connection status for review
  const renderConnectionStatus = () => {
    if (platform === 'jira') {
      if (jiraTestResult?.ok) return <StatusIndicator type="success">{jiraBaseUrl}</StatusIndicator>;
      if (config?.jira?.validated) return <StatusIndicator type="success">{config.jira.baseUrl}</StatusIndicator>;
      if (jiraBaseUrl) return <StatusIndicator type="warning">{jiraBaseUrl} (not tested)</StatusIndicator>;
      return <StatusIndicator type="stopped">Not configured</StatusIndicator>;
    }
    if (snowTestResult?.ok) return <StatusIndicator type="success">{snowInstanceUrl}</StatusIndicator>;
    if (config?.servicenow?.validated) return <StatusIndicator type="success">{config.servicenow.instanceUrl}</StatusIndicator>;
    if (snowInstanceUrl) return <StatusIndicator type="warning">{snowInstanceUrl} (not tested)</StatusIndicator>;
    return <StatusIndicator type="stopped">Not configured</StatusIndicator>;
  };

  // --- STEP CONTENT RENDERERS ---

  // Step 0: Platform Selection (multi-platform checkboxes)
  const renderPlatformStep = () => (
    <Container header={<Header variant="h3">Enable ITSM Platforms</Header>}>
      <SpaceBetween size="m">
        <Box variant="p">Select one or more ITSM platforms to create tickets in. Both can be enabled simultaneously.</Box>
        <Checkbox
          checked={enabledPlatforms.includes('jira')}
          onChange={({ detail }) => togglePlatform('jira', detail.checked)}
        >
          JIRA Cloud — Atlassian-hosted JIRA (*.atlassian.net)
        </Checkbox>
        <Checkbox
          checked={enabledPlatforms.includes('servicenow')}
          onChange={({ detail }) => togglePlatform('servicenow', detail.checked)}
        >
          ServiceNow — ServiceNow ITSM instance (*.service-now.com)
        </Checkbox>
        {enabledPlatforms.length === 0 && (
          <Alert type="warning">At least one platform must be enabled.</Alert>
        )}
      </SpaceBetween>
    </Container>
  );

  // Step 1: Connection — show forms for ALL enabled platforms
  const renderConnectionStep = () => (
    <SpaceBetween size="l">
      {enabledPlatforms.includes('jira') && (
        <Container header={<Header variant="h3">JIRA Connection</Header>}>
          <SpaceBetween size="m">
            <FormField label="JIRA Base URL"><Input value={jiraBaseUrl} onChange={e => setJiraBaseUrl(e.detail.value)} placeholder="https://yourorg.atlassian.net" /></FormField>
            <FormField label="JIRA Email"><Input value={jiraEmail} onChange={e => setJiraEmail(e.detail.value)} placeholder="automation@company.com" autoComplete="one-time-code" /></FormField>
            <FormField label="JIRA API Token" description="Stored in AWS Secrets Manager"><Input value={jiraApiToken} onChange={e => setJiraApiToken(e.detail.value)} type="password" autoComplete="one-time-code" /></FormField>
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={testJira} loading={jiraTesting}>Test Connection</Button>
              {jiraTestResult && (jiraTestResult.ok
                ? <StatusIndicator type="success">{jiraTestResult.msg}</StatusIndicator>
                : <StatusIndicator type="error">{jiraTestResult.msg}</StatusIndicator>)}
            </SpaceBetween>
            {config?.jira?.credentialsConfigured && <Alert type="info">Existing JIRA credentials configured. Leave fields blank to keep current credentials.</Alert>}
          </SpaceBetween>
        </Container>
      )}

      {enabledPlatforms.includes('servicenow') && (
        <Container header={<Header variant="h3">ServiceNow Connection</Header>}>
          {snowConfigured ? (
            <SpaceBetween size="m">
              <StatusIndicator type="success">Connected to: {config?.servicenow?.instanceUrl}</StatusIndicator>
              <Box variant="small">Auth type: OAuth 2.0 | Last validated: {config?.servicenow?.validatedAt || 'Unknown'}</Box>
              <Button variant="link" onClick={() => setSnowShowForm(true)}>Reconfigure</Button>
            </SpaceBetween>
          ) : (
            <SpaceBetween size="m">
              <FormField label="Instance URL" constraintText="Must be https:// and end with .service-now.com">
                <Input value={snowInstanceUrl} onChange={e => setSnowInstanceUrl(e.detail.value)} placeholder="https://yourorg.service-now.com" inputMode="url" />
              </FormField>
              <FormField label="Client ID" description="OAuth application Client ID">
                <Input value={snowClientId} onChange={e => setSnowClientId(e.detail.value)} placeholder="OAuth Client ID" autoComplete="one-time-code" />
              </FormField>
              <FormField label="Client Secret">
                <Input value={snowClientSecret} onChange={e => setSnowClientSecret(e.detail.value)} type="password" autoComplete="one-time-code" />
              </FormField>
              <FormField label="Username" description="Integration user (requires 'itil' role)">
                <Input value={snowUsername} onChange={e => setSnowUsername(e.detail.value)} placeholder="resolve_integration" autoComplete="one-time-code" />
              </FormField>
              <FormField label="Password">
                <Input value={snowPassword} onChange={e => setSnowPassword(e.detail.value)} type="password" autoComplete="one-time-code" />
              </FormField>
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={testServiceNow} loading={snowTesting} disabled={!snowAllFieldsFilled}>Test Connection</Button>
                {snowTestResult && (snowTestResult.ok
                  ? <StatusIndicator type="success">{snowTestResult.msg}</StatusIndicator>
                  : <StatusIndicator type="error">{snowTestResult.msg}</StatusIndicator>)}
              </SpaceBetween>
              {config?.servicenow?.validated && <Alert type="info">ServiceNow connection configured. Leave fields blank to keep current credentials.</Alert>}
            </SpaceBetween>
          )}
        </Container>
      )}

      {enabledPlatforms.length === 0 && (
        <Alert type="warning">No platforms enabled. Go back to Step 1 to enable at least one platform.</Alert>
      )}
    </SpaceBetween>
  );

  // Step 2: Routing — show fields for ALL enabled platforms
  const renderRoutingStep = () => (
    <SpaceBetween size="m">
      <Container header={<Header variant="h3">Default Routing (required)</Header>}>
        <SpaceBetween size="m">
          {enabledPlatforms.includes('jira') && (
            <FormField label="Default JIRA Project" description="Unmapped accounts route here (orphan queue)">
              <Input value={defaultProject} onChange={e => setDefaultProject(e.detail.value)} placeholder="UNASSIGNED" />
            </FormField>
          )}
          {enabledPlatforms.includes('servicenow') && (
            <SpaceBetween size="m">
              <FormField label="Default Assignment Group ID" description="32-character sys_id. Unmapped accounts route here.">
                <Input value={defaultSnowGroupId} onChange={e => setDefaultSnowGroupId(e.detail.value)} placeholder="a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6" />
              </FormField>
              <FormField label="Record Type">
                <Select
                  selectedOption={{ value: defaultSnowRecordType, label: defaultSnowRecordType === 'incident' ? 'Incident' : 'Change Request' }}
                  onChange={({ detail }) => setDefaultSnowRecordType(detail.selectedOption.value || 'change_request')}
                  options={[
                    { value: 'change_request', label: 'Change Request' },
                    { value: 'incident', label: 'Incident' },
                  ]}
                />
              </FormField>
            </SpaceBetween>
          )}
        </SpaceBetween>
      </Container>

      <Container header={<Header variant="h3" actions={
        <SpaceBetween direction="horizontal" size="xs">
          <Button onClick={loadOrgAccounts} loading={loadingOrg}>Load from Organizations</Button>
          <Button onClick={() => setBulkMode(!bulkMode)}>{bulkMode ? 'Manual Entry' : 'Bulk Import (CSV)'}</Button>
        </SpaceBetween>
      }>Account Mappings ({accountMappings.length})</Header>}>
        {bulkMode ? (
          <SpaceBetween size="s">
            <FormField label="Paste CSV" description={`Format: ${labels.bulkFormat}${enabledPlatforms.includes('servicenow') ? ',snow_assignment_group_id' : ''} (one per line)`}>
              <Textarea value={bulkText} onChange={e => setBulkText(e.detail.value)} rows={8} placeholder="111111111111,CLOUDOPS&#10;222222222222,APPTEAM" />
            </FormField>
            <Button onClick={parseBulk}>Parse & Preview</Button>
          </SpaceBetween>
        ) : (
          <SpaceBetween size="s">
            <SpaceBetween direction="horizontal" size="xs">
              <Input value={newAcct} onChange={e => setNewAcct(e.detail.value)} placeholder="Account ID (12 digits)" />
              <Input value={newProj} onChange={e => setNewProj(e.detail.value)} placeholder={labels.routingPlaceholder} />
              {enabledPlatforms.includes('servicenow') && (
                <Input value={newSnowGroup} onChange={e => setNewSnowGroup(e.detail.value)} placeholder="Assignment Group ID" />
              )}
              <Button onClick={() => { if (newAcct && (newProj || (enabledPlatforms.includes('servicenow') && newSnowGroup))) { setAccountMappings([...accountMappings, { account_id: newAcct, jira_project: newProj, ...(newSnowGroup ? { snow_assignment_group_id: newSnowGroup } : {}) }]); setNewAcct(''); setNewProj(''); setNewSnowGroup(''); } }}>Add</Button>
            </SpaceBetween>
            {accountMappings.length > 0 && (
              <Table items={accountMappings} columnDefinitions={[
                { id: 'acct', header: 'Account ID', cell: (m: any) => m.account_id },
                { id: 'name', header: 'Name', cell: (m: any) => m.account_name || '—' },
                { id: 'proj', header: labels.routingTarget, cell: (m: any) =>
                  <Input value={m.jira_project} onChange={e => {
                    const updated = [...accountMappings];
                    const idx = updated.findIndex(x => x.account_id === m.account_id);
                    if (idx >= 0) { updated[idx] = { ...updated[idx], jira_project: e.detail.value }; setAccountMappings(updated); }
                  }} placeholder="Project key" />
                },
                ...(enabledPlatforms.includes('servicenow') ? [{
                  id: 'snow', header: 'Assignment Group ID', cell: (m: any) =>
                    <Input value={m.snow_assignment_group_id || ''} onChange={(e: any) => {
                      const updated = [...accountMappings];
                      const idx = updated.findIndex(x => x.account_id === m.account_id);
                      if (idx >= 0) { updated[idx] = { ...updated[idx], snow_assignment_group_id: e.detail.value }; setAccountMappings(updated); }
                    }} placeholder="sys_id" />,
                }] : []),
                { id: 'remove', header: '', cell: (m: any) =>
                  <Button variant="icon" iconName="remove" onClick={() => setAccountMappings(accountMappings.filter(x => x.account_id !== m.account_id))} />
                },
              ]} />
            )}
          </SpaceBetween>
        )}
      </Container>

      <Container header={<Header variant="h3">Tag-Based Routing</Header>}>
        <SpaceBetween size="m">
          <Toggle checked={tagRoutingEnabled} onChange={({ detail }) => { setTagRoutingEnabled(detail.checked); setTagRoutingSaveState('unsaved'); }}>
            Enable tag-based routing
          </Toggle>
          {tagRoutingEnabled && (
            <>
              <FormField
                label="Tag Key"
                description="Resource or account tag key used for routing (e.g., Team, Owner)"
                errorText={!tagRoutingKey.trim() ? 'Tag key is required when tag routing is enabled.' : undefined}
              >
                <Input value={tagRoutingKey} onChange={e => { setTagRoutingKey(e.detail.value); setTagRoutingSaveState('unsaved'); }} placeholder="Team" />
              </FormField>
              <FormField label="Tag source" description={TAG_SOURCE_GROUP_DESCRIPTION}>
                <RadioGroup
                  value={tagSource}
                  onChange={({ detail }) => { setTagSource(normalizeTagSource(detail.value)); setTagRoutingSaveState('unsaved'); }}
                  items={TAG_SOURCE_ITEMS}
                />
              </FormField>
              <FormField
                label="Tag value mappings"
                description={`Map specific ${tagRoutingKey.trim() || 'tag'} values to JIRA projects. Events whose value matches a row route to that project; unmatched events fall through to account → default routing.`}
              >
                <TagRoutingMappingsEditor
                  enabled={tagRoutingEnabled}
                  tagKey={tagRoutingKey}
                  mappings={tagMappings}
                  onMappingsChange={setTagMappings}
                  removedTagValues={removedTagValues}
                  onRemovedChange={setRemovedTagValues}
                  rowErrors={tagMappingRowErrors}
                  loadError={tagMappingsLoadError}
                  onRetryLoad={loadTagMappings}
                  loading={tagMappingsLoading}
                  disabled={saving}
                />
              </FormField>
            </>
          )}
        </SpaceBetween>
      </Container>

      {routingValidating && <StatusIndicator type="loading">Validating routing targets…</StatusIndicator>}
      {routingValidationError && <Alert type="error">{routingValidationError}</Alert>}
      {Object.keys(routingValidation).length > 0 && !routingValidating && (
        <Container header={<Header variant="h3">Target Validation</Header>}>
          <SpaceBetween size="xs">
            {Object.entries(routingValidation).map(([target, result]) => (
              <StatusIndicator key={target} type={result.valid ? 'success' : 'error'}>
                {target}{result.valid ? ` — ${result.displayName || 'OK'}` : ` — ${result.error || 'Invalid'}`}
              </StatusIndicator>
            ))}
          </SpaceBetween>
        </Container>
      )}
    </SpaceBetween>
  );

  // Step 3: Dispatch Window
  const renderDispatchStep = () => (
    <Container>
      <SpaceBetween size="m">
        <FormField label="Which Health events should create tickets?">
          <RadioGroup value={dispatchMode} onChange={e => setDispatchMode(e.detail.value as DispatchMode)} items={[
            { value: 'all', label: 'All actionable events', description: 'Tickets for all scheduledChange + accountNotification with ACTION_REQUIRED or ACTION_MAY_BE_REQUIRED' },
            { value: 'ple_only', label: 'Planned Lifecycle Events only', description: 'Only AWS_*_PLANNED_LIFECYCLE_EVENT event types' },
            { value: 'custom', label: 'Custom rules', description: 'Define which event types create tickets' },
          ]} />
        </FormField>
        <FormField label="Actionability filter">
          <RadioGroup value={actionabilityFilter} onChange={({ detail }) => setActionabilityFilter(detail.value as ActionabilityFilter)} items={[
            { value: 'all_actionable', label: 'All actionable events (ACTION_REQUIRED + ACTION_MAY_BE_REQUIRED)' },
            { value: 'action_required_only', label: 'Only ACTION_REQUIRED events' },
          ]} />
        </FormField>
        {dispatchMode === 'custom' && (
          <SpaceBetween size="s">
            <SpaceBetween direction="horizontal" size="xs">
              <Input value={newPattern} onChange={e => setNewPattern(e.detail.value)} placeholder="AWS_EKS_*" />
              <Select
                selectedOption={DISPATCH_CATEGORY_OPTIONS.find(o => o.value === newCategory) || DISPATCH_CATEGORY_OPTIONS[0]}
                onChange={({ detail }) => setNewCategory(detail.selectedOption.value || 'scheduledChange')}
                options={DISPATCH_CATEGORY_OPTIONS}
                ariaLabel="Event category"
              />
              <Button onClick={() => {
                if (newPattern) {
                  setCustomRules([...customRules, { ruleId: `rule-${Date.now()}`, eventTypePattern: newPattern, eventCategories: selectValueToCategories(newCategory), enabled: true }]);
                  setNewPattern('');
                }
              }}>Add Rule</Button>
            </SpaceBetween>
            {customRules.length > 0 && (
              <TokenGroup items={customRules.map(r => ({ label: `${r.eventTypePattern} (${r.eventCategories.join(', ')})`, dismissLabel: 'Remove' }))}
                onDismiss={({ detail: d }) => setCustomRules(customRules.filter((_, i) => i !== d.itemIndex))} />
            )}
          </SpaceBetween>
        )}
      </SpaceBetween>
    </Container>
  );

  // Tag Routing summary row for the Review step.
  // The affirmative green "Enabled" appears ONLY when the strategy POST returned
  // 200 (tagRoutingSaveState === 'saved'); a failed or not-yet-persisted state
  // never claims "Enabled". Toggle-off always reads "Disabled" in every save
  // state, because no tag configuration is claimed.
  const renderTagRoutingSummary = () => {
    if (!tagRoutingEnabled) return 'Disabled';
    const n = tagMappings.length;
    const mappingClause = n > 0 ? ` · ${n} mapping${n === 1 ? '' : 's'}` : ' · no mappings yet';
    switch (tagRoutingSaveState) {
      case 'saving':
        return <StatusIndicator type="loading">Saving tag-routing strategy…</StatusIndicator>;
      case 'saved':
        return <StatusIndicator type="success">Enabled (key: {tagRoutingKey}, source: {TAG_SOURCE_LABEL[tagSource]}){mappingClause}</StatusIndicator>;
      case 'error':
        return <StatusIndicator type="error">Not enabled — strategy save failed. Fix the error above and click Save &amp; Activate again.</StatusIndicator>;
      case 'unsaved':
      default:
        if (!tagRoutingKey.trim()) {
          return <StatusIndicator type="warning">Enabled — tag key required before activation</StatusIndicator>;
        }
        return <StatusIndicator type="pending">Enabled (key: {tagRoutingKey}, source: {TAG_SOURCE_LABEL[tagSource]}){mappingClause} — will be activated on Save &amp; Activate</StatusIndicator>;
    }
  };

  // Step 4: Review & Activate
  const renderReviewStep = () => (
    <SpaceBetween size="m">
      {saveErrors.length > 0 && (
        <Alert type="error" header="Some configuration steps failed">
          <ul>{saveErrors.map((err, i) => <li key={i}>{err}</li>)}</ul>
        </Alert>
      )}
      <Container header={<Header variant="h3">Configuration Summary</Header>}>
        <ColumnLayout columns={2}>
          <div><Box variant="awsui-key-label">Enabled Platforms</Box>{enabledPlatforms.map(p => p === 'jira' ? 'JIRA Cloud' : 'ServiceNow').join(', ')}</div>
          <div><Box variant="awsui-key-label">Connection</Box>{renderConnectionStatus()}</div>
          <div><Box variant="awsui-key-label">Default JIRA Project</Box>{enabledPlatforms.includes('jira') ? (defaultProject || 'Not set') : 'N/A (disabled)'}</div>
          <div><Box variant="awsui-key-label">Default ServiceNow Group</Box>{enabledPlatforms.includes('servicenow') ? (defaultSnowGroupId ? `${defaultSnowGroupId} (${defaultSnowRecordType})` : 'Not set') : 'N/A (disabled)'}</div>
          <div><Box variant="awsui-key-label">Account Mappings</Box>{accountMappings.length} accounts</div>
          <div><Box variant="awsui-key-label">Tag Routing</Box>{renderTagRoutingSummary()}</div>
          <div><Box variant="awsui-key-label">Dispatch Window</Box>{dispatchMode === 'all' ? 'All actionable events' : dispatchMode === 'ple_only' ? 'PLEs only' : `${customRules.length} custom rules`}</div>
          <div><Box variant="awsui-key-label">Actionability</Box>{actionabilityFilter === 'all_actionable' ? 'ACTION_REQUIRED + ACTION_MAY_BE_REQUIRED' : 'ACTION_REQUIRED only'}</div>
        </ColumnLayout>
      </Container>
    </SpaceBetween>
  );

  return (
    <SpaceBetween size="m">
      {timer?.completed && (
        <Box variant="awsui-key-label">
          Setup completed in: {timer.durationMinutes} minutes
          {(timer.durationMinutes ?? 0) < 1440 ? ' ✓ Within target' : ' ⚠ Above target'}
        </Box>
      )}
      <Wizard
        i18nStrings={{
          stepNumberLabel: n => `Step ${n}`,
          collapsedStepsLabel: (s, t) => `Step ${s} of ${t}`,
          submitButton: 'Save & Activate',
          cancelButton: 'Cancel',
          nextButton: 'Next',
          previousButton: 'Previous',
        }}
        activeStepIndex={activeStep}
        onNavigate={async ({ detail }) => {
          // Validate routing targets when leaving Step 2 (Routing)
          if (activeStep === 2 && detail.requestedStepIndex > 2) {
            // Submit guard: block advancing past Routing when tag
            // routing is enabled but the tag key is blank (mirrors
            // RoutingEditModal.saveDisabled). The Tag Key FormField already shows
            // its errorText; this prevents a guaranteed 400 on Save & Activate.
            if (tagRoutingEnabled && !tagRoutingKey.trim()) return;
            // Block leaving Routing when tag mappings failed to load
            // (can't safely save) or have unresolved inline errors — mirrors the
            // tag-key guard; the editor already shows the inline reason.
            if (tagRoutingEnabled && tagMappingsLoadError) return;
            if (tagRoutingEnabled && hasTagMappingClientErrors(tagMappings)) return;
            const valid = await validateRoutingTargets();
            if (!valid) return;
          }
          setActiveStep(detail.requestedStepIndex);
        }}
        onSubmit={saveAll}
        isLoadingNextStep={saving || routingValidating}
        steps={[
          { title: 'Platform Selection', content: renderPlatformStep() },
          { title: 'Connection', content: renderConnectionStep() },
          { title: 'Routing', content: renderRoutingStep() },
          { title: 'Dispatch Window', content: renderDispatchStep() },
          { title: 'Review & Activate', content: renderReviewStep() },
        ]}
      />
    </SpaceBetween>
  );
}
