/**
 * Fix Edit Routing Rules Modal Data Loss
 *
 * Tests verifying the fixes for:
 * - RoutingEditModal reads `accounts` key (not `mappings`) from API response
 * - RoutingEditModal reads tag routing from summaryResp nested path
 * - Save button disabled on loadError or loading state
 * - ConfigurationSummary reads `accounts` key from routing response
 * - ConfigurationSummary reads tag routing from `config.routing.tagRouting.*`
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import React from 'react';

// ---------------------------------------------------------------------------
// Module mocks (same pattern as existing RoutingEditModal.test.tsx)
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
  }),
}));

import { apiFetch } from '../api';
import type { OnboardingConfig } from '../types';

const mockApiFetch = vi.mocked(apiFetch);

// ---------------------------------------------------------------------------
// Fixtures — Realistic API response shapes (matches handle_routing_get output)
// ---------------------------------------------------------------------------

/**
 * Returns the ACTUAL API response shape from handle_routing_get.
 * The fix changes the frontend to read `accounts` (not `mappings`).
 */
function makeRoutingApiResponse(accounts: any[] = []) {
  return {
    default: {
      jiraProject: 'CLOUDOPS',
      jiraIssueType: 'Task',
      snowAssignmentGroupId: '',
      snowRecordType: 'change_request',
    },
    accounts,
    totalAccounts: accounts.length,
  };
}

/**
 * Returns the ACTUAL /config/summary response shape.
 * Tag routing is at `routing.tagRouting.enabled` / `routing.tagRouting.tagKey`.
 */
function makeSummaryResponse(tagRoutingEnabled = false, tagRoutingKey = '') {
  return {
    platform: 'jira',
    platforms: ['jira'],
    routing: {
      defaultProject: 'CLOUDOPS',
      accountMappingCount: 3,
      tagRouting: {
        enabled: tagRoutingEnabled,
        tagKey: tagRoutingKey,
      },
    },
    dispatch: { mode: 'all' },
    setupComplete: true,
  };
}

function makeAccounts() {
  return [
    { accountId: '111111111111', accountName: 'Production', jiraProject: 'CLOUDOPS', snowAssignmentGroupId: '' },
    { accountId: '222222222222', accountName: 'Staging', jiraProject: 'APPTEAM', snowAssignmentGroupId: '' },
    { accountId: '333333333333', accountName: 'Security', jiraProject: 'SECURITY', snowAssignmentGroupId: '' },
  ];
}

function makeConfig(overrides: Partial<OnboardingConfig> = {}): OnboardingConfig {
  return {
    platform: 'jira',
    platforms: ['jira'],
    jira: {
      baseUrl: 'https://myorg.atlassian.net',
      validated: true,
      validatedAt: '2026-07-15T10:00:00Z',
      validatedUser: 'automation@company.com',
    },
    routing: {
      defaultProject: 'CLOUDOPS',
      accountMappingCount: 3,
    },
    dispatch: {
      mode: 'all',
      actionabilityFilter: 'all_actionable',
    },
    setupComplete: true,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function setupMocks(overrides: Record<string, any> = {}) {
  mockApiFetch.mockImplementation(async (path: string) => {
    if (path in overrides) return overrides[path];
    if (path === '/config/routing') return makeRoutingApiResponse(makeAccounts());
    if (path === '/config/summary') return makeSummaryResponse();
    if (path === '/config/routing/discover') return { accounts: [] };
    if (path === '/config/routing/validate') return { results: [] };
    if (path === '/config/routing/default') return { success: true };
    if (path === '/config/routing/import') return { importId: 'imp-001', preview: { valid: 3, invalid: 0 } };
    if (path === '/config/routing/import/confirm') return { success: true };
    if (path === '/config/routing/strategy') return { success: true };
    if (path === '/config/dispatch') return { mode: 'all', rules: [] };
    if (path === '/config/servicenow') return null;
    return {};
  });
}

async function renderModal(props: {
  visible?: boolean;
  onDismiss?: () => void;
  onSave?: () => void;
} = {}) {
  const { default: RoutingEditModal } = await import('../modals/RoutingEditModal');
  const defaults = {
    visible: true,
    onDismiss: vi.fn(),
    onSave: vi.fn(),
  };
  const merged = { ...defaults, ...props };
  const result = render(
    <RoutingEditModal
      visible={merged.visible}
      onDismiss={merged.onDismiss}
      onSave={merged.onSave}
    />
  );
  return { ...result, onDismiss: merged.onDismiss, onSave: merged.onSave };
}

async function renderSummary(config: OnboardingConfig = makeConfig()) {
  setupMocks();
  const { default: ConfigurationSummary } = await import('../ConfigurationSummary');
  const onRunWizard = vi.fn();
  const onConfigChanged = vi.fn();
  const result = render(
    <ConfigurationSummary config={config} onRunWizard={onRunWizard} onConfigChanged={onConfigChanged} />
  );
  return { ...result, onRunWizard, onConfigChanged };
}

// ===========================================================================
// SECTION 1: RoutingEditModal — reads `accounts` key correctly
// ===========================================================================

describe('RoutingEditModal reads accounts key from API', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setupMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('populates account mappings from `accounts` array in API response', async () => {
    await renderModal({ visible: true });

    // The fix reads rData.accounts instead of rData.mappings
    await waitFor(() => {
      expect(screen.getByText('111111111111')).toBeInTheDocument();
      expect(screen.getByText('Production')).toBeInTheDocument();
    });

    expect(screen.getByText('222222222222')).toBeInTheDocument();
    expect(screen.getByText('Staging')).toBeInTheDocument();
    expect(screen.getByText('333333333333')).toBeInTheDocument();
    expect(screen.getByText('Security')).toBeInTheDocument();
  });

  it('renders empty table (not broken) when accounts array is empty', async () => {
    setupMocks({
      '/config/routing': makeRoutingApiResponse([]),
    });

    await renderModal({ visible: true });

    // Modal should load successfully — look for the default project field
    await waitFor(() => {
      expect(screen.getByTestId('routing-default-project')).toBeInTheDocument();
    });

    // No account rows should exist
    expect(screen.queryByText('111111111111')).not.toBeInTheDocument();
    expect(screen.queryByText('222222222222')).not.toBeInTheDocument();
  });

  it('normalizes camelCase account fields from API to snake_case', async () => {
    // API returns camelCase: accountId, accountName, jiraProject
    setupMocks({
      '/config/routing': makeRoutingApiResponse([
        { accountId: '999888777666', accountName: 'CamelCase Account', jiraProject: 'CAMEL', snowAssignmentGroupId: '' },
      ]),
    });

    await renderModal({ visible: true });

    await waitFor(() => {
      expect(screen.getByText('999888777666')).toBeInTheDocument();
      expect(screen.getByText('CamelCase Account')).toBeInTheDocument();
    });
  });
});

// ===========================================================================
// SECTION 2: RoutingEditModal — tag routing from nested summary path
// ===========================================================================

describe('RoutingEditModal reads tag routing from summary nested path', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('tag routing enabled checkbox reflects summaryResp.routing.tagRouting.enabled = true', async () => {
    setupMocks({
      '/config/summary': makeSummaryResponse(true, 'Team'),
    });

    await renderModal({ visible: true });

    // Wait for the modal content to load and tag routing toggle to appear
    await waitFor(() => {
      expect(screen.getByText('Enable tag-based routing')).toBeInTheDocument();
    });

    // When tag routing is enabled from summary, the Tag Key input should be visible
    // (it only renders when tagRoutingEnabled is true)
    await waitFor(() => {
      expect(screen.getByText('Tag Key')).toBeInTheDocument();
    });

    // The tag key input should have the value from summaryResp.routing.tagRouting.tagKey
    const tagKeyInput = screen.getByPlaceholderText('Team');
    expect(tagKeyInput).toHaveValue('Team');
  });

  it('tag routing disabled when summaryResp.routing.tagRouting.enabled = false', async () => {
    setupMocks({
      '/config/summary': makeSummaryResponse(false, ''),
    });

    await renderModal({ visible: true });

    await waitFor(() => {
      expect(screen.getByTestId('routing-default-project')).toBeInTheDocument();
    });

    // When tag routing is disabled, the "Tag Key" label/input should NOT be visible
    // (it's conditionally rendered only when tagRoutingEnabled is true)
    expect(screen.queryByText('Tag Key')).not.toBeInTheDocument();
  });
});

// ===========================================================================
// SECTION 3: RoutingEditModal — save button disabled on loadError / loading
// ===========================================================================

describe('RoutingEditModal save button disabled on error/loading', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('save button disabled when load fails', async () => {
    // Make API call throw an error
    mockApiFetch.mockImplementation(async (path: string) => {
      if (path === '/config/routing') throw new Error('API 500: Internal Server Error');
      if (path === '/config/summary') return makeSummaryResponse();
      return {};
    });

    await renderModal({ visible: true });

    // Wait for error state to render
    await waitFor(() => {
      expect(screen.getByText(/Failed to load routing configuration/)).toBeInTheDocument();
    });

    // Save button should be disabled
    const saveBtn = screen.getByTestId('save-routing');
    expect(saveBtn).toBeDisabled();
  });

  it('save button disabled while loading', async () => {
    // Create a mock that never resolves — simulates perpetual loading
    let resolveRouting: (value: any) => void;
    const routingPromise = new Promise((resolve) => { resolveRouting = resolve; });

    mockApiFetch.mockImplementation(async (path: string) => {
      if (path === '/config/routing') return routingPromise;
      if (path === '/config/summary') return makeSummaryResponse();
      return {};
    });

    await renderModal({ visible: true });

    // While loading, the spinner is shown and save button should be disabled
    const saveBtn = screen.getByTestId('save-routing');
    expect(saveBtn).toBeDisabled();

    // Clean up — resolve the promise so test doesn't hang
    resolveRouting!(makeRoutingApiResponse([]));
  });

  it('shows retry button when load fails', async () => {
    mockApiFetch.mockImplementation(async (path: string) => {
      if (path === '/config/routing') throw new Error('API 503: Service Unavailable');
      if (path === '/config/summary') return makeSummaryResponse();
      return {};
    });

    await renderModal({ visible: true });

    await waitFor(() => {
      expect(screen.getByText(/Failed to load routing configuration/)).toBeInTheDocument();
    });

    // Retry button should be present in the error alert
    expect(screen.getByText('Retry')).toBeInTheDocument();
  });
});

// ===========================================================================
// SECTION 4: ConfigurationSummary — accounts key and tag routing path
// ===========================================================================

describe('ConfigurationSummary reads accounts key from routing API', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('routing table shows correct mapping count from `accounts` array', async () => {
    // Mock API to return 3 accounts using the real API shape
    mockApiFetch.mockImplementation(async (path: string) => {
      if (path === '/config/routing') {
        return makeRoutingApiResponse(makeAccounts());
      }
      if (path === '/config/dispatch') return { mode: 'all', rules: [] };
      if (path === '/config/servicenow') return null;
      return {};
    });

    const config = makeConfig({ routing: { defaultProject: 'CLOUDOPS', accountMappingCount: 3 } });
    const { default: ConfigurationSummary } = await import('../ConfigurationSummary');
    const result = render(
      <ConfigurationSummary config={config} onRunWizard={vi.fn()} onConfigChanged={vi.fn()} />
    );

    // Wait for routing data to load and display
    await waitFor(() => {
      // The summary shows "N configured" for account mappings
      expect(screen.getByText('3 configured')).toBeInTheDocument();
    });
  });

  it('routing table renders account rows from `accounts` array', async () => {
    mockApiFetch.mockImplementation(async (path: string) => {
      if (path === '/config/routing') {
        return makeRoutingApiResponse([
          { accountId: '111111111111', accountName: 'Production', jiraProject: 'CLOUDOPS' },
          { accountId: '222222222222', accountName: 'Staging', jiraProject: 'APPTEAM' },
        ]);
      }
      if (path === '/config/dispatch') return { mode: 'all', rules: [] };
      if (path === '/config/servicenow') return null;
      return {};
    });

    const config = makeConfig({ routing: { defaultProject: 'CLOUDOPS', accountMappingCount: 2 } });
    const { default: ConfigurationSummary } = await import('../ConfigurationSummary');
    render(
      <ConfigurationSummary config={config} onRunWizard={vi.fn()} onConfigChanged={vi.fn()} />
    );

    await waitFor(() => {
      expect(screen.getByText('111111111111')).toBeInTheDocument();
    });

    expect(screen.getByText('222222222222')).toBeInTheDocument();
  });
});

describe('ConfigurationSummary tag routing from nested path', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('shows tag routing as Enabled with correct key from config.routing.tagRouting', async () => {
    mockApiFetch.mockImplementation(async (path: string) => {
      if (path === '/config/routing') return makeRoutingApiResponse(makeAccounts());
      if (path === '/config/dispatch') return { mode: 'all', rules: [] };
      if (path === '/config/servicenow') return null;
      return {};
    });

    const config = makeConfig({
      routing: {
        defaultProject: 'CLOUDOPS',
        accountMappingCount: 3,
        tagRouting: { enabled: true, tagKey: 'Team' },
      },
    });

    const { default: ConfigurationSummary } = await import('../ConfigurationSummary');
    render(
      <ConfigurationSummary config={config} onRunWizard={vi.fn()} onConfigChanged={vi.fn()} />
    );

    await waitFor(() => {
      expect(screen.getByText('Enabled (Key: Team)')).toBeInTheDocument();
    });
  });

  it('shows tag routing as Disabled when config.routing.tagRouting.enabled is false', async () => {
    mockApiFetch.mockImplementation(async (path: string) => {
      if (path === '/config/routing') return makeRoutingApiResponse(makeAccounts());
      if (path === '/config/dispatch') return { mode: 'all', rules: [] };
      if (path === '/config/servicenow') return null;
      return {};
    });

    const config = makeConfig({
      routing: {
        defaultProject: 'CLOUDOPS',
        accountMappingCount: 3,
        tagRouting: { enabled: false, tagKey: '' },
      },
    });

    const { default: ConfigurationSummary } = await import('../ConfigurationSummary');
    render(
      <ConfigurationSummary config={config} onRunWizard={vi.fn()} onConfigChanged={vi.fn()} />
    );

    await waitFor(() => {
      expect(screen.getByText('Disabled')).toBeInTheDocument();
    });
  });

  it('shows tag routing as Disabled when tagRouting field is absent', async () => {
    mockApiFetch.mockImplementation(async (path: string) => {
      if (path === '/config/routing') return makeRoutingApiResponse(makeAccounts());
      if (path === '/config/dispatch') return { mode: 'all', rules: [] };
      if (path === '/config/servicenow') return null;
      return {};
    });

    // No tagRouting field at all
    const config = makeConfig({
      routing: {
        defaultProject: 'CLOUDOPS',
        accountMappingCount: 3,
      },
    });

    const { default: ConfigurationSummary } = await import('../ConfigurationSummary');
    render(
      <ConfigurationSummary config={config} onRunWizard={vi.fn()} onConfigChanged={vi.fn()} />
    );

    await waitFor(() => {
      expect(screen.getByText('Disabled')).toBeInTheDocument();
    });
  });
});
