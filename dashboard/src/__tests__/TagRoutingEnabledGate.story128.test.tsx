/**
 * STORY-128 / RT-11 — Layer B (Vitest) ENABLED-PATH completion GATE.
 *
 * Owner: Moody (QA). Implementation-flow Step 7. Branch: feature/resource-tags.
 *
 * This file is the STORY-128 dashboard GATE confirmation. The exhaustive
 * per-story coverage lives in the owning-story suites (all GREEN):
 *   B1  tagSource selector       → TagSourceSelector.story124.test.tsx (STORY-124)
 *   B2  mapping editor CRUD/copy  → TagRoutingMappingsEditor.story125.test.tsx +
 *                                   tagMappings.story125.test.tsx (STORY-125)
 *   B3  strategy persist + Review → ConfigurationWizardTagRouting.story123.test.tsx
 *                                   (STORY-123)
 *
 * Per Luna's interface review (08_luna_interface_review.md §1), STORY-128 asserts
 * against the OWNING Luna specs, never ad-hoc strings. This gate file locks the
 * marquee cross-story invariants — the ones whose regression would let a green
 * tree certify a feature that is unsafe (Snape STC-9 stored-XSS) or dishonest
 * (RT-05 false "Enabled", RT-14 partial-save-as-success):
 *
 *   B1  — selector payload vocabulary is EXACTLY {resource|account|both}, default
 *         'account' persisted explicitly (STORY-124 §1–2).
 *   B2  — editor renders tag values + backend reasons as INERT TEXT (Snape STC-9:
 *         no dangerouslySetInnerHTML); DELETE-on-remove (not POST-omission);
 *         V1–V3 add-error copy verbatim; partial-save {created,updated,
 *         validationErrors} is a WARNING, not success (STORY-125 §4/§5.3/§10/§15).
 *   B3  — saveAll POSTs /config/routing/strategy BEFORE /config/activate; green
 *         "Enabled" appears ONLY after a 200 (STORY-123 §2.3/§3.1).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React, { useState } from 'react';

// ---------------------------------------------------------------------------
// Module mocks (mirror the owning-story suites)
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

// ===========================================================================
// B2 — Mapping editor: text-only rendering (STC-9), CRUD, verbatim copy
// ===========================================================================

import TagRoutingMappingsEditor from '../components/TagRoutingMappingsEditor';
import {
  TagMappingRow,
  persistTagMappings,
  validateTagValueClient,
  MAX_TAG_VALUE_LEN,
} from '../components/tagMappings';

/** Controlled host wrapper — the editor is a controlled component (host state). */
function EditorHost({
  initial = [],
  tagKey = 'Team',
  rowErrors = {},
  loadError = null,
}: {
  initial?: TagMappingRow[];
  tagKey?: string;
  rowErrors?: Record<string, string>;
  loadError?: string | null;
}) {
  const [rows, setRows] = useState<TagMappingRow[]>(initial);
  const [removed, setRemoved] = useState<string[]>([]);
  // Expose the live removed set for assertions.
  (EditorHost as any)._removed = removed;
  (EditorHost as any)._rows = rows;
  return (
    <TagRoutingMappingsEditor
      enabled={true}
      tagKey={tagKey}
      mappings={rows}
      onMappingsChange={setRows}
      removedTagValues={removed}
      onRemovedChange={setRemoved}
      rowErrors={rowErrors}
      loadError={loadError}
      onRetryLoad={() => {}}
    />
  );
}

describe('STORY-128 B2 — tag mapping editor (STORY-125 authority)', () => {
  beforeEach(() => vi.clearAllMocks());

  it('gates on enabled=false (renders nothing)', () => {
    const { container } = render(
      <TagRoutingMappingsEditor
        enabled={false}
        tagKey="Team"
        mappings={[]}
        onMappingsChange={() => {}}
        removedTagValues={[]}
        onRemovedChange={() => {}}
      />
    );
    expect(container.innerHTML).toBe('');
  });

  it('shows the empty-state copy referencing the configured tag key', () => {
    render(<EditorHost tagKey="Team" />);
    expect(screen.getByText(/No tag-value mappings yet\./)).toBeInTheDocument();
  });

  it('STC-9 — renders a hostile tag value as INERT TEXT (no dangerouslySetInnerHTML, no live node)', () => {
    const xss = '<img src=x onerror=alert(1)>';
    const initial: TagMappingRow[] = [
      { tagValue: xss, jiraProject: 'CLOUDOPS', jiraIssueType: 'Task', rowStatus: 'persisted' },
    ];
    const { container } = render(<EditorHost initial={initial} />);
    // The value is present as literal text...
    expect(screen.getByText(xss)).toBeInTheDocument();
    // ...and NOT interpreted as HTML — no injected <img>/<script> node exists.
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
  });

  it('STC-9 — renders a hostile backend row reason as inert text (echoed rejected value)', () => {
    const initial: TagMappingRow[] = [
      { tagValue: 'platform', jiraProject: 'CLOUDOPS', jiraIssueType: 'Task', rowStatus: 'persisted' },
    ];
    const reason = 'Rejected <script>alert(2)</script>';
    const { container } = render(<EditorHost initial={initial} rowErrors={{ platform: reason }} />);
    expect(screen.getByText(reason)).toBeInTheDocument();
    expect(container.querySelector('script')).toBeNull();
  });

  it('V1 — add with empty tag value shows the verbatim required copy', async () => {
    const user = userEvent.setup();
    render(<EditorHost />);
    await user.click(screen.getByTestId('add-tag-mapping-row'));
    expect(screen.getByText('Tag value is required.')).toBeInTheDocument();
  });

  it('V2 — add with a value but no project shows the verbatim required copy', async () => {
    const user = userEvent.setup();
    render(<EditorHost />);
    await user.type(screen.getByLabelText('New tag value'), 'platform');
    await user.click(screen.getByTestId('add-tag-mapping-row'));
    expect(screen.getByText('JIRA project is required.')).toBeInTheDocument();
  });

  it('V3 — adding a duplicate tag value shows the verbatim duplicate copy', async () => {
    const user = userEvent.setup();
    const initial: TagMappingRow[] = [
      { tagValue: 'platform', jiraProject: 'CLOUDOPS', jiraIssueType: 'Task', rowStatus: 'persisted' },
    ];
    render(<EditorHost initial={initial} />);
    await user.type(screen.getByLabelText('New tag value'), 'platform');
    await user.type(screen.getByLabelText('JIRA project for new tag value'), 'PLATFORM');
    await user.click(screen.getByTestId('add-tag-mapping-row'));
    expect(
      screen.getByText('A mapping for tag value "platform" already exists. Edit the existing row instead.')
    ).toBeInTheDocument();
  });

  it('add → the new row appears with the tag value rendered', async () => {
    const user = userEvent.setup();
    render(<EditorHost />);
    await user.type(screen.getByLabelText('New tag value'), 'payments');
    await user.type(screen.getByLabelText('JIRA project for new tag value'), 'APPTEAM');
    await user.click(screen.getByTestId('add-tag-mapping-row'));
    expect(screen.getByText('payments')).toBeInTheDocument();
  });

  it('remove of a PERSISTED row records it for DELETE (not POST-omission)', async () => {
    const user = userEvent.setup();
    const initial: TagMappingRow[] = [
      { tagValue: 'platform', jiraProject: 'CLOUDOPS', jiraIssueType: 'Task', rowStatus: 'persisted' },
    ];
    render(<EditorHost initial={initial} />);
    await user.click(screen.getByRole('button', { name: /Remove mapping for tag value platform/i }));
    await waitFor(() => expect((EditorHost as any)._removed).toContain('platform'));
  });

  it('load error shows an error affordance instead of an empty editor', () => {
    render(<EditorHost loadError="boom" />);
    expect(screen.getByText(/Failed to load tag mappings/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Retry/i })).toBeInTheDocument();
  });
});

describe('STORY-128 B2 — tagMappings module: partial-save honesty + validation copy', () => {
  beforeEach(() => vi.clearAllMocks());

  it('validateTagValueClient — V1/V4/V5 copy verbatim + MAX length constant', () => {
    expect(MAX_TAG_VALUE_LEN).toBe(256);
    expect(validateTagValueClient('')).toBe('Tag value is required.');
    expect(validateTagValueClient('a'.repeat(257))).toBe(
      `Tag value is too long (max ${MAX_TAG_VALUE_LEN} characters).`
    );
    expect(validateTagValueClient('bad\nvalue')).toBe(
      "Tag value can't contain line breaks or control characters."
    );
    expect(validateTagValueClient('platform')).toBeNull();
  });

  it('partial save surfaces {created, updated, validationErrors} — a rejected row is NOT silent success', async () => {
    mockApiFetch.mockImplementation(async (path: string, opts?: any) => {
      if (path === '/config/routing/tags' && opts?.method === 'POST') {
        return {
          created: 1,
          updated: 0,
          validationErrors: [{ tagValue: 'bad value', reason: 'Tag value is invalid.' }],
        };
      }
      return {};
    });
    const result = await persistTagMappings(
      [
        { tagValue: 'platform', jiraProject: 'PLATFORM', jiraIssueType: 'Task' },
        { tagValue: 'bad value', jiraProject: 'X', jiraIssueType: 'Task' },
      ],
      [],
    );
    expect(result.created).toBe(1);
    expect(result.updated).toBe(0);
    expect(result.validationErrors).toHaveLength(1);
    expect(result.validationErrors[0].tagValue).toBe('bad value');
    // The presence of validationErrors is the "warning, not success" signal.
    expect(result.transportError).toBeUndefined();
  });

  it('DELETE-first: removed values are DELETEd before the upsert POST', async () => {
    const order: string[] = [];
    mockApiFetch.mockImplementation(async (path: string, opts?: any) => {
      order.push(`${opts?.method ?? 'GET'} ${path}`);
      if (path.startsWith('/config/routing/tags/')) return {};
      if (path === '/config/routing/tags') return { created: 1, updated: 0, validationErrors: [] };
      return {};
    });
    await persistTagMappings(
      [{ tagValue: 'platform', jiraProject: 'PLATFORM', jiraIssueType: 'Task' }],
      ['old-value'],
    );
    const delIdx = order.findIndex(o => o.startsWith('DELETE /config/routing/tags/'));
    const postIdx = order.findIndex(o => o === 'POST /config/routing/tags');
    expect(delIdx).toBeGreaterThanOrEqual(0);
    expect(postIdx).toBeGreaterThanOrEqual(0);
    expect(delIdx).toBeLessThan(postIdx);
  });

  it('a POST transport failure is reported (caller must not claim success)', async () => {
    mockApiFetch.mockImplementation(async (path: string, opts?: any) => {
      if (path === '/config/routing/tags' && opts?.method === 'POST') {
        throw new Error('API 500: ConfigTable put_item failed');
      }
      return {};
    });
    const result = await persistTagMappings(
      [{ tagValue: 'platform', jiraProject: 'PLATFORM', jiraIssueType: 'Task' }],
      [],
    );
    expect(result.transportError).toBeTruthy();
    expect(result.created).toBe(0);
  });
});

// ===========================================================================
// B1 — tagSource selector payload vocabulary + explicit default (STORY-124)
// B3 — saveAll POSTs strategy before activate + truthful "Enabled" (STORY-123)
// ===========================================================================

function installHappyMock() {
  mockApiFetch.mockImplementation(async (path: string, opts?: any) => {
    if (path === '/config/integrations') return { platforms: [] };
    if (path === '/config/setup-timer') return { elapsed: 0, completed: false };
    if (path === '/config/setup-timer/start') return {};
    if (path === '/config/setup-timer/complete') return {};
    if (path === '/config/routing/validate') return { results: [] };
    if (path === '/config/routing/tags') return { mappings: [] };
    if (path === '/config/routing/strategy') {
      const b = opts?.body ? JSON.parse(opts.body) : {};
      return {
        mode: b.mode ?? 'account',
        tagKey: b.mode === 'tag' ? b.tagKey ?? null : null,
        tagSource: b.mode === 'tag' ? b.tagSource ?? 'account' : null,
        updatedAt: '2026-08-19T00:00:00Z',
      };
    }
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
  await user.click(await screen.findByRole('button', { name: /^Next$/ }));
}

async function advanceToRoutingStep(user: ReturnType<typeof userEvent.setup>) {
  await clickNext(user);
  await clickNext(user);
  await screen.findByText('Tag-Based Routing');
}

async function enableTagRouting(user: ReturnType<typeof userEvent.setup>, key = 'Team') {
  await user.click(await screen.findByRole('checkbox', { name: /Enable tag-based routing/i }));
  if (key) await user.type(await screen.findByPlaceholderText('Team'), key);
}

async function advanceToReviewStep(user: ReturnType<typeof userEvent.setup>) {
  await clickNext(user);
  await screen.findByText('Which Health events should create tickets?');
  await clickNext(user);
  await screen.findByText('Configuration Summary');
}

async function saveAndActivate(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('button', { name: /Save & Activate/i }));
}

function wasCalled(path: string): boolean {
  return mockApiFetch.mock.calls.some(c => c[0] === path);
}
function callIndex(path: string): number {
  return mockApiFetch.mock.calls.findIndex(c => c[0] === path);
}
function strategyBody(): any {
  const call = mockApiFetch.mock.calls.find(c => c[0] === '/config/routing/strategy');
  if (!call) throw new Error('POST /config/routing/strategy was never called');
  return JSON.parse((call[1] as any).body);
}

describe('STORY-128 B1 — tagSource selector payload vocabulary (STORY-124 authority)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    installHappyMock();
  });

  it('default selection persists tagSource:"account" explicitly (never silently omitted)', async () => {
    const user = userEvent.setup();
    await renderWizard();
    await advanceToRoutingStep(user);
    await enableTagRouting(user, 'Team');
    // Default is visibly account.
    expect(screen.getByRole('radio', { name: /Account tags/i })).toBeChecked();
    await advanceToReviewStep(user);
    await saveAndActivate(user);
    await waitFor(() => expect(wasCalled('/config/routing/strategy')).toBe(true));
    const body = strategyBody();
    expect(body.tagSource).toBe('account');
    // Vocabulary is exactly resource|account|both.
    expect(['resource', 'account', 'both']).toContain(body.tagSource);
  });

  it('selecting Resource tags persists tagSource:"resource"', async () => {
    const user = userEvent.setup();
    await renderWizard();
    await advanceToRoutingStep(user);
    await enableTagRouting(user, 'Team');
    await user.click(screen.getByRole('radio', { name: /Resource tags/i }));
    await advanceToReviewStep(user);
    await saveAndActivate(user);
    await waitFor(() => expect(wasCalled('/config/routing/strategy')).toBe(true));
    expect(strategyBody().tagSource).toBe('resource');
  });
});

describe('STORY-128 B3 — saveAll sequences strategy before activate + truthful Enabled (STORY-123 authority)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    installHappyMock();
  });

  it('POSTs /config/routing/strategy BEFORE /config/activate and reaches green "Enabled" only after 200', async () => {
    const user = userEvent.setup();
    const { onSave } = await renderWizard();
    await advanceToRoutingStep(user);
    await enableTagRouting(user, 'Team');
    await advanceToReviewStep(user);

    // Before Save: pending confirmation, NOT the bare green "Enabled".
    expect(screen.queryByText(/^Enabled \(key: Team, source: Account tags\) · no mappings yet$/)).not.toBeInTheDocument();

    await saveAndActivate(user);

    await waitFor(() => expect(wasCalled('/config/routing/strategy')).toBe(true));
    await waitFor(() => expect(wasCalled('/config/activate')).toBe(true));
    expect(callIndex('/config/routing/strategy')).toBeLessThan(callIndex('/config/activate'));

    // After 200: bare green "Enabled" (no "will be activated" suffix).
    await waitFor(() =>
      expect(screen.getByText(/^Enabled \(key: Team, source: Account tags\) · no mappings yet$/)).toBeInTheDocument()
    );
    await waitFor(() => expect(onSave).toHaveBeenCalled());
  });
});
