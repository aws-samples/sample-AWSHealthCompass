/**
 * Unit/integration tests for STORY-120:
 *   Fix wizard silently dropping ServiceNow assignment-group-only account
 *   mappings from the POST /config/routing/import payload.
 *
 * Style mirrors ConfigurationWizardDispatch.test.tsx: mock ../api, ../config,
 * ../PlatformContext; dynamic-import the component; drive the Cloudscape
 * Wizard via its Next / Save & Activate buttons; use the two-checkbox lookup
 * pattern from ConfigurationWizardPlatformInference.test.tsx to enable
 * ServiceNow on Step 0.
 *
 * Covers:
 *   - AC-8: asserts the EXACT request body shape sent to
 *     POST /config/routing/import, including `snowAssignmentGroupId` when
 *     the source mapping has one set, for both a combined (JIRA + SNOW) row
 *     and a SNOW-only row.
 *   - AC-9: a negative-control test reproducing the pre-fix bug scenario
 *     (account_id + snow_assignment_group_id only, no JIRA project) and
 *     proving the value is present in the request body — the exact case
 *     the pre-fix filter (`m.account_id && m.jira_project`) and map
 *     (`{accountId, jiraProject}` only) would have silently dropped.
 *   - AC-5 regression: a JIRA-only mapping (no ServiceNow platform enabled,
 *     no snow_assignment_group_id) continues to save with the unchanged
 *     `{accountId, jiraProject}` shape and no `snowAssignmentGroupId` key.
 *
 * Per Dumbledore's design §6.5 / Harry's handoff note: `jiraProject` MUST
 * remain present (as an empty string) on SNOW-only rows, never omitted —
 * every assertion below checks for its presence explicitly, not just
 * `snowAssignmentGroupId`'s presence, to avoid the exact false-fail Dumbledore
 * warned about.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';

// ---------------------------------------------------------------------------
// Module mocks
// ---------------------------------------------------------------------------

vi.mock('../api', () => ({
  apiFetch: vi.fn(),
}));

vi.mock('../config', () => ({
  getConfig: () => ({
    userPoolId: 'fake-pool',
    clientId: 'fake-client',
    apiUrl: 'http://localhost:3000',
    region: 'us-east-1',
  }),
  loadConfig: vi.fn().mockResolvedValue({
    userPoolId: 'fake-pool',
    clientId: 'fake-client',
    apiUrl: 'http://localhost:3000',
    region: 'us-east-1',
  }),
}));

vi.mock('../PlatformContext', () => ({
  PlatformProvider: ({ children }: any) => <>{children}</>,
  usePlatformLabels: () => ({
    connectionTitle: 'JIRA Connection',
    projectLabel: 'JIRA Project',
    platform: 'jira',
    routingPlaceholder: 'CLOUDOPS',
    routingTarget: 'JIRA Project',
    bulkFormat: 'account_id,jira_project',
  }),
}));

import { apiFetch } from '../api';
import type { OnboardingConfig } from '../types';

const mockApiFetch = vi.mocked(apiFetch);

// Two distinct valid-format ServiceNow sys_ids (32 lowercase hex chars),
// matching the shape enforced backend-side by Snape's Finding 3.
const SNOW_SYS_ID_A = 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6';
const SNOW_SYS_ID_B = 'b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6a1';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function installHappyMock() {
  mockApiFetch.mockImplementation(async (path: string) => {
    if (path === '/config/integrations') return { platforms: [] };
    if (path === '/config/setup-timer') return { elapsed: 0, completed: false };
    if (path === '/config/setup-timer/start') return {};
    if (path === '/config/setup-timer/complete') return {};
    if (path === '/config/routing/validate') return { results: [] };
    if (path === '/config/routing/import') return { importId: 'import-story120' };
    if (path === '/config/routing/import/confirm') return {};
    if (path === '/config/dispatch') return {};
    if (path === '/config/activate') return {};
    return {};
  });
}

async function renderWizard(config: OnboardingConfig | null = null) {
  const { default: ConfigurationWizard } = await import('../ConfigurationWizard');
  const onSave = vi.fn();
  const result = render(<ConfigurationWizard config={config} onSave={onSave} />);
  return { ...result, onSave };
}

async function clickNext(user: ReturnType<typeof userEvent.setup>) {
  const next = await screen.findByRole('button', { name: /^Next$/ });
  await user.click(next);
}

/** Step 0 checkboxes: index 0 = JIRA, index 1 = ServiceNow (per
 * ConfigurationWizardPlatformInference.test.tsx's established convention). */
function getPlatformCheckboxes(container: HTMLElement): [HTMLInputElement, HTMLInputElement] {
  const checkboxes = container.querySelectorAll('input[type="checkbox"]');
  return [checkboxes[0] as HTMLInputElement, checkboxes[1] as HTMLInputElement];
}

/** Add one account mapping row via the wizard's manual-entry inputs + Add
 * button (Step 2 — Routing). Leave `proj` as '' to add a ServiceNow-only row.
 */
async function addMappingRow(
  user: ReturnType<typeof userEvent.setup>,
  acct: string,
  proj: string,
  snowGroup: string,
) {
  const acctInput = await screen.findByPlaceholderText('Account ID (12 digits)');
  await user.clear(acctInput);
  await user.type(acctInput, acct);

  if (proj) {
    const projInput = await screen.findByPlaceholderText('CLOUDOPS');
    await user.clear(projInput);
    await user.type(projInput, proj);
  }

  if (snowGroup) {
    const snowInput = await screen.findByPlaceholderText('Assignment Group ID');
    await user.clear(snowInput);
    await user.type(snowInput, snowGroup);
  }

  const addButton = screen.getByRole('button', { name: 'Add' });
  await user.click(addButton);
}

/** Advance from Step 0 to Step 2 (Routing), optionally enabling ServiceNow
 * on Step 0 first. */
async function advanceToRoutingStep(
  user: ReturnType<typeof userEvent.setup>,
  container: HTMLElement,
  { enableServiceNow }: { enableServiceNow: boolean },
) {
  if (enableServiceNow) {
    const [, snowCb] = getPlatformCheckboxes(container);
    await user.click(snowCb);
  }
  await clickNext(user); // 0 -> 1 (Connection)
  await clickNext(user); // 1 -> 2 (Routing)
  await screen.findByText(/Account Mappings/);
}

/** Advance from Routing (Step 2) all the way through Dispatch + Review and
 * submit. Step 2 -> 3 triggers `/config/routing/validate`. */
async function advanceFromRoutingAndSubmit(user: ReturnType<typeof userEvent.setup>) {
  await clickNext(user); // 2 -> 3 (Dispatch) — triggers routing validate
  await screen.findByText('Which Health events should create tickets?');
  await clickNext(user); // 3 -> 4 (Review)
  await screen.findByText('Configuration Summary');
  const submit = await screen.findByRole('button', { name: /Save & Activate/i });
  await user.click(submit);
}

function wasCalled(path: string): boolean {
  return mockApiFetch.mock.calls.some(c => c[0] === path);
}

/** Parse the JSON body sent to POST /config/routing/import into the
 * account-mapping array (the payload's `data` field is itself a JSON
 * string — matches `saveAll()`'s `data: JSON.stringify(validMappings.map(...))`). */
function importedMappings(): any[] {
  const call = mockApiFetch.mock.calls.find(c => c[0] === '/config/routing/import');
  if (!call) throw new Error('POST /config/routing/import was never called');
  const outerBody = JSON.parse((call[1] as any).body);
  expect(outerBody.format).toBe('json');
  return JSON.parse(outerBody.data);
}

function findMapping(mappings: any[], accountId: string): any {
  const m = mappings.find(x => x.accountId === accountId);
  if (!m) throw new Error(`No mapping found for accountId ${accountId} in ${JSON.stringify(mappings)}`);
  return m;
}

// ---------------------------------------------------------------------------
// AC-8 — exact request body shape, including snowAssignmentGroupId when set
// ---------------------------------------------------------------------------

describe('ConfigurationWizard saveAll() — AC-8: exact request body shape (STORY-120)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    installHappyMock();
  });

  it('includes snowAssignmentGroupId in the mapped payload for a combined JIRA+ServiceNow row', async () => {
    const user = userEvent.setup();
    const { container } = await renderWizard();

    await advanceToRoutingStep(user, container, { enableServiceNow: true });
    await addMappingRow(user, '111111111111', 'CLOUDOPS', SNOW_SYS_ID_A);
    await advanceFromRoutingAndSubmit(user);

    await waitFor(() => expect(wasCalled('/config/routing/import')).toBe(true));

    const mappings = importedMappings();
    expect(mappings).toHaveLength(1);
    const m = findMapping(mappings, '111111111111');

    // Exact shape: accountId, jiraProject, snowAssignmentGroupId — all three
    // keys present with the values entered in the UI.
    expect(m.accountId).toBe('111111111111');
    expect(m.jiraProject).toBe('CLOUDOPS');
    expect(m.snowAssignmentGroupId).toBe(SNOW_SYS_ID_A);
  });

  it('includes snowAssignmentGroupId for a ServiceNow-only row while keeping jiraProject present as an empty string', async () => {
    const user = userEvent.setup();
    const { container } = await renderWizard();

    await advanceToRoutingStep(user, container, { enableServiceNow: true });
    // No JIRA project entered — ServiceNow-only mapping.
    await addMappingRow(user, '222222222222', '', SNOW_SYS_ID_B);
    await advanceFromRoutingAndSubmit(user);

    await waitFor(() => expect(wasCalled('/config/routing/import')).toBe(true));

    const mappings = importedMappings();
    const m = findMapping(mappings, '222222222222');

    expect(m.snowAssignmentGroupId).toBe(SNOW_SYS_ID_B);
    // Dumbledore §6.5 / Harry's handoff: jiraProject must be present as ''
    // (unconditional `jiraProject: m.jira_project`), NOT omitted from the
    // object. A test asserting it is absent would be testing the wrong shape.
    expect(m).toHaveProperty('jiraProject');
    expect(m.jiraProject).toBe('');
  });

  it('successfully completes the wizard (onSave fires) after a ServiceNow-only mapping is saved', async () => {
    const user = userEvent.setup();
    const { container, onSave } = await renderWizard();

    await advanceToRoutingStep(user, container, { enableServiceNow: true });
    await addMappingRow(user, '333333333333', '', SNOW_SYS_ID_A);
    await advanceFromRoutingAndSubmit(user);

    await waitFor(() => expect(wasCalled('/config/routing/import')).toBe(true));
    await waitFor(() => expect(wasCalled('/config/routing/import/confirm')).toBe(true));
    await waitFor(() => expect(onSave).toHaveBeenCalled());
  });
});

// ---------------------------------------------------------------------------
// AC-9 — negative control reproducing the pre-fix bug
// ---------------------------------------------------------------------------

describe('ConfigurationWizard saveAll() — AC-9: negative control for the pre-fix bug (STORY-120)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    installHappyMock();
  });

  it('does NOT silently drop a ServiceNow-only mapping from the import payload (pre-fix: filter excluded it entirely)', async () => {
    // Pre-fix behavior being reproduced: `accountMappings.filter(m => m.account_id && m.jira_project)`
    // would exclude this exact row (account_id set, jira_project empty,
    // snow_assignment_group_id set) BEFORE the map step ever ran — so
    // `validMappings.length` would be 0 and POST /config/routing/import
    // would never even be called for this mapping. This test proves that
    // no longer happens: the mapping survives the filter and reaches the
    // wire.
    const user = userEvent.setup();
    const { container } = await renderWizard();

    await advanceToRoutingStep(user, container, { enableServiceNow: true });
    await addMappingRow(user, '444444444444', '', SNOW_SYS_ID_A);
    await advanceFromRoutingAndSubmit(user);

    await waitFor(() => expect(wasCalled('/config/routing/import')).toBe(true));

    const mappings = importedMappings();
    // Negative-control assertion #1: the pre-fix filter would have produced
    // an empty array (and skipped the API call entirely) — length must be 1.
    expect(mappings).toHaveLength(1);
    const m = mappings[0];
    expect(m.accountId).toBe('444444444444');
  });

  it('does NOT drop the snowAssignmentGroupId field even when jiraProject is empty (pre-fix: map omitted the field unconditionally)', async () => {
    // Pre-fix behavior being reproduced: even if the filter had let the row
    // through, `.map(m => ({ accountId: m.account_id, jiraProject: m.jira_project }))`
    // never referenced `m.snow_assignment_group_id` at all — the field was
    // unconditionally absent from every emitted object, regardless of
    // whether the user had set it.
    const user = userEvent.setup();
    const { container } = await renderWizard();

    await advanceToRoutingStep(user, container, { enableServiceNow: true });
    await addMappingRow(user, '555555555555', '', SNOW_SYS_ID_B);
    await advanceFromRoutingAndSubmit(user);

    await waitFor(() => expect(wasCalled('/config/routing/import')).toBe(true));

    const mappings = importedMappings();
    const m = findMapping(mappings, '555555555555');

    // Negative-control assertion #2: the key must actually be present with
    // the entered value, not merely "not undefined" by coincidence.
    expect(m).toHaveProperty('snowAssignmentGroupId');
    expect(m.snowAssignmentGroupId).toBe(SNOW_SYS_ID_B);
    expect(m.snowAssignmentGroupId).not.toBe('');
    expect(m.snowAssignmentGroupId).not.toBeUndefined();
  });

  it('reproduces both defects together in one multi-row save: a mixed set of mappings all survive with correct fields', async () => {
    // Combines both negative-control angles in a single realistic save: one
    // JIRA-only row, one combined row, and one ServiceNow-only row all in
    // the same saveAll() call — proves the fix generalizes across a mixed
    // batch, not just a single-mapping happy path.
    const user = userEvent.setup();
    const { container } = await renderWizard();

    await advanceToRoutingStep(user, container, { enableServiceNow: true });
    await addMappingRow(user, '111111111111', 'CLOUDOPS', '');
    await addMappingRow(user, '222222222222', 'APPTEAM', SNOW_SYS_ID_A);
    await addMappingRow(user, '333333333333', '', SNOW_SYS_ID_B);
    await advanceFromRoutingAndSubmit(user);

    await waitFor(() => expect(wasCalled('/config/routing/import')).toBe(true));

    const mappings = importedMappings();
    expect(mappings).toHaveLength(3);

    const jiraOnly = findMapping(mappings, '111111111111');
    expect(jiraOnly.jiraProject).toBe('CLOUDOPS');
    expect(jiraOnly.snowAssignmentGroupId).toBeUndefined();

    const combined = findMapping(mappings, '222222222222');
    expect(combined.jiraProject).toBe('APPTEAM');
    expect(combined.snowAssignmentGroupId).toBe(SNOW_SYS_ID_A);

    const snowOnly = findMapping(mappings, '333333333333');
    expect(snowOnly.jiraProject).toBe('');
    expect(snowOnly.snowAssignmentGroupId).toBe(SNOW_SYS_ID_B);
  });
});

// ---------------------------------------------------------------------------
// AC-5 regression — JIRA-only mappings unchanged
// ---------------------------------------------------------------------------

describe('ConfigurationWizard saveAll() — AC-5 regression: JIRA-only mappings unchanged (STORY-120)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    installHappyMock();
  });

  it('saves a JIRA-only mapping (ServiceNow not enabled) with the pre-existing {accountId, jiraProject} shape', async () => {
    const user = userEvent.setup();
    const { container } = await renderWizard();

    // Default enabledPlatforms is ['jira'] — do not enable ServiceNow.
    await advanceToRoutingStep(user, container, { enableServiceNow: false });
    await addMappingRow(user, '666666666666', 'CLOUDOPS', '');
    await advanceFromRoutingAndSubmit(user);

    await waitFor(() => expect(wasCalled('/config/routing/import')).toBe(true));

    const mappings = importedMappings();
    expect(mappings).toHaveLength(1);
    const m = mappings[0];

    expect(m.accountId).toBe('666666666666');
    expect(m.jiraProject).toBe('CLOUDOPS');
    // No ServiceNow platform enabled, no group entered — the key must not
    // appear at all (JSON.stringify drops `undefined` values), matching
    // exactly the pre-fix behavior for this JIRA-only case.
    expect(m).not.toHaveProperty('snowAssignmentGroupId');
    expect(wasCalled('/config/routing/import/confirm')).toBe(true);
  });

  it('does not regress the manual Add button for a plain JIRA row when ServiceNow is enabled but left blank', async () => {
    // Guards TR-3/D3's widened guard: enabling ServiceNow must not require
    // a ServiceNow value for a row where the user only wants JIRA routing.
    const user = userEvent.setup();
    const { container } = await renderWizard();

    await advanceToRoutingStep(user, container, { enableServiceNow: true });
    await addMappingRow(user, '777777777777', 'APPTEAM', '');
    await advanceFromRoutingAndSubmit(user);

    await waitFor(() => expect(wasCalled('/config/routing/import')).toBe(true));

    const mappings = importedMappings();
    const m = findMapping(mappings, '777777777777');
    expect(m.jiraProject).toBe('APPTEAM');
    expect(m.snowAssignmentGroupId).toBeUndefined();
  });
});
