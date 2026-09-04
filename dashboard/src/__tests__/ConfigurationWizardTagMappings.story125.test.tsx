/**
 * STORY-125 (RT-03) — wizard-level integration coverage: the tag-mapping editor
 * wired into ConfigurationWizard's mount-load (GET round-trip) and Save &
 * Activate path (POST /config/routing/tags), with the STORY-113 nested shape
 * preserved on the wire.
 *
 * Source of truth: 01_hermione_story.md (AC-2/AC-3/AC-7), 10_harry_code.md
 * (saveAll Step 3.6), 12_luna_interface_validation.md.
 *
 * Coverage:
 *   AC-3 — GET /config/routing/tags is called on wizard mount (round-trip load)
 *   AC-2 — a mapping added in the tag-routing step is persisted via
 *          POST /config/routing/tags {mappings:[…]} on Save & Activate,
 *          sequenced AFTER the strategy POST and BEFORE /config/activate
 *   AC-7 — STORY-113 nested shape preserved: no flat `tagRoutingEnabled`, no
 *          nested `routing.tagRouting` object ever appears on the wire; the
 *          strategy body stays the flat {mode:'tag', tagKey}
 *
 * Scaffold mirrors ConfigurationWizardTagRouting.story123.test.tsx.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';

vi.mock('../api', () => ({ apiFetch: vi.fn() }));
vi.mock('../config', () => ({
  getConfig: () => ({ userPoolId: 'fake-pool', clientId: 'fake-client', apiUrl: 'http://localhost:3000', region: 'us-east-1' }),
  loadConfig: vi.fn().mockResolvedValue({ userPoolId: 'fake-pool', clientId: 'fake-client', apiUrl: 'http://localhost:3000', region: 'us-east-1' }),
}));
vi.mock('../PlatformContext', () => ({
  PlatformProvider: ({ children }: any) => <>{children}</>,
  usePlatformLabels: () => ({
    connectionTitle: 'JIRA Connection', projectLabel: 'JIRA Project', platform: 'jira',
    routingPlaceholder: 'CLOUDOPS', routingTarget: 'JIRA Project', bulkFormat: 'account_id,jira_project',
  }),
}));

import { apiFetch } from '../api';
import type { OnboardingConfig } from '../types';

const mockApiFetch = vi.mocked(apiFetch);

function installHappyMock() {
  mockApiFetch.mockImplementation(async (path: string, opts?: any) => {
    const method = opts?.method ?? 'GET';
    if (path === '/config/integrations') return { platforms: [] };
    if (path === '/config/setup-timer') return { elapsed: 0, completed: false };
    if (path === '/config/setup-timer/start') return {};
    if (path === '/config/setup-timer/complete') return {};
    if (path === '/config/routing/validate') return { results: [] };
    if (path === '/config/routing/tags' && method === 'GET') return { mappings: [], total: 0 };
    if (path === '/config/routing/tags' && method === 'POST') return { created: 1, updated: 0, validationErrors: [] };
    if (path === '/config/routing/strategy') {
      const b = opts?.body ? JSON.parse(opts.body) : {};
      return { mode: b.mode ?? 'account', tagKey: b.mode === 'tag' ? b.tagKey ?? null : null, tagSource: b.mode === 'tag' ? 'account' : null, updatedAt: '2026-08-19T00:00:00Z' };
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

const clickNext = async (u: ReturnType<typeof userEvent.setup>) =>
  u.click(await screen.findByRole('button', { name: /^Next$/ }));

async function advanceToRoutingStep(u: ReturnType<typeof userEvent.setup>) {
  await clickNext(u); // Platform -> Connection
  await clickNext(u); // Connection -> Routing
  await screen.findByText('Tag-Based Routing');
}

async function enableTagRouting(u: ReturnType<typeof userEvent.setup>, key: string) {
  await u.click(await screen.findByRole('checkbox', { name: /Enable tag-based routing/i }));
  await u.type(await screen.findByPlaceholderText('Team'), key);
}

async function advanceToReview(u: ReturnType<typeof userEvent.setup>) {
  await clickNext(u); // Routing -> Dispatch
  await screen.findByText('Which Health events should create tickets?');
  await clickNext(u); // Dispatch -> Review
  await screen.findByText('Configuration Summary');
}

const clickSave = async (u: ReturnType<typeof userEvent.setup>) =>
  u.click(await screen.findByRole('button', { name: /Save & Activate/i }));

function callsTo(path: string, method?: string) {
  return mockApiFetch.mock.calls.filter(c => {
    const m = ((c[1] as any)?.method) ?? 'GET';
    return c[0] === path && (method ? m === method : true);
  });
}
function firstIndex(path: string, method?: string) {
  return mockApiFetch.mock.calls.findIndex(c => {
    const m = ((c[1] as any)?.method) ?? 'GET';
    return c[0] === path && (method ? m === method : true);
  });
}
function allBodies(): any[] {
  const out: any[] = [];
  for (const c of mockApiFetch.mock.calls) {
    const b = (c[1] as any)?.body;
    if (typeof b === 'string') { try { out.push(JSON.parse(b)); } catch { /* skip */ } }
  }
  return out;
}

beforeEach(() => { vi.clearAllMocks(); installHappyMock(); });

describe('STORY-125 wizard integration — mount load + save wiring + STORY-113 shape', () => {
  it('AC-3: GET /config/routing/tags is called on wizard mount (round-trip load)', async () => {
    await renderWizard();
    // loadTagMappings() fires from a mount effect.
    await vi.waitFor(() => expect(callsTo('/config/routing/tags', 'GET').length).toBeGreaterThan(0));
  });

  it('AC-2: a mapping added in the tag-routing step is POSTed to /config/routing/tags on Save & Activate, after strategy and before activate', async () => {
    const user = userEvent.setup();
    await renderWizard();
    await advanceToRoutingStep(user);
    await enableTagRouting(user, 'Team');

    // Add a mapping via the editor (mounted in the Tag-Based Routing container).
    await user.type(screen.getByLabelText('New tag value'), 'platform');
    await user.type(screen.getByLabelText('JIRA project for new tag value'), 'CLOUDOPS');
    await user.click(screen.getByTestId('add-tag-mapping-row'));

    await advanceToReview(user);
    await clickSave(user);

    await vi.waitFor(() => expect(callsTo('/config/routing/tags', 'POST').length).toBe(1));
    const post = callsTo('/config/routing/tags', 'POST')[0];
    const body = JSON.parse((post[1] as any).body);
    expect(body.mappings).toEqual([
      { tagValue: 'platform', jiraProject: 'CLOUDOPS', jiraIssueType: 'Task' },
    ]);

    // Sequencing: strategy POST < tag-mappings POST < activate.
    const iStrategy = firstIndex('/config/routing/strategy', 'POST');
    const iTags = firstIndex('/config/routing/tags', 'POST');
    const iActivate = firstIndex('/config/activate', 'POST');
    expect(iStrategy).toBeGreaterThanOrEqual(0);
    expect(iStrategy).toBeLessThan(iTags);
    expect(iTags).toBeLessThan(iActivate);
  });

  it('AC-7: STORY-113 nested shape preserved — no flat tagRoutingEnabled and no nested routing.tagRouting on the wire', async () => {
    const user = userEvent.setup();
    await renderWizard();
    await advanceToRoutingStep(user);
    await enableTagRouting(user, 'Team');
    await user.type(screen.getByLabelText('New tag value'), 'platform');
    await user.type(screen.getByLabelText('JIRA project for new tag value'), 'CLOUDOPS');
    await user.click(screen.getByTestId('add-tag-mapping-row'));
    await advanceToReview(user);
    await clickSave(user);
    await vi.waitFor(() => expect(callsTo('/config/routing/tags', 'POST').length).toBe(1));

    const bodies = allBodies();
    for (const b of bodies) {
      expect(b).not.toHaveProperty('tagRoutingEnabled');
      expect(b?.routing?.tagRouting).toBeUndefined();
    }
    // The strategy body stays the flat {mode:'tag', tagKey} STORY-113 contract.
    const strategyBody = bodies.find(b => b.mode === 'tag');
    expect(strategyBody).toBeTruthy();
    expect(strategyBody.tagKey).toBe('Team');
  });
});
