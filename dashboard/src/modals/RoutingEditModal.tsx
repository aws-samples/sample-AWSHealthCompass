import React, { useState, useEffect, useRef, useMemo, useCallback } from 'react';
import Modal from '@cloudscape-design/components/modal';
import Box from '@cloudscape-design/components/box';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Button from '@cloudscape-design/components/button';
import FormField from '@cloudscape-design/components/form-field';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import Input from '@cloudscape-design/components/input';
import Select from '@cloudscape-design/components/select';
import Table from '@cloudscape-design/components/table';
import Toggle from '@cloudscape-design/components/toggle';
import Alert from '@cloudscape-design/components/alert';
import RadioGroup from '@cloudscape-design/components/radio-group';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Pagination from '@cloudscape-design/components/pagination';
import TextFilter from '@cloudscape-design/components/text-filter';
import Textarea from '@cloudscape-design/components/textarea';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Spinner from '@cloudscape-design/components/spinner';
import { apiFetch } from '../api';
import { APP_NAME } from '../branding';
import TagRoutingMappingsEditor from '../components/TagRoutingMappingsEditor';
import { TagMappingRow, TagMappingsSaveResult, getUpsertRows, hasTagMappingClientErrors, persistTagMappings } from '../components/tagMappings';

// --- Types ---

interface AccountMapping {
  account_id: string;
  account_name: string;
  jira_project: string;
  snow_assignment_group_id: string;
}

interface ValidationResult {
  target: string;
  valid: boolean;
  displayName?: string;
  error?: string;
  platform?: string;
}

interface RoutingEditModalProps {
  visible: boolean;
  onDismiss: () => void;
  onSave: () => void;
}

// --- Constants ---

const PAGE_SIZE = 20;
const VALIDATE_COOLDOWN_MS = 10000;

const RECORD_TYPE_OPTIONS = [
  { label: 'Change Request', value: 'change_request' },
  { label: 'Incident', value: 'incident' },
];

// STORY-124 (RT-02): tag-source selector. Values are sent verbatim as `tagSource`
// and match the backend _VALID_TAG_SOURCES exactly — no transform layer. Resource
// listed FIRST for discoverability (the marquee capability); default selection
// stays 'account' (engine-aligned; zero behavior change on re-save).
const TAG_SOURCE_ITEMS = [
  { value: 'resource', label: 'Resource tags', description: "Read the routing tag from each affected resource's own tags (resourceTags). Best for per-workload / per-team ownership. If a resource has no value for this tag, routing falls back to the account-ID mapping — the event is never dropped." },
  { value: 'account', label: 'Account tags', description: "Read the routing tag from the AWS account's tags (accountTags, from AWS Organizations). If the account has no value for this tag, routing falls back to the account-ID mapping." },
  { value: 'both', label: 'Both (resource, then account)', description: 'Prefer the resource tag; if the resource has no value, use the account tag. If neither is present, routing falls back to the account-ID mapping.' },
];

const TAG_SOURCE_GROUP_DESCRIPTION =
  `Choose where ${APP_NAME} reads the routing tag. If the selected source has no value for an event, routing automatically falls back to the account-ID mapping, then the default project — no event is dropped.`;

/** Normalize a stored tag source to a valid selector value (SR-018-06: unknown -> account). */
function normalizeTagSource(raw: unknown): 'resource' | 'account' | 'both' {
  return raw === 'resource' || raw === 'both' ? raw : 'account';
}

// --- Helpers ---

function isValidAccountId(id: string): { valid: boolean; error?: string } {
  if (!id) return { valid: false, error: 'Account ID is required' };
  if (!/^\d{12}$/.test(id)) return { valid: false, error: 'Account ID must be exactly 12 digits' };
  return { valid: true };
}

function parseCSV(text: string, snowEnabled: boolean): { valid: AccountMapping[]; invalid: { raw: string; error: string }[] } {
  const lines = text.trim().split('\n').filter(l => l.trim());
  if (lines.length === 0) return { valid: [], invalid: [] };

  // Skip header if present
  const firstLine = lines[0].toLowerCase();
  const startIdx = (firstLine.includes('account') || firstLine.includes('id')) ? 1 : 0;

  const valid: AccountMapping[] = [];
  const invalid: { raw: string; error: string }[] = [];
  const seen = new Map<string, number>();

  for (let i = startIdx; i < lines.length; i++) {
    const parts = lines[i].split(',').map(s => s.trim());
    const [accountId, jiraProject, snowGroup] = parts;

    const check = isValidAccountId(accountId || '');
    if (!check.valid) {
      invalid.push({ raw: lines[i], error: check.error! });
      continue;
    }
    if (!jiraProject) {
      invalid.push({ raw: lines[i], error: 'JIRA project is required' });
      continue;
    }

    const mapping: AccountMapping = {
      account_id: accountId,
      account_name: '',
      jira_project: jiraProject,
      snow_assignment_group_id: snowGroup || '',
    };

    if (seen.has(accountId)) {
      valid[seen.get(accountId)!] = mapping;
    } else {
      seen.set(accountId, valid.length);
      valid.push(mapping);
    }
  }

  return { valid, invalid };
}

function parseJSON(text: string): { valid: AccountMapping[]; invalid: { raw: string; error: string }[] } {
  let arr: any[];
  try {
    arr = JSON.parse(text);
  } catch {
    return { valid: [], invalid: [{ raw: text.substring(0, 80), error: 'Invalid JSON format' }] };
  }
  if (!Array.isArray(arr)) {
    return { valid: [], invalid: [{ raw: '', error: 'Expected a JSON array' }] };
  }

  const valid: AccountMapping[] = [];
  const invalid: { raw: string; error: string }[] = [];
  const seen = new Map<string, number>();

  for (const item of arr) {
    const accountId = item.account_id || item.accountId || '';
    const jiraProject = item.jira_project || item.jiraProject || '';
    const snowGroup = item.snow_assignment_group_id || item.snowAssignmentGroupId || '';

    const check = isValidAccountId(accountId);
    if (!check.valid) {
      invalid.push({ raw: JSON.stringify(item).substring(0, 60), error: check.error! });
      continue;
    }
    if (!jiraProject) {
      invalid.push({ raw: JSON.stringify(item).substring(0, 60), error: 'JIRA project is required' });
      continue;
    }

    const mapping: AccountMapping = { account_id: accountId, account_name: '', jira_project: jiraProject, snow_assignment_group_id: snowGroup };
    if (seen.has(accountId)) {
      valid[seen.get(accountId)!] = mapping;
    } else {
      seen.set(accountId, valid.length);
      valid.push(mapping);
    }
  }

  return { valid, invalid };
}

function parseApiError(error: unknown): string {
  if (!(error instanceof Error)) return 'An unexpected error occurred.';
  const msg = error.message;
  const match = msg.match(/^API (\d+): (.+)$/s);
  if (!match) return 'Unable to reach the server. Check your network connection.';
  const status = parseInt(match[1], 10);
  const body = match[2].substring(0, 200);
  if (status === 400) return body;
  if (status === 403) return "You don't have permission to modify this configuration.";
  if (status === 429) return 'Too many requests. Please wait and try again.';
  if (status >= 500) return 'An unexpected error occurred. Please try again.';
  return body;
}

// --- STORY-139 (§2.5): structured SNOW routing error-code surfacing ---
//
// Presentation-only. Consumes STORY-137's structured error contract:
//   - Default-save 400: top-level { error: { code, message } }
//   - Per-row (account save / import preview) HTTP 200: data.validationErrors[]
//     / data.invalid[], each row { accountId?, field, code, message }
// All strings and sys_ids are treated as UNTRUSTED TEXT and rendered ONLY in
// text positions (Alert body / FormField errorText / Table cell / Box) — never
// as HTML, and never interpolated into href/onClick (SR-139-1/2/3).

/** Recognized SNOW routing error codes (STORY-137). */
interface RoutingErrorInfo {
  code: string;
  message: string;
  /** Optional per-row account association for placement. */
  accountId?: string;
  /** Optional field association (e.g. 'snowAssignmentGroupId'). */
  field?: string;
}

/**
 * Extract the JSON body from an apiFetch Error("API {status}: {body}") string
 * and attempt to recognize STORY-137's top-level `{ error: { code, message } }`
 * shape. Returns null when the body is absent or unrecognized (caller falls
 * back to the raw-string parseApiError behavior — unchanged).
 */
function parseRoutingError(error: unknown): RoutingErrorInfo | null {
  if (!(error instanceof Error)) return null;
  const match = error.message.match(/^API (\d+): (.+)$/s);
  if (!match) return null;
  try {
    const body = JSON.parse(match[2]);
    const err = body?.error;
    if (err && typeof err.code === 'string') {
      // SR-139-4: render ONLY the intended user-facing message field.
      return { code: err.code, message: typeof err.message === 'string' ? err.message : '' };
    }
  } catch {
    // Not JSON / not the recognized shape — fall back to raw handling.
  }
  return null;
}

/**
 * Collect per-row SNOW validation errors from a HTTP-200 account-save / import
 * response. STORY-137 returns them under data.validationErrors[] or
 * data.invalid[] with { accountId?, field, code, message }. Only rows carrying
 * a recognized message are returned. Presentation-only.
 */
function collectRowRoutingErrors(resp: any): RoutingErrorInfo[] {
  const rows: any[] = [
    ...(resp?.data?.validationErrors ?? resp?.validationErrors ?? []),
    ...(resp?.data?.invalid ?? resp?.invalid ?? []),
  ];
  const out: RoutingErrorInfo[] = [];
  for (const r of rows) {
    if (r && typeof r.code === 'string') {
      out.push({
        code: r.code,
        message: typeof r.message === 'string' ? r.message : (typeof r.error === 'string' ? r.error : ''),
        accountId: typeof r.accountId === 'string' ? r.accountId : undefined,
        field: typeof r.field === 'string' ? r.field : undefined,
      });
    }
  }
  return out;
}

// --- Component ---

export default function RoutingEditModal({ visible, onDismiss, onSave }: RoutingEditModalProps) {
  // --- Loading state ---
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  // --- Platform awareness ---
  const [jiraEnabled, setJiraEnabled] = useState(true);
  const [snowEnabled, setSnowEnabled] = useState(false);

  // --- Section A: Default routing ---
  const [defaultProject, setDefaultProject] = useState('');
  const [defaultSnowGroupId, setDefaultSnowGroupId] = useState('');
  const [defaultSnowRecordType, setDefaultSnowRecordType] = useState('change_request');

  // --- Section B: Account mappings ---
  const [accountMappings, setAccountMappings] = useState<AccountMapping[]>([]);
  const [filterText, setFilterText] = useState('');
  const [currentPage, setCurrentPage] = useState(1);

  // --- Section B: Add row ---
  const [newAcctId, setNewAcctId] = useState('');
  const [newAcctName, setNewAcctName] = useState('');
  const [newJiraProject, setNewJiraProject] = useState('');
  const [newSnowGroup, setNewSnowGroup] = useState('');
  const [addError, setAddError] = useState('');

  // --- Section B: Bulk import ---
  const [bulkMode, setBulkMode] = useState(false);
  const [bulkFormat, setBulkFormat] = useState<'csv' | 'json'>('csv');
  const [bulkText, setBulkText] = useState('');
  const [bulkPreview, setBulkPreview] = useState<{ valid: AccountMapping[]; invalid: { raw: string; error: string }[] } | null>(null);
  const [bulkConfirmVisible, setBulkConfirmVisible] = useState(false);

  // --- Section B: Org discovery ---
  const [loadingOrg, setLoadingOrg] = useState(false);
  const [orgError, setOrgError] = useState<string | null>(null);

  // --- Section C: Tag routing ---
  const [tagRoutingEnabled, setTagRoutingEnabled] = useState(false);
  const [tagRoutingKey, setTagRoutingKey] = useState('');
  // STORY-124 (RT-02): which source the routing tag is read from. Default
  // 'account' matches the engine/handler default; always visible when tag
  // routing is enabled so the value is never silently persisted. Excluded from
  // saveDisabled (always valid); included in the dirty snapshot.
  const [tagSource, setTagSource] = useState<'resource' | 'account' | 'both'>('account');
  // STORY-125 (RT-03): tag value → routing-target mapping editor state. The
  // modal keeps its DOM mounted while visible, so lifted host state (mirroring
  // accountMappings) is used for a single controlled contract across surfaces.
  const [tagMappings, setTagMappings] = useState<TagMappingRow[]>([]);
  const [removedTagValues, setRemovedTagValues] = useState<string[]>([]);
  const [tagMappingRowErrors, setTagMappingRowErrors] = useState<Record<string, string>>({});

  // --- Section D: Validation ---
  const [validating, setValidating] = useState(false);
  const [validationResults, setValidationResults] = useState<ValidationResult[] | null>(null);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [validateCooldown, setValidateCooldown] = useState(false);

  // --- Save state ---
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  // --- STORY-139 (§2.5): structured SNOW error surfacing state ---
  // Populated from parseRoutingError / collectRowRoutingErrors on save; reset
  // to null/{} on a clean save. All values render as inert text (SR-139-1/2/3).
  const [defaultSnowGroupError, setDefaultSnowGroupError] = useState<string | null>(null);
  const [defaultSnowRecordTypeError, setDefaultSnowRecordTypeError] = useState<string | null>(null);
  const [snowConnectionError, setSnowConnectionError] = useState<string | null>(null);
  const [rowSnowErrors, setRowSnowErrors] = useState<Record<string, string>>({});

  // --- Discard confirm ---
  const [showDiscardConfirm, setShowDiscardConfirm] = useState(false);

  // --- Dirty detection snapshot ---
  const initialSnapshot = useRef<string>('');

  // --- Load data on modal open ---
  const loadData = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const [routingResp, summaryResp, tagsResp] = await Promise.all([
        apiFetch('/config/routing'),
        apiFetch('/config/summary'),
        apiFetch('/config/routing/tags'),
      ]);

      // Determine platforms
      const platforms: string[] = summaryResp.platforms || [summaryResp.platform || 'jira'];
      setJiraEnabled(platforms.includes('jira'));
      setSnowEnabled(platforms.includes('servicenow'));

      // Extract routing data (handle nested .data or flat response)
      const rData = routingResp?.data ?? routingResp;
      const mappings: AccountMapping[] = (rData?.accounts ?? []).map((m: any) => ({
        account_id: m.account_id || m.accountId || '',
        account_name: m.account_name || m.accountName || '',
        jira_project: m.jira_project || m.jiraProject || '',
        snow_assignment_group_id: m.snow_assignment_group_id || m.snowAssignmentGroupId || '',
      }));

      const dp = rData?.defaultProject || rData?.default?.jiraProject || summaryResp?.routing?.defaultProject || '';
      const dsg = rData?.snowAssignmentGroupId || rData?.default?.snowAssignmentGroupId || (summaryResp?.routing as any)?.snowAssignmentGroupId || '';
      const dsrt = rData?.snowRecordType || rData?.default?.snowRecordType || (summaryResp?.routing as any)?.snowRecordType || 'change_request';
      const tre = summaryResp?.routing?.tagRouting?.enabled ?? false;
      const trk = summaryResp?.routing?.tagRouting?.tagKey ?? '';
      // STORY-124 (RT-02): read the persisted tag source; unknown/legacy/empty
      // normalizes to 'account' (SR-018-06 alignment) without error.
      const trs = normalizeTagSource(summaryResp?.routing?.tagRouting?.tagSource);

      setDefaultProject(dp);
      setDefaultSnowGroupId(dsg);
      setDefaultSnowRecordType(dsrt);
      setAccountMappings(mappings);
      setTagRoutingEnabled(tre);
      setTagRoutingKey(trk);
      setTagSource(trs);

      // STORY-125 (RT-03): seed persisted tag→target mappings for round-trip.
      const tagRows: TagMappingRow[] = (tagsResp?.mappings ?? []).map((m: any) => ({
        tagValue: m.tagValue ?? '',
        jiraProject: m.jiraProject ?? '',
        jiraIssueType: m.jiraIssueType ?? 'Task',
        rowStatus: 'persisted' as const,
      }));
      setTagMappings(tagRows);
      setRemovedTagValues([]);
      setTagMappingRowErrors({});

      // Reset transient state
      setFilterText('');
      setCurrentPage(1);
      setNewAcctId(''); setNewAcctName(''); setNewJiraProject(''); setNewSnowGroup('');
      setAddError('');
      setBulkMode(false); setBulkText(''); setBulkPreview(null); setBulkConfirmVisible(false);
      setOrgError(null);
      setValidationResults(null); setValidationError(null);
      setSaveError(null);
      setDefaultSnowGroupError(null);
      setDefaultSnowRecordTypeError(null);
      setSnowConnectionError(null);
      setRowSnowErrors({});
      setShowDiscardConfirm(false);

      // Snapshot for dirty detection
      initialSnapshot.current = JSON.stringify({
        defaultProject: dp, defaultSnowGroupId: dsg, defaultSnowRecordType: dsrt,
        accountMappings: mappings, tagRoutingEnabled: tre, tagRoutingKey: trk, tagSource: trs,
        tagMappings: tagRows, removedTagValues: [],
      });
    } catch (err: unknown) {
      setLoadError(parseApiError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!visible) return;
    loadData();
  }, [visible, loadData]);

  // --- Derived values ---

  const isDirty = useMemo(() => {
    const current = JSON.stringify({
      defaultProject, defaultSnowGroupId, defaultSnowRecordType,
      accountMappings, tagRoutingEnabled, tagRoutingKey, tagSource,
      tagMappings, removedTagValues,
    });
    return current !== initialSnapshot.current;
  }, [defaultProject, defaultSnowGroupId, defaultSnowRecordType, accountMappings, tagRoutingEnabled, tagRoutingKey, tagSource, tagMappings, removedTagValues]);

  const saveDisabled = useMemo(() => {
    if (loadError) return true;
    if (loading) return true;
    if (jiraEnabled && !defaultProject.trim()) return true;
    if (snowEnabled && !defaultSnowGroupId.trim()) return true;
    if (tagRoutingEnabled && !tagRoutingKey.trim()) return true;
    // STORY-125: block save while any tag-mapping row has an unresolved
    // client-side error (empty/invalid project or invalid tag value).
    if (tagRoutingEnabled && hasTagMappingClientErrors(tagMappings)) return true;
    return false;
  }, [loadError, loading, jiraEnabled, snowEnabled, defaultProject, defaultSnowGroupId, tagRoutingEnabled, tagRoutingKey, tagMappings]);

  // Filtered + paginated mappings
  const filteredMappings = useMemo(() => {
    if (!filterText.trim()) return accountMappings;
    const lower = filterText.toLowerCase();
    return accountMappings.filter(m =>
      m.account_id.toLowerCase().includes(lower) ||
      m.account_name.toLowerCase().includes(lower) ||
      m.jira_project.toLowerCase().includes(lower) ||
      m.snow_assignment_group_id.toLowerCase().includes(lower)
    );
  }, [accountMappings, filterText]);

  const totalPages = Math.ceil(filteredMappings.length / PAGE_SIZE);
  const paginatedMappings = useMemo(() => {
    const start = (currentPage - 1) * PAGE_SIZE;
    return filteredMappings.slice(start, start + PAGE_SIZE);
  }, [filteredMappings, currentPage]);

  // --- Handlers ---

  const handleAddMapping = () => {
    setAddError('');
    const check = isValidAccountId(newAcctId.trim());
    if (!check.valid) { setAddError(check.error!); return; }
    if (!newJiraProject.trim() && jiraEnabled) { setAddError('JIRA project is required'); return; }
    // STORY-139 (§2.3): symmetric SNOW-only add-row guard. Without this, a
    // SNOW-only user could add a mapping with an empty assignment group that
    // then fails opaquely at save. Client-side convenience check only — the
    // server (STORY-137) remains authoritative (SR-139 note in 04_snape).
    if (snowEnabled && !jiraEnabled && !newSnowGroup.trim()) {
      setAddError('Assignment group sys_id is required'); return;
    }

    const existing = accountMappings.findIndex(m => m.account_id === newAcctId.trim());
    const newMapping: AccountMapping = {
      account_id: newAcctId.trim(),
      account_name: newAcctName.trim(),
      jira_project: newJiraProject.trim(),
      snow_assignment_group_id: newSnowGroup.trim(),
    };

    if (existing >= 0) {
      const updated = [...accountMappings];
      updated[existing] = newMapping;
      setAccountMappings(updated);
    } else {
      setAccountMappings([...accountMappings, newMapping]);
    }
    setNewAcctId(''); setNewAcctName(''); setNewJiraProject(''); setNewSnowGroup('');
  };

  const handleRemoveMapping = (accountId: string) => {
    setAccountMappings(accountMappings.filter(m => m.account_id !== accountId));
  };

  const handleUpdateMapping = (accountId: string, field: keyof AccountMapping, value: string) => {
    setAccountMappings(accountMappings.map(m =>
      m.account_id === accountId ? { ...m, [field]: value } : m
    ));
  };

  // --- Bulk Import ---

  const handleBulkParse = () => {
    if (!bulkText.trim()) return;
    const result = bulkFormat === 'csv' ? parseCSV(bulkText, snowEnabled) : parseJSON(bulkText);
    setBulkPreview(result);
  };

  const handleBulkConfirm = () => {
    if (!bulkPreview) return;
    setAccountMappings(bulkPreview.valid);
    setBulkMode(false);
    setBulkPreview(null);
    setBulkText('');
    setBulkConfirmVisible(false);
    setCurrentPage(1);
  };

  // --- Org Discovery ---

  const handleLoadOrganizations = async () => {
    setLoadingOrg(true);
    setOrgError(null);
    try {
      const resp = await apiFetch('/config/routing/discover', { method: 'POST' });
      const accounts: { accountId: string; accountName: string }[] = resp.accounts || [];
      if (accounts.length === 0) {
        setOrgError('No accounts found. Ensure AWS Organizations is enabled.');
        return;
      }
      // Merge: only add accounts that don't already exist
      const existingIds = new Set(accountMappings.map(m => m.account_id));
      const newAccounts = accounts
        .filter(a => !existingIds.has(a.accountId))
        .map(a => ({ account_id: a.accountId, account_name: a.accountName, jira_project: '', snow_assignment_group_id: '' }));

      if (newAccounts.length === 0) {
        setOrgError(`All ${accounts.length} accounts are already in your mappings.`);
      } else {
        setAccountMappings([...accountMappings, ...newAccounts]);
        setOrgError(null);
      }
    } catch (err: unknown) {
      setOrgError(parseApiError(err));
    } finally {
      setLoadingOrg(false);
    }
  };

  // --- Validation (SEC-098-M1: 10-second cooldown) ---

  const handleValidate = async () => {
    setValidating(true);
    setValidationResults(null);
    setValidationError(null);
    setValidateCooldown(true);

    // Collect unique targets
    const jiraTargets = new Set<string>();
    const snowTargets = new Set<string>();
    if (jiraEnabled && defaultProject.trim()) jiraTargets.add(defaultProject.trim());
    if (snowEnabled && defaultSnowGroupId.trim()) snowTargets.add(defaultSnowGroupId.trim());
    for (const m of accountMappings) {
      if (m.jira_project.trim()) jiraTargets.add(m.jira_project.trim());
      if (m.snow_assignment_group_id.trim()) snowTargets.add(m.snow_assignment_group_id.trim());
    }

    const targets = [
      ...Array.from(jiraTargets).map(t => ({ type: 'jira_project', value: t })),
      ...Array.from(snowTargets).map(t => ({ type: 'snow_group', value: t })),
    ];

    if (targets.length === 0) {
      setValidating(false);
      setTimeout(() => setValidateCooldown(false), VALIDATE_COOLDOWN_MS);
      return;
    }

    try {
      const resp = await apiFetch('/config/routing/validate', {
        method: 'POST',
        body: JSON.stringify({ targets }),
      });
      setValidationResults(resp.results ?? []);
    } catch (err: unknown) {
      setValidationError(parseApiError(err));
    } finally {
      setValidating(false);
      setTimeout(() => setValidateCooldown(false), VALIDATE_COOLDOWN_MS);
    }
  };

  // --- Save ---

  const handleSave = async () => {
    setSaving(true);
    setSaveError(null);
    // STORY-139 (§2.5): clear prior structured SNOW errors before this attempt.
    setDefaultSnowGroupError(null);
    setDefaultSnowRecordTypeError(null);
    setSnowConnectionError(null);
    setRowSnowErrors({});
    const failures: string[] = [];
    // Accumulate structured SNOW errors so a clean save can be distinguished
    // from one that surfaced field/row/section errors.
    let structuredHandled = false;
    const nextRowSnowErrors: Record<string, string> = {};

    // Step 1: Default routing
    try {
      const body: any = {};
      if (jiraEnabled) { body.jiraProject = defaultProject.trim(); body.jiraIssueType = 'Task'; }
      if (snowEnabled) { body.snowAssignmentGroupId = defaultSnowGroupId.trim(); body.snowRecordType = defaultSnowRecordType; }
      await apiFetch('/config/routing/default', { method: 'POST', body: JSON.stringify(body) });
    } catch (e: unknown) {
      // STORY-139 (§2.5): map STORY-137 top-level { error: { code, message } }
      // to the correct control. Falls back to the raw-string blob when the
      // body is unrecognized (unchanged behavior).
      const structured = parseRoutingError(e);
      if (structured) {
        structuredHandled = true;
        switch (structured.code) {
          case 'CFG_SNOW_NOT_CONFIGURED':
            setSnowConnectionError(structured.message);
            break;
          case 'CFG_INVALID_SNOW_RECORD_TYPE':
            setDefaultSnowRecordTypeError(structured.message);
            break;
          case 'CFG_INVALID_SNOW_GROUP_ID':
          case 'CFG_INVALID_SNOW_GROUP_NAME':
          case 'CFG_SNOW_GROUP_NOT_FOUND':
            setDefaultSnowGroupError(structured.message);
            break;
          default:
            // Unrecognized structured code — surface via the generic blob.
            failures.push(`Default routing: ${structured.message || parseApiError(e)}`);
        }
      } else {
        failures.push(`Default routing: ${parseApiError(e)}`);
      }
    }

    // Step 2+3: Account mappings import + confirm
    try {
      const validMappings = accountMappings
        .filter(m => m.account_id && (m.jira_project || m.snow_assignment_group_id))
        .map(m => ({ accountId: m.account_id, jiraProject: m.jira_project, snowAssignmentGroupId: m.snow_assignment_group_id || undefined }));

      const importPayload = { format: 'json', data: JSON.stringify(validMappings) };
      const importResp = await apiFetch('/config/routing/import', { method: 'POST', body: JSON.stringify(importPayload) });
      // STORY-139 (§2.5): per-row SNOW failures surface at HTTP 200 in the
      // import response's validationErrors[]/invalid[] — capture them for
      // inline placement keyed by accountId + snow-group field.
      for (const re of collectRowRoutingErrors(importResp)) {
        const isSnowField = re.field === 'snowAssignmentGroupId' ||
          re.code === 'CFG_SNOW_GROUP_NOT_FOUND' ||
          re.code === 'CFG_INVALID_SNOW_GROUP_ID' ||
          re.code === 'CFG_INVALID_SNOW_GROUP_NAME';
        if (re.accountId && isSnowField && re.message) {
          nextRowSnowErrors[re.accountId] = re.message;
          structuredHandled = true;
        }
      }
      if (importResp?.importId) {
        const confirmResp = await apiFetch('/config/routing/import/confirm', { method: 'POST', body: JSON.stringify({ importId: importResp.importId }) });
        for (const re of collectRowRoutingErrors(confirmResp)) {
          const isSnowField = re.field === 'snowAssignmentGroupId' ||
            re.code === 'CFG_SNOW_GROUP_NOT_FOUND' ||
            re.code === 'CFG_INVALID_SNOW_GROUP_ID' ||
            re.code === 'CFG_INVALID_SNOW_GROUP_NAME';
          if (re.accountId && isSnowField && re.message) {
            nextRowSnowErrors[re.accountId] = re.message;
            structuredHandled = true;
          }
        }
      }
    } catch (e: unknown) {
      const structured = parseRoutingError(e);
      if (structured && structured.code === 'CFG_SNOW_NOT_CONFIGURED') {
        setSnowConnectionError(structured.message);
        structuredHandled = true;
      } else {
        failures.push(`Account mappings: ${structured?.message || parseApiError(e)}`);
      }
    }

    // Step 4: Tag routing strategy
    try {
      await apiFetch('/config/routing/strategy', {
        method: 'POST',
        body: JSON.stringify({ mode: tagRoutingEnabled ? 'tag' : 'account', tagKey: tagRoutingEnabled ? tagRoutingKey.trim() : undefined, tagSource: tagRoutingEnabled ? tagSource : undefined }),
      });
    } catch (e: unknown) { failures.push(`Tag routing: ${parseApiError(e)}`); }

    // Step 5: Tag→target mappings (STORY-125 / RT-03) — sequenced AFTER the
    // strategy save. persistTagMappings runs DELETEs first then one upsert POST.
    // Only runs when tag routing is enabled (disabling never deletes mappings).
    // A partial/failed result appends to failures[] and flags rows so the modal
    // stays open (does not call onSave); a clean result reconciles local rows.
    if (tagRoutingEnabled) {
      const upsertRows = getUpsertRows(tagMappings);
      if (upsertRows.length > 0 || removedTagValues.length > 0) {
        const result: TagMappingsSaveResult = await persistTagMappings(upsertRows, removedTagValues);
        if (result.transportError) {
          failures.push(`Tag mappings: ${result.transportError}`);
        } else if (result.validationErrors.length > 0) {
          const rowErr: Record<string, string> = {};
          for (const ve of result.validationErrors) rowErr[ve.tagValue] = ve.reason;
          setTagMappingRowErrors(rowErr);
          failures.push(`Tag mappings: ${result.validationErrors.length} of ${upsertRows.length} row(s) rejected — fix the flagged rows and save again.`);
        } else {
          setTagMappingRowErrors({});
          setTagMappings(prev => prev.map(r => ({ ...r, rowStatus: 'persisted' as const })));
          setRemovedTagValues([]);
        }
      }
    }

    setSaving(false);

    // STORY-139 (§2.5): commit any collected per-row SNOW errors.
    setRowSnowErrors(nextRowSnowErrors);

    if (failures.length > 0) {
      setSaveError(failures.join('\n'));
    } else if (structuredHandled) {
      // Field/row/section SNOW errors were surfaced on their controls — keep the
      // modal open so the user can correct and re-save. Do NOT call onSave.
    } else {
      onSave();
    }
  };

  // --- Cancel / Dismiss ---

  const handleDismiss = () => {
    if (saving) return;
    if (isDirty) {
      setShowDiscardConfirm(true);
    } else {
      onDismiss();
    }
  };

  const handleDiscardConfirm = () => {
    setShowDiscardConfirm(false);
    onDismiss();
  };

  // --- Table column definitions ---

  const columnDefinitions = useMemo(() => {
    const cols: any[] = [
      {
        id: 'account_id',
        header: 'Account ID',
        cell: (item: AccountMapping) => <Box variant="code">{item.account_id}</Box>,
        width: 150,
      },
      {
        id: 'account_name',
        header: 'Account Name',
        cell: (item: AccountMapping) => item.account_name || '—',
        width: 150,
      },
    ];

    if (jiraEnabled) {
      cols.push({
        id: 'jira_project',
        header: 'JIRA Project',
        cell: (item: AccountMapping) => (
          <Input
            value={item.jira_project}
            onChange={({ detail }) => handleUpdateMapping(item.account_id, 'jira_project', detail.value)}
            placeholder="PROJECT"
            ariaLabel={`JIRA project for account ${item.account_id}`}
          />
        ),
        width: 150,
      });
    }

    if (snowEnabled) {
      cols.push({
        id: 'snow_group',
        header: 'ServiceNow Group',
        cell: (item: AccountMapping) => (
          <FormField errorText={rowSnowErrors[item.account_id] || undefined}>
            <Input
              value={item.snow_assignment_group_id}
              onChange={({ detail }) => handleUpdateMapping(item.account_id, 'snow_assignment_group_id', detail.value)}
              placeholder="sys_id"
              ariaLabel={`ServiceNow group for account ${item.account_id}`}
            />
          </FormField>
        ),
        width: 180,
      });
    }

    cols.push({
      id: 'actions',
      header: '',
      cell: (item: AccountMapping) => (
        <Button
          variant="icon"
          iconName="remove"
          ariaLabel={`Remove mapping for account ${item.account_id}`}
          onClick={() => handleRemoveMapping(item.account_id)}
        />
      ),
      width: 60,
    });

    return cols;
  }, [jiraEnabled, snowEnabled, accountMappings, rowSnowErrors]);

  // --- Render ---

  return (
    <>
      <Modal
        visible={visible}
        onDismiss={saving ? () => {} : handleDismiss}
        closeAriaLabel="Close"
        size="max"
        data-testid="routing-modal"
        header="Edit Routing Rules"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                onClick={handleDismiss}
                disabled={saving}
                data-testid="cancel-routing"
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleSave}
                loading={saving}
                disabled={saveDisabled}
                data-testid="save-routing"
              >
                Save Changes
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        {loading ? (
          <Box textAlign="center" padding="l"><Spinner size="large" /></Box>
        ) : loadError ? (
          <Alert
            type="error"
            action={<Button variant="normal" onClick={loadData}>Retry</Button>}
          >
            Failed to load routing configuration. Changes cannot be saved until data loads successfully.
          </Alert>
        ) : (
          <SpaceBetween size="l">
            {/* Save error */}
            {saveError && (
              <Alert type="error" dismissible onDismiss={() => setSaveError(null)}>
                {saveError}
              </Alert>
            )}

            {/* STORY-139 (§2.5): section-level ServiceNow precondition failure
                (CFG_SNOW_NOT_CONFIGURED). Rendered as inert text (SR-139-1/2). */}
            {snowConnectionError && (
              <Alert type="warning" dismissible onDismiss={() => setSnowConnectionError(null)}>
                {snowConnectionError}
              </Alert>
            )}

            {/* Section A: Default Routing */}
            <Container header={<Header variant="h3">Default Routing (required)</Header>}>
              <ColumnLayout columns={jiraEnabled && snowEnabled ? 2 : 1}>
                {jiraEnabled && (
                  <FormField
                    label="Default JIRA Project"
                    constraintText="Unmapped accounts route to this project (orphan queue)"
                    errorText={jiraEnabled && !defaultProject.trim() ? 'Default JIRA project is required' : undefined}
                  >
                    <Input
                      value={defaultProject}
                      onChange={({ detail }) => setDefaultProject(detail.value)}
                      placeholder="UNASSIGNED"
                      data-testid="routing-default-project"
                    />
                  </FormField>
                )}
                {snowEnabled && (
                  <SpaceBetween size="s">
                    <FormField
                      label="Default Assignment Group"
                      constraintText="32-character sys_id"
                      errorText={
                        defaultSnowGroupError
                          ?? (snowEnabled && !defaultSnowGroupId.trim() ? 'Default assignment group is required' : undefined)
                      }
                    >
                      <Input
                        value={defaultSnowGroupId}
                        onChange={({ detail }) => setDefaultSnowGroupId(detail.value)}
                        placeholder="a1b2c3d4e5f6g7h8i9j0..."
                      />
                    </FormField>
                    <FormField label="Record Type" errorText={defaultSnowRecordTypeError ?? undefined}>
                      <Select
                        selectedOption={RECORD_TYPE_OPTIONS.find(o => o.value === defaultSnowRecordType) || RECORD_TYPE_OPTIONS[0]}
                        onChange={({ detail }) => setDefaultSnowRecordType(detail.selectedOption.value || 'change_request')}
                        options={RECORD_TYPE_OPTIONS}
                      />
                    </FormField>
                  </SpaceBetween>
                )}
              </ColumnLayout>
            </Container>

            {/* Section B: Account Mappings */}
            <Container header={
              <Header variant="h3" counter={`(${accountMappings.length})`} actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button
                    iconName="download"
                    onClick={handleLoadOrganizations}
                    loading={loadingOrg}
                    data-testid="load-org-accounts"
                  >
                    Load from Organizations
                  </Button>
                  <Button
                    onClick={() => { setBulkMode(!bulkMode); setBulkPreview(null); setBulkText(''); }}
                    data-testid="bulk-import-toggle"
                  >
                    {bulkMode ? 'Manual Entry' : 'Bulk Import'}
                  </Button>
                </SpaceBetween>
              }>
                Account Mappings
              </Header>
            }>
              <SpaceBetween size="m">
                {/* Org discovery error/info */}
                {orgError && (
                  <Alert type="info" dismissible onDismiss={() => setOrgError(null)}>
                    {orgError}
                  </Alert>
                )}

                {/* Bulk Import Mode */}
                {bulkMode ? (
                  <SpaceBetween size="m">
                    <SpaceBetween direction="horizontal" size="xs">
                      <Button variant={bulkFormat === 'csv' ? 'primary' : 'normal'} onClick={() => setBulkFormat('csv')}>CSV</Button>
                      <Button variant={bulkFormat === 'json' ? 'primary' : 'normal'} onClick={() => setBulkFormat('json')}>JSON</Button>
                    </SpaceBetween>

                    {!bulkPreview ? (
                      <SpaceBetween size="s">
                        <FormField
                          label={bulkFormat === 'csv' ? 'Paste CSV data' : 'Paste JSON array'}
                          description={bulkFormat === 'csv'
                            ? (snowEnabled
                              ? 'Format: account_id,jira_project,snow_group_id (one per line)'
                              : 'Format: account_id,jira_project (one per line)')
                            : (snowEnabled
                              ? 'Format: [{"account_id": "...", "jira_project": "...", "snow_assignment_group_id": "..."}]'
                              : 'Format: [{"account_id": "...", "jira_project": "..."}]')
                          }
                        >
                          <Textarea
                            value={bulkText}
                            onChange={({ detail }) => setBulkText(detail.value)}
                            placeholder={bulkFormat === 'csv'
                              ? (snowEnabled
                                ? '111111111111,CLOUDOPS,a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6\n222222222222,APPTEAM,p6o5n4m3l2k1j0i9h8g7f6e5d4c3b2a1'
                                : '111111111111,CLOUDOPS\n222222222222,APPTEAM')
                              : (snowEnabled
                                ? '[{"account_id": "111111111111", "jira_project": "CLOUDOPS", "snow_assignment_group_id": "a1b2c3d4..."}]'
                                : '[{"account_id": "111111111111", "jira_project": "CLOUDOPS"}]')
                            }
                            rows={8}
                            ariaLabel={`${bulkFormat.toUpperCase()} data for bulk import`}
                          />
                        </FormField>
                        <Button onClick={handleBulkParse} disabled={!bulkText.trim()}>Parse &amp; Preview</Button>
                      </SpaceBetween>
                    ) : (
                      <SpaceBetween size="m">
                        <Alert type="info">
                          Parsed {bulkPreview.valid.length + bulkPreview.invalid.length} rows.
                          {bulkPreview.invalid.length > 0 && ` ${bulkPreview.invalid.length} rows have errors.`}
                        </Alert>

                        {/* SEC-098-M2: Bulk import confirmation mandatory */}
                        {bulkConfirmVisible && (
                          <Alert
                            type="warning"
                            action={
                              <SpaceBetween direction="horizontal" size="xs">
                                <Button onClick={() => setBulkConfirmVisible(false)}>Cancel</Button>
                                <Button variant="primary" onClick={handleBulkConfirm}>Replace Mappings</Button>
                              </SpaceBetween>
                            }
                          >
                            This will replace all {accountMappings.length} existing account mappings with {bulkPreview.valid.length} valid rows.
                            {bulkPreview.invalid.length > 0 && ` ${bulkPreview.invalid.length} invalid rows will be skipped.`}
                          </Alert>
                        )}

                        <Table
                          variant="embedded"
                          items={bulkPreview.valid.slice(0, 100)}
                          columnDefinitions={[
                            { id: 'status', header: 'Status', cell: () => <StatusIndicator type="success">Valid</StatusIndicator>, width: 80 },
                            { id: 'acct', header: 'Account ID', cell: (item: AccountMapping) => item.account_id, width: 140 },
                            { id: 'proj', header: 'JIRA Project', cell: (item: AccountMapping) => item.jira_project, width: 140 },
                            ...(snowEnabled ? [{ id: 'snow', header: 'ServiceNow Group', cell: (item: AccountMapping) => item.snow_assignment_group_id || '—', width: 180 }] : []),
                          ]}
                          ariaLabels={{ tableLabel: 'Import preview - valid rows' }}
                        />
                        {bulkPreview.invalid.length > 0 && (
                          <Table
                            variant="embedded"
                            items={bulkPreview.invalid.slice(0, 20)}
                            columnDefinitions={[
                              { id: 'status', header: 'Status', cell: () => <StatusIndicator type="error">Invalid</StatusIndicator>, width: 80 },
                              { id: 'raw', header: 'Row', cell: (item: { raw: string; error: string }) => item.raw, width: 200 },
                              { id: 'error', header: 'Error', cell: (item: { raw: string; error: string }) => <Box color="text-status-error">{item.error}</Box>, width: 200 },
                            ]}
                            ariaLabels={{ tableLabel: 'Import preview - invalid rows' }}
                          />
                        )}

                        <SpaceBetween direction="horizontal" size="xs">
                          <Button onClick={() => setBulkPreview(null)}>Back to Edit</Button>
                          <Button
                            variant="primary"
                            onClick={() => setBulkConfirmVisible(true)}
                            disabled={bulkPreview.valid.length === 0}
                          >
                            Confirm Import ({bulkPreview.valid.length} rows)
                          </Button>
                        </SpaceBetween>
                      </SpaceBetween>
                    )}
                  </SpaceBetween>
                ) : (
                  /* Manual Entry Mode */
                  <SpaceBetween size="m">
                    {/* Account mappings table */}
                    <Table
                      variant="embedded"
                      items={paginatedMappings}
                      columnDefinitions={columnDefinitions}
                      empty={
                        <Box textAlign="center" color="text-body-secondary" padding="l">
                          No account mappings configured. All events will route to the default project.
                        </Box>
                      }
                      filter={
                        accountMappings.length > 0 ? (
                          <TextFilter
                            filteringText={filterText}
                            filteringPlaceholder="Search by account ID, name, or project"
                            filteringAriaLabel="Filter account mappings"
                            onChange={({ detail }) => { setFilterText(detail.filteringText); setCurrentPage(1); }}
                          />
                        ) : undefined
                      }
                      pagination={
                        filteredMappings.length > PAGE_SIZE ? (
                          <Pagination
                            currentPageIndex={currentPage}
                            pagesCount={totalPages}
                            onChange={({ detail }) => setCurrentPage(detail.currentPageIndex)}
                            ariaLabels={{
                              nextPageLabel: 'Next page',
                              previousPageLabel: 'Previous page',
                              pageLabel: (n) => `Page ${n}`,
                            }}
                          />
                        ) : undefined
                      }
                      ariaLabels={{ tableLabel: 'Account routing mappings' }}
                    />

                    {/* Add row */}
                    <SpaceBetween size="xs">
                      {addError && <Box color="text-status-error" variant="small">{addError}</Box>}
                      <SpaceBetween direction="horizontal" size="xs">
                        <Input
                          value={newAcctId}
                          onChange={({ detail }) => setNewAcctId(detail.value)}
                          placeholder="123456789012"
                          ariaLabel="New account ID"
                          inputMode="numeric"
                        />
                        <Input
                          value={newAcctName}
                          onChange={({ detail }) => setNewAcctName(detail.value)}
                          placeholder="Account name"
                          ariaLabel="Account name"
                        />
                        {jiraEnabled && (
                          <Input
                            value={newJiraProject}
                            onChange={({ detail }) => setNewJiraProject(detail.value)}
                            placeholder="CLOUDOPS"
                            ariaLabel="JIRA project for new account"
                          />
                        )}
                        {snowEnabled && (
                          <Input
                            value={newSnowGroup}
                            onChange={({ detail }) => setNewSnowGroup(detail.value)}
                            placeholder="sys_id"
                            ariaLabel="ServiceNow group for new account"
                          />
                        )}
                        <Button
                          onClick={handleAddMapping}
                          data-testid="add-mapping-row"
                        >
                          Add
                        </Button>
                      </SpaceBetween>
                    </SpaceBetween>
                  </SpaceBetween>
                )}
              </SpaceBetween>
            </Container>

            {/* Section C: Tag Routing */}
            <Container header={<Header variant="h3">Tag-Based Routing</Header>}>
              <SpaceBetween size="m">
                <Toggle
                  checked={tagRoutingEnabled}
                  onChange={({ detail }) => setTagRoutingEnabled(detail.checked)}
                >
                  Enable tag-based routing
                </Toggle>
                {tagRoutingEnabled && (
                  <>
                    <FormField
                      label="Tag Key"
                      description="Resource or account tag key used for routing (e.g., Team, Owner, CostCenter)"
                      errorText={tagRoutingEnabled && !tagRoutingKey.trim() ? 'Tag key is required when tag routing is enabled' : undefined}
                    >
                      <Input
                        value={tagRoutingKey}
                        onChange={({ detail }) => setTagRoutingKey(detail.value)}
                        placeholder="Team"
                      />
                    </FormField>
                    <FormField label="Tag source" description={TAG_SOURCE_GROUP_DESCRIPTION}>
                      <RadioGroup
                        value={tagSource}
                        onChange={({ detail }) => setTagSource(normalizeTagSource(detail.value))}
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
                        disabled={saving}
                      />
                    </FormField>
                    <Alert
                      type="info"
                      header={`Routing resolution order (tag source: ${tagSource === 'resource' ? 'Resource tags' : tagSource === 'both' ? 'Both (resource, then account)' : 'Account tags'})`}
                    >
                      <ol>
                        <li>Tag value from {tagSource === 'both' ? 'resource, then account' : tagSource} → TAG_ROUTING#value → target</li>
                        <li>Account ID → ROUTING#accountId → target</li>
                        <li>Default → ROUTING_DEFAULT → target (orphan queue)</li>
                      </ol>
                      <Box variant="small">A missing resource or account tag never drops the event — it falls through to steps 2–3.</Box>
                    </Alert>
                  </>
                )}
              </SpaceBetween>
            </Container>

            {/* Section D: Validation */}
            <Container header={<Header variant="h3">Target Validation</Header>}>
              <SpaceBetween size="m">
                <Box color="text-body-secondary">
                  Verify that all routing targets exist in your ITSM platform.
                </Box>
                <Button
                  onClick={handleValidate}
                  loading={validating}
                  disabled={validateCooldown && !validating}
                  data-testid="validate-routing"
                >
                  {validationResults ? 'Re-validate' : 'Validate All Targets'}
                </Button>
                {validationError && <Alert type="error">{validationError}</Alert>}
                {validationResults && validationResults.length > 0 && (
                  <SpaceBetween size="xxs">
                    {validationResults.map((r, idx) => (
                      <StatusIndicator key={idx} type={r.valid ? 'success' : 'error'}>
                        <Box variant="span" fontWeight="bold">{r.target}</Box>
                        {' — '}
                        {r.valid ? (r.displayName || 'Valid') : (r.error || 'Not found')}
                      </StatusIndicator>
                    ))}
                  </SpaceBetween>
                )}
                {validationResults && validationResults.some(r => !r.valid) && (
                  <Alert type="info">
                    Invalid targets will not prevent saving, but tickets routed to them will fail at runtime.
                  </Alert>
                )}
              </SpaceBetween>
            </Container>

          </SpaceBetween>
        )}
      </Modal>

      {/* Discard confirmation dialog */}
      <Modal
        visible={showDiscardConfirm}
        onDismiss={() => setShowDiscardConfirm(false)}
        closeAriaLabel="Close"
        size="small"
        header="Discard unsaved changes?"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShowDiscardConfirm(false)}>
                Keep editing
              </Button>
              <Button variant="primary" onClick={handleDiscardConfirm}>
                Discard
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        Your routing configuration changes have not been saved and will be lost.
      </Modal>
    </>
  );
}
