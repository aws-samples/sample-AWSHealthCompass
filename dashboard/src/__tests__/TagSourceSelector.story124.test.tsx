/**
 * Vitest coverage for the tagSource selector:
 *   Expose a selectable, PERSISTED `tagSource` (resource / account / both) in the
 *   onboarding wizard tag-routing step AND the RoutingEditModal, sent on the
 *   EXISTING POST /config/routing/strategy body, and
 *   round-tripped via the additive `routing.tagRouting.tagSource` summary field.
 *
 * Coverage map:
 *   - selector present on BOTH surfaces only when tag routing enabled
 *   - selecting resource/account/both persists that verbatim value as
 *     `tagSource` on the SAME /config/routing/strategy POST (both surfaces)
 *   - default 'account' is persisted explicitly (never silent); an
 *     unknown/legacy stored value renders as 'account' without error
 *   - round-trip: summary `routing.tagRouting.tagSource` is read back and the
 *     control reflects it (wizard via config prop; modal via /config/summary)
 *   - nested shape preserved: no flat/nested tagRouting or
 *     tagRoutingEnabled ever appears on the wire alongside tagSource
 *
 * Style mirrors the other tag-routing suites: mock ../api, ../config,
 * ../PlatformContext; dynamic-import the component; drive the UI via
 * role/label queries.
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
// Shared helpers
// ---------------------------------------------------------------------------

function strategyBody(): any {
  const call = mockApiFetch.mock.calls.find(c => c[0] === '/config/routing/strategy');
  if (!call) throw new Error('POST /config/routing/strategy was never called');
  return JSON.parse((call[1] as any).body);
}

function wasCalled(path: string): boolean {
  return mockApiFetch.mock.calls.some(c => c[0] === path);
}

/** Every JSON request body sent through apiFetch, as parsed objects. */
function allRequestBodies(): any[] {
  const bodies: any[] = [];
  for (const c of mockApiFetch.mock.calls) {
    const opts = c[1] as any;
    if (opts?.body && typeof opts.body === 'string') {
      try { bodies.push(JSON.parse(opts.body)); } catch { /* non-JSON */ }
    }
  }
  return bodies;
}

// ===========================================================================
// SURFACE 1 — ConfigurationWizard
// ===========================================================================

describe('tagSource selector — ConfigurationWizard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    installWizardHappyMock();
  });

  function installWizardHappyMock() {
    mockApiFetch.mockImplementation(async (path: string, opts?: any) => {
      if (path === '/config/integrations') return { platforms: [] };
      if (path === '/config/setup-timer') return { elapsed: 0, completed: false };
      if (path === '/config/setup-timer/start') return {};
      if (path === '/config/setup-timer/complete') return {};
      if (path === '/config/routing/validate') return { results: [] };
      if (path === '/config/routing/strategy') {
        const b = opts?.body ? JSON.parse(opts.body) : {};
        return {
          mode: b.mode ?? 'account',
          tagKey: b.mode === 'tag' ? b.tagKey ?? null : null,
          tagSource: b.mode === 'tag' ? b.tagSource ?? 'account' : null,
          updatedAt: '2026-08-18T13:21:00Z',
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
    await clickNext(user); // 0 -> 1 (Connection)
    await clickNext(user); // 1 -> 2 (Routing)
    await screen.findByText('Tag-Based Routing');
  }

  async function enableTagRouting(user: ReturnType<typeof userEvent.setup>, key = 'Team') {
    const toggle = await screen.findByRole('checkbox', { name: /Enable tag-based routing/i });
    await user.click(toggle);
    if (key) {
      const input = await screen.findByPlaceholderText('Team');
      await user.type(input, key);
    }
  }

  async function advanceToReviewStep(user: ReturnType<typeof userEvent.setup>) {
    await clickNext(user); // 2 -> 3 (Dispatch)
    await screen.findByText('Which Health events should create tickets?');
    await clickNext(user); // 3 -> 4 (Review)
    await screen.findByText('Configuration Summary');
  }

  async function saveAndActivate(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await screen.findByRole('button', { name: /Save & Activate/i }));
  }

  // Selector hidden until tag routing enabled, then present.
  it('shows the "Tag source" selector only after tag routing is enabled', async () => {
    const user = userEvent.setup();
    await renderWizard();
    await advanceToRoutingStep(user);

    // Not present while tag routing is OFF.
    expect(screen.queryByRole('radio', { name: /Resource tags/i })).not.toBeInTheDocument();

    await enableTagRouting(user, 'Team');

    // Present once enabled; offers exactly resource / account / both.
    expect(await screen.findByRole('radio', { name: /Resource tags/i })).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: /Account tags/i })).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: /Both \(resource, then account\)/i })).toBeInTheDocument();
  });

  // Default is 'account', persisted explicitly (never silent).
  it('defaults to account and persists tagSource:"account" explicitly when the operator makes no choice', async () => {
    const user = userEvent.setup();
    const { onSave } = await renderWizard();
    await advanceToRoutingStep(user);
    await enableTagRouting(user, 'Team');

    // Default selection is visibly 'account'.
    expect(screen.getByRole('radio', { name: /Account tags/i })).toBeChecked();

    await advanceToReviewStep(user);
    await saveAndActivate(user);

    await waitFor(() => expect(wasCalled('/config/routing/strategy')).toBe(true));
    expect(strategyBody().tagSource).toBe('account');   // explicit, not omitted
    await waitFor(() => expect(onSave).toHaveBeenCalled());
  });

  // Selecting resource persists tag_source="resource".
  it('persists tagSource:"resource" when the operator selects Resource tags', async () => {
    const user = userEvent.setup();
    await renderWizard();
    await advanceToRoutingStep(user);
    await enableTagRouting(user, 'Team');

    await user.click(screen.getByRole('radio', { name: /Resource tags/i }));
    await advanceToReviewStep(user);
    await saveAndActivate(user);

    await waitFor(() => expect(wasCalled('/config/routing/strategy')).toBe(true));
    const body = strategyBody();
    expect(body.mode).toBe('tag');
    expect(body.tagKey).toBe('Team');
    expect(body.tagSource).toBe('resource');
  });

  // Selecting both persists tag_source="both".
  it('persists tagSource:"both" when the operator selects Both', async () => {
    const user = userEvent.setup();
    await renderWizard();
    await advanceToRoutingStep(user);
    await enableTagRouting(user, 'Team');

    await user.click(screen.getByRole('radio', { name: /Both \(resource, then account\)/i }));
    await advanceToReviewStep(user);
    await saveAndActivate(user);

    await waitFor(() => expect(wasCalled('/config/routing/strategy')).toBe(true));
    expect(strategyBody().tagSource).toBe('both');
  });

  // Round-trip: a persisted 'resource' config hydrates the selector.
  it('round-trips a persisted "resource" tagSource from the config into the selector', async () => {
    const user = userEvent.setup();
    const config = {
      platform: 'jira',
      platforms: ['jira'],
      jira: { baseUrl: 'https://myorg.atlassian.net', validated: true },
      routing: {
        defaultProject: 'CLOUDOPS',
        tagRouting: { enabled: true, tagKey: 'Team', tagSource: 'resource' },
      },
      dispatch: { mode: 'all' },
    } as unknown as OnboardingConfig;

    await renderWizard(config);
    await advanceToRoutingStep(user);
    await enableTagRouting(user, ''); // reveal the section without retyping the key

    // Reflects the persisted source, not the default.
    expect(screen.getByRole('radio', { name: /Resource tags/i })).toBeChecked();
    expect(screen.getByRole('radio', { name: /Account tags/i })).not.toBeChecked();
  });

  // Unknown/legacy stored value renders as 'account' without error.
  it('renders an unknown/legacy stored tagSource as the safe default "account"', async () => {
    const user = userEvent.setup();
    const config = {
      platform: 'jira',
      platforms: ['jira'],
      jira: { baseUrl: 'https://myorg.atlassian.net', validated: true },
      routing: {
        defaultProject: 'CLOUDOPS',
        tagRouting: { enabled: true, tagKey: 'Team', tagSource: 'bogus-legacy' },
      },
      dispatch: { mode: 'all' },
    } as unknown as OnboardingConfig;

    await renderWizard(config);
    await advanceToRoutingStep(user);
    await enableTagRouting(user, '');

    expect(screen.getByRole('radio', { name: /Account tags/i })).toBeChecked();
    expect(screen.getByRole('radio', { name: /Resource tags/i })).not.toBeChecked();
  });

  // Nested shape preserved; no stray tagRouting on wire.
  it('adds tagSource to the strategy body without introducing any flat/nested tagRouting field', async () => {
    const user = userEvent.setup();
    await renderWizard();
    await advanceToRoutingStep(user);
    await enableTagRouting(user, 'Team');
    await user.click(screen.getByRole('radio', { name: /Resource tags/i }));
    await advanceToReviewStep(user);
    await saveAndActivate(user);

    await waitFor(() => expect(wasCalled('/config/routing/strategy')).toBe(true));
    const body = strategyBody();
    expect(Object.keys(body).sort()).toEqual(['mode', 'tagKey', 'tagSource']);

    for (const b of allRequestBodies()) {
      expect(b).not.toHaveProperty('tagRoutingEnabled');
      expect(b).not.toHaveProperty('tagRouting');
      expect(b?.routing?.tagRouting).toBeUndefined();
    }
  });
});

// ===========================================================================
// SURFACE 2 — RoutingEditModal
// ===========================================================================

describe('tagSource selector — RoutingEditModal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  function makeRoutingApiResponse() {
    return { default: { jiraProject: 'CLOUDOPS', jiraIssueType: 'Task' }, accounts: [], totalAccounts: 0 };
  }

  /** /config/summary shape with tag-routing sub-object (tagSource optional). */
  function makeSummary(opts: { enabled?: boolean; tagKey?: string; tagSource?: string } = {}) {
    const tagRouting: any = { enabled: opts.enabled ?? false, tagKey: opts.tagKey ?? '' };
    if (opts.tagSource !== undefined) tagRouting.tagSource = opts.tagSource;
    return {
      platform: 'jira',
      platforms: ['jira'],
      routing: { defaultProject: 'CLOUDOPS', accountMappingCount: 0, tagRouting },
      dispatch: { mode: 'all' },
      setupComplete: true,
    };
  }

  function installModalMock(summary: any) {
    mockApiFetch.mockImplementation(async (path: string) => {
      if (path === '/config/routing') return makeRoutingApiResponse();
      if (path === '/config/summary') return summary;
      if (path === '/config/routing/discover') return { accounts: [] };
      if (path === '/config/routing/validate') return { results: [] };
      if (path === '/config/routing/default') return { success: true };
      if (path === '/config/routing/strategy') return { success: true };
      return {};
    });
  }

  async function renderModal() {
    const { default: RoutingEditModal } = await import('../modals/RoutingEditModal');
    const onSave = vi.fn();
    const onDismiss = vi.fn();
    const result = render(
      <RoutingEditModal visible={true} onDismiss={onDismiss} onSave={onSave} />
    );
    return { ...result, onSave, onDismiss };
  }

  async function enableTagRouting(user: ReturnType<typeof userEvent.setup>, key = 'Team') {
    const toggle = await screen.findByRole('checkbox', { name: /Enable tag-based routing/i });
    await user.click(toggle);
    const input = await screen.findByPlaceholderText('Team');
    await user.clear(input);
    await user.type(input, key);
  }

  async function saveChanges(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await screen.findByRole('button', { name: /Save Changes/i }));
  }

  // Selector hidden until enabled, then offers all three.
  it('shows the "Tag source" selector only after the toggle is on, offering resource/account/both', async () => {
    installModalMock(makeSummary({ enabled: false }));
    const user = userEvent.setup();
    await renderModal();
    await screen.findByText('Tag-Based Routing');

    expect(screen.queryByRole('radio', { name: /Resource tags/i })).not.toBeInTheDocument();

    await enableTagRouting(user, 'Team');

    expect(await screen.findByRole('radio', { name: /Resource tags/i })).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: /Account tags/i })).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: /Both \(resource, then account\)/i })).toBeInTheDocument();
  });

  // Default 'account' when enabling fresh.
  it('defaults the selector to account when enabling tag routing on a fresh config', async () => {
    installModalMock(makeSummary({ enabled: false }));
    const user = userEvent.setup();
    await renderModal();
    await screen.findByText('Tag-Based Routing');
    await enableTagRouting(user, 'Team');
    expect(screen.getByRole('radio', { name: /Account tags/i })).toBeChecked();
  });

  // Selecting resource persists tag_source="resource".
  it('persists tagSource:"resource" on the strategy POST when Resource tags is selected', async () => {
    installModalMock(makeSummary({ enabled: false }));
    const user = userEvent.setup();
    const { onSave } = await renderModal();
    await screen.findByText('Tag-Based Routing');
    await enableTagRouting(user, 'Team');
    await user.click(screen.getByRole('radio', { name: /Resource tags/i }));
    await saveChanges(user);

    await waitFor(() => expect(wasCalled('/config/routing/strategy')).toBe(true));
    const body = strategyBody();
    expect(body.mode).toBe('tag');
    expect(body.tagKey).toBe('Team');
    expect(body.tagSource).toBe('resource');
    await waitFor(() => expect(onSave).toHaveBeenCalled());
  });

  // Selecting both persists tag_source="both".
  it('persists tagSource:"both" when Both is selected', async () => {
    installModalMock(makeSummary({ enabled: false }));
    const user = userEvent.setup();
    await renderModal();
    await screen.findByText('Tag-Based Routing');
    await enableTagRouting(user, 'Team');
    await user.click(screen.getByRole('radio', { name: /Both \(resource, then account\)/i }));
    await saveChanges(user);

    await waitFor(() => expect(wasCalled('/config/routing/strategy')).toBe(true));
    expect(strategyBody().tagSource).toBe('both');
  });

  // Round-trip: modal reads back persisted tagSource from /config/summary.
  it('round-trips a persisted "both" tagSource from /config/summary into the selector', async () => {
    installModalMock(makeSummary({ enabled: true, tagKey: 'Team', tagSource: 'both' }));
    await renderModal();
    // Section renders because summary.enabled=true → selector reflects 'both'.
    expect(await screen.findByRole('radio', { name: /Both \(resource, then account\)/i })).toBeChecked();
    expect(screen.getByRole('radio', { name: /Account tags/i })).not.toBeChecked();
  });

  // Legacy/unknown persisted value renders as safe default 'account'.
  it('renders an unknown persisted tagSource as the safe default "account"', async () => {
    installModalMock(makeSummary({ enabled: true, tagKey: 'Team', tagSource: 'legacy-value' }));
    await renderModal();
    expect(await screen.findByRole('radio', { name: /Account tags/i })).toBeChecked();
    expect(screen.getByRole('radio', { name: /Resource tags/i })).not.toBeChecked();
  });

  // Nested shape preserved on the wire.
  it('adds only tagSource to the strategy body — no flat/nested tagRouting field', async () => {
    installModalMock(makeSummary({ enabled: false }));
    const user = userEvent.setup();
    await renderModal();
    await screen.findByText('Tag-Based Routing');
    await enableTagRouting(user, 'Team');
    await user.click(screen.getByRole('radio', { name: /Resource tags/i }));
    await saveChanges(user);

    await waitFor(() => expect(wasCalled('/config/routing/strategy')).toBe(true));
    const body = strategyBody();
    expect(Object.keys(body).sort()).toEqual(['mode', 'tagKey', 'tagSource']);
    for (const b of allRequestBodies()) {
      expect(b).not.toHaveProperty('tagRoutingEnabled');
      expect(b).not.toHaveProperty('tagRouting');
    }
  });
});
