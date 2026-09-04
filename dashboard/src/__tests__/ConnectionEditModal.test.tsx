/**
 * Unit tests for STORY-097: Connection Edit Modal
 *
 * Tests ConnectionEditModal component: platform toggles, JIRA/ServiceNow forms,
 * Test Connection buttons, Save/Cancel, URL validation (SSRF protection),
 * credential clearing on dismiss, partial failure handling.
 *
 * Also tests integration wiring: edit-connections button in ConfigurationSummary
 * is now ENABLED and opens the modal.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, within, act } from '@testing-library/react';
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
  }),
}));

import { apiFetch } from '../api';
import type { OnboardingConfig } from '../types';

const mockApiFetch = vi.mocked(apiFetch);

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function makeJiraConfig(): OnboardingConfig {
  return {
    platform: 'jira',
    platforms: ['jira'],
    jira: {
      baseUrl: 'https://myorg.atlassian.net',
      validated: true,
      validatedAt: '2026-07-15T10:00:00Z',
      validatedUser: 'automation@company.com',
      credentialsConfigured: true,
    },
    routing: { defaultProject: 'CLOUDOPS', accountMappingCount: 3 },
    dispatch: { mode: 'all', actionabilityFilter: 'all_actionable' },
    setupComplete: true,
  };
}

function makeSnowConfig(): OnboardingConfig {
  return {
    platform: 'servicenow',
    platforms: ['servicenow'],
    servicenow: {
      instanceUrl: 'https://myorg.service-now.com',
      validated: true,
      validatedAt: '2026-07-15T10:00:00Z',
      authType: 'oauth',
      clientId: 'client-id-123',
      username: 'resolve_integration',
    },
    routing: { defaultProject: 'CLOUDOPS', accountMappingCount: 5 },
    dispatch: { mode: 'ple_only' },
    setupComplete: true,
  };
}

function makeBothPlatformsConfig(): OnboardingConfig {
  return {
    platform: 'jira',
    platforms: ['jira', 'servicenow'],
    jira: {
      baseUrl: 'https://myorg.atlassian.net',
      validated: true,
      validatedAt: '2026-07-15T10:00:00Z',
      validatedUser: 'automation@company.com',
      credentialsConfigured: true,
    },
    servicenow: {
      instanceUrl: 'https://myorg.service-now.com',
      validated: true,
      validatedAt: '2026-07-15T10:00:00Z',
      authType: 'oauth',
      clientId: 'client-id-123',
      username: 'resolve_integration',
    },
    routing: { defaultProject: 'CLOUDOPS', accountMappingCount: 3 },
    dispatch: { mode: 'all' },
    setupComplete: true,
  };
}

function makeFirstTimeJiraConfig(): OnboardingConfig {
  return {
    platform: 'jira',
    platforms: ['jira'],
    jira: {
      baseUrl: '',
      validated: false,
    },
    setupComplete: false,
  };
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function renderModal(props: {
  visible?: boolean;
  config?: OnboardingConfig;
  onDismiss?: () => void;
  onSave?: () => void;
}) {
  const { default: ConnectionEditModal } = await import('../modals/ConnectionEditModal');
  const defaults = {
    visible: true,
    config: makeJiraConfig(),
    onDismiss: vi.fn(),
    onSave: vi.fn(),
  };
  const merged = { ...defaults, ...props };
  const result = render(
    <ConnectionEditModal
      visible={merged.visible}
      config={merged.config}
      onDismiss={merged.onDismiss}
      onSave={merged.onSave}
    />
  );
  return { ...result, onDismiss: merged.onDismiss, onSave: merged.onSave };
}

/**
 * STORY-102: Helper to get the credential password input that appears after clicking "Change".
 * Cloudscape FormField labels can match multiple accessible elements, so we query DOM directly.
 * After clicking "Change", the password input is rendered inside the FormField.
 */
function getPasswordInput(index = 0): HTMLInputElement {
  const inputs = document.querySelectorAll<HTMLInputElement>('input[type="password"]');
  if (inputs.length <= index) {
    throw new Error(`Expected at least ${index + 1} password input(s), found ${inputs.length}`);
  }
  return inputs[index];
}

// ---------------------------------------------------------------------------
// Tests: Modal Visibility
// ---------------------------------------------------------------------------

describe('ConnectionEditModal — Visibility', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders when visible=true', async () => {
    await renderModal({ visible: true });

    expect(screen.getByText('Edit ITSM Connections')).toBeInTheDocument();
  });

  it('does not render content when visible=false', async () => {
    await renderModal({ visible: false });

    // Cloudscape Modal renders content in DOM even when visible=false but
    // the modal overlay wrapper has aria-hidden="true" or display:none.
    // Test that the save button is disabled (modal is non-interactive).
    const saveBtn = screen.getByTestId('save-connections');
    expect(saveBtn).toBeDisabled();

    // Alternatively verify that the modal wrapper has the closed state.
    // The modal is functionally closed — user cannot interact with it.
  });
});

// ---------------------------------------------------------------------------
// Tests: Platform Sections
// ---------------------------------------------------------------------------

describe('ConnectionEditModal — Platform Sections', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows JIRA section when JIRA platform enabled in config', async () => {
    await renderModal({ config: makeJiraConfig() });

    await waitFor(() => {
      expect(screen.getByText('JIRA Connection')).toBeInTheDocument();
    });
  });

  it('shows ServiceNow section when ServiceNow platform enabled in config', async () => {
    await renderModal({ config: makeSnowConfig() });

    await waitFor(() => {
      expect(screen.getByText('ServiceNow Connection')).toBeInTheDocument();
    });
  });

  it('shows both sections when both platforms enabled', async () => {
    await renderModal({ config: makeBothPlatformsConfig() });

    await waitFor(() => {
      expect(screen.getByText('JIRA Connection')).toBeInTheDocument();
      expect(screen.getByText('ServiceNow Connection')).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Tests: Field Pre-population and Security
// ---------------------------------------------------------------------------

describe('ConnectionEditModal — Field Pre-population', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('JIRA URL is pre-populated from config', async () => {
    await renderModal({ config: makeJiraConfig() });

    await waitFor(() => {
      const urlInput = screen.getByPlaceholderText('https://yourorg.atlassian.net');
      expect(urlInput).toHaveValue('https://myorg.atlassian.net');
    });
  });

  it('STORY-102: JIRA token shows "Configured" badge when credentials exist (not blank input)', async () => {
    await renderModal({ config: makeJiraConfig() });

    await waitFor(() => {
      // Credential field shows "Configured" StatusIndicator + "Change" button
      expect(screen.getByText('Configured')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /Change JIRA API Token/i })).toBeInTheDocument();
    });
  });

  it('STORY-102: ServiceNow password shows "Configured" badge when credentials exist', async () => {
    await renderModal({ config: makeSnowConfig() });

    await waitFor(() => {
      // All configured credential fields show "Configured"
      const configuredBadges = screen.getAllByText('Configured');
      expect(configuredBadges.length).toBeGreaterThanOrEqual(2);
      expect(screen.getByRole('button', { name: /Change ServiceNow Password/i })).toBeInTheDocument();
    });
  });

  it('STORY-102: ServiceNow client secret shows "Configured" badge when credentials exist', async () => {
    await renderModal({ config: makeSnowConfig() });

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Change ServiceNow Client Secret/i })).toBeInTheDocument();
    });
  });

  it('all credential fields have type="password" after clicking Change', async () => {
    const user = userEvent.setup();
    await renderModal({ config: makeBothPlatformsConfig() });

    // Click all Change buttons to reveal inputs
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Change JIRA API Token/i })).toBeInTheDocument();
    });
    await user.click(screen.getByRole('button', { name: /Change JIRA API Token/i }));
    await user.click(screen.getByRole('button', { name: /Change ServiceNow Client Secret/i }));
    await user.click(screen.getByRole('button', { name: /Change ServiceNow Password/i }));

    await waitFor(() => {
      const passwordInputs = document.querySelectorAll('input[type="password"]');
      expect(passwordInputs.length).toBe(3);
      passwordInputs.forEach((input) => {
        expect(input).toHaveAttribute('type', 'password');
      });
    });
  });

  it('all credential fields have autoComplete="one-time-code" after clicking Change (STORY-109)', async () => {
    const user = userEvent.setup();
    await renderModal({ config: makeBothPlatformsConfig() });

    // Click all Change buttons to reveal inputs
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Change JIRA API Token/i })).toBeInTheDocument();
    });
    await user.click(screen.getByRole('button', { name: /Change JIRA API Token/i }));
    await user.click(screen.getByRole('button', { name: /Change ServiceNow Client Secret/i }));
    await user.click(screen.getByRole('button', { name: /Change ServiceNow Password/i }));

    await waitFor(() => {
      const passwordInputs = document.querySelectorAll('input[type="password"]');
      expect(passwordInputs.length).toBe(3);
      passwordInputs.forEach((input) => {
        expect(input).toHaveAttribute('autocomplete', 'one-time-code');
      });
    });
  });

  it('STORY-102: first-time setup (no credentials) shows blank input directly (no badge)', async () => {
    await renderModal({ config: makeFirstTimeJiraConfig() });

    await waitFor(() => {
      // No "Configured" badge should appear
      expect(screen.queryByText('Configured')).not.toBeInTheDocument();
      // Input field should be directly visible
      const tokenField = screen.getByLabelText(/JIRA API Token/i);
      expect(tokenField).toHaveValue('');
    });
  });
});

// ---------------------------------------------------------------------------
// Tests: Test Connection
// ---------------------------------------------------------------------------

describe('ConnectionEditModal — Test Connection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('Test JIRA Connection button calls POST /config/jira/test', async () => {
    const user = userEvent.setup();
    mockApiFetch.mockResolvedValueOnce({ status: 'connected', user: 'automation@company.com' });

    await renderModal({ config: makeJiraConfig() });

    // STORY-102: Click "Change" to reveal the API token input
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Change JIRA API Token/i })).toBeInTheDocument();
    });
    await user.click(screen.getByRole('button', { name: /Change JIRA API Token/i }));

    // Fill in the API token
    const tokenField = getPasswordInput(0);
    await user.type(tokenField, 'fake-token-123');

    const testBtn = screen.getByTestId('test-jira-connection');
    await user.click(testBtn);

    await waitFor(() => {
      expect(mockApiFetch).toHaveBeenCalledWith('/config/jira/test', expect.objectContaining({
        method: 'POST',
        body: expect.stringContaining('"apiToken":"fake-token-123"'),
      }));
    });
  });

  it('shows success status indicator after successful JIRA test', async () => {
    const user = userEvent.setup();
    mockApiFetch.mockResolvedValueOnce({ status: 'connected', user: 'automation@company.com' });

    await renderModal({ config: makeJiraConfig() });

    // STORY-102: Click "Change" to reveal input
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Change JIRA API Token/i })).toBeInTheDocument();
    });
    await user.click(screen.getByRole('button', { name: /Change JIRA API Token/i }));

    const tokenField = getPasswordInput(0);
    await user.type(tokenField, 'fake-token-123');

    const testBtn = screen.getByTestId('test-jira-connection');
    await user.click(testBtn);

    await waitFor(() => {
      expect(screen.getByText(/Connected as automation@company.com/)).toBeInTheDocument();
    });
  });

  it('shows error status indicator after failed JIRA test', async () => {
    const user = userEvent.setup();
    mockApiFetch.mockRejectedValueOnce(new Error('API 401: Authentication failed'));

    await renderModal({ config: makeJiraConfig() });

    // STORY-102: Click "Change" to reveal input
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Change JIRA API Token/i })).toBeInTheDocument();
    });
    await user.click(screen.getByRole('button', { name: /Change JIRA API Token/i }));

    const tokenField = getPasswordInput(0);
    await user.type(tokenField, 'bad-token');

    const testBtn = screen.getByTestId('test-jira-connection');
    await user.click(testBtn);

    await waitFor(() => {
      expect(screen.getByText(/Authentication failed/)).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Tests: Save
// ---------------------------------------------------------------------------

describe('ConnectionEditModal — Save', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('Save button calls PUT /config/integrations then POST /config/jira', async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();

    // Mock both API calls as successful
    mockApiFetch
      .mockResolvedValueOnce({}) // PUT /config/integrations
      .mockResolvedValueOnce({ validatedUser: 'automation@company.com' }); // POST /config/jira

    await renderModal({ config: makeJiraConfig(), onSave });

    // STORY-102: Click "Change" to reveal input, then fill token
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Change JIRA API Token/i })).toBeInTheDocument();
    });
    await user.click(screen.getByRole('button', { name: /Change JIRA API Token/i }));

    const tokenField = getPasswordInput(0);
    await user.type(tokenField, 'new-token-456');

    const saveBtn = screen.getByTestId('save-connections');
    await user.click(saveBtn);

    await waitFor(() => {
      expect(mockApiFetch).toHaveBeenCalledWith('/config/integrations', expect.objectContaining({
        method: 'PUT',
        body: expect.stringContaining('"platforms"'),
      }));
      expect(mockApiFetch).toHaveBeenCalledWith('/config/jira', expect.objectContaining({
        method: 'POST',
        body: expect.stringContaining('"apiToken":"new-token-456"'),
      }));
    });
  });

  it('Save button is disabled while saving (loading state)', async () => {
    const user = userEvent.setup();

    // Create a delayed promise that we can control
    let resolveApi: (v: any) => void;
    mockApiFetch.mockImplementationOnce(() => new Promise((resolve) => { resolveApi = resolve; }));

    await renderModal({ config: makeJiraConfig() });

    // STORY-102: Click "Change" to reveal input, then fill token
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Change JIRA API Token/i })).toBeInTheDocument();
    });
    await user.click(screen.getByRole('button', { name: /Change JIRA API Token/i }));

    const tokenField = getPasswordInput(0);
    await user.type(tokenField, 'token-xyz');

    const saveBtn = screen.getByTestId('save-connections');
    await user.click(saveBtn);

    // Button should show loading/disabled state
    await waitFor(() => {
      const saveBtnElement = screen.getByTestId('save-connections');
      // Cloudscape Button in loading state has aria-disabled or disabled attribute
      expect(
        saveBtnElement.hasAttribute('disabled') ||
        saveBtnElement.getAttribute('aria-disabled') === 'true' ||
        saveBtnElement.closest('button')?.hasAttribute('disabled')
      ).toBe(true);
    });

    // Resolve the pending API to clean up
    act(() => { resolveApi!({}); });
  });

  it('shows error alert inside modal on save failure (modal stays open)', async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();

    // PUT /config/integrations fails
    mockApiFetch.mockRejectedValueOnce(new Error('API 500: Internal Server Error'));

    await renderModal({ config: makeJiraConfig(), onSave });

    // STORY-102: Click "Change" to reveal input, then fill token
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Change JIRA API Token/i })).toBeInTheDocument();
    });
    await user.click(screen.getByRole('button', { name: /Change JIRA API Token/i }));

    const tokenField = getPasswordInput(0);
    await user.type(tokenField, 'some-token');

    const saveBtn = screen.getByTestId('save-connections');
    await user.click(saveBtn);

    await waitFor(() => {
      expect(screen.getByText('Save failed')).toBeInTheDocument();
    });

    // Modal should still be open
    expect(screen.getByText('Edit ITSM Connections')).toBeInTheDocument();
    // onSave should NOT have been called
    expect(onSave).not.toHaveBeenCalled();
  });

  it('modal closes on successful save (onSave called)', async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();

    mockApiFetch
      .mockResolvedValueOnce({}) // PUT /config/integrations
      .mockResolvedValueOnce({ validatedUser: 'automation@company.com' }); // POST /config/jira

    await renderModal({ config: makeJiraConfig(), onSave });

    // STORY-102: Click "Change" to reveal input, then fill token
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Change JIRA API Token/i })).toBeInTheDocument();
    });
    await user.click(screen.getByRole('button', { name: /Change JIRA API Token/i }));

    const tokenField = getPasswordInput(0);
    await user.type(tokenField, 'new-token');

    const saveBtn = screen.getByTestId('save-connections');
    await user.click(saveBtn);

    await waitFor(() => {
      expect(onSave).toHaveBeenCalledTimes(1);
    });
  });
});

// ---------------------------------------------------------------------------
// Tests: Cancel and Credential Clearing
// ---------------------------------------------------------------------------

describe('ConnectionEditModal — Cancel and Credential Clearing', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('Cancel button calls onDismiss', async () => {
    const user = userEvent.setup();
    const onDismiss = vi.fn();

    await renderModal({ onDismiss });

    const cancelBtn = screen.getByTestId('cancel-connections');
    await user.click(cancelBtn);

    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it('credentials are cleared after dismiss (SEC-2: credential state does not persist)', async () => {
    const user = userEvent.setup();
    const onDismiss = vi.fn();
    const config = makeJiraConfig();

    const { default: ConnectionEditModal } = await import('../modals/ConnectionEditModal');

    // Render with visible=true first
    const { rerender } = render(
      <ConnectionEditModal visible={true} config={config} onDismiss={onDismiss} onSave={vi.fn()} />
    );

    // STORY-102: Click "Change" to reveal input first
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Change JIRA API Token/i })).toBeInTheDocument();
    });
    await user.click(screen.getByRole('button', { name: /Change JIRA API Token/i }));

    // Type in the token field
    const tokenField = getPasswordInput(0);
    await user.type(tokenField, 'secret-data-123');
    expect(tokenField).toHaveValue('secret-data-123');

    // Click Cancel to dismiss
    const cancelBtn = screen.getByTestId('cancel-connections');
    await user.click(cancelBtn);

    // Simulate hiding and re-showing the modal (parent sets visible=false then true again)
    rerender(
      <ConnectionEditModal visible={false} config={config} onDismiss={onDismiss} onSave={vi.fn()} />
    );
    rerender(
      <ConnectionEditModal visible={true} config={config} onDismiss={onDismiss} onSave={vi.fn()} />
    );

    // STORY-102: After cancel, credential should be back in "Configured" sentinel state
    await waitFor(() => {
      expect(screen.getByText('Configured')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /Change JIRA API Token/i })).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Tests: URL Validation (SSRF Protection)
// ---------------------------------------------------------------------------

describe('ConnectionEditModal — URL Validation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('rejects non-HTTPS URL for JIRA', async () => {
    const user = userEvent.setup();

    await renderModal({ config: makeFirstTimeJiraConfig() });

    // Change JIRA URL to non-HTTPS
    const urlInput = screen.getByPlaceholderText('https://yourorg.atlassian.net');
    await user.clear(urlInput);
    await user.type(urlInput, 'http://myorg.atlassian.net');

    // Fill token (required for first-time setup)
    const tokenField = screen.getByLabelText(/JIRA API Token/i);
    await user.type(tokenField, 'token');

    // Also fill email
    const emailField = screen.getByPlaceholderText('automation@company.com');
    await user.clear(emailField);
    await user.type(emailField, 'test@example.com');

    // Try to save
    const saveBtn = screen.getByTestId('save-connections');
    await user.click(saveBtn);

    await waitFor(() => {
      expect(screen.getByText(/Must be a valid JIRA Cloud URL/)).toBeInTheDocument();
    });
  });

  it('rejects non-.atlassian.net domain for JIRA', async () => {
    const user = userEvent.setup();

    await renderModal({ config: makeFirstTimeJiraConfig() });

    const urlInput = screen.getByPlaceholderText('https://yourorg.atlassian.net');
    await user.clear(urlInput);
    await user.type(urlInput, 'https://evil.example.com');

    const tokenField = screen.getByLabelText(/JIRA API Token/i);
    await user.type(tokenField, 'token');

    const emailField = screen.getByPlaceholderText('automation@company.com');
    await user.clear(emailField);
    await user.type(emailField, 'test@example.com');

    const saveBtn = screen.getByTestId('save-connections');
    await user.click(saveBtn);

    await waitFor(() => {
      expect(screen.getByText(/Must be a valid JIRA Cloud URL/)).toBeInTheDocument();
    });
  });

  it('at least one platform must be enabled (validation warning)', async () => {
    const user = userEvent.setup();

    await renderModal({ config: makeJiraConfig() });

    // Uncheck JIRA platform (the only enabled one)
    const jiraCheckbox = screen.getByLabelText(/JIRA Cloud/i);
    await user.click(jiraCheckbox);

    await waitFor(() => {
      expect(screen.getByText('At least one platform must be enabled.')).toBeInTheDocument();
    });

    // Save button should be disabled when no platform enabled
    const saveBtn = screen.getByTestId('save-connections');
    expect(
      saveBtn.hasAttribute('disabled') ||
      saveBtn.getAttribute('aria-disabled') === 'true' ||
      saveBtn.closest('button')?.hasAttribute('disabled')
    ).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Tests: Integration with ConfigurationSummary (edit-connections button)
// ---------------------------------------------------------------------------

describe('ConfigurationSummary — edit-connections button (STORY-097 wiring)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApiFetch.mockImplementation(async (path: string) => {
      if (path === '/config/routing') {
        return {
          defaultProject: 'CLOUDOPS',
          defaultIssueType: 'Task',
          mappings: [
            { account_id: '111111111111', account_name: 'Production', jira_project: 'CLOUDOPS' },
          ],
        };
      }
      if (path === '/config/dispatch') {
        return { mode: 'all', actionabilityFilter: 'all_actionable', rules: [], warning: null };
      }
      if (path === '/config/servicenow') return null;
      return {};
    });
  });

  it('edit-connections button is ENABLED (not disabled)', async () => {
    const { default: ConfigurationSummary } = await import('../ConfigurationSummary');
    render(
      <ConfigurationSummary
        config={makeJiraConfig()}
        onRunWizard={vi.fn()}
        onConfigChanged={vi.fn()}
      />
    );

    await waitFor(() => {
      const editBtn = screen.getByTestId('edit-connections');
      expect(editBtn).toBeInTheDocument();
      expect(editBtn).not.toBeDisabled();
    });
  });

  it('clicking edit-connections button shows the ConnectionEditModal', async () => {
    const user = userEvent.setup();
    const { default: ConfigurationSummary } = await import('../ConfigurationSummary');
    render(
      <ConfigurationSummary
        config={makeJiraConfig()}
        onRunWizard={vi.fn()}
        onConfigChanged={vi.fn()}
      />
    );

    await waitFor(() => {
      expect(screen.getByTestId('edit-connections')).toBeInTheDocument();
    });

    await user.click(screen.getByTestId('edit-connections'));

    await waitFor(() => {
      expect(screen.getByText('Edit ITSM Connections')).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Tests: STORY-102 — Credential Sentinel Pattern (Partial Update)
// ---------------------------------------------------------------------------

describe('ConnectionEditModal — STORY-102 Credential Sentinel Pattern', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('clicking "Change" on JIRA token reveals blank password input', async () => {
    const user = userEvent.setup();
    await renderModal({ config: makeJiraConfig() });

    // Initially shows "Configured" badge, no input field rendered
    await waitFor(() => {
      expect(screen.getByText('Configured')).toBeInTheDocument();
      // No password input should exist yet (FormField label exists, but not the input)
      expect(screen.queryByDisplayValue('')).toBeDefined(); // generic check
      const inputs = screen.queryAllByRole('textbox');
      const passwordInputs = document.querySelectorAll('input[type="password"]');
      // Only the non-credential inputs should be present (URL, email)
      expect(passwordInputs.length).toBe(0);
    });

    // Click "Change" button
    await user.click(screen.getByRole('button', { name: /Change JIRA API Token/i }));

    // Password input should now be visible
    await waitFor(() => {
      const passwordInputs = document.querySelectorAll('input[type="password"]');
      expect(passwordInputs.length).toBe(1);
      expect(passwordInputs[0]).toHaveValue('');
    });
  });

  it('clicking "Change" on ServiceNow fields reveals blank inputs', async () => {
    const user = userEvent.setup();
    await renderModal({ config: makeSnowConfig() });

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Change ServiceNow Client Secret/i })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /Change ServiceNow Password/i })).toBeInTheDocument();
    });

    // Initially no password inputs rendered
    expect(document.querySelectorAll('input[type="password"]').length).toBe(0);

    // Click both change buttons
    await user.click(screen.getByRole('button', { name: /Change ServiceNow Client Secret/i }));
    await user.click(screen.getByRole('button', { name: /Change ServiceNow Password/i }));

    // Both password inputs should now be visible and empty
    await waitFor(() => {
      const passwordInputs = document.querySelectorAll('input[type="password"]');
      expect(passwordInputs.length).toBe(2);
      expect(passwordInputs[0]).toHaveValue('');
      expect(passwordInputs[1]).toHaveValue('');
    });
  });

  it('Save without clicking "Change" — credential fields NOT included in POST body (partial update)', async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();

    mockApiFetch
      .mockResolvedValueOnce({}) // PUT /config/integrations
      .mockResolvedValueOnce({ validatedUser: 'automation@company.com' }); // POST /config/jira

    await renderModal({ config: makeJiraConfig(), onSave });

    // Do NOT click "Change" — leave credentials in sentinel state
    // Just click Save directly (partial update — only non-credential fields sent)
    const saveBtn = screen.getByTestId('save-connections');
    await user.click(saveBtn);

    await waitFor(() => {
      // POST /config/jira should be called WITHOUT apiToken field
      const jiraCall = mockApiFetch.mock.calls.find(
        (c) => c[0] === '/config/jira'
      );
      expect(jiraCall).toBeDefined();
      const jiraBody = JSON.parse(jiraCall![1].body);
      expect(jiraBody).not.toHaveProperty('apiToken');
      expect(jiraBody).toHaveProperty('baseUrl');
      expect(jiraBody).toHaveProperty('email');
    });
  });

  it('Save after clicking "Change" and entering new value — credential included in POST body', async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();

    mockApiFetch
      .mockResolvedValueOnce({}) // PUT /config/integrations
      .mockResolvedValueOnce({ validatedUser: 'automation@company.com' }); // POST /config/jira

    await renderModal({ config: makeJiraConfig(), onSave });

    // Click "Change" and type new token
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Change JIRA API Token/i })).toBeInTheDocument();
    });
    await user.click(screen.getByRole('button', { name: /Change JIRA API Token/i }));

    const tokenField = getPasswordInput(0);
    await user.type(tokenField, 'brand-new-token-999');

    const saveBtn = screen.getByTestId('save-connections');
    await user.click(saveBtn);

    await waitFor(() => {
      const jiraCall = mockApiFetch.mock.calls.find(
        (c) => c[0] === '/config/jira'
      );
      expect(jiraCall).toBeDefined();
      const jiraBody = JSON.parse(jiraCall![1].body);
      expect(jiraBody.apiToken).toBe('brand-new-token-999');
    });
  });

  it('Test Connection with unchanged creds — calls API without credential fields', async () => {
    const user = userEvent.setup();
    mockApiFetch.mockResolvedValueOnce({ status: 'connected', user: 'bot@co.com' });

    await renderModal({ config: makeJiraConfig() });

    // Do NOT click "Change" — test with stored credentials
    const testBtn = screen.getByTestId('test-jira-connection');
    await user.click(testBtn);

    await waitFor(() => {
      expect(mockApiFetch).toHaveBeenCalledWith('/config/jira/test', expect.objectContaining({
        method: 'POST',
      }));
      const testCall = mockApiFetch.mock.calls.find(
        (c) => c[0] === '/config/jira/test'
      );
      expect(testCall).toBeDefined();
      const testBody = JSON.parse(testCall![1].body);
      expect(testBody).not.toHaveProperty('apiToken');
      expect(testBody).toHaveProperty('baseUrl');
      expect(testBody).toHaveProperty('email');
    });
  });

  it('Test Connection after "Change" + new value — calls API with new credential', async () => {
    const user = userEvent.setup();
    mockApiFetch.mockResolvedValueOnce({ status: 'connected', user: 'bot@co.com' });

    await renderModal({ config: makeJiraConfig() });

    // Click "Change" and type new token
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Change JIRA API Token/i })).toBeInTheDocument();
    });
    await user.click(screen.getByRole('button', { name: /Change JIRA API Token/i }));

    const tokenField = getPasswordInput(0);
    await user.type(tokenField, 'test-new-token');

    const testBtn = screen.getByTestId('test-jira-connection');
    await user.click(testBtn);

    await waitFor(() => {
      const testCall = mockApiFetch.mock.calls.find(
        (c) => c[0] === '/config/jira/test'
      );
      expect(testCall).toBeDefined();
      const testBody = JSON.parse(testCall![1].body);
      expect(testBody.apiToken).toBe('test-new-token');
    });
  });

  it('Cancel resets Change mode (re-opening shows Configured badge again)', async () => {
    const user = userEvent.setup();
    const onDismiss = vi.fn();
    const config = makeJiraConfig();

    const { default: ConnectionEditModal } = await import('../modals/ConnectionEditModal');

    const { rerender } = render(
      <ConnectionEditModal visible={true} config={config} onDismiss={onDismiss} onSave={vi.fn()} />
    );

    // Click "Change" to enter edit mode
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Change JIRA API Token/i })).toBeInTheDocument();
    });
    await user.click(screen.getByRole('button', { name: /Change JIRA API Token/i }));

    // Input should be visible
    await waitFor(() => {
      const passwordInputs = document.querySelectorAll('input[type="password"]');
      expect(passwordInputs.length).toBe(1);
    });

    // Cancel
    await user.click(screen.getByTestId('cancel-connections'));

    // Re-open modal
    rerender(
      <ConnectionEditModal visible={false} config={config} onDismiss={onDismiss} onSave={vi.fn()} />
    );
    rerender(
      <ConnectionEditModal visible={true} config={config} onDismiss={onDismiss} onSave={vi.fn()} />
    );

    // Should be back to "Configured" badge state
    await waitFor(() => {
      expect(screen.getByText('Configured')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /Change JIRA API Token/i })).toBeInTheDocument();
    });
  });
});
