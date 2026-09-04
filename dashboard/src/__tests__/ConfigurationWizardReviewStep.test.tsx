/**
 * Unit tests for the removal of the dead telemetry consent checkbox from
 * the onboarding wizard's Review & Activate step.
 *
 * Context: the Review step rendered
 * a "Telemetry" `<Container>` with a `<Checkbox>` bound to a `telemetryConsent`
 * local `useState`. That state was never passed to `saveAll()` and never sent
 * in any `apiFetch` call or persisted anywhere — checking/unchecking it did
 * nothing. Both the dead state declaration and the JSX block were removed
 * from `renderReviewStep()` in `ConfigurationWizard.tsx`. This file locks in
 * that removal (dead-state regression coverage) and verifies the rest of the
 * Review step — the "Configuration Summary" container and the
 * "Save & Activate" submit flow — is unaffected.
 *
 * NOTE ON SCOPE: `ConfigurationSummary.tsx` (the *separate*, post-activation
 * summary view) still has an unrelated read-only display of
 * `(config as any).telemetryConsent` in its "System Information" section.
 * That component is NOT touched by this change and is therefore intentionally
 * NOT covered by this file, which is
 * scoped strictly to the wizard (`ConfigurationWizard.tsx`).
 *
 * Style mirrors ConfigurationWizardDispatch.test.tsx: mock ../api, ../config,
 * ../PlatformContext; dynamic-import the component; drive the Cloudscape
 * Wizard via its Next / Save & Activate buttons.
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
// Helpers (mirrors ConfigurationWizardDispatch.test.tsx)
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

/** Advance from Step 0 (Platform) all the way to Step 4 (Review & Activate). */
async function advanceToReviewStep(user: ReturnType<typeof userEvent.setup>) {
  await clickNext(user); // 0 -> 1 (Connection)
  await clickNext(user); // 1 -> 2 (Routing)
  await clickNext(user); // 2 -> 3 (Dispatch) — routing validate returns valid (no targets)
  await screen.findByText('Which Health events should create tickets?');
  await clickNext(user); // 3 -> 4 (Review)
  await screen.findByText('Configuration Summary');
}

function wasCalled(path: string): boolean {
  return mockApiFetch.mock.calls.some(c => c[0] === path);
}

// ---------------------------------------------------------------------------
// 1. Telemetry checkbox / copy no longer render on the Review step
// ---------------------------------------------------------------------------

describe('ConfigurationWizard Review step — telemetry checkbox removed', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    installHappyMock();
  });

  it('does not render a "Telemetry" header or any telemetry-related copy', async () => {
    const user = userEvent.setup();
    await renderWizard();

    await advanceToReviewStep(user);

    expect(screen.queryByText(/Telemetry/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/anonymized usage metrics/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/consent/i)).not.toBeInTheDocument();
  });

  it('renders no checkbox at all on the Review step (the removed telemetry checkbox was the only one)', async () => {
    const user = userEvent.setup();
    await renderWizard();

    await advanceToReviewStep(user);

    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
  });

  it('never issues an apiFetch call referencing telemetry (dead state had no wire path, and still has none)', async () => {
    const user = userEvent.setup();
    await renderWizard();

    await advanceToReviewStep(user);

    const submit = await screen.findByRole('button', { name: /Save & Activate/i });
    await user.click(submit);

    await waitFor(() => expect(mockApiFetch).toHaveBeenCalled());

    const telemetryCalls = mockApiFetch.mock.calls.filter(c =>
      typeof c[0] === 'string' && /telemetry/i.test(c[0])
    );
    expect(telemetryCalls).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// 2. Rest of the Review step still renders and functions correctly
// ---------------------------------------------------------------------------

describe('ConfigurationWizard Review step — Configuration Summary unaffected', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    installHappyMock();
  });

  it('still renders the Configuration Summary container with its field labels', async () => {
    const user = userEvent.setup();
    await renderWizard();

    await advanceToReviewStep(user);

    // "Configuration Summary" and most field labels are unique to the summary
    // container. "Connection" and "Dispatch Window" are ALSO used as wizard
    // step-navigation titles (Cloudscape keeps the nav rendered alongside the
    // active step's content), so those two are asserted via getAllByText
    // (present at least once) rather than the single-match getByText used for
    // labels that are unique to the summary itself.
    expect(screen.getByText('Configuration Summary')).toBeInTheDocument();
    expect(screen.getByText('Enabled Platforms')).toBeInTheDocument();
    expect(screen.getByText('Account Mappings')).toBeInTheDocument();
    expect(screen.getByText('Actionability')).toBeInTheDocument();
    expect(screen.getAllByText('Connection').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('Dispatch Window').length).toBeGreaterThanOrEqual(1);
  });

  it('still renders an enabled "Save & Activate" button', async () => {
    const user = userEvent.setup();
    await renderWizard();

    await advanceToReviewStep(user);

    const submit = await screen.findByRole('button', { name: /Save & Activate/i });
    expect(submit).toBeInTheDocument();
    expect(submit).not.toBeDisabled();
  });

  it('"Save & Activate" still saves config and activates on the happy path (onSave fires)', async () => {
    const user = userEvent.setup();
    const { onSave } = await renderWizard();

    await advanceToReviewStep(user);

    const submit = await screen.findByRole('button', { name: /Save & Activate/i });
    await user.click(submit);

    await waitFor(() => expect(wasCalled('/config/dispatch')).toBe(true));
    await waitFor(() => expect(wasCalled('/config/activate')).toBe(true));
    await waitFor(() => expect(onSave).toHaveBeenCalled());
  });
});
