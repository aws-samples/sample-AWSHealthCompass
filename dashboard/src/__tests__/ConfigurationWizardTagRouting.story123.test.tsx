/**
 * Unit/integration tests for the resource-tag routing strategy:
 *   Persist the wizard's tag-routing strategy on "Save & Activate" (previously a
 *   cosmetic no-op) via the already-deployed POST /config/routing/strategy, and
 *   make the Review step's "Tag Routing: Enabled" confirmation TRUE — an
 *   affirmative green "Enabled" appears ONLY after a successful HTTP 200 persist.
 *
 * Coverage map:
 *   - strategy POST {mode:'tag', tagKey} sequenced BEFORE
 *     /config/activate (and after account-mappings, before dispatch)
 *   - toggle off → POST {mode:'account'}; no mode:'tag';
 *     Review reads "Disabled"; never green "Enabled"
 *   - strategy 500 → error surfaced in saveErrors, onSave NOT
 *     called, Review reads "Not enabled" (no false success)
 *   - strategy 400 → friendly parseApiError copy (no raw "API 400:")
 *   - green "Enabled (key: …)" ONLY in 'saved' (post-200)
 *   - editing the key after a save resets to unsaved
 *     (pending "will be activated"), never a stale green "Enabled"
 *   - flat {mode,tagKey} body byte-compatible with the modal
 *     write path; NO parallel/flat tagRoutingEnabled, NO nested
 *     routing.tagRouting payload, NO tagSource on the wire
 *   - a tag-strategy failure does NOT gate /config/activate
 *     (independent of the dispatch invariant)
 *   - blank-key submit guard blocks leaving Routing and shows
 *     errorText; no strategy POST is made
 *   - dispatch-failure regression — the dispatch-failure activation-halt invariant
 *     still holds with the new Step 3.5 present (activate + onSave skipped)
 *
 * Style mirrors ConfigurationWizardDispatch.test.tsx / ...ReviewStep.test.tsx:
 * mock ../api, ../config, ../PlatformContext; dynamic-import the component; drive
 * the Cloudscape Wizard via its Next / Previous / Save & Activate buttons.
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

/**
 * Default happy-path mock: every endpoint resolves. The strategy endpoint
 * returns the authoritative 200 body shape (mode, tagKey, tagSource, updatedAt)
 * that handle_routing_strategy returns; the wizard sets 'saved' from this 200,
 * not from a re-GET (eventual-consistency).
 */
function installHappyMock() {
  mockApiFetch.mockImplementation(async (path: string, opts?: any) => {
    if (path === '/config/integrations') return { platforms: [] };
    if (path === '/config/setup-timer') return { elapsed: 0, completed: false };
    if (path === '/config/setup-timer/start') return {};
    if (path === '/config/setup-timer/complete') return {};
    if (path === '/config/routing/validate') return { results: [] };
    if (path === '/config/routing/strategy') {
      const body = opts?.body ? JSON.parse(opts.body) : {};
      return {
        mode: body.mode ?? 'account',
        tagKey: body.mode === 'tag' ? body.tagKey ?? null : null,
        tagSource: body.mode === 'tag' ? 'account' : null,
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
  const next = await screen.findByRole('button', { name: /^Next$/ });
  await user.click(next);
}

async function clickPrevious(user: ReturnType<typeof userEvent.setup>) {
  const prev = await screen.findByRole('button', { name: /^Previous$/ });
  await user.click(prev);
}

/** Advance from Step 0 (Platform) to Step 2 (Routing). */
async function advanceToRoutingStep(user: ReturnType<typeof userEvent.setup>) {
  await clickNext(user); // 0 -> 1 (Connection)
  await clickNext(user); // 1 -> 2 (Routing)
  await screen.findByText('Tag-Based Routing');
}

/** Enable the tag-routing toggle and (optionally) type a tag key. */
async function enableTagRouting(user: ReturnType<typeof userEvent.setup>, key?: string) {
  const toggle = await screen.findByRole('checkbox', { name: /Enable tag-based routing/i });
  await user.click(toggle);
  if (key) {
    const input = await screen.findByPlaceholderText('Team');
    await user.type(input, key);
  }
}

/** From Routing (step 2), advance to Review (step 4). */
async function advanceToReviewStep(user: ReturnType<typeof userEvent.setup>) {
  await clickNext(user); // 2 -> 3 (Dispatch)
  await screen.findByText('Which Health events should create tickets?');
  await clickNext(user); // 3 -> 4 (Review)
  await screen.findByText('Configuration Summary');
}

async function clickSaveAndActivate(user: ReturnType<typeof userEvent.setup>) {
  const submit = await screen.findByRole('button', { name: /Save & Activate/i });
  await user.click(submit);
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

/** Every request body sent through apiFetch that has a JSON body, as parsed objects. */
function allRequestBodies(): any[] {
  const bodies: any[] = [];
  for (const c of mockApiFetch.mock.calls) {
    const opts = c[1] as any;
    if (opts?.body && typeof opts.body === 'string') {
      try { bodies.push(JSON.parse(opts.body)); } catch { /* non-JSON body */ }
    }
  }
  return bodies;
}

// ===========================================================================
// Strategy persisted when enabled, sequenced before activate
// ===========================================================================

describe('ConfigurationWizard tag routing — persists strategy on Save & Activate', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    installHappyMock();
  });

  it('POSTs /config/routing/strategy {mode:"tag", tagKey} and sequences it before /config/activate', async () => {
    const user = userEvent.setup();
    const { onSave } = await renderWizard();

    await advanceToRoutingStep(user);
    await enableTagRouting(user, 'Team');
    await advanceToReviewStep(user);
    await clickSaveAndActivate(user);

    await waitFor(() => expect(wasCalled('/config/routing/strategy')).toBe(true));

    const body = strategyBody();
    expect(body.mode).toBe('tag');
    expect(body.tagKey).toBe('Team');

    // Sequencing: after account-mappings (none here) and dispatch
    // ordering — strategy MUST precede both dispatch and activate.
    await waitFor(() => expect(wasCalled('/config/activate')).toBe(true));
    const idxStrategy = callIndex('/config/routing/strategy');
    const idxDispatch = callIndex('/config/dispatch');
    const idxActivate = callIndex('/config/activate');
    expect(idxStrategy).toBeGreaterThanOrEqual(0);
    expect(idxStrategy).toBeLessThan(idxDispatch);   // Step 3.5 before Step 4 (dispatch)
    expect(idxStrategy).toBeLessThan(idxActivate);    // ...and before Step 5 (activate)

    // Happy path completes.
    await waitFor(() => expect(onSave).toHaveBeenCalled());
  });

  it('trims the tag key on the wire (raw kept in state, trim only at submit)', async () => {
    const user = userEvent.setup();
    await renderWizard();

    await advanceToRoutingStep(user);
    // Cannot type leading spaces reliably through the guard/nav, so append a
    // trailing space and confirm it is trimmed in the POST body.
    await enableTagRouting(user, 'Team ');
    await advanceToReviewStep(user);
    await clickSaveAndActivate(user);

    await waitFor(() => expect(wasCalled('/config/routing/strategy')).toBe(true));
    expect(strategyBody().tagKey).toBe('Team');
  });
});

// ===========================================================================
// Review shows green "Enabled" ONLY after 200
// ===========================================================================

describe('ConfigurationWizard tag routing — Review confirmation is truthful', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    installHappyMock();
  });

  it('shows a pending (NOT green) confirmation before Save, then the bare green "Enabled" only after a 200', async () => {
    const user = userEvent.setup();
    await renderWizard();

    await advanceToRoutingStep(user);
    await enableTagRouting(user, 'Team');
    await advanceToReviewStep(user);

    // BEFORE Save: unsaved+key → pending "will be activated" (not an affirmative
    // green Enabled). The anchored success copy must NOT be present yet.
    // The Review token now carries the persisted tag source
    // ("source: Account tags" — the default selection).
    // The summary now also appends a mappings clause (" · no mappings
    // yet" when none exist). Assertions updated to the current contract
    // (renderTagRoutingSummary) — the green-vs-pending distinction (the
    // "— will be activated" suffix) is unchanged and still the thing under test.
    expect(screen.getByText(/Enabled \(key: Team, source: Account tags\) · no mappings yet — will be activated on Save & Activate/)).toBeInTheDocument();
    expect(screen.queryByText(/^Enabled \(key: Team, source: Account tags\) · no mappings yet$/)).not.toBeInTheDocument();

    await clickSaveAndActivate(user);

    // AFTER a 200: bare green success (no pending "will be activated" suffix),
    // including the source token and the mappings clause.
    await waitFor(() =>
      expect(screen.getByText(/^Enabled \(key: Team, source: Account tags\) · no mappings yet$/)).toBeInTheDocument()
    );
    expect(screen.queryByText(/will be activated on Save & Activate/)).not.toBeInTheDocument();
  });
});

// ===========================================================================
// Toggle off → mode:'account', never "Enabled"
// ===========================================================================

describe('ConfigurationWizard tag routing — disabled path writes account mode', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    installHappyMock();
  });

  it('POSTs {mode:"account"} (no mode:"tag", no tagKey) and Review reads "Disabled"', async () => {
    const user = userEvent.setup();
    const { onSave } = await renderWizard();

    await advanceToRoutingStep(user);
    // Leave the toggle OFF.
    await advanceToReviewStep(user);

    // Review row reads "Disabled" (plain text) — never "Enabled".
    expect(screen.getByText('Disabled')).toBeInTheDocument();

    await clickSaveAndActivate(user);

    await waitFor(() => expect(wasCalled('/config/routing/strategy')).toBe(true));
    const body = strategyBody();
    expect(body.mode).toBe('account');
    expect(body.mode).not.toBe('tag');
    expect(body.tagKey).toBeUndefined(); // JSON.stringify drops the undefined key

    // No false "Enabled" anywhere.
    expect(screen.queryByText(/^Enabled \(key:/)).not.toBeInTheDocument();
    await waitFor(() => expect(onSave).toHaveBeenCalled());
  });
});

// ===========================================================================
// Save failure surfaces + blocks a false success
// ===========================================================================

describe('ConfigurationWizard tag routing — save failure is honest', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('on a 500, surfaces "Tag routing: …" in saveErrors, reads "Not enabled", and does NOT call onSave', async () => {
    mockApiFetch.mockImplementation(async (path: string) => {
      if (path === '/config/routing/strategy') {
        throw new Error('API 500: ConfigTable put_item failed');
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

    await advanceToRoutingStep(user);
    await enableTagRouting(user, 'Team');
    await advanceToReviewStep(user);
    await clickSaveAndActivate(user);

    await waitFor(() => expect(wasCalled('/config/routing/strategy')).toBe(true));

    // Failure itemized in the existing saveErrors Alert, via parseApiError (500 → friendly).
    await waitFor(() =>
      expect(screen.getByText(/Tag routing: An unexpected error occurred/)).toBeInTheDocument()
    );
    // Review row explicitly reads "Not enabled" — never a green "Enabled".
    expect(screen.getByText(/Not enabled — strategy save failed/)).toBeInTheDocument();
    expect(screen.queryByText(/^Enabled \(key: Team\)$/)).not.toBeInTheDocument();

    // No false success.
    expect(onSave).not.toHaveBeenCalled();
  });

  it('on a 400, surfaces the friendly parseApiError body (never the raw "API 400:" string)', async () => {
    mockApiFetch.mockImplementation(async (path: string) => {
      if (path === '/config/routing/strategy') {
        throw new Error("API 400: tagKey exceeds maximum length");
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

    await advanceToRoutingStep(user);
    await enableTagRouting(user, 'Team');
    await advanceToReviewStep(user);
    await clickSaveAndActivate(user);

    await waitFor(() =>
      expect(screen.getByText(/Tag routing: tagKey exceeds maximum length/)).toBeInTheDocument()
    );
    expect(screen.queryByText(/API 400:/)).not.toBeInTheDocument();
  });
});

// ===========================================================================
// Tag failure does NOT gate /config/activate
// ===========================================================================

describe('ConfigurationWizard tag routing — failure is independent of the activate gate', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('when only the strategy POST fails, /config/activate STILL runs (unlike the dispatch gate) but onSave does NOT', async () => {
    mockApiFetch.mockImplementation(async (path: string) => {
      if (path === '/config/routing/strategy') {
        throw new Error('API 500: transient');
      }
      // dispatch + everything else succeed
      if (path === '/config/integrations') return { platforms: [] };
      if (path === '/config/setup-timer') return { elapsed: 0, completed: false };
      if (path === '/config/setup-timer/start') return {};
      if (path === '/config/setup-timer/complete') return {};
      if (path === '/config/routing/validate') return { results: [] };
      return {};
    });

    const user = userEvent.setup();
    const { onSave } = await renderWizard();

    await advanceToRoutingStep(user);
    await enableTagRouting(user, 'Team');
    await advanceToReviewStep(user);
    await clickSaveAndActivate(user);

    await waitFor(() => expect(wasCalled('/config/routing/strategy')).toBe(true));
    await waitFor(() => expect(wasCalled('/config/dispatch')).toBe(true));

    // A tag-strategy failure has no dispatch-widening/security consequence,
    // so it does NOT set dispatchFailed and does NOT block activation.
    await waitFor(() => expect(wasCalled('/config/activate')).toBe(true));
    // ...but the overall flow still reports failure (errors present → no onSave).
    expect(onSave).not.toHaveBeenCalled();
  });
});

// ===========================================================================
// Dispatch-failure regression — activation halt on dispatch failure still holds
// ===========================================================================

describe('ConfigurationWizard tag routing — activation-halt still respected (regression)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('with tag routing enabled AND dispatch failing: strategy is still POSTed, but /config/activate and onSave are skipped', async () => {
    mockApiFetch.mockImplementation(async (path: string, opts?: any) => {
      if (path === '/config/dispatch') {
        throw new Error('API 400: Invalid dispatch pattern');
      }
      if (path === '/config/routing/strategy') {
        const body = opts?.body ? JSON.parse(opts.body) : {};
        return { mode: body.mode, tagKey: body.tagKey ?? null, tagSource: 'account', updatedAt: 'x' };
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

    await advanceToRoutingStep(user);
    await enableTagRouting(user, 'Team');
    await advanceToReviewStep(user);
    await clickSaveAndActivate(user);

    // Strategy runs (Step 3.5) before dispatch (Step 4).
    await waitFor(() => expect(wasCalled('/config/routing/strategy')).toBe(true));
    await waitFor(() => expect(wasCalled('/config/dispatch')).toBe(true));
    expect(callIndex('/config/routing/strategy')).toBeLessThan(callIndex('/config/dispatch'));

    // SECURITY INVARIANT: dispatch failure halts activation + success.
    expect(wasCalled('/config/activate')).toBe(false);
    expect(onSave).not.toHaveBeenCalled();

    // The dispatch failure is surfaced (friendly copy).
    await waitFor(() =>
      expect(screen.getByText(/Dispatch window: Invalid dispatch pattern/)).toBeInTheDocument()
    );
  });
});

// ===========================================================================
// Blank-key submit guard
// ===========================================================================

describe('ConfigurationWizard tag routing — blank-key submit guard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    installHappyMock();
  });

  it('blocks advancing past Routing when enabled with a blank key, shows errorText, and makes no strategy POST', async () => {
    const user = userEvent.setup();
    await renderWizard();

    await advanceToRoutingStep(user);
    await enableTagRouting(user); // toggle on, NO key

    // errorText is shown on the Tag Key field as soon as the toggle is on + blank.
    expect(screen.getByText('Tag key is required when tag routing is enabled.')).toBeInTheDocument();

    // Attempt to advance past Routing — the guard returns early (navigation blocked).
    await clickNext(user);

    // Still on the Routing step; the Review step was never reached.
    expect(screen.getByText('Tag-Based Routing')).toBeInTheDocument();
    expect(screen.queryByText('Configuration Summary')).not.toBeInTheDocument();

    // No strategy POST was made (saveAll never ran).
    expect(wasCalled('/config/routing/strategy')).toBe(false);
  });
});

// ===========================================================================
// Editing the key after a save resets the confirmation
// ===========================================================================

describe('ConfigurationWizard tag routing — editing after save invalidates the confirmation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    installHappyMock();
  });

  it('after a successful save, editing the tag key resets Review to pending (no stale green "Enabled")', async () => {
    const user = userEvent.setup();
    await renderWizard();

    await advanceToRoutingStep(user);
    await enableTagRouting(user, 'Team');
    await advanceToReviewStep(user);
    await clickSaveAndActivate(user);

    // Confirm we reached the green success state (source token;
    // " · no mappings yet" clause appended).
    await waitFor(() =>
      expect(screen.getByText(/^Enabled \(key: Team, source: Account tags\) · no mappings yet$/)).toBeInTheDocument()
    );

    // Navigate back to the Routing step and edit the key.
    await clickPrevious(user); // 4 -> 3 (Dispatch)
    await clickPrevious(user); // 3 -> 2 (Routing)
    await screen.findByText('Tag-Based Routing');
    const input = await screen.findByPlaceholderText('Team');
    await user.type(input, 's'); // "Teams" → resets tagRoutingSaveState to 'unsaved'

    // Return to Review — the confirmation must be back to pending, never a stale green.
    await advanceToReviewStep(user);
    expect(screen.getByText(/Enabled \(key: Teams, source: Account tags\) · no mappings yet — will be activated on Save & Activate/)).toBeInTheDocument();
    expect(screen.queryByText(/^Enabled \(key: Teams, source: Account tags\) · no mappings yet$/)).not.toBeInTheDocument();
  });
});

// ===========================================================================
// Nested-shape write path preserved
// ===========================================================================

describe('ConfigurationWizard tag routing — nested-shape write path preserved', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    installHappyMock();
  });

  it('sends a flat {mode,tagKey,tagSource} body identical to the modal write path — no flat/nested tagRouting field anywhere', async () => {
    const user = userEvent.setup();
    await renderWizard();

    await advanceToRoutingStep(user);
    await enableTagRouting(user, 'Team');
    await advanceToReviewStep(user);
    await clickSaveAndActivate(user);

    await waitFor(() => expect(wasCalled('/config/routing/strategy')).toBe(true));

    // Byte-compatible with RoutingEditModal.handleSave Step 4.
    // The strategy body now carries `tagSource` on BOTH
    // surfaces (default 'account' when the operator makes no explicit choice),
    // so the wire shape is {mode, tagKey, tagSource} — still identical to the
    // modal write path. (Superseded the earlier "no tagSource" assertion.)
    const body = strategyBody();
    expect(Object.keys(body).sort()).toEqual(['mode', 'tagKey', 'tagSource']);
    expect(body.tagSource).toBe('account');

    // No request anywhere introduces a parallel/flat tagRoutingEnabled or a nested
    // routing.tagRouting payload — the single source of truth stays ROUTING_STRATEGY.
    for (const b of allRequestBodies()) {
      expect(b).not.toHaveProperty('tagRoutingEnabled');
      expect(b).not.toHaveProperty('tagRouting');
      expect(b?.routing?.tagRouting).toBeUndefined();
    }
  });

  it('loads tag mappings on mount (GET) and makes NO upsert POST when no mappings are added', async () => {
    const user = userEvent.setup();
    await renderWizard();

    await advanceToRoutingStep(user);
    await enableTagRouting(user, 'Team');
    await advanceToReviewStep(user);
    await clickSaveAndActivate(user);

    await waitFor(() => expect(wasCalled('/config/routing/strategy')).toBe(true));

    // The editor round-trips existing mappings via a mount-time GET
    // With no mappings added in this flow, saveAll's persistTagMappings
    // guard (upserts.length || removed.length) skips the upsert POST entirely.
    const tagsCalls = mockApiFetch.mock.calls.filter(c => c[0] === '/config/routing/tags');
    const tagsGet = tagsCalls.filter(c => (((c[1] as any)?.method) ?? 'GET') === 'GET');
    const tagsPost = tagsCalls.filter(c => ((c[1] as any)?.method) === 'POST');
    expect(tagsGet.length).toBeGreaterThan(0); // mount round-trip load
    expect(tagsPost.length).toBe(0);           // nothing to persist -> no POST
  });
});
