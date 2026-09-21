/**
 * Unit/integration tests for:
 *   Fix wizard dispatch casing + prevent silent activation under the wrong mode.
 *
 * Covers the two halves of the defect:
 *   1. Casing — the wizard's custom-mode save must send camelCase rules
 *      (`eventTypePattern`, not `event_type_pattern`) via `buildDispatchBody`.
 *   2. Safety inversion (CRITICAL regression) — if the dispatch save fails,
 *      `/config/activate` MUST NOT be called and `onSave` MUST NOT fire; when
 *      dispatch succeeds, activation proceeds.
 *
 * Style mirrors ConfigurationWizardPlatformInference.test.tsx: mock ../api,
 * ../config, ../PlatformContext; dynamic-import the component; drive the
 * Cloudscape Wizard via its Next / Save & Activate buttons.
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

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Default happy-path mock: every endpoint resolves. */
function installHappyMock() {
  mockApiFetch.mockImplementation(async (path: string) => {
    if (path === '/config/integrations') return { platforms: [] };
    if (path === '/config/setup-timer') return { elapsed: 0, completed: false };
    if (path === '/config/setup-timer/start') return {};
    if (path === '/config/setup-timer/complete') return {};
    if (path === '/config/routing/validate') return { results: [] };
    return {};
  });
}

async function renderWizard(config: OnboardingConfig | null = null) {
  const { default: ConfigurationWizard } = await import('../ConfigurationWizard');
  const onSave = vi.fn();
  const result = render(<ConfigurationWizard config={config} onSave={onSave} />);
  return { ...result, onSave };
}

/** Click the wizard's primary "Next" button once and wait for re-render. */
async function clickNext(user: ReturnType<typeof userEvent.setup>) {
  const next = await screen.findByRole('button', { name: /^Next$/ });
  await user.click(next);
}

/** Advance from Step 0 (Platform) to Step 3 (Dispatch Window). */
async function advanceToDispatchStep(user: ReturnType<typeof userEvent.setup>) {
  await clickNext(user); // 0 -> 1 (Connection)
  await clickNext(user); // 1 -> 2 (Routing)
  await clickNext(user); // 2 -> 3 (Dispatch) — routing validate returns valid (no targets)
  await screen.findByText('Which Health events should create tickets?');
}

/** Advance from Dispatch (Step 3) to Review (Step 4) and submit. */
async function submitFromDispatch(user: ReturnType<typeof userEvent.setup>) {
  await clickNext(user); // 3 -> 4 (Review)
  await screen.findByText('Configuration Summary');
  const submit = await screen.findByRole('button', { name: /Save & Activate/i });
  await user.click(submit);
}

function dispatchCallBody(): any {
  const call = mockApiFetch.mock.calls.find(c => c[0] === '/config/dispatch');
  if (!call) throw new Error('POST /config/dispatch was never called');
  return JSON.parse((call[1] as any).body);
}

function wasCalled(path: string): boolean {
  return mockApiFetch.mock.calls.some(c => c[0] === path);
}

// ---------------------------------------------------------------------------
// 1. Casing — custom-mode save sends camelCase body
// ---------------------------------------------------------------------------

describe('ConfigurationWizard dispatch — custom-mode casing', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    installHappyMock();
  });

  it('sends camelCase rules (eventTypePattern, not event_type_pattern)', async () => {
    const user = userEvent.setup();
    await renderWizard();

    await advanceToDispatchStep(user);

    // Select custom mode
    const customRadio = await screen.findByRole('radio', { name: /Custom rules/i });
    await user.click(customRadio);

    // Add a rule: AWS_EKS_* (category defaults to Scheduled Change)
    const patternInput = await screen.findByPlaceholderText('AWS_EKS_*');
    await user.type(patternInput, 'AWS_EKS_*');
    await user.click(screen.getByRole('button', { name: /Add Rule/i }));

    await submitFromDispatch(user);

    await waitFor(() => expect(wasCalled('/config/dispatch')).toBe(true));

    const body = dispatchCallBody();
    expect(body.mode).toBe('custom');
    expect(body.actionabilityFilter).toBe('all_actionable');
    expect(body.rules).toHaveLength(1);

    const rule = body.rules[0];
    // The exact defect: camelCase keys must be on the wire.
    expect(rule).toHaveProperty('eventTypePattern', 'AWS_EKS_*');
    expect(rule).toHaveProperty('ruleId');
    expect(rule).toHaveProperty('eventCategories', ['scheduledChange']);
    expect(rule).toHaveProperty('enabled', true);
    // snake_case must never appear.
    expect(rule).not.toHaveProperty('event_type_pattern');
    expect(rule).not.toHaveProperty('rule_id');
    expect(rule).not.toHaveProperty('event_categories');
  });
});

// ---------------------------------------------------------------------------
// 2. Non-custom modes save without a rules field
// ---------------------------------------------------------------------------

describe('ConfigurationWizard dispatch — non-custom modes omit rules', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    installHappyMock();
  });

  it('all mode (default) submits a body with no rules field', async () => {
    const user = userEvent.setup();
    const { onSave } = await renderWizard();

    await advanceToDispatchStep(user);
    // Leave mode as default 'all' — do not open the custom editor.
    await submitFromDispatch(user);

    await waitFor(() => expect(wasCalled('/config/dispatch')).toBe(true));

    const body = dispatchCallBody();
    expect(body.mode).toBe('all');
    expect(body).not.toHaveProperty('rules');
    // No errors → onSave fires.
    await waitFor(() => expect(onSave).toHaveBeenCalled());
  });

  it('ple_only mode submits a body with no rules field', async () => {
    const user = userEvent.setup();
    await renderWizard();

    await advanceToDispatchStep(user);
    const pleRadio = await screen.findByRole('radio', { name: /Planned Lifecycle Events only/i });
    await user.click(pleRadio);
    await submitFromDispatch(user);

    await waitFor(() => expect(wasCalled('/config/dispatch')).toBe(true));

    const body = dispatchCallBody();
    expect(body.mode).toBe('ple_only');
    expect(body).not.toHaveProperty('rules');
  });
});

// ---------------------------------------------------------------------------
// 3. CRITICAL — activation-halt regression
// ---------------------------------------------------------------------------

describe('ConfigurationWizard — activation halt on dispatch failure (CRITICAL)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('does NOT call /config/activate and does NOT call onSave when dispatch save fails (400)', async () => {
    // Dispatch rejects with a validation-style 400; everything else resolves.
    mockApiFetch.mockImplementation(async (path: string) => {
      if (path === '/config/dispatch') {
        throw new Error('API 400: Invalid dispatch pattern: eventTypePattern must match ^AWS_[A-Z0-9_]+\\*?$');
      }
      if (path === '/config/integrations') return { platforms: [] };
      if (path === '/config/setup-timer') return { elapsed: 0, completed: false };
      if (path === '/config/setup-timer/start') return {};
      if (path === '/config/setup-timer/complete') return {};
      if (path === '/config/routing/validate') return { results: [] };
      return {};
    });

    const user = userEvent.setup();
    const { onSave } = await renderWizard();

    await advanceToDispatchStep(user);
    await submitFromDispatch(user);

    // Dispatch was attempted...
    await waitFor(() => expect(wasCalled('/config/dispatch')).toBe(true));

    // SECURITY INVARIANT: activation must be skipped and success not reported.
    expect(wasCalled('/config/activate')).toBe(false);
    expect(onSave).not.toHaveBeenCalled();
  });

  it('surfaces a friendly dispatch error (not the raw "API 400:" string) on the Review step', async () => {
    mockApiFetch.mockImplementation(async (path: string) => {
      if (path === '/config/dispatch') {
        throw new Error('API 400: Invalid dispatch pattern');
      }
      if (path === '/config/integrations') return { platforms: [] };
      if (path === '/config/setup-timer') return { elapsed: 0, completed: false };
      if (path === '/config/setup-timer/start') return {};
      if (path === '/config/setup-timer/complete') return {};
      if (path === '/config/routing/validate') return { results: [] };
      return {};
    });

    const user = userEvent.setup();
    await renderWizard();

    await advanceToDispatchStep(user);
    await submitFromDispatch(user);

    // Friendly copy (parseApiError strips the "API 400:" prefix to the body).
    await waitFor(() => {
      expect(screen.getByText(/Dispatch window: Invalid dispatch pattern/)).toBeInTheDocument();
    });
    // The raw transport string must not be shown to the user.
    expect(screen.queryByText(/API 400:/)).not.toBeInTheDocument();
  });

  it('DOES call /config/activate and onSave when dispatch save succeeds', async () => {
    installHappyMock();

    const user = userEvent.setup();
    const { onSave } = await renderWizard();

    await advanceToDispatchStep(user);
    await submitFromDispatch(user);

    await waitFor(() => expect(wasCalled('/config/dispatch')).toBe(true));
    await waitFor(() => expect(wasCalled('/config/activate')).toBe(true));
    await waitFor(() => expect(onSave).toHaveBeenCalled());
  });
});
