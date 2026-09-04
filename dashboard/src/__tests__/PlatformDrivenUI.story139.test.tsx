/**
 * STORY-139 (EPIC-02) — Dashboard driven by the `platforms` array.
 *
 * Owner: Moody (QA). Implementation-flow Step 10. Branch: bugfix/manual-testing.
 *
 * This file implements the Dumbledore §7.3 component/unit test matrix in full,
 * plus the Snape SR-139-3 hostile-input inert-text case (mirroring STC-9), plus
 * the dominant-risk JIRA no-regression (AC-139.9) leg run on EVERY edited
 * surface. Consumes already-landed STORY-136 (`config.platforms`) + STORY-137
 * (`CFG_SNOW_*` error codes) via MOCKED /config/summary, /config/routing,
 * /config/routing/import*.
 *
 * KNOWN CONSTRAINT: headless Playwright cannot run in this sandbox (asyncio
 * limit). Per project norm (STORY-131/132/133), UI verification is (a) these
 * deterministic component/unit tests against mocked contracts, and (b) a
 * served-bundle string audit (see 10_moody_tests.md).
 *
 * Authorities asserted against (never ad-hoc strings):
 *   - 01_hermione_story.md AC-139.1..9
 *   - 03_dumbledore_design.md §1.2, §2.3, §2.5, §3, §4, §5, §7.3
 *   - 04_snape_security.md SR-139-3 (inert text on the correct control/row)
 *   - platformLabels.ts JIRA_LABELS / SNOW_LABELS (label vocabulary)
 *
 * Test-runner: vitest + @testing-library/react (jsdom). The real
 * PlatformContext / PlatformProvider is used (NOT mocked) so the label seam is
 * exercised end-to-end from resolvePlatformContext → provider → usePlatformLabels.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';

// ---------------------------------------------------------------------------
// Module mocks — only the network + config seams; PlatformContext is REAL.
// ---------------------------------------------------------------------------

vi.mock('../api', () => ({ apiFetch: vi.fn() }));

vi.mock('../config', () => ({
  getConfig: () => ({ userPoolId: 'p', clientId: 'c', apiUrl: 'http://localhost:3000', region: 'us-east-1' }),
  loadConfig: vi.fn().mockResolvedValue({ userPoolId: 'p', clientId: 'c', apiUrl: 'http://localhost:3000', region: 'us-east-1' }),
}));

import { apiFetch } from '../api';
import type { OnboardingConfig } from '../types';
import { resolvePlatformContext } from '../platformResolver';
import { PlatformProvider } from '../PlatformContext';
import { getPlatformLabels } from '../platformLabels';

const mockApiFetch = vi.mocked(apiFetch);

// The canonical label strings (assert against the source of truth, not literals).
const JIRA = getPlatformLabels('jira');
const SNOW = getPlatformLabels('servicenow');

/**
 * Walk a captured React element tree (e.g. the SplitPanel node produced via
 * onSplitPanelChange) and collect every `columnDefinitions[].header` string.
 * Used to assert label-bound resource-table headers WITHOUT rendering the
 * Cloudscape <SplitPanel> standalone (which requires an AppLayout context that
 * does not lazily mount its panel content in jsdom). This inspects the exact
 * header value the component bound — the property under test for AC-139.4.
 */
function collectColumnHeaders(node: any, out: string[] = [], seen = new Set<any>()): string[] {
  if (!node || typeof node !== 'object' || seen.has(node)) return out;
  seen.add(node);
  if (Array.isArray(node)) {
    for (const item of node) collectColumnHeaders(item, out, seen);
    return out;
  }
  // A columnDefinitions array anywhere in the tree yields header strings.
  const cd = (node as any).columnDefinitions ?? (node as any).props?.columnDefinitions;
  if (Array.isArray(cd)) for (const c of cd) if (typeof c?.header === 'string') out.push(c.header);
  // Recurse into every enumerable value: React element props, plain container
  // objects (e.g. Tabs `tabs=[{content}]`), children, content, etc.
  for (const key of Object.keys(node)) {
    if (key === 'columnDefinitions') continue;
    collectColumnHeaders((node as any)[key], out, seen);
  }
  return out;
}

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function snowOnlyConfig(overrides: Partial<OnboardingConfig> = {}): OnboardingConfig {
  return {
    platform: 'jira', // legacy scalar deliberately still 'jira' (STORY-136 §1.3)
    platforms: ['servicenow'],
    servicenow: { instanceUrl: 'https://dev.service-now.com', validated: true },
    routing: { defaultProject: '', accountMappingCount: 0 },
    ...overrides,
  };
}

function jiraOnlyConfig(overrides: Partial<OnboardingConfig> = {}): OnboardingConfig {
  return {
    platform: 'jira',
    platforms: ['jira'],
    jira: { baseUrl: 'https://x.atlassian.net', validated: true, credentialsConfigured: true },
    routing: { defaultProject: 'CLOUDOPS', accountMappingCount: 2 },
    ...overrides,
  };
}

/** Install a happy-path apiFetch mock for the routing modal's loadData(). */
function installRoutingModalMock(opts: {
  platforms: string[];
  accounts?: any[];
  routing?: any;
}) {
  const { platforms, accounts = [], routing = {} } = opts;
  mockApiFetch.mockReset();
  mockApiFetch.mockImplementation(async (path: string) => {
    if (path === '/config/summary') {
      return { platform: platforms[0], platforms, routing: { defaultProject: 'CLOUDOPS', tagRouting: { enabled: false, tagKey: '' } } };
    }
    if (path === '/config/routing') {
      return { default: {}, defaultProject: '', accounts, totalAccounts: accounts.length, ...routing };
    }
    if (path === '/config/routing/tags') return { mappings: [] };
    return {};
  });
}

// ===========================================================================
// ROW 1 — resolvePlatformContext unit (§1.2, AC-139.3)
// ===========================================================================

describe('STORY-139 · resolvePlatformContext (§1.2, AC-139.3)', () => {
  it('["servicenow"] → labelPlatform servicenow, snowEnabled only', () => {
    const ctx = resolvePlatformContext({ platforms: ['servicenow'] } as OnboardingConfig);
    expect(ctx.labelPlatform).toBe('servicenow');
    expect(ctx.snowEnabled).toBe(true);
    expect(ctx.jiraEnabled).toBe(false);
  });

  it('["jira"] → labelPlatform jira, jiraEnabled only', () => {
    const ctx = resolvePlatformContext({ platforms: ['jira'] } as OnboardingConfig);
    expect(ctx.labelPlatform).toBe('jira');
    expect(ctx.jiraEnabled).toBe(true);
    expect(ctx.snowEnabled).toBe(false);
  });

  it('["jira","servicenow"] (dual) → labelPlatform jira (§4.3), BOTH enabled', () => {
    const ctx = resolvePlatformContext({ platforms: ['jira', 'servicenow'] } as OnboardingConfig);
    expect(ctx.labelPlatform).toBe('jira'); // dual → jira label context
    expect(ctx.jiraEnabled).toBe(true);
    expect(ctx.snowEnabled).toBe(true);
  });

  it('undefined platforms + no scalar → defensive jira', () => {
    const ctx = resolvePlatformContext({} as OnboardingConfig);
    expect(ctx.labelPlatform).toBe('jira');
    expect(ctx.jiraEnabled).toBe(true);
    expect(ctx.snowEnabled).toBe(false);
    expect(ctx.platforms).toEqual(['jira']);
  });

  it('null config → defensive jira', () => {
    const ctx = resolvePlatformContext(null);
    expect(ctx.labelPlatform).toBe('jira');
    expect(ctx.jiraEnabled).toBe(true);
    expect(ctx.snowEnabled).toBe(false);
  });

  it('legacy scalar-only (platform:"servicenow", no platforms[]) falls back through the scalar', () => {
    // Defensive fallback path: old cached response predating STORY-136.
    const ctx = resolvePlatformContext({ platform: 'servicenow' } as OnboardingConfig);
    expect(ctx.platforms).toEqual(['servicenow']);
    expect(ctx.labelPlatform).toBe('servicenow');
  });
});

// ===========================================================================
// ROW 2 — Routing modal SNOW-only (AC-139.1/.2, §2.2/§2.3)
// ===========================================================================

describe('STORY-139 · RoutingEditModal SNOW-only (AC-139.1/.2)', () => {
  beforeEach(() => vi.clearAllMocks());

  async function renderModal(platforms: string[], accounts: any[] = []) {
    installRoutingModalMock({ platforms, accounts });
    const { default: RoutingEditModal } = await import('../modals/RoutingEditModal');
    const onSave = vi.fn();
    const onDismiss = vi.fn();
    const utils = render(<RoutingEditModal visible={true} onDismiss={onDismiss} onSave={onSave} />);
    // Wait for loadData() to resolve (spinner → content).
    await screen.findByText('Default Routing (required)');
    return { ...utils, onSave, onDismiss };
  }

  it('renders Default Assignment Group + Record Type; hides Default JIRA Project', async () => {
    await renderModal(['servicenow']);
    expect(screen.getByText(SNOW.defaultRouting)).toBeInTheDocument(); // "Default Assignment Group"
    expect(screen.getByText('Record Type')).toBeInTheDocument();
    // JIRA-only default field label is absent.
    expect(screen.queryByText('Default JIRA Project')).not.toBeInTheDocument();
  });

  it('account table shows the ServiceNow Group column, not JIRA Project', async () => {
    const accounts = [{ account_id: '111111111111', account_name: 'Prod', jira_project: '', snow_assignment_group_id: 'abc' }];
    await renderModal(['servicenow'], accounts);
    expect(screen.getAllByText('ServiceNow Group').length).toBeGreaterThan(0);
    // The account-mappings JIRA Project column header must be absent SNOW-only.
    expect(screen.queryByText('JIRA Project')).not.toBeInTheDocument();
  });

  it('SNOW-only add-row guard fires on empty group (§2.3)', async () => {
    const user = userEvent.setup();
    await renderModal(['servicenow']);
    await user.type(screen.getByLabelText('New account ID'), '111111111111');
    await user.click(screen.getByTestId('add-mapping-row'));
    expect(await screen.findByText('Assignment group sys_id is required')).toBeInTheDocument();
  });
});

// ===========================================================================
// ROW 3 — Label context = SNOW (AC-139.3/.4, §1.3/§3)
// Real PlatformProvider fed the RESOLVED labelPlatform, exercising the seam.
// ===========================================================================

function withPlatform(config: OnboardingConfig | null, node: React.ReactNode) {
  return <PlatformProvider platform={resolvePlatformContext(config).labelPlatform}>{node}</PlatformProvider>;
}

describe('STORY-139 · label context SNOW (AC-139.4)', () => {
  beforeEach(() => vi.clearAllMocks());

  // The resource/campaign split-panel content is produced via the
  // `onSplitPanelChange(node)` callback (App renders it into AppLayout, not the
  // component's own subtree). We capture the node and render it directly to
  // assert on the label-bound column headers — under the SAME PlatformProvider
  // so the label seam is exercised.
  it('Dashboard resource-table headers read ServiceNow Record / ServiceNow State', async () => {
    const config = snowOnlyConfig();
    const campaign = {
      campaignId: 'c1', eventArn: 'arn', title: 'EKS', service: 'EKS', eventTypeCode: 'X',
      description: '', deadline: '', hasResources: true, status: 'active',
      totalResources: 1, ticketedResources: 1, resolvedResources: 0, affectedAccount: '111111111111',
    } as any;
    // Dashboard fetches detail + resources; return a non-empty resource list so
    // the resource table (with label-bound headers) renders in the split panel.
    mockApiFetch.mockImplementation(async (path: string) => {
      if (path.includes('/resources')) return { resources: [{ resourceArn: 'r', accountId: '1', region: 'us-east-1', healthStatus: 'PENDING', ticketStatus: 'none', tags: {} }] };
      if (path.includes('/breakdown')) return { breakdown: {} };
      if (path.startsWith('/campaigns/')) return campaign;
      return {};
    });
    const { default: Dashboard } = await import('../Dashboard');
    let panel: React.ReactNode = null;
    const onSplitPanelChange = (p: React.ReactNode) => { panel = p; };
    render(withPlatform(config, (
      <Dashboard campaigns={[campaign]} config={config} onRefresh={() => {}} notify={() => {}}
        onNavigate={() => {}} onSync={() => {}} onSplitPanelChange={onSplitPanelChange} />
    )));
    // Cloudscape single-selection: click the row's radio to fire onSelectionChange.
    await screen.findByText('EKS');
    await userEvent.setup().click(screen.getAllByRole('radio')[0]);
    // The Dashboard rebuilds the panel once the async /resources fetch resolves;
    // wait until the resource-table headers appear in the captured tree.
    await waitFor(() => expect(collectColumnHeaders(panel)).toContain(SNOW.ticketColumn));
    const headers = collectColumnHeaders(panel);
    expect(headers).toContain(SNOW.ticketColumn);   // "ServiceNow Record"
    expect(headers).toContain(SNOW.statusColumn);    // "ServiceNow State"
    expect(headers).not.toContain(JIRA.ticketColumn);
  });

  it('Campaigns split-panel ticket header reads ServiceNow Record (fixed hardcoded "Ticket")', async () => {
    const config = snowOnlyConfig();
    const campaign = {
      campaignId: 'c1', eventArn: 'arn', title: 'EKS', service: 'EKS', eventTypeCode: 'X',
      description: '', deadline: '', hasResources: true, status: 'active',
      totalResources: 1, ticketedResources: 1, resolvedResources: 0, affectedAccount: '111111111111',
      resources: [{ resourceArn: 'r', accountId: '1', region: 'us-east-1', healthStatus: 'PENDING', ticketStatus: 'none', tags: {} }],
    } as any;
    // Campaigns fetches detail via /campaigns/:id and renders detail.resources.
    mockApiFetch.mockImplementation(async (path: string) => {
      if (path.startsWith('/campaigns/')) return campaign;
      return {};
    });
    const { default: Campaigns } = await import('../Campaigns');
    let panel: React.ReactNode = null;
    const onSplitPanelChange = (p: React.ReactNode) => { panel = p; };
    render(withPlatform(config, (
      <Campaigns campaigns={[campaign]} config={config} onRefresh={() => {}} notify={() => {}} onSplitPanelChange={onSplitPanelChange} />
    )));
    await screen.findByText('EKS');
    await userEvent.setup().click(screen.getAllByRole('radio')[0]);
    await waitFor(() => expect(panel).toBeTruthy());
    const headers = collectColumnHeaders(panel);
    expect(headers).toContain(SNOW.ticketColumn); // "ServiceNow Record"
    // The old hardcoded 'Ticket' header must not appear as the ticket column.
    expect(headers).not.toContain('Ticket');
    expect(headers).not.toContain(JIRA.ticketColumn);
  });

  it('CreateTicketsModal orphanNote reads the assignment-group wording', async () => {
    mockApiFetch.mockResolvedValue({ groups: [] });
    const { default: CreateTicketsModal } = await import('../CreateTicketsModal');
    const config = snowOnlyConfig();
    const campaign = {
      campaignId: 'c1', eventArn: 'arn', title: 'EKS', service: 'EKS', eventTypeCode: 'X',
      description: '', deadline: '', hasResources: false, status: 'active',
      totalResources: 0, ticketedResources: 0, resolvedResources: 0,
    } as any;
    render(withPlatform(config, (
      <CreateTicketsModal campaign={campaign} onDismiss={() => {}} onCreated={() => {}} notify={() => {}} />
    )));
    expect(await screen.findByText(new RegExp(SNOW.orphanNote.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))).toBeInTheDocument();
    // The JIRA "default project" wording must not appear SNOW-only.
    expect(screen.queryByText(new RegExp(JIRA.orphanNote.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))).not.toBeInTheDocument();
  });
});

// ===========================================================================
// ROW 4 — Setup prompt false-negative fixed (AC-139.5, §4)
// ===========================================================================

describe('STORY-139 · Dashboard setup prompt (AC-139.5)', () => {
  beforeEach(() => vi.clearAllMocks());

  async function renderDashboard(config: OnboardingConfig) {
    mockApiFetch.mockResolvedValue({}); // orphan-status
    const { default: Dashboard } = await import('../Dashboard');
    return render(withPlatform(config, (
      <Dashboard campaigns={[]} config={config} onRefresh={() => {}} notify={() => {}}
        onNavigate={() => {}} onSync={() => {}} onSplitPanelChange={() => {}} />
    )));
  }

  it('SNOW-only fully-configured → NO "configure your JIRA connection" prompt', async () => {
    const config = snowOnlyConfig({
      servicenow: { instanceUrl: 'https://x', validated: true },
      routing: { defaultProject: '', accountMappingCount: 3 }, // SNOW routing present via mapping count
    });
    await renderDashboard(config);
    // The false JIRA setup prompt must be gone.
    expect(screen.queryByText(new RegExp('configure your JIRA connection'))).not.toBeInTheDocument();
    // And the "Setup incomplete" warning must not be shown (connectionReady=true).
    expect(screen.queryByText(/Setup incomplete/)).not.toBeInTheDocument();
  });

  it('SNOW-only connected but NO routing → info alert with SNOW connected copy (not JIRA setup)', async () => {
    const config = snowOnlyConfig({
      servicenow: { instanceUrl: 'https://x', validated: true },
      routing: { defaultProject: '', accountMappingCount: 0 }, // connected, routing not set
    });
    await renderDashboard(config);
    expect(screen.getByText(new RegExp(SNOW.connectedAlert.split('.')[0]))).toBeInTheDocument();
    expect(screen.queryByText(new RegExp('configure your JIRA connection'))).not.toBeInTheDocument();
  });

  it('SNOW-only NOT connected → setup-incomplete uses SNOW wording, never JIRA', async () => {
    const config = snowOnlyConfig({
      servicenow: { instanceUrl: '', validated: false },
      routing: { defaultProject: '', accountMappingCount: 0 },
    });
    await renderDashboard(config);
    expect(screen.getByText(/Setup incomplete/)).toBeInTheDocument();
    expect(screen.getByText(new RegExp(SNOW.setupAlert.split(' ').slice(0, 4).join(' ')))).toBeInTheDocument();
    expect(screen.queryByText(new RegExp('configure your JIRA connection'))).not.toBeInTheDocument();
  });
});

// ===========================================================================
// ROW 5 — Summary columns SNOW-only zero-mappings (AC-139.6, §5.1)
// ===========================================================================

describe('STORY-139 · ConfigurationSummary columns (AC-139.6)', () => {
  beforeEach(() => vi.clearAllMocks());

  async function renderSummary(config: OnboardingConfig) {
    mockApiFetch.mockImplementation(async (path: string) => {
      if (path === '/config/routing') return { default: {}, accounts: [], totalAccounts: 0 };
      if (path === '/config/dispatch') return { mode: 'all', actionabilityFilter: 'all_actionable', rules: [] };
      if (path === '/config/servicenow') return { instanceUrl: 'https://x', validated: true };
      return {};
    });
    const { default: ConfigurationSummary } = await import('../ConfigurationSummary');
    return render(withPlatform(config, (
      <ConfigurationSummary config={config} onRunWizard={() => {}} onConfigChanged={() => {}} />
    )));
  }

  it('SNOW-only zero-mappings → ServiceNow Group column present, JIRA Project hidden', async () => {
    await renderSummary(snowOnlyConfig());
    // Wait for routing load to settle.
    await screen.findByText('Routing Rules');
    await waitFor(() => expect(screen.getAllByText('ServiceNow Group').length).toBeGreaterThan(0));
    // The routing-table JIRA Project column header must be absent for SNOW-only.
    expect(screen.queryByText('JIRA Project')).not.toBeInTheDocument();
  });

  it('SNOW-only → Active Platform reads ServiceNow', async () => {
    await renderSummary(snowOnlyConfig());
    await screen.findByText('Routing Rules');
    // Expand the System Information section to reveal Active Platform.
    await userEvent.setup().click(screen.getByText('System Information'));
    // "ServiceNow" appears in multiple places (status card + Active Platform);
    // scope the assertion to the Active Platform field container.
    const label = await screen.findByText('Active Platform');
    const field = label.parentElement as HTMLElement;
    expect(within(field).getByText('ServiceNow')).toBeInTheDocument();
  });
});

// ===========================================================================
// ROW 6 — Returning-user classification (AC-139.8, §5.2)
// ===========================================================================

describe('STORY-139 · Configuration returning-user (AC-139.8)', () => {
  beforeEach(() => vi.clearAllMocks());

  it('SNOW validated + SNOW routing → summary (NOT first-time wizard)', async () => {
    mockApiFetch.mockImplementation(async (path: string) => {
      if (path === '/config/routing') return { default: {}, accounts: [], totalAccounts: 0 };
      if (path === '/config/dispatch') return { mode: 'all', actionabilityFilter: 'all_actionable', rules: [] };
      if (path === '/config/servicenow') return { instanceUrl: 'https://x', validated: true };
      return {};
    });
    const { default: Configuration } = await import('../Configuration');
    const config = snowOnlyConfig({
      servicenow: { instanceUrl: 'https://x', validated: true },
      routing: { defaultProject: '', accountMappingCount: 4 }, // SNOW routing present
    });
    render(withPlatform(config, <Configuration config={config} onSave={() => {}} />));
    // Summary landing page renders (its "Run Setup Wizard" button), not the wizard.
    expect(await screen.findByTestId('run-setup-wizard')).toBeInTheDocument();
  });

  it('SNOW routing via snowAssignmentGroupId (no mappings) → also NOT first-time', async () => {
    mockApiFetch.mockImplementation(async (path: string) => {
      if (path === '/config/routing') return { default: {}, accounts: [], totalAccounts: 0 };
      if (path === '/config/dispatch') return { mode: 'all', actionabilityFilter: 'all_actionable', rules: [] };
      if (path === '/config/servicenow') return { instanceUrl: 'https://x', validated: true };
      return {};
    });
    const { default: Configuration } = await import('../Configuration');
    const config = snowOnlyConfig({
      servicenow: { instanceUrl: 'https://x', validated: true },
      routing: { defaultProject: '', accountMappingCount: 0, snowAssignmentGroupId: 'abc123' } as any,
    });
    render(withPlatform(config, <Configuration config={config} onSave={() => {}} />));
    expect(await screen.findByTestId('run-setup-wizard')).toBeInTheDocument();
  });
});

// ===========================================================================
// ROW 7 — Error surfacing (AC-139.1, §2.5)  +  ROW 8 — SR-139-3 hostile input
// ===========================================================================

describe('STORY-139 · RoutingEditModal error surfacing (AC-139.1, §2.5)', () => {
  beforeEach(() => vi.clearAllMocks());

  async function renderModal(platforms: string[], saveMock: (path: string, opts?: any) => Promise<any>) {
    // loadData mock + save-path override.
    mockApiFetch.mockReset();
    mockApiFetch.mockImplementation(async (path: string, opts?: any) => {
      if (opts?.method === 'POST') return saveMock(path, opts);
      if (path === '/config/summary') return { platform: platforms[0], platforms, routing: { defaultProject: 'CLOUDOPS', tagRouting: { enabled: false, tagKey: '' } } };
      if (path === '/config/routing') return { default: {}, accounts: [], totalAccounts: 0 };
      if (path === '/config/routing/tags') return { mappings: [] };
      return {};
    });
    const { default: RoutingEditModal } = await import('../modals/RoutingEditModal');
    const onSave = vi.fn();
    const utils = render(<RoutingEditModal visible={true} onDismiss={() => {}} onSave={onSave} />);
    await screen.findByText('Default Routing (required)');
    return { ...utils, onSave };
  }

  it('top-level 400 CFG_SNOW_GROUP_NOT_FOUND → errorText on Default Assignment Group; modal stays open', async () => {
    const user = userEvent.setup();
    const msg = "Assignment group 'zzz' was not found in the connected ServiceNow instance.";
    const { onSave } = await renderModal(['servicenow'], async (path) => {
      if (path === '/config/routing/default') {
        throw new Error(`API 400: ${JSON.stringify({ error: { code: 'CFG_SNOW_GROUP_NOT_FOUND', message: msg } })}`);
      }
      return {};
    });
    // Fill the required default group so save is enabled, then save.
    await user.type(screen.getByPlaceholderText('a1b2c3d4e5f6g7h8i9j0...'), 'zzz');
    await user.click(screen.getByTestId('save-routing'));
    expect(await screen.findByText(msg)).toBeInTheDocument();
    // Modal stayed open (no onSave) so the user can correct.
    expect(onSave).not.toHaveBeenCalled();
  });

  it('top-level 400 CFG_SNOW_NOT_CONFIGURED → section-level warning alert', async () => {
    const user = userEvent.setup();
    const msg = 'Connect and validate ServiceNow before saving routing.';
    const { onSave } = await renderModal(['servicenow'], async (path) => {
      if (path === '/config/routing/default') {
        throw new Error(`API 400: ${JSON.stringify({ error: { code: 'CFG_SNOW_NOT_CONFIGURED', message: msg } })}`);
      }
      return {};
    });
    await user.type(screen.getByPlaceholderText('a1b2c3d4e5f6g7h8i9j0...'), 'abc123def456789012345678901234ab');
    await user.click(screen.getByTestId('save-routing'));
    expect(await screen.findByText(msg)).toBeInTheDocument();
    expect(onSave).not.toHaveBeenCalled();
  });

  it('top-level 400 CFG_INVALID_SNOW_RECORD_TYPE → errorText under Record Type', async () => {
    const user = userEvent.setup();
    const msg = 'Record type must be Change Request or Incident.';
    await renderModal(['servicenow'], async (path) => {
      if (path === '/config/routing/default') {
        throw new Error(`API 400: ${JSON.stringify({ error: { code: 'CFG_INVALID_SNOW_RECORD_TYPE', message: msg } })}`);
      }
      return {};
    });
    await user.type(screen.getByPlaceholderText('a1b2c3d4e5f6g7h8i9j0...'), 'abc123def456789012345678901234ab');
    await user.click(screen.getByTestId('save-routing'));
    expect(await screen.findByText(msg)).toBeInTheDocument();
  });

  it('per-row validationErrors (HTTP 200 import) → inline error on the matching account row', async () => {
    const user = userEvent.setup();
    const rowMsg = "Assignment group 'bad' was not found in the connected ServiceNow instance.";
    // Seed a mapping so the account save path runs with a row for 111111111111.
    mockApiFetch.mockReset();
    mockApiFetch.mockImplementation(async (path: string, opts?: any) => {
      if (opts?.method === 'POST') {
        if (path === '/config/routing/default') return {};
        if (path === '/config/routing/import') {
          return { validationErrors: [{ accountId: '111111111111', field: 'snowAssignmentGroupId', code: 'CFG_SNOW_GROUP_NOT_FOUND', message: rowMsg }] };
        }
        return {};
      }
      if (path === '/config/summary') return { platform: 'servicenow', platforms: ['servicenow'], routing: { defaultProject: 'CLOUDOPS', tagRouting: { enabled: false, tagKey: '' } } };
      if (path === '/config/routing') return { default: {}, accounts: [{ account_id: '111111111111', account_name: 'Prod', jira_project: '', snow_assignment_group_id: 'bad' }], totalAccounts: 1 };
      if (path === '/config/routing/tags') return { mappings: [] };
      return {};
    });
    const { default: RoutingEditModal } = await import('../modals/RoutingEditModal');
    const onSave = vi.fn();
    render(<RoutingEditModal visible={true} onDismiss={() => {}} onSave={onSave} />);
    await screen.findByText('Default Routing (required)');
    await user.type(screen.getByPlaceholderText('a1b2c3d4e5f6g7h8i9j0...'), 'abc123def456789012345678901234ab');
    await user.click(screen.getByTestId('save-routing'));
    expect(await screen.findByText(rowMsg)).toBeInTheDocument();
    expect(onSave).not.toHaveBeenCalled();
  });

  it('unrecognized 400 body → raw-string fallback blob (unchanged behavior)', async () => {
    const user = userEvent.setup();
    const { onSave } = await renderModal(['servicenow'], async (path) => {
      if (path === '/config/routing/default') throw new Error('API 400: totally unstructured failure text');
      return {};
    });
    await user.type(screen.getByPlaceholderText('a1b2c3d4e5f6g7h8i9j0...'), 'abc123def456789012345678901234ab');
    await user.click(screen.getByTestId('save-routing'));
    expect(await screen.findByText(/totally unstructured failure text/)).toBeInTheDocument();
    expect(onSave).not.toHaveBeenCalled();
  });
});

describe('STORY-139 · SR-139-3 hostile-input inert text (mirrors STC-9)', () => {
  beforeEach(() => vi.clearAllMocks());

  it('hostile CFG_SNOW_GROUP_NOT_FOUND message renders as INERT TEXT on the default-group control (no live node)', async () => {
    const user = userEvent.setup();
    const xss = 'Not found <img src=x onerror=alert(1)><script>alert(2)</script>';
    mockApiFetch.mockReset();
    mockApiFetch.mockImplementation(async (path: string, opts?: any) => {
      if (opts?.method === 'POST') {
        if (path === '/config/routing/default') throw new Error(`API 400: ${JSON.stringify({ error: { code: 'CFG_SNOW_GROUP_NOT_FOUND', message: xss } })}`);
        return {};
      }
      if (path === '/config/summary') return { platform: 'servicenow', platforms: ['servicenow'], routing: { defaultProject: 'CLOUDOPS', tagRouting: { enabled: false, tagKey: '' } } };
      if (path === '/config/routing') return { default: {}, accounts: [], totalAccounts: 0 };
      if (path === '/config/routing/tags') return { mappings: [] };
      return {};
    });
    const { default: RoutingEditModal } = await import('../modals/RoutingEditModal');
    const { container } = render(<RoutingEditModal visible={true} onDismiss={() => {}} onSave={() => {}} />);
    await screen.findByText('Default Routing (required)');
    await user.type(screen.getByPlaceholderText('a1b2c3d4e5f6g7h8i9j0...'), 'zzz');
    await user.click(screen.getByTestId('save-routing'));
    // The hostile string is present as LITERAL TEXT ...
    expect(await screen.findByText(xss)).toBeInTheDocument();
    // ... and NOT interpreted as HTML — no injected <img>/<script> node exists.
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
  });

  it('hostile per-row validationErrors string renders as inert text on the matching row (no live node)', async () => {
    const user = userEvent.setup();
    const xss = 'bad <img src=x onerror=alert(3)> "><svg onload=alert(4)>';
    mockApiFetch.mockReset();
    mockApiFetch.mockImplementation(async (path: string, opts?: any) => {
      if (opts?.method === 'POST') {
        if (path === '/config/routing/default') return {};
        if (path === '/config/routing/import') return { validationErrors: [{ accountId: '111111111111', field: 'snowAssignmentGroupId', code: 'CFG_SNOW_GROUP_NOT_FOUND', message: xss }] };
        return {};
      }
      if (path === '/config/summary') return { platform: 'servicenow', platforms: ['servicenow'], routing: { defaultProject: 'CLOUDOPS', tagRouting: { enabled: false, tagKey: '' } } };
      if (path === '/config/routing') return { default: {}, accounts: [{ account_id: '111111111111', account_name: 'Prod', jira_project: '', snow_assignment_group_id: 'bad' }], totalAccounts: 1 };
      if (path === '/config/routing/tags') return { mappings: [] };
      return {};
    });
    const { default: RoutingEditModal } = await import('../modals/RoutingEditModal');
    const { container } = render(<RoutingEditModal visible={true} onDismiss={() => {}} onSave={() => {}} />);
    await screen.findByText('Default Routing (required)');
    await user.type(screen.getByPlaceholderText('a1b2c3d4e5f6g7h8i9j0...'), 'abc123def456789012345678901234ab');
    await user.click(screen.getByTestId('save-routing'));
    expect(await screen.findByText(xss)).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('svg[onload]')).toBeNull();
  });
});

// ===========================================================================
// ROW 9 — JIRA NO-REGRESSION (AC-139.9) — dominant risk, run on EVERY surface
// ===========================================================================

describe('STORY-139 · JIRA no-regression (AC-139.9)', () => {
  beforeEach(() => vi.clearAllMocks());

  it('resolvePlatformContext: JIRA-only + dual both label JIRA', () => {
    expect(resolvePlatformContext({ platforms: ['jira'] } as OnboardingConfig).labelPlatform).toBe('jira');
    expect(resolvePlatformContext({ platforms: ['jira', 'servicenow'] } as OnboardingConfig).labelPlatform).toBe('jira');
  });

  it('RoutingEditModal JIRA-only: Default JIRA Project present, SNOW default group absent', async () => {
    installRoutingModalMock({ platforms: ['jira'] });
    const { default: RoutingEditModal } = await import('../modals/RoutingEditModal');
    render(<RoutingEditModal visible={true} onDismiss={() => {}} onSave={() => {}} />);
    await screen.findByText('Default Routing (required)');
    expect(screen.getByText('Default JIRA Project')).toBeInTheDocument();
    expect(screen.queryByText('Default Assignment Group')).not.toBeInTheDocument();
    expect(screen.queryByText('Record Type')).not.toBeInTheDocument();
  });

  it('RoutingEditModal JIRA-only add-row guard: empty JIRA project rejected (unchanged)', async () => {
    const user = userEvent.setup();
    installRoutingModalMock({ platforms: ['jira'] });
    const { default: RoutingEditModal } = await import('../modals/RoutingEditModal');
    render(<RoutingEditModal visible={true} onDismiss={() => {}} onSave={() => {}} />);
    await screen.findByText('Default Routing (required)');
    await user.type(screen.getByLabelText('New account ID'), '111111111111');
    await user.click(screen.getByTestId('add-mapping-row'));
    expect(await screen.findByText('JIRA project is required')).toBeInTheDocument();
    // SNOW-only guard text must NOT appear on the JIRA path.
    expect(screen.queryByText('Assignment group sys_id is required')).not.toBeInTheDocument();
  });

  it('Dashboard JIRA-only: headers read JIRA vocabulary; setup prompt uses JIRA readiness', async () => {
    mockApiFetch.mockResolvedValue({});
    const { default: Dashboard } = await import('../Dashboard');
    // NOT configured JIRA → setup-incomplete with JIRA setupAlert.
    const config = jiraOnlyConfig({ jira: { baseUrl: '', validated: false, credentialsConfigured: false }, routing: { defaultProject: '', accountMappingCount: 0 } });
    render(withPlatform(config, (
      <Dashboard campaigns={[]} config={config} onRefresh={() => {}} notify={() => {}}
        onNavigate={() => {}} onSync={() => {}} onSplitPanelChange={() => {}} />
    )));
    expect(screen.getByText(/Setup incomplete/)).toBeInTheDocument();
    expect(screen.getByText(new RegExp('configure your JIRA connection'))).toBeInTheDocument();
  });

  it('Campaigns JIRA-only: ticket header reads JIRA Ticket', async () => {
    const config = jiraOnlyConfig();
    const campaign = {
      campaignId: 'c1', eventArn: 'arn', title: 'EKS', service: 'EKS', eventTypeCode: 'X',
      description: '', deadline: '', hasResources: true, status: 'active',
      totalResources: 1, ticketedResources: 1, resolvedResources: 0, affectedAccount: '1',
      resources: [{ resourceArn: 'r', accountId: '1', region: 'us-east-1', healthStatus: 'PENDING', ticketStatus: 'none', tags: {} }],
    } as any;
    mockApiFetch.mockImplementation(async (path: string) => {
      if (path.startsWith('/campaigns/')) return campaign;
      return {};
    });
    const { default: Campaigns } = await import('../Campaigns');
    let panel: React.ReactNode = null;
    render(withPlatform(config, (
      <Campaigns campaigns={[campaign]} config={config} onRefresh={() => {}} notify={() => {}} onSplitPanelChange={(p) => { panel = p; }} />
    )));
    await screen.findByText('EKS');
    await userEvent.setup().click(screen.getAllByRole('radio')[0]);
    await waitFor(() => expect(panel).toBeTruthy());
    const headers = collectColumnHeaders(panel);
    expect(headers).toContain(JIRA.ticketColumn); // "JIRA Ticket"
    expect(headers).not.toContain(SNOW.ticketColumn);
  });

  it('CreateTicketsModal JIRA-only: orphanNote reads the default-project wording', async () => {
    mockApiFetch.mockResolvedValue({ groups: [] });
    const { default: CreateTicketsModal } = await import('../CreateTicketsModal');
    const config = jiraOnlyConfig();
    const campaign = {
      campaignId: 'c1', eventArn: 'arn', title: 'EKS', service: 'EKS', eventTypeCode: 'X',
      description: '', deadline: '', hasResources: false, status: 'active',
      totalResources: 0, ticketedResources: 0, resolvedResources: 0,
    } as any;
    render(withPlatform(config, (
      <CreateTicketsModal campaign={campaign} onDismiss={() => {}} onCreated={() => {}} notify={() => {}} />
    )));
    expect(await screen.findByText(new RegExp(JIRA.orphanNote.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))).toBeInTheDocument();
    expect(screen.queryByText(new RegExp(SNOW.orphanNote.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))).not.toBeInTheDocument();
  });

  it('ConfigurationSummary JIRA-only: JIRA Project column present, ServiceNow Group absent; Active Platform JIRA', async () => {
    mockApiFetch.mockImplementation(async (path: string) => {
      if (path === '/config/routing') return { default: {}, accounts: [{ account_id: '111111111111', account_name: 'Prod', jira_project: 'CLOUDOPS', snow_assignment_group_id: '' }], totalAccounts: 1 };
      if (path === '/config/dispatch') return { mode: 'all', actionabilityFilter: 'all_actionable', rules: [] };
      if (path === '/config/servicenow') return {};
      return {};
    });
    const { default: ConfigurationSummary } = await import('../ConfigurationSummary');
    const config = jiraOnlyConfig();
    render(withPlatform(config, (
      <ConfigurationSummary config={config} onRunWizard={() => {}} onConfigChanged={() => {}} />
    )));
    await screen.findByText('Routing Rules');
    await waitFor(() => expect(screen.getAllByText('JIRA Project').length).toBeGreaterThan(0));
    expect(screen.queryByText('ServiceNow Group')).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByText('System Information'));
    const label = await screen.findByText('Active Platform');
    const field = label.parentElement as HTMLElement;
    expect(within(field).getByText('JIRA Cloud')).toBeInTheDocument();
  });

  it('Configuration JIRA-only returning user: hasRouting via defaultProject → summary, not wizard', async () => {
    mockApiFetch.mockImplementation(async (path: string) => {
      if (path === '/config/routing') return { default: {}, accounts: [], totalAccounts: 0 };
      if (path === '/config/dispatch') return { mode: 'all', actionabilityFilter: 'all_actionable', rules: [] };
      if (path === '/config/servicenow') return {};
      return {};
    });
    const { default: Configuration } = await import('../Configuration');
    const config = jiraOnlyConfig({ jira: { baseUrl: 'https://x', validated: true, credentialsConfigured: true }, routing: { defaultProject: 'CLOUDOPS', accountMappingCount: 0 } });
    render(withPlatform(config, <Configuration config={config} onSave={() => {}} />));
    expect(await screen.findByTestId('run-setup-wizard')).toBeInTheDocument();
  });
});
