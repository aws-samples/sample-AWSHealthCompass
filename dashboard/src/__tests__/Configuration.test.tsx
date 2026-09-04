/**
 * Unit tests for the Configuration Summary Landing Page
 *
 * Tests the orchestrator (Configuration.tsx) and summary page (ConfigurationSummary.tsx).
 * The orchestrator decides whether to show the wizard or the summary based on config state.
 * The summary displays 4 sections: Connections, Routing, Dispatch, System.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';

// Mock the api module to prevent real network calls
vi.mock('../api', () => ({
  apiFetch: vi.fn(),
}));

// Mock the config module so api.ts doesn't throw "Config not loaded"
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

// Mock ConfigurationWizard — we're testing the orchestrator, not wizard internals
vi.mock('../ConfigurationWizard', () => ({
  default: ({ config, onSave }: any) => (
    <div data-testid="configuration-wizard">
      <span>Configuration Wizard</span>
      <button onClick={onSave} data-testid="wizard-save">Save</button>
    </div>
  ),
}));

// Mock the PlatformContext used by ConfigurationWizard
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

// --- Helper: Build config fixtures ---

function makeConfigFirstTime(): OnboardingConfig {
  return {
    platform: 'jira',
    jira: undefined,
    servicenow: undefined,
    routing: undefined,
    dispatch: undefined,
    setupComplete: false,
  };
}

function makeConfigJiraConnected(): OnboardingConfig {
  return {
    platform: 'jira',
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
  };
}

function makeConfigSnowConnected(): OnboardingConfig {
  return {
    platform: 'servicenow',
    jira: undefined,
    servicenow: {
      instanceUrl: 'https://myorg.service-now.com',
      validated: true,
      validatedAt: '2026-07-15T10:00:00Z',
      authType: 'oauth',
    },
    routing: {
      defaultProject: 'CLOUDOPS',
      accountMappingCount: 5,
    },
    dispatch: {
      mode: 'ple_only',
    },
    setupComplete: true,
  };
}

function makeConfigNoConnection(): OnboardingConfig {
  return {
    platform: 'jira',
    jira: {
      baseUrl: 'https://myorg.atlassian.net',
      validated: false,
    },
    routing: undefined,
    dispatch: undefined,
    setupComplete: false,
  };
}

// --- Mock API responses ---

function mockRoutingResponse(mappings: any[] = []) {
  return {
    defaultProject: 'CLOUDOPS',
    defaultIssueType: 'Task',
    mappings,
  };
}

function mockDispatchResponse(mode = 'all', rules: any[] = []) {
  return {
    mode,
    actionabilityFilter: 'all_actionable',
    rules,
    warning: null,
  };
}

// --- Tests: Configuration Orchestrator ---

describe('Configuration Orchestrator', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Default: API calls return empty successful responses
    mockApiFetch.mockImplementation(async (path: string) => {
      if (path === '/config/routing') return mockRoutingResponse();
      if (path === '/config/dispatch') return mockDispatchResponse();
      if (path === '/config/servicenow') return null;
      return {};
    });
  });

  // Dynamically import to get fresh module state
  async function renderConfiguration(config: OnboardingConfig | null) {
    const { default: Configuration } = await import('../Configuration');
    const onSave = vi.fn();
    const result = render(<Configuration config={config} onSave={onSave} />);
    return { ...result, onSave };
  }

  it('shows loading spinner when config is null', async () => {
    const { default: Configuration } = await import('../Configuration');
    const { container } = render(<Configuration config={null} onSave={vi.fn()} />);
    // When config is null, neither wizard nor summary is rendered
    expect(screen.queryByTestId('configuration-wizard')).not.toBeInTheDocument();
    expect(screen.queryByTestId('run-setup-wizard')).not.toBeInTheDocument();
    // Cloudscape Spinner renders with role="img" or as a decorative element
    // Verify something is rendered (the spinner container with textAlign center)
    expect(container.firstChild).toBeInTheDocument();
    // Ensure no wizard/summary content
    expect(screen.queryByText('Configuration Wizard')).not.toBeInTheDocument();
    expect(screen.queryByText('ITSM Connections')).not.toBeInTheDocument();
  });

  it('shows wizard when config has no validated connection and no routing (first-time user)', async () => {
    await renderConfiguration(makeConfigFirstTime());
    expect(screen.getByTestId('configuration-wizard')).toBeInTheDocument();
    expect(screen.getByText('Configuration Wizard')).toBeInTheDocument();
  });

  it('shows wizard when JIRA connection exists but is not validated and no routing', async () => {
    await renderConfiguration(makeConfigNoConnection());
    expect(screen.getByTestId('configuration-wizard')).toBeInTheDocument();
  });

  it('shows summary when config has validated JIRA connection + routing', async () => {
    await renderConfiguration(makeConfigJiraConnected());
    // Summary shows "Run Setup Wizard" button
    await waitFor(() => {
      expect(screen.getByTestId('run-setup-wizard')).toBeInTheDocument();
    });
    // Wizard should NOT be present
    expect(screen.queryByTestId('configuration-wizard')).not.toBeInTheDocument();
  });

  it('shows summary when config has validated ServiceNow connection + routing', async () => {
    await renderConfiguration(makeConfigSnowConnected());
    await waitFor(() => {
      expect(screen.getByTestId('run-setup-wizard')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('configuration-wizard')).not.toBeInTheDocument();
  });

  it('shows wizard when "Run Setup Wizard" button is clicked (forceWizard)', async () => {
    const user = userEvent.setup();
    await renderConfiguration(makeConfigJiraConnected());

    // Summary is initially shown
    await waitFor(() => {
      expect(screen.getByTestId('run-setup-wizard')).toBeInTheDocument();
    });

    // Click "Run Setup Wizard"
    await user.click(screen.getByTestId('run-setup-wizard'));

    // Now wizard should be shown
    expect(screen.getByTestId('configuration-wizard')).toBeInTheDocument();
  });

  it('returns to summary when wizard saves (forceWizard reset)', async () => {
    const user = userEvent.setup();
    const { onSave } = await renderConfiguration(makeConfigJiraConnected());

    await waitFor(() => {
      expect(screen.getByTestId('run-setup-wizard')).toBeInTheDocument();
    });

    // Force wizard
    await user.click(screen.getByTestId('run-setup-wizard'));
    expect(screen.getByTestId('configuration-wizard')).toBeInTheDocument();

    // Click save on wizard mock
    await user.click(screen.getByTestId('wizard-save'));

    // Should return to summary
    await waitFor(() => {
      expect(screen.getByTestId('run-setup-wizard')).toBeInTheDocument();
    });
    expect(onSave).toHaveBeenCalled();
  });
});

// --- Tests: ConfigurationSummary Component ---

describe('ConfigurationSummary', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  async function renderSummary(config: OnboardingConfig, apiOverrides?: Record<string, any>) {
    // Set up API mock responses
    mockApiFetch.mockImplementation(async (path: string) => {
      if (apiOverrides && path in apiOverrides) return apiOverrides[path];
      if (path === '/config/routing') {
        return mockRoutingResponse([
          { account_id: '111111111111', account_name: 'Production', jira_project: 'CLOUDOPS' },
          { account_id: '222222222222', account_name: 'Staging', jira_project: 'APPTEAM' },
          { account_id: '333333333333', account_name: 'Security', jira_project: 'SECURITY' },
        ]);
      }
      if (path === '/config/dispatch') return mockDispatchResponse();
      if (path === '/config/servicenow') return null;
      return {};
    });

    const { default: ConfigurationSummary } = await import('../ConfigurationSummary');
    const onRunWizard = vi.fn();
    const onConfigChanged = vi.fn();
    const result = render(<ConfigurationSummary config={config} onRunWizard={onRunWizard} onConfigChanged={onConfigChanged} />);
    return { ...result, onRunWizard };
  }

  describe('Section Rendering', () => {
    it('renders 4 sections: Connections, Routing, Dispatch, System', async () => {
      await renderSummary(makeConfigJiraConnected());

      await waitFor(() => {
        expect(screen.getByText('ITSM Connections')).toBeInTheDocument();
        expect(screen.getByText('Routing Rules')).toBeInTheDocument();
        expect(screen.getByText('Dispatch Window')).toBeInTheDocument();
        expect(screen.getByText('System Information')).toBeInTheDocument();
      });
    });
  });

  describe('ITSM Connections Section', () => {
    it('shows JIRA connection status as "Connected" when validated', async () => {
      await renderSummary(makeConfigJiraConnected());

      await waitFor(() => {
        expect(screen.getByText('Connected')).toBeInTheDocument();
      });
      expect(screen.getByText('https://myorg.atlassian.net')).toBeInTheDocument();
    });

    it('shows "Not configured" when JIRA connection not validated and no URL', async () => {
      await renderSummary(makeConfigFirstTime());

      await waitFor(() => {
        expect(screen.getByText('ITSM Connections')).toBeInTheDocument();
      });
      // "Not configured" for both JIRA and ServiceNow — find at least one
      const notConfigured = screen.getAllByText('Not configured');
      expect(notConfigured.length).toBeGreaterThanOrEqual(1);
    });

    it('shows ServiceNow connection status as "Connected" when validated', async () => {
      const snowConfig = makeConfigSnowConnected();
      mockApiFetch.mockImplementation(async (path: string) => {
        if (path === '/config/routing') return mockRoutingResponse();
        if (path === '/config/dispatch') return mockDispatchResponse();
        if (path === '/config/servicenow') {
          return {
            instanceUrl: 'https://myorg.service-now.com',
            validated: true,
            validatedAt: '2026-07-15T10:00:00Z',
            validatedUser: 'resolve_integration',
          };
        }
        return {};
      });

      const { default: ConfigurationSummary } = await import('../ConfigurationSummary');
      render(<ConfigurationSummary config={snowConfig} onRunWizard={vi.fn()} onConfigChanged={vi.fn()} />);

      await waitFor(() => {
        const connectedElements = screen.getAllByText('Connected');
        expect(connectedElements.length).toBeGreaterThanOrEqual(1);
      });
    });

    it('shows JIRA URL and validated user when connected', async () => {
      await renderSummary(makeConfigJiraConnected());

      await waitFor(() => {
        expect(screen.getByText('https://myorg.atlassian.net')).toBeInTheDocument();
        expect(screen.getByText('User: automation@company.com')).toBeInTheDocument();
      });
    });

    it('shows "Connection failed" when JIRA has URL but not validated', async () => {
      const config: OnboardingConfig = {
        platform: 'jira',
        jira: {
          baseUrl: 'https://myorg.atlassian.net',
          validated: false,
        },
        routing: { defaultProject: 'CLOUDOPS', accountMappingCount: 1 },
        dispatch: { mode: 'all' },
        setupComplete: true,
      };

      await renderSummary(config);

      await waitFor(() => {
        expect(screen.getByText('Connection failed')).toBeInTheDocument();
      });
    });
  });

  describe('Routing Section', () => {
    it('shows account mapping count and default project', async () => {
      await renderSummary(makeConfigJiraConnected());

      await waitFor(() => {
        // CLOUDOPS appears in both the default project field and possibly table rows
        const cloudopsElements = screen.getAllByText('CLOUDOPS');
        expect(cloudopsElements.length).toBeGreaterThanOrEqual(1);
        expect(screen.getByText('3 configured')).toBeInTheDocument();
      });
    });

    it('shows routing table with account mappings', async () => {
      await renderSummary(makeConfigJiraConnected());

      await waitFor(() => {
        expect(screen.getByText('111111111111')).toBeInTheDocument();
        expect(screen.getByText('Production')).toBeInTheDocument();
        expect(screen.getByText('222222222222')).toBeInTheDocument();
        expect(screen.getByText('Staging')).toBeInTheDocument();
      });
    });

    it('shows "(3)" counter in routing header', async () => {
      await renderSummary(makeConfigJiraConnected());

      await waitFor(() => {
        expect(screen.getByText('(3)')).toBeInTheDocument();
      });
    });

    it('shows error alert when routing API fails', async () => {
      mockApiFetch.mockImplementation(async (path: string) => {
        if (path === '/config/routing') throw new Error('API 500: Internal Server Error');
        if (path === '/config/dispatch') return mockDispatchResponse();
        return {};
      });

      const { default: ConfigurationSummary } = await import('../ConfigurationSummary');
      render(<ConfigurationSummary config={makeConfigJiraConnected()} onRunWizard={vi.fn()} onConfigChanged={vi.fn()} />);

      await waitFor(() => {
        expect(screen.getByText(/Request failed/)).toBeInTheDocument();
      });
    });
  });

  describe('Dispatch Section', () => {
    it('shows "All actionable events" for mode "all"', async () => {
      await renderSummary(makeConfigJiraConnected());

      await waitFor(() => {
        // "All actionable events" text appears in both the summary display and the
        // hidden DispatchEditModal. Verify at least one instance is present.
        const matches = screen.getAllByText('All actionable events');
        expect(matches.length).toBeGreaterThanOrEqual(1);
      });
    });

    it('shows "Planned Lifecycle Events only" for mode "ple_only"', async () => {
      mockApiFetch.mockImplementation(async (path: string) => {
        if (path === '/config/routing') return mockRoutingResponse();
        if (path === '/config/dispatch') return mockDispatchResponse('ple_only');
        return {};
      });

      const config: OnboardingConfig = {
        ...makeConfigJiraConnected(),
        dispatch: { mode: 'ple_only' },
      };

      const { default: ConfigurationSummary } = await import('../ConfigurationSummary');
      render(<ConfigurationSummary config={config} onRunWizard={vi.fn()} onConfigChanged={vi.fn()} />);

      await waitFor(() => {
        const matches = screen.getAllByText('Planned Lifecycle Events only');
        expect(matches.length).toBeGreaterThanOrEqual(1);
      });
    });

    it('shows "Custom rules" for mode "custom" with rule count', async () => {
      mockApiFetch.mockImplementation(async (path: string) => {
        if (path === '/config/routing') return mockRoutingResponse();
        if (path === '/config/dispatch') {
          return {
            mode: 'custom',
            actionabilityFilter: 'all_actionable',
            rules: [
              { ruleId: 'r1', eventTypePattern: 'AWS_EKS_*', eventCategories: ['scheduledChange'], enabled: true },
              { ruleId: 'r2', eventTypePattern: 'AWS_RDS_*', eventCategories: ['scheduledChange'], enabled: false },
            ],
            warning: null,
          };
        }
        return {};
      });

      const config: OnboardingConfig = {
        ...makeConfigJiraConnected(),
        dispatch: { mode: 'custom' },
      };

      const { default: ConfigurationSummary } = await import('../ConfigurationSummary');
      render(<ConfigurationSummary config={config} onRunWizard={vi.fn()} onConfigChanged={vi.fn()} />);

      await waitFor(() => {
        const matches = screen.getAllByText('Custom rules');
        expect(matches.length).toBeGreaterThanOrEqual(1);
        expect(screen.getByText('1 active, 1 disabled')).toBeInTheDocument();
      });
    });

    it('shows actionability filter value', async () => {
      await renderSummary(makeConfigJiraConnected());

      await waitFor(() => {
        expect(screen.getByText('ACTION_REQUIRED + ACTION_MAY_BE_REQUIRED')).toBeInTheDocument();
      });
    });
  });

  describe('Edit Buttons', () => {
    it('edit-connections button is present and enabled', async () => {
      await renderSummary(makeConfigJiraConnected());

      await waitFor(() => {
        const editConnections = screen.getByTestId('edit-connections');
        expect(editConnections).toBeInTheDocument();
        expect(editConnections).not.toBeDisabled();
      });
    });

    it('edit-routing button is present and ENABLED', async () => {
      await renderSummary(makeConfigJiraConnected());

      await waitFor(() => {
        const editRouting = screen.getByTestId('edit-routing');
        expect(editRouting).toBeInTheDocument();
        expect(editRouting).not.toBeDisabled();
      });
    });

    it('clicking edit-routing opens RoutingEditModal', async () => {
      const user = userEvent.setup();
      await renderSummary(makeConfigJiraConnected());

      await waitFor(() => {
        expect(screen.getByTestId('edit-routing')).toBeInTheDocument();
      });

      await user.click(screen.getByTestId('edit-routing'));

      await waitFor(() => {
        expect(screen.getByText('Edit Routing Rules')).toBeInTheDocument();
      });
    });

    it('edit-dispatch button is present and ENABLED', async () => {
      await renderSummary(makeConfigJiraConnected());

      await waitFor(() => {
        const editDispatch = screen.getByTestId('edit-dispatch');
        expect(editDispatch).toBeInTheDocument();
        expect(editDispatch).not.toBeDisabled();
      });
    });
  });

  describe('Run Setup Wizard Button', () => {
    it('"Run Setup Wizard" button is present and enabled', async () => {
      await renderSummary(makeConfigJiraConnected());

      await waitFor(() => {
        const wizardBtn = screen.getByTestId('run-setup-wizard');
        expect(wizardBtn).toBeInTheDocument();
        expect(wizardBtn).not.toBeDisabled();
      });
    });

    it('clicking "Run Setup Wizard" calls onRunWizard callback', async () => {
      const user = userEvent.setup();
      const { onRunWizard } = await renderSummary(makeConfigJiraConnected());

      await waitFor(() => {
        expect(screen.getByTestId('run-setup-wizard')).toBeInTheDocument();
      });

      await user.click(screen.getByTestId('run-setup-wizard'));
      expect(onRunWizard).toHaveBeenCalledTimes(1);
    });
  });

  describe('System Information Section', () => {
    it('shows active platform as JIRA Cloud', async () => {
      const user = userEvent.setup();
      await renderSummary(makeConfigJiraConnected());

      // System Information is in an expandable section, click to expand
      await waitFor(() => {
        expect(screen.getByText('System Information')).toBeInTheDocument();
      });

      // Click to expand (Cloudscape ExpandableSection)
      await user.click(screen.getByText('System Information'));

      await waitFor(() => {
        // "JIRA Cloud" appears in both Connections section (h3) and System section
        const jiraCloudElements = screen.getAllByText('JIRA Cloud');
        expect(jiraCloudElements.length).toBeGreaterThanOrEqual(2);
      });
    });

    it('shows active platform as ServiceNow for servicenow config', async () => {
      const user = userEvent.setup();
      await renderSummary(makeConfigSnowConnected());

      await waitFor(() => {
        expect(screen.getByText('System Information')).toBeInTheDocument();
      });

      await user.click(screen.getByText('System Information'));

      await waitFor(() => {
        // "ServiceNow" appears in both Connections section (h3) and System section
        const snowElements = screen.getAllByText('ServiceNow');
        expect(snowElements.length).toBeGreaterThanOrEqual(2);
      });
    });

    it('shows integration status as Active when connection validated', async () => {
      const user = userEvent.setup();
      await renderSummary(makeConfigJiraConnected());

      await waitFor(() => {
        expect(screen.getByText('System Information')).toBeInTheDocument();
      });

      await user.click(screen.getByText('System Information'));

      await waitFor(() => {
        expect(screen.getByText('Active')).toBeInTheDocument();
      });
    });
  });
});
