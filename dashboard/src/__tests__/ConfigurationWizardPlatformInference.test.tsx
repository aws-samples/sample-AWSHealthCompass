/**
 * Unit tests for STORY-108: Wizard ServiceNow configured status fix
 *
 * Tests that ConfigurationWizard correctly infers enabledPlatforms from
 * the config object when the /config/integrations API returns empty data.
 *
 * The fix adds 4 lines of platform inference logic:
 *   if (config.jira?.validated || config.jira?.baseUrl) inferred.push('jira');
 *   if (config.servicenow?.validated || config.servicenow?.instanceUrl) inferred.push('servicenow');
 *   if (inferred.length > 0) setEnabledPlatforms(inferred);
 *
 * Verified by checking checkbox state on Step 0 (Platform Selection):
 *   - Checkbox index 0 = JIRA Cloud
 *   - Checkbox index 1 = ServiceNow
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import React from 'react';

// Mock the api module
vi.mock('../api', () => ({
  apiFetch: vi.fn(),
}));

// Mock the config module
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

// Mock PlatformContext
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

/**
 * Helper: get the platform checkboxes from the wizard's Step 0.
 * Returns [jiraCheckbox, servicenowCheckbox] as native input elements.
 */
function getPlatformCheckboxes(container: HTMLElement): [HTMLInputElement, HTMLInputElement] {
  const checkboxes = container.querySelectorAll('input[type="checkbox"]');
  // Wizard Step 0 renders JIRA at index 0, ServiceNow at index 1
  return [checkboxes[0] as HTMLInputElement, checkboxes[1] as HTMLInputElement];
}

describe('ConfigurationWizard — Platform Inference (STORY-108)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // /config/integrations returns empty platforms array — forces fallback to inference logic
    mockApiFetch.mockImplementation(async (path: string) => {
      if (path === '/config/integrations') return { platforms: [] };
      if (path === '/config/setup-timer') return { elapsed: 0 };
      if (path === '/config/setup-timer/start') return {};
      return {};
    });
  });

  async function renderWizard(config: OnboardingConfig) {
    const { default: ConfigurationWizard } = await import('../ConfigurationWizard');
    const onSave = vi.fn();
    const result = render(<ConfigurationWizard config={config} onSave={onSave} />);
    return { ...result, onSave };
  }

  it('infers enabledPlatforms = ["servicenow"] when config.servicenow.validated = true (no jira)', async () => {
    const config: OnboardingConfig = {
      platform: 'servicenow',
      jira: undefined,
      servicenow: {
        instanceUrl: 'https://myorg.service-now.com',
        validated: true,
        validatedAt: '2026-07-15T10:00:00Z',
        authType: 'oauth',
      },
      routing: { defaultProject: 'CLOUDOPS', accountMappingCount: 1 },
      dispatch: { mode: 'all' },
      setupComplete: true,
    };

    const { container } = await renderWizard(config);

    await waitFor(() => {
      const [jiraCb, snowCb] = getPlatformCheckboxes(container);
      // ServiceNow is inferred from config.servicenow.validated
      expect(snowCb.checked).toBe(true);
      // JIRA is NOT inferred (no jira config)
      expect(jiraCb.checked).toBe(false);
    });
  });

  it('infers enabledPlatforms includes "servicenow" when config.servicenow.instanceUrl is set (not validated)', async () => {
    const config: OnboardingConfig = {
      platform: 'servicenow',
      jira: undefined,
      servicenow: {
        instanceUrl: 'https://devtest.service-now.com',
        validated: false,
      },
      routing: undefined,
      dispatch: undefined,
      setupComplete: false,
    };

    const { container } = await renderWizard(config);

    await waitFor(() => {
      const [jiraCb, snowCb] = getPlatformCheckboxes(container);
      // ServiceNow inferred from instanceUrl presence
      expect(snowCb.checked).toBe(true);
      // JIRA not inferred
      expect(jiraCb.checked).toBe(false);
    });
  });

  it('defaults enabledPlatforms to ["jira"] when neither platform has config data', async () => {
    const config: OnboardingConfig = {
      platform: 'jira',
      jira: undefined,
      servicenow: undefined,
      routing: undefined,
      dispatch: undefined,
      setupComplete: false,
    };

    const { container } = await renderWizard(config);

    await waitFor(() => {
      const [jiraCb, snowCb] = getPlatformCheckboxes(container);
      // Default: JIRA checked, ServiceNow unchecked
      expect(jiraCb.checked).toBe(true);
      expect(snowCb.checked).toBe(false);
    });
  });

  it('infers both platforms when both JIRA and ServiceNow have config data', async () => {
    const config: OnboardingConfig = {
      platform: 'jira',
      jira: {
        baseUrl: 'https://myorg.atlassian.net',
        validated: true,
        validatedAt: '2026-07-15T10:00:00Z',
        validatedUser: 'automation@company.com',
      },
      servicenow: {
        instanceUrl: 'https://myorg.service-now.com',
        validated: true,
        validatedAt: '2026-07-15T10:00:00Z',
        authType: 'oauth',
      },
      routing: { defaultProject: 'CLOUDOPS', accountMappingCount: 2 },
      dispatch: { mode: 'all' },
      setupComplete: true,
    };

    const { container } = await renderWizard(config);

    await waitFor(() => {
      const [jiraCb, snowCb] = getPlatformCheckboxes(container);
      // Both inferred
      expect(jiraCb.checked).toBe(true);
      expect(snowCb.checked).toBe(true);
    });
  });

  it('integrations API overrides inference when it returns valid platforms', async () => {
    // API returns ['jira'] only — should override inference from config
    mockApiFetch.mockImplementation(async (path: string) => {
      if (path === '/config/integrations') return { platforms: ['jira'] };
      if (path === '/config/setup-timer') return { elapsed: 0 };
      if (path === '/config/setup-timer/start') return {};
      return {};
    });

    const config: OnboardingConfig = {
      platform: 'servicenow',
      jira: undefined,
      servicenow: {
        instanceUrl: 'https://myorg.service-now.com',
        validated: true,
        validatedAt: '2026-07-15T10:00:00Z',
        authType: 'oauth',
      },
      routing: { defaultProject: 'OPS', accountMappingCount: 1 },
      dispatch: { mode: 'all' },
      setupComplete: true,
    };

    const { container } = await renderWizard(config);

    // Initial: inference sets ['servicenow']. Then API overrides to ['jira'].
    // Wait for the final state.
    await waitFor(() => {
      const [jiraCb, snowCb] = getPlatformCheckboxes(container);
      expect(jiraCb.checked).toBe(true);
    });
  });
});
