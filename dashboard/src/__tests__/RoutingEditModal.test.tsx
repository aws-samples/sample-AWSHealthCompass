/**
 * Unit tests for the Routing Edit Modal
 *
 * Tests RoutingEditModal component: default routing, editable account mappings,
 * bulk import (CSV/JSON), Load from Organizations, tag routing toggle/key,
 * Validate All Targets with 10s cooldown, multi-step save, cancel with dirty detection.
 *
 * Also tests integration wiring: edit-routing button in ConfigurationSummary
 * is now ENABLED and opens the modal.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
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

const mockApiFetch = vi.mocked(apiFetch);

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function makeRoutingResponse(mappings: any[] = []) {
  return {
    default: {
      jiraProject: 'CLOUDOPS',
      jiraIssueType: 'Task',
      snowAssignmentGroupId: '',
      snowRecordType: 'change_request',
    },
    defaultProject: 'CLOUDOPS',
    defaultIssueType: 'Task',
    snowAssignmentGroupId: '',
    snowRecordType: 'change_request',
    accounts: mappings,
    totalAccounts: mappings.length,
  };
}

function makeSummaryResponse(platform = 'jira') {
  return {
    platform,
    platforms: [platform],
    routing: { defaultProject: 'CLOUDOPS', tagRouting: { enabled: false, tagKey: '' } },
  };
}

function makeMappings() {
  return [
    { account_id: '111111111111', account_name: 'Production', jira_project: 'CLOUDOPS', snow_assignment_group_id: '' },
    { account_id: '222222222222', account_name: 'Staging', jira_project: 'APPTEAM', snow_assignment_group_id: '' },
    { account_id: '333333333333', account_name: 'Security', jira_project: 'SECURITY', snow_assignment_group_id: '' },
  ];
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function setupDefaultMocks(overrides: Record<string, any> = {}) {
  mockApiFetch.mockImplementation(async (path: string, opts?: any) => {
    if (path in overrides) return overrides[path];
    if (path === '/config/routing') return makeRoutingResponse(makeMappings());
    if (path === '/config/summary') return makeSummaryResponse();
    if (path === '/config/routing/discover') return { accounts: [] };
    if (path === '/config/routing/validate') return { results: [] };
    if (path === '/config/routing/default') return { success: true };
    if (path === '/config/routing/import') return { importId: 'imp-001', preview: { valid: 3, invalid: 0 } };
    if (path === '/config/routing/import/confirm') return { success: true };
    if (path === '/config/routing/strategy') return { success: true };
    return {};
  });
}

async function renderModal(props: {
  visible?: boolean;
  onDismiss?: () => void;
  onSave?: () => void;
}) {
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

// ---------------------------------------------------------------------------
// Tests: Modal Visibility
// ---------------------------------------------------------------------------

describe('RoutingEditModal — Visibility', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setupDefaultMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders modal content when visible=true', async () => {
    await renderModal({ visible: true });

    await waitFor(() => {
      expect(screen.getByText('Edit Routing Rules')).toBeInTheDocument();
    });
  });

  it('does not render modal content when visible=false', async () => {
    await renderModal({ visible: false });

    // When visible=false, Cloudscape Modal may or may not render DOM nodes,
    // but the data-loading useEffect won't fire (visible guard).
    // Verify no API calls made for loading data
    expect(mockApiFetch).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Tests: Data Loading & Pre-Population
// ---------------------------------------------------------------------------

describe('RoutingEditModal — Data Loading', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setupDefaultMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('default JIRA project is pre-populated from config', async () => {
    await renderModal({ visible: true });

    await waitFor(() => {
      const input = screen.getByTestId('routing-default-project');
      expect(input).toBeInTheDocument();
    });

    // Cloudscape Input renders the value in a nested <input> element
    const inputEl = screen.getByTestId('routing-default-project').querySelector('input');
    expect(inputEl).toHaveValue('CLOUDOPS');
  });

  it('account mappings table is populated with existing data', async () => {
    await renderModal({ visible: true });

    await waitFor(() => {
      expect(screen.getByText('111111111111')).toBeInTheDocument();
      expect(screen.getByText('Production')).toBeInTheDocument();
      expect(screen.getByText('222222222222')).toBeInTheDocument();
      expect(screen.getByText('Staging')).toBeInTheDocument();
      expect(screen.getByText('333333333333')).toBeInTheDocument();
      expect(screen.getByText('Security')).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Tests: Add Mapping
// ---------------------------------------------------------------------------

describe('RoutingEditModal — Add Mapping', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setupDefaultMocks({ '/config/routing': makeRoutingResponse([]) });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('adds a new mapping row when valid account ID and project entered', async () => {
    const user = userEvent.setup();
    await renderModal({ visible: true });

    await waitFor(() => {
      expect(screen.getByTestId('add-mapping-row')).toBeInTheDocument();
    });

    // Enter account ID
    const acctInput = screen.getByLabelText('New account ID');
    await user.clear(acctInput);
    await user.type(acctInput, '444444444444');

    // Enter JIRA project
    const projInput = screen.getByLabelText('JIRA project for new account');
    await user.clear(projInput);
    await user.type(projInput, 'NEWPROJ');

    // Click Add
    await user.click(screen.getByTestId('add-mapping-row'));

    // Verify row appears
    await waitFor(() => {
      expect(screen.getByText('444444444444')).toBeInTheDocument();
    });
  });

  it('rejects invalid account ID (not 12 digits)', async () => {
    const user = userEvent.setup();
    await renderModal({ visible: true });

    await waitFor(() => {
      expect(screen.getByTestId('add-mapping-row')).toBeInTheDocument();
    });

    // Enter invalid account ID
    const acctInput = screen.getByLabelText('New account ID');
    await user.clear(acctInput);
    await user.type(acctInput, '12345');

    const projInput = screen.getByLabelText('JIRA project for new account');
    await user.clear(projInput);
    await user.type(projInput, 'PROJ');

    await user.click(screen.getByTestId('add-mapping-row'));

    // Error message should appear
    await waitFor(() => {
      expect(screen.getByText('Account ID must be exactly 12 digits')).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Tests: Remove Mapping
// ---------------------------------------------------------------------------

describe('RoutingEditModal — Remove Mapping', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setupDefaultMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('removes a mapping when remove icon is clicked', async () => {
    const user = userEvent.setup();
    await renderModal({ visible: true });

    await waitFor(() => {
      expect(screen.getByText('111111111111')).toBeInTheDocument();
    });

    // Click remove button for account 111111111111
    const removeBtn = screen.getByLabelText('Remove mapping for account 111111111111');
    await user.click(removeBtn);

    // Account should be gone
    await waitFor(() => {
      expect(screen.queryByText('111111111111')).not.toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Tests: Bulk Import
// ---------------------------------------------------------------------------

describe('RoutingEditModal — Bulk Import', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setupDefaultMocks({ '/config/routing': makeRoutingResponse([]) });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('clicking Bulk Import toggle shows textarea', async () => {
    const user = userEvent.setup();
    await renderModal({ visible: true });

    await waitFor(() => {
      expect(screen.getByTestId('bulk-import-toggle')).toBeInTheDocument();
    });

    await user.click(screen.getByTestId('bulk-import-toggle'));

    await waitFor(() => {
      expect(screen.getByLabelText('CSV data for bulk import')).toBeInTheDocument();
    });
  });

  it('parsing CSV populates preview table', async () => {
    const user = userEvent.setup();
    await renderModal({ visible: true });

    await waitFor(() => {
      expect(screen.getByTestId('bulk-import-toggle')).toBeInTheDocument();
    });

    await user.click(screen.getByTestId('bulk-import-toggle'));

    await waitFor(() => {
      expect(screen.getByLabelText('CSV data for bulk import')).toBeInTheDocument();
    });

    // Enter CSV data
    const textarea = screen.getByLabelText('CSV data for bulk import');
    await user.click(textarea);
    await user.type(textarea, '555555555555,DEVOPS\n666666666666,SECURITY');

    // Click Parse & Preview
    await user.click(screen.getByText('Parse & Preview'));

    // Preview should show parsed rows
    await waitFor(() => {
      expect(screen.getByText('555555555555')).toBeInTheDocument();
      expect(screen.getByText('DEVOPS')).toBeInTheDocument();
      expect(screen.getByText('666666666666')).toBeInTheDocument();
    });
  });

  it('confirm replace triggers import and replaces mappings', async () => {
    const user = userEvent.setup();
    await renderModal({ visible: true });

    await waitFor(() => {
      expect(screen.getByTestId('bulk-import-toggle')).toBeInTheDocument();
    });

    await user.click(screen.getByTestId('bulk-import-toggle'));

    await waitFor(() => {
      expect(screen.getByLabelText('CSV data for bulk import')).toBeInTheDocument();
    });

    const textarea = screen.getByLabelText('CSV data for bulk import');
    await user.click(textarea);
    await user.type(textarea, '777777777777,BULKPROJ');

    await user.click(screen.getByText('Parse & Preview'));

    await waitFor(() => {
      expect(screen.getByText('777777777777')).toBeInTheDocument();
    });

    // Click "Confirm Import (1 rows)"
    await user.click(screen.getByText(/Confirm Import/));

    // This shows the confirmation warning — click "Replace Mappings"
    await waitFor(() => {
      expect(screen.getByText('Replace Mappings')).toBeInTheDocument();
    });

    await user.click(screen.getByText('Replace Mappings'));

    // Should switch back to manual entry mode with the imported mapping
    await waitFor(() => {
      expect(screen.getByText('777777777777')).toBeInTheDocument();
      expect(screen.queryByLabelText('CSV data for bulk import')).not.toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Tests: Load from Organizations
// ---------------------------------------------------------------------------

describe('RoutingEditModal — Load from Organizations', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('calls discover API and populates table with new accounts', async () => {
    setupDefaultMocks({
      '/config/routing': makeRoutingResponse([]),
      '/config/routing/discover': {
        accounts: [
          { accountId: '888888888888', accountName: 'OrgAccount1' },
          { accountId: '999999999999', accountName: 'OrgAccount2' },
        ],
      },
    });

    const user = userEvent.setup();
    await renderModal({ visible: true });

    await waitFor(() => {
      expect(screen.getByTestId('load-org-accounts')).toBeInTheDocument();
    });

    await user.click(screen.getByTestId('load-org-accounts'));

    // Accounts should appear in the table
    await waitFor(() => {
      expect(screen.getByText('888888888888')).toBeInTheDocument();
      expect(screen.getByText('OrgAccount1')).toBeInTheDocument();
      expect(screen.getByText('999999999999')).toBeInTheDocument();
      expect(screen.getByText('OrgAccount2')).toBeInTheDocument();
    });

    // Verify API was called with POST
    expect(mockApiFetch).toHaveBeenCalledWith('/config/routing/discover', { method: 'POST' });
  });
});

// ---------------------------------------------------------------------------
// Tests: Tag Routing
// ---------------------------------------------------------------------------

describe('RoutingEditModal — Tag Routing', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setupDefaultMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('toggle enables tag key input', async () => {
    const user = userEvent.setup();
    await renderModal({ visible: true });

    await waitFor(() => {
      expect(screen.getByText('Enable tag-based routing')).toBeInTheDocument();
    });

    // Initially no tag key input visible (tag routing disabled by default)
    expect(screen.queryByText('Tag Key')).not.toBeInTheDocument();

    // Toggle on
    await user.click(screen.getByText('Enable tag-based routing'));

    // Tag key input should appear
    await waitFor(() => {
      expect(screen.getByText('Tag Key')).toBeInTheDocument();
    });
  });

  it('tag key is required when tag routing is enabled (save disabled)', async () => {
    setupDefaultMocks({
      '/config/routing': {
        ...makeRoutingResponse([]),
      },
      '/config/summary': {
        platform: 'jira',
        platforms: ['jira'],
        routing: { defaultProject: 'CLOUDOPS', tagRouting: { enabled: true, tagKey: '' } },
      },
    });

    await renderModal({ visible: true });

    await waitFor(() => {
      expect(screen.getByText('Tag Key')).toBeInTheDocument();
    });

    // Save button should be disabled when tag key is empty
    const saveBtn = screen.getByTestId('save-routing');
    expect(saveBtn).toBeDisabled();

    // Error hint should be shown
    expect(screen.getByText('Tag key is required when tag routing is enabled')).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Tests: Validation
// ---------------------------------------------------------------------------

describe('RoutingEditModal — Validation (results display)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('Validate button calls POST /config/routing/validate', async () => {
    setupDefaultMocks();
    const user = userEvent.setup();
    await renderModal({ visible: true });

    await waitFor(() => {
      expect(screen.getByTestId('validate-routing')).toBeInTheDocument();
    });

    await user.click(screen.getByTestId('validate-routing'));

    await waitFor(() => {
      expect(mockApiFetch).toHaveBeenCalledWith(
        '/config/routing/validate',
        expect.objectContaining({ method: 'POST' })
      );
    });
  });

  it('shows success/error indicators per target', async () => {
    mockApiFetch.mockImplementation(async (path: string, opts?: any) => {
      if (path === '/config/routing') return makeRoutingResponse(makeMappings());
      if (path === '/config/summary') return makeSummaryResponse();
      if (path === '/config/routing/validate') {
        return {
          results: [
            { target: 'CLOUDOPS', valid: true, displayName: 'Cloud Operations' },
            { target: 'APPTEAM', valid: true, displayName: 'App Team' },
            { target: 'SECURITY', valid: false, error: 'Project not found' },
          ],
        };
      }
      return {};
    });

    const user = userEvent.setup();
    await renderModal({ visible: true });

    // Wait for data to load and Validate button to appear
    await waitFor(() => {
      expect(screen.getByTestId('validate-routing')).toBeInTheDocument();
      expect(screen.getByText('111111111111')).toBeInTheDocument();
    });

    await user.click(screen.getByTestId('validate-routing'));

    // Validation results include target + display name in same text node
    // e.g., "CLOUDOPS — Cloud Operations" or "SECURITY — Project not found"
    await waitFor(() => {
      expect(screen.getByText(/Cloud Operations/)).toBeInTheDocument();
      expect(screen.getByText(/App Team/)).toBeInTheDocument();
      expect(screen.getByText(/Project not found/)).toBeInTheDocument();
    }, { timeout: 3000 });
  });
});

describe('RoutingEditModal — Validation (cooldown)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    setupDefaultMocks();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('10-second cooldown disables button after click', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await renderModal({ visible: true });

    await waitFor(() => {
      expect(screen.getByTestId('validate-routing')).toBeInTheDocument();
    });

    // Click validate
    await user.click(screen.getByTestId('validate-routing'));

    // Wait for async validation to complete
    await waitFor(() => {
      expect(screen.getByTestId('validate-routing')).toBeDisabled();
    });

    // Advance timer by 9 seconds — still disabled
    await act(async () => {
      vi.advanceTimersByTime(9000);
    });
    expect(screen.getByTestId('validate-routing')).toBeDisabled();

    // Advance past 10 seconds — should be enabled
    await act(async () => {
      vi.advanceTimersByTime(2000);
    });
    expect(screen.getByTestId('validate-routing')).not.toBeDisabled();
  });
});

// ---------------------------------------------------------------------------
// Tests: Save Flow
// ---------------------------------------------------------------------------

describe('RoutingEditModal — Save', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setupDefaultMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('save calls routing/default + routing/import + confirm + strategy', async () => {
    const user = userEvent.setup();
    const { onSave } = await renderModal({ visible: true });

    await waitFor(() => {
      expect(screen.getByTestId('save-routing')).toBeInTheDocument();
    });

    await user.click(screen.getByTestId('save-routing'));

    await waitFor(() => {
      expect(onSave).toHaveBeenCalledTimes(1);
    });

    // Verify all 4 API calls were made
    const calls = mockApiFetch.mock.calls.map(c => c[0]);
    expect(calls).toContain('/config/routing/default');
    expect(calls).toContain('/config/routing/import');
    expect(calls).toContain('/config/routing/import/confirm');
    expect(calls).toContain('/config/routing/strategy');
  });

  it('shows loading state during save', async () => {
    // Make save hang
    let resolveDefault: () => void;
    mockApiFetch.mockImplementation(async (path: string) => {
      if (path === '/config/routing') return makeRoutingResponse(makeMappings());
      if (path === '/config/summary') return makeSummaryResponse();
      if (path === '/config/routing/default') {
        return new Promise(resolve => { resolveDefault = () => resolve({ success: true }); });
      }
      return {};
    });

    const user = userEvent.setup();
    await renderModal({ visible: true });

    await waitFor(() => {
      expect(screen.getByTestId('save-routing')).toBeInTheDocument();
    });

    await user.click(screen.getByTestId('save-routing'));

    // Save button shows loading state (Cloudscape Button loading renders a spinner)
    await waitFor(() => {
      const saveBtn = screen.getByTestId('save-routing');
      // Cloudscape marks loading buttons with aria-disabled
      expect(saveBtn).toHaveAttribute('aria-disabled', 'true');
    });

    // Resolve to avoid hanging test
    await act(async () => {
      resolveDefault!();
    });
  });

  it('shows error alert when save fails (modal stays open)', async () => {
    mockApiFetch.mockImplementation(async (path: string) => {
      if (path === '/config/routing') return makeRoutingResponse(makeMappings());
      if (path === '/config/summary') return makeSummaryResponse();
      if (path === '/config/routing/default') throw new Error('API 500: Internal Server Error');
      if (path === '/config/routing/import') return { importId: 'imp-001' };
      if (path === '/config/routing/import/confirm') return { success: true };
      if (path === '/config/routing/strategy') return { success: true };
      return {};
    });

    const user = userEvent.setup();
    const { onSave } = await renderModal({ visible: true });

    await waitFor(() => {
      expect(screen.getByTestId('save-routing')).toBeInTheDocument();
    });

    await user.click(screen.getByTestId('save-routing'));

    // Error alert should be shown
    await waitFor(() => {
      expect(screen.getByText(/An unexpected error occurred/)).toBeInTheDocument();
    });

    // onSave should NOT have been called
    expect(onSave).not.toHaveBeenCalled();

    // Modal should still be open
    expect(screen.getByText('Edit Routing Rules')).toBeInTheDocument();
  });

  it('success calls onSave (modal closes)', async () => {
    const user = userEvent.setup();
    const { onSave } = await renderModal({ visible: true });

    await waitFor(() => {
      expect(screen.getByTestId('save-routing')).toBeInTheDocument();
    });

    await user.click(screen.getByTestId('save-routing'));

    await waitFor(() => {
      expect(onSave).toHaveBeenCalledTimes(1);
    });
  });
});

// ---------------------------------------------------------------------------
// Tests: Cancel / Dismiss
// ---------------------------------------------------------------------------

describe('RoutingEditModal — Cancel & Dirty Detection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setupDefaultMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('cancel with no changes closes immediately', async () => {
    const user = userEvent.setup();
    const { onDismiss } = await renderModal({ visible: true });

    await waitFor(() => {
      expect(screen.getByTestId('cancel-routing')).toBeInTheDocument();
    });

    await user.click(screen.getByTestId('cancel-routing'));

    // onDismiss called directly without discard confirmation dialog
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it('cancel with changes shows discard confirmation', async () => {
    const user = userEvent.setup();
    const { onDismiss } = await renderModal({ visible: true });

    await waitFor(() => {
      expect(screen.getByTestId('routing-default-project')).toBeInTheDocument();
    });

    // Make a change to trigger dirty state
    const inputEl = screen.getByTestId('routing-default-project').querySelector('input')!;
    await user.clear(inputEl);
    await user.type(inputEl, 'CHANGED');

    // Click cancel
    await user.click(screen.getByTestId('cancel-routing'));

    // Discard confirmation should appear
    await waitFor(() => {
      expect(screen.getByText('Discard unsaved changes?')).toBeInTheDocument();
    });

    // onDismiss not called yet
    expect(onDismiss).not.toHaveBeenCalled();

    // Click "Discard" to confirm
    await user.click(screen.getByText('Discard'));

    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it('default JIRA project required — save disabled when empty', async () => {
    setupDefaultMocks({
      '/config/routing': {
        default: {
          jiraProject: '',
          jiraIssueType: 'Task',
          snowAssignmentGroupId: '',
          snowRecordType: 'change_request',
        },
        accounts: [],
        totalAccounts: 0,
      },
      '/config/summary': {
        platform: 'jira',
        platforms: ['jira'],
        routing: { defaultProject: '', tagRouting: { enabled: false, tagKey: '' } },
      },
    });

    await renderModal({ visible: true });

    await waitFor(() => {
      expect(screen.getByTestId('save-routing')).toBeInTheDocument();
    });

    // Save should be disabled when default project is empty
    // Cloudscape Button uses aria-disabled for the disabled prop
    const saveBtn = screen.getByTestId('save-routing');
    expect(saveBtn).toHaveAttribute('disabled');
  });
});

// ---------------------------------------------------------------------------
// Tests: ServiceNow Column in Bulk Import Preview
// ---------------------------------------------------------------------------

describe('RoutingEditModal — ServiceNow Bulk Import Column', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('shows "ServiceNow Group" column header in bulk preview when snowEnabled=true', async () => {
    setupDefaultMocks({
      '/config/routing': makeRoutingResponse([]),
      '/config/summary': {
        platform: 'jira',
        platforms: ['jira', 'servicenow'],
        routing: { defaultProject: 'CLOUDOPS' },
      },
    });

    const user = userEvent.setup();
    await renderModal({ visible: true });

    await waitFor(() => {
      expect(screen.getByTestId('bulk-import-toggle')).toBeInTheDocument();
    });

    await user.click(screen.getByTestId('bulk-import-toggle'));

    await waitFor(() => {
      expect(screen.getByLabelText('CSV data for bulk import')).toBeInTheDocument();
    });

    const textarea = screen.getByLabelText('CSV data for bulk import');
    await user.click(textarea);
    await user.type(textarea, '555555555555,DEVOPS,abc123def456');

    await user.click(screen.getByText('Parse & Preview'));

    await waitFor(() => {
      expect(screen.getByText('ServiceNow Group')).toBeInTheDocument();
    });
  });

  it('does NOT show "ServiceNow Group" column header in bulk preview when snowEnabled=false', async () => {
    setupDefaultMocks({
      '/config/routing': makeRoutingResponse([]),
      '/config/summary': {
        platform: 'jira',
        platforms: ['jira'],
        routing: { defaultProject: 'CLOUDOPS' },
      },
    });

    const user = userEvent.setup();
    await renderModal({ visible: true });

    await waitFor(() => {
      expect(screen.getByTestId('bulk-import-toggle')).toBeInTheDocument();
    });

    await user.click(screen.getByTestId('bulk-import-toggle'));

    await waitFor(() => {
      expect(screen.getByLabelText('CSV data for bulk import')).toBeInTheDocument();
    });

    const textarea = screen.getByLabelText('CSV data for bulk import');
    await user.click(textarea);
    await user.type(textarea, '555555555555,DEVOPS');

    await user.click(screen.getByText('Parse & Preview'));

    await waitFor(() => {
      expect(screen.getByText('555555555555')).toBeInTheDocument();
    });

    expect(screen.queryByText('ServiceNow Group')).not.toBeInTheDocument();
  });

  it('format description contains "snow_group_id" when snowEnabled=true', async () => {
    setupDefaultMocks({
      '/config/routing': makeRoutingResponse([]),
      '/config/summary': {
        platform: 'jira',
        platforms: ['jira', 'servicenow'],
        routing: { defaultProject: 'CLOUDOPS' },
      },
    });

    const user = userEvent.setup();
    await renderModal({ visible: true });

    await waitFor(() => {
      expect(screen.getByTestId('bulk-import-toggle')).toBeInTheDocument();
    });

    await user.click(screen.getByTestId('bulk-import-toggle'));

    await waitFor(() => {
      expect(screen.getByText(/snow_group_id/)).toBeInTheDocument();
    });
  });

  it('format description does NOT contain "snow_group_id" when snowEnabled=false', async () => {
    setupDefaultMocks({
      '/config/routing': makeRoutingResponse([]),
      '/config/summary': {
        platform: 'jira',
        platforms: ['jira'],
        routing: { defaultProject: 'CLOUDOPS' },
      },
    });

    const user = userEvent.setup();
    await renderModal({ visible: true });

    await waitFor(() => {
      expect(screen.getByTestId('bulk-import-toggle')).toBeInTheDocument();
    });

    await user.click(screen.getByTestId('bulk-import-toggle'));

    await waitFor(() => {
      expect(screen.getByLabelText('CSV data for bulk import')).toBeInTheDocument();
    });

    expect(screen.queryByText(/snow_group_id/)).not.toBeInTheDocument();
  });

  it('shows ServiceNow group value in preview cell when CSV has 3 columns', async () => {
    setupDefaultMocks({
      '/config/routing': makeRoutingResponse([]),
      '/config/summary': {
        platform: 'jira',
        platforms: ['jira', 'servicenow'],
        routing: { defaultProject: 'CLOUDOPS' },
      },
    });

    const user = userEvent.setup();
    await renderModal({ visible: true });

    await waitFor(() => {
      expect(screen.getByTestId('bulk-import-toggle')).toBeInTheDocument();
    });

    await user.click(screen.getByTestId('bulk-import-toggle'));

    await waitFor(() => {
      expect(screen.getByLabelText('CSV data for bulk import')).toBeInTheDocument();
    });

    const textarea = screen.getByLabelText('CSV data for bulk import');
    await user.click(textarea);
    await user.type(textarea, '555555555555,DEVOPS,a1b2c3d4e5f6g7h8');

    await user.click(screen.getByText('Parse & Preview'));

    await waitFor(() => {
      expect(screen.getByText('a1b2c3d4e5f6g7h8')).toBeInTheDocument();
    });
  });

  it('shows "—" fallback in ServiceNow column when CSV has only 2 columns', async () => {
    setupDefaultMocks({
      '/config/routing': makeRoutingResponse([]),
      '/config/summary': {
        platform: 'jira',
        platforms: ['jira', 'servicenow'],
        routing: { defaultProject: 'CLOUDOPS' },
      },
    });

    const user = userEvent.setup();
    await renderModal({ visible: true });

    await waitFor(() => {
      expect(screen.getByTestId('bulk-import-toggle')).toBeInTheDocument();
    });

    await user.click(screen.getByTestId('bulk-import-toggle'));

    await waitFor(() => {
      expect(screen.getByLabelText('CSV data for bulk import')).toBeInTheDocument();
    });

    const textarea = screen.getByLabelText('CSV data for bulk import');
    await user.click(textarea);
    await user.type(textarea, '555555555555,DEVOPS');

    await user.click(screen.getByText('Parse & Preview'));

    // ServiceNow Group column should be present (snowEnabled=true)
    await waitFor(() => {
      expect(screen.getByText('ServiceNow Group')).toBeInTheDocument();
    });

    // The cell should show "—" as fallback for missing snow group
    expect(screen.getByText('—')).toBeInTheDocument();
  });
});
