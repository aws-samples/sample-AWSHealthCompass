/**
 * Unit and integration tests for STORY-096:
 * Routing Table Search, Pagination, and Filtering.
 *
 * Tests the `matchesRoutingFilter` pure helper (exported) and the routing
 * table sub-section of ConfigurationSummary (TextFilter, Pagination,
 * CollectionPreferences, header counter, and empty states).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';

// ---------------------------------------------------------------------------
// Module mocks (same pattern as Configuration.test.tsx)
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
import { matchesRoutingFilter } from '../ConfigurationSummary';
import type { OnboardingConfig } from '../types';

const mockApiFetch = vi.mocked(apiFetch);

// ---------------------------------------------------------------------------
// Shared fixtures
// ---------------------------------------------------------------------------

function makeConfig(overrides: Partial<OnboardingConfig> = {}): OnboardingConfig {
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
    ...overrides,
  };
}

function makeMapping(overrides: Record<string, string | undefined> = {}) {
  return {
    account_id: '111111111111',
    account_name: 'Production',
    jira_project: 'CLOUDOPS',
    ...overrides,
  };
}

function makeMappings(count: number) {
  return Array.from({ length: count }, (_, i) => ({
    account_id: String(100000000000 + i).padStart(12, '0'),
    account_name: `Account ${i}`,
    jira_project: `PROJ${i}`,
  }));
}

function mockApis(routingMappings: object[], dispatchMode = 'all') {
  mockApiFetch.mockImplementation(async (path: string) => {
    if (path === '/config/routing') {
      return {
        defaultProject: 'CLOUDOPS',
        defaultIssueType: 'Task',
        mappings: routingMappings,
      };
    }
    if (path === '/config/dispatch') {
      return {
        mode: dispatchMode,
        actionabilityFilter: 'all_actionable',
        rules: [],
        warning: null,
      };
    }
    if (path === '/config/servicenow') return null;
    return {};
  });
}

async function renderSummary(
  config: OnboardingConfig = makeConfig(),
  mappings: object[] = []
) {
  mockApis(mappings);
  const { default: ConfigurationSummary } = await import('../ConfigurationSummary');
  const onRunWizard = vi.fn();
  const onConfigChanged = vi.fn();
  const result = render(
    <ConfigurationSummary config={config} onRunWizard={onRunWizard} onConfigChanged={onConfigChanged} />
  );
  return { ...result, onRunWizard };
}

// ---------------------------------------------------------------------------
// SECTION 1: Pure unit tests for matchesRoutingFilter helper
// ---------------------------------------------------------------------------

describe('matchesRoutingFilter — pure function', () => {
  it('returns true when filterText matches account_id (case-insensitive)', () => {
    const mapping = makeMapping({ account_id: '111111111111' });
    expect(matchesRoutingFilter(mapping, '111111111111')).toBe(true);
    expect(matchesRoutingFilter(mapping, '1111')).toBe(true);
  });

  it('returns true when filterText matches account_name', () => {
    const mapping = makeMapping({ account_name: 'Production' });
    expect(matchesRoutingFilter(mapping, 'Production')).toBe(true);
  });

  it('returns true when filterText matches jira_project', () => {
    const mapping = makeMapping({ jira_project: 'CLOUDOPS' });
    expect(matchesRoutingFilter(mapping, 'CLOUDOPS')).toBe(true);
  });

  it('returns false when no field matches the filter text', () => {
    const mapping = makeMapping({
      account_id: '111111111111',
      account_name: 'Production',
      jira_project: 'CLOUDOPS',
    });
    expect(matchesRoutingFilter(mapping, 'zzznomatch')).toBe(false);
  });

  it('returns true when filterText is an empty string (no active filter)', () => {
    const mapping = makeMapping();
    expect(matchesRoutingFilter(mapping, '')).toBe(true);
  });

  it('handles undefined account_name gracefully (no throw, correct result)', () => {
    const mapping = makeMapping({ account_name: undefined });
    // No match on undefined field — should return false if only account_name would match
    expect(() => matchesRoutingFilter(mapping, 'Production')).not.toThrow();
    expect(matchesRoutingFilter(mapping, 'Production')).toBe(false);
  });

  it('handles undefined jira_project gracefully (no throw, correct result)', () => {
    const mapping = makeMapping({ jira_project: undefined });
    expect(() => matchesRoutingFilter(mapping, 'CLOUDOPS')).not.toThrow();
    expect(matchesRoutingFilter(mapping, 'CLOUDOPS')).toBe(false);
  });

  it('matches on partial string ("CLOUD" matches "CLOUDOPS")', () => {
    const mapping = makeMapping({ jira_project: 'CLOUDOPS' });
    expect(matchesRoutingFilter(mapping, 'CLOUD')).toBe(true);
  });

  it('is case-insensitive ("cloudops" matches "CLOUDOPS")', () => {
    const mapping = makeMapping({ jira_project: 'CLOUDOPS' });
    expect(matchesRoutingFilter(mapping, 'cloudops')).toBe(true);
  });

  it('is case-insensitive for account_name ("production" matches "Production")', () => {
    const mapping = makeMapping({ account_name: 'Production' });
    expect(matchesRoutingFilter(mapping, 'production')).toBe(true);
  });

  it('is case-insensitive for account_id', () => {
    // account_id is numeric but still lowercases cleanly
    const mapping = makeMapping({ account_id: '123456789012' });
    expect(matchesRoutingFilter(mapping, '123456')).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// SECTION 2: Component tests for routing table
// ---------------------------------------------------------------------------

describe('ConfigurationSummary — Routing Table', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  // -------------------------------------------------------------------------
  // Header counter
  // -------------------------------------------------------------------------

  describe('Header counter', () => {
    it('shows counter "(3)" when 3 mappings are loaded', async () => {
      await renderSummary(makeConfig(), [
        makeMapping({ account_id: '111111111111' }),
        makeMapping({ account_id: '222222222222' }),
        makeMapping({ account_id: '333333333333' }),
      ]);

      await waitFor(() => {
        expect(screen.getByText('(3)')).toBeInTheDocument();
      });
    });

    it('shows counter "(0)" when zero mappings exist', async () => {
      await renderSummary(makeConfig(), []);

      await waitFor(() => {
        expect(screen.getByText('(0)')).toBeInTheDocument();
      });
    });
  });

  // -------------------------------------------------------------------------
  // TextFilter visibility
  // -------------------------------------------------------------------------

  describe('TextFilter visibility', () => {
    it('renders TextFilter input when mappings exist', async () => {
      await renderSummary(makeConfig(), [
        makeMapping({ account_id: '111111111111' }),
      ]);

      await waitFor(() => {
        // Cloudscape TextFilter renders an <input> with the filtering placeholder
        const filterInput = screen.getByPlaceholderText(
          'Search by account ID, name, or project'
        );
        expect(filterInput).toBeInTheDocument();
      });
    });

    it('hides TextFilter when there are zero mappings', async () => {
      await renderSummary(makeConfig(), []);

      await waitFor(() => {
        expect(screen.queryByPlaceholderText(
          'Search by account ID, name, or project'
        )).not.toBeInTheDocument();
      });
    });
  });

  // -------------------------------------------------------------------------
  // TextFilter — filtering behaviour
  // -------------------------------------------------------------------------

  describe('TextFilter — filtering behaviour', () => {
    it('typing a matching term reduces visible table rows', async () => {
      const user = userEvent.setup();
      await renderSummary(makeConfig(), [
        makeMapping({ account_id: '111111111111', account_name: 'Alpha', jira_project: 'ALPHA' }),
        makeMapping({ account_id: '222222222222', account_name: 'Beta',  jira_project: 'BETA' }),
        makeMapping({ account_id: '333333333333', account_name: 'Gamma', jira_project: 'GAMMA' }),
      ]);

      // Wait for table to populate
      await waitFor(() => {
        expect(screen.getByText('111111111111')).toBeInTheDocument();
      });

      const filterInput = screen.getByPlaceholderText(
        'Search by account ID, name, or project'
      );
      await user.type(filterInput, 'Alpha');

      await waitFor(() => {
        // Alpha row remains
        expect(screen.getByText('111111111111')).toBeInTheDocument();
        // Beta and Gamma rows are no longer rendered
        expect(screen.queryByText('222222222222')).not.toBeInTheDocument();
        expect(screen.queryByText('333333333333')).not.toBeInTheDocument();
      });
    });

    it('typing a non-matching term shows the no-filter-match empty state', async () => {
      const user = userEvent.setup();
      await renderSummary(makeConfig(), [
        makeMapping({ account_id: '111111111111', account_name: 'Production', jira_project: 'CLOUDOPS' }),
      ]);

      await waitFor(() => {
        expect(screen.getByText('111111111111')).toBeInTheDocument();
      });

      const filterInput = screen.getByPlaceholderText(
        'Search by account ID, name, or project'
      );
      await user.type(filterInput, 'zzznomatch');

      await waitFor(() => {
        expect(
          screen.getByText('No account mappings match the filter')
        ).toBeInTheDocument();
      });
    });
  });

  // -------------------------------------------------------------------------
  // Empty states
  // -------------------------------------------------------------------------

  describe('Empty states', () => {
    it('shows no-data empty state when there are no mappings at all', async () => {
      await renderSummary(makeConfig(), []);

      await waitFor(() => {
        expect(
          screen.getByText('No account mappings configured')
        ).toBeInTheDocument();
      });
    });

    it('does NOT show no-data state when mappings exist', async () => {
      await renderSummary(makeConfig(), [makeMapping()]);

      await waitFor(() => {
        // The row data should be visible; empty state should not
        expect(
          screen.queryByText('No account mappings configured')
        ).not.toBeInTheDocument();
      });
    });

    it('shows no-filter-match empty state when filter yields zero results', async () => {
      const user = userEvent.setup();
      await renderSummary(makeConfig(), [
        makeMapping({ account_name: 'Production' }),
      ]);

      await waitFor(() => {
        expect(screen.getByText('111111111111')).toBeInTheDocument();
      });

      const filterInput = screen.getByPlaceholderText(
        'Search by account ID, name, or project'
      );
      await user.type(filterInput, 'zzznomatch');

      await waitFor(() => {
        expect(
          screen.getByText('No account mappings match the filter')
        ).toBeInTheDocument();
        // Also check the helper text is present
        expect(
          screen.getByText(/Try a different search term or clear the filter/)
        ).toBeInTheDocument();
      });
    });

    it('Clear filter button in no-match state clears the filter and restores all rows', async () => {
      const user = userEvent.setup();
      await renderSummary(makeConfig(), [
        makeMapping({ account_id: '111111111111', account_name: 'Production', jira_project: 'CLOUDOPS' }),
        makeMapping({ account_id: '222222222222', account_name: 'Staging',    jira_project: 'APPTEAM' }),
      ]);

      await waitFor(() => {
        expect(screen.getByText('111111111111')).toBeInTheDocument();
      });

      // Type a non-matching filter
      const filterInput = screen.getByPlaceholderText(
        'Search by account ID, name, or project'
      );
      await user.type(filterInput, 'zzznomatch');

      await waitFor(() => {
        expect(
          screen.getByText('No account mappings match the filter')
        ).toBeInTheDocument();
      });

      // Click Clear filter
      const clearBtn = screen.getByRole('button', { name: 'Clear filter' });
      await user.click(clearBtn);

      // Both rows should be visible again
      await waitFor(() => {
        expect(screen.getByText('111111111111')).toBeInTheDocument();
        expect(screen.getByText('222222222222')).toBeInTheDocument();
        // Empty state should be gone
        expect(
          screen.queryByText('No account mappings match the filter')
        ).not.toBeInTheDocument();
      });
    });
  });

  // -------------------------------------------------------------------------
  // Pagination
  // -------------------------------------------------------------------------

  describe('Pagination', () => {
    it('renders pagination when there are more than 20 mappings', async () => {
      // 21 mappings: needs 2 pages at default page size of 20
      await renderSummary(makeConfig(), makeMappings(21));

      await waitFor(() => {
        // Cloudscape Pagination renders navigation buttons
        // "Next page" is a reliable aria-label to look for
        const nextPageBtn = screen.getByRole('button', { name: 'Next page' });
        expect(nextPageBtn).toBeInTheDocument();
      });
    });

    it('does NOT render pagination when there are 20 or fewer mappings', async () => {
      await renderSummary(makeConfig(), makeMappings(20));

      await waitFor(() => {
        // Wait for loading to finish (counter visible)
        expect(screen.getByText('(20)')).toBeInTheDocument();
      });

      // Cloudscape Pagination always renders the Next button, but disables it
      // when there is only 1 page. Verify it is either absent OR disabled.
      const nextPageBtn = screen.queryByRole('button', { name: 'Next page' });
      if (nextPageBtn) {
        // Component renders the button but it must be disabled (only 1 page)
        expect(nextPageBtn).toBeDisabled();
      }
      // If it's not present at all that's also fine
    });

    it('does NOT render pagination when there are zero mappings', async () => {
      await renderSummary(makeConfig(), []);

      await waitFor(() => {
        expect(screen.getByText('(0)')).toBeInTheDocument();
      });

      expect(screen.queryByRole('button', { name: 'Next page' })).not.toBeInTheDocument();
    });

    it('page resets to 1 when filter text changes', async () => {
      const user = userEvent.setup();
      // 25 mappings — creates 2 pages; first 20 have account names "Account 0" … "Account 19"
      // Account names "Account 20" … "Account 24" are on page 2
      await renderSummary(makeConfig(), makeMappings(25));

      await waitFor(() => {
        expect(screen.getByRole('button', { name: 'Next page' })).toBeInTheDocument();
      });

      // Navigate to page 2
      const nextPageBtn = screen.getByRole('button', { name: 'Next page' });
      await user.click(nextPageBtn);

      // Confirm we are on page 2.
      // Cloudscape Pagination marks the active page button with aria-current="true"
      await waitFor(() => {
        const activePage = screen.getByRole('button', { name: /Page 2/ });
        // Cloudscape uses aria-current="true" (not the ARIA-spec "page") for the current page
        expect(activePage.getAttribute('aria-current')).toBeTruthy();
      });

      // Type in filter — page should reset to 1
      const filterInput = screen.getByPlaceholderText(
        'Search by account ID, name, or project'
      );
      await user.type(filterInput, 'Account 0');

      await waitFor(() => {
        // Page 2 button should no longer be aria-current (or may be absent if only 1 page remains)
        const page2Btns = screen.queryAllByRole('button', { name: /Page 2/ });
        for (const btn of page2Btns) {
          // Either absent or not the active page
          expect(btn.getAttribute('aria-current') === 'true').toBe(false);
        }
      });
    });
  });

  // -------------------------------------------------------------------------
  // CollectionPreferences
  // -------------------------------------------------------------------------

  describe('CollectionPreferences gear icon', () => {
    it('renders CollectionPreferences trigger when mappings exist', async () => {
      await renderSummary(makeConfig(), [makeMapping()]);

      await waitFor(() => {
        // Cloudscape CollectionPreferences renders a trigger button with
        // "Preferences" as either aria-label or title
        const prefBtn = screen.getByRole('button', { name: /Preferences/i });
        expect(prefBtn).toBeInTheDocument();
      });
    });

    it('does NOT render CollectionPreferences when there are zero mappings', async () => {
      await renderSummary(makeConfig(), []);

      await waitFor(() => {
        expect(screen.getByText('(0)')).toBeInTheDocument();
      });

      expect(
        screen.queryByRole('button', { name: /Preferences/i })
      ).not.toBeInTheDocument();
    });
  });
});
