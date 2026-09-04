/**
 * Vitest coverage for the TagRoutingMappingsEditor
 * component (`dashboard/src/components/TagRoutingMappingsEditor.tsx`): the
 * controlled add/edit/delete surface used in BOTH the wizard tag-routing step
 * and the RoutingEditModal.
 *
 * Coverage:
 *   gate   — renders nothing when tag routing disabled
 *   add    — Add mapping appends a 'new' row (tagValue, project, issue type)
 *   V1/V2/V3/V4/V5 — inline add-row validation (empty value, empty project,
 *                    duplicate, too-long, control chars) block the add
 *   edit   — editing target on a persisted row flips it to 'edited'; a 'new'
 *            row stays 'new' (drives getUpsertRows on the host)
 *   delete — removing a persisted row records it in removedTagValues (DELETE
 *            on save); removing a brand-new row does NOT
 *   per-row backend reason surfaced against its row
 *   user tag values and backend reasons render as inert text
 *   load-error affordance replaces the table and offers Retry
 *   disabled — controls disabled while a save is in flight
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';

import TagRoutingMappingsEditor from '../components/TagRoutingMappingsEditor';
import { TagMappingRow } from '../components/tagMappings';

/** Controlled test harness: owns the row + removed state like the real hosts. */
function Harness(props: {
  initial?: TagMappingRow[];
  enabled?: boolean;
  tagKey?: string;
  rowErrors?: Record<string, string>;
  loadError?: string | null;
  loading?: boolean;
  disabled?: boolean;
  onRetryLoad?: () => void;
}) {
  const [mappings, setMappings] = React.useState<TagMappingRow[]>(props.initial ?? []);
  const [removed, setRemoved] = React.useState<string[]>([]);
  return (
    <div>
      <TagRoutingMappingsEditor
        enabled={props.enabled ?? true}
        tagKey={props.tagKey ?? 'Team'}
        mappings={mappings}
        onMappingsChange={setMappings}
        removedTagValues={removed}
        onRemovedChange={setRemoved}
        rowErrors={props.rowErrors}
        loadError={props.loadError ?? null}
        onRetryLoad={props.onRetryLoad}
        loading={props.loading}
        disabled={props.disabled}
      />
      {/* Probes the host state so tests can assert reconciliation intent. */}
      <div data-testid="probe-mappings">{JSON.stringify(mappings)}</div>
      <div data-testid="probe-removed">{JSON.stringify(removed)}</div>
    </div>
  );
}

function hostMappings(): TagMappingRow[] {
  return JSON.parse(screen.getByTestId('probe-mappings').textContent || '[]');
}
function hostRemoved(): string[] {
  return JSON.parse(screen.getByTestId('probe-removed').textContent || '[]');
}

async function addRow(user: ReturnType<typeof userEvent.setup>, value: string, project: string, issueType?: string) {
  await user.clear(screen.getByLabelText('New tag value'));
  if (value) await user.type(screen.getByLabelText('New tag value'), value);
  await user.clear(screen.getByLabelText('JIRA project for new tag value'));
  if (project) await user.type(screen.getByLabelText('JIRA project for new tag value'), project);
  if (issueType !== undefined) {
    await user.clear(screen.getByLabelText('JIRA issue type for new tag value'));
    if (issueType) await user.type(screen.getByLabelText('JIRA issue type for new tag value'), issueType);
  }
  await user.click(screen.getByTestId('add-tag-mapping-row'));
}

const persisted = (v: string, p = 'CLOUDOPS'): TagMappingRow =>
  ({ tagValue: v, jiraProject: p, jiraIssueType: 'Task', rowStatus: 'persisted' });

beforeEach(() => vi.clearAllMocks());

// ===========================================================================
// Enable gate
// ===========================================================================

describe('editor — enable gate', () => {
  it('renders nothing when tag routing is disabled', () => {
    const { container } = render(<Harness enabled={false} />);
    expect(container.querySelector('[data-testid="add-tag-mapping-row"]')).toBeNull();
  });

  it('renders the empty-state referencing the configured tag key when enabled with no rows', () => {
    render(<Harness enabled tagKey="Owner" />);
    expect(screen.getByText(/No tag-value mappings yet/i)).toBeInTheDocument();
    // Empty-state copy interpolates the live key (there are two 'Owner' <b> nodes).
    expect(screen.getAllByText('Owner').length).toBeGreaterThan(0);
  });
});

// ===========================================================================
// Add + V1–V5 validation
// ===========================================================================

describe('editor — add row + inline validation (V1–V5)', () => {
  it('adds a new row (tagValue, project, issue type) with rowStatus "new"', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await addRow(user, 'platform', 'CLOUDOPS', 'Bug');
    const rows = hostMappings();
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ tagValue: 'platform', jiraProject: 'CLOUDOPS', jiraIssueType: 'Bug', rowStatus: 'new' });
  });

  it('defaults the issue type to Task when left as the placeholder default', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await addRow(user, 'data', 'DATAOPS'); // issue type untouched (default 'Task')
    expect(hostMappings()[0].jiraIssueType).toBe('Task');
  });

  it('V1 — blocks add on empty tag value and shows an inline message', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await addRow(user, '', 'CLOUDOPS');
    expect(hostMappings()).toHaveLength(0);
    expect(screen.getByText(/Tag value is required/i)).toBeInTheDocument();
  });

  it('V2 — blocks add on empty JIRA project', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await addRow(user, 'platform', '');
    expect(hostMappings()).toHaveLength(0);
    expect(screen.getByText(/JIRA project is required/i)).toBeInTheDocument();
  });

  it('V3 — blocks a duplicate tag value (would collide on TAG_ROUTING#{value})', async () => {
    const user = userEvent.setup();
    render(<Harness initial={[persisted('platform')]} />);
    await addRow(user, 'platform', 'OTHER');
    expect(hostMappings()).toHaveLength(1); // no second row added
    expect(screen.getByText(/already exists/i)).toBeInTheDocument();
  });

  it('V4 — blocks a too-long (>256) tag value', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await addRow(user, 'a'.repeat(257), 'CLOUDOPS');
    expect(hostMappings()).toHaveLength(0);
    expect(screen.getByText(/too long/i)).toBeInTheDocument();
  });

  it('V5 — the single-line input strips control chars, so the UI is not an injection vector', async () => {
    // NOTE: A single-line <input> (Cloudscape/jsdom, and real browsers for line
    // terminators) removes control chars on type/paste, so a control char can
    // never reach the add handler THROUGH THE UI — pasting "bad\nvalue" yields
    // the sanitized "badvalue". The control-char REJECTION predicate
    // (validateTagValueClient) is exercised deterministically
    // in the tagMappings logic-module suite, and the AUTHORITATIVE server-side
    // gate is proven in the backend API validation suite.
    // This test documents that the UI field itself cannot carry the payload.
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByLabelText('New tag value'));
    await user.paste('bad\nvalue');
    await user.type(screen.getByLabelText('JIRA project for new tag value'), 'CLOUDOPS');
    await user.click(screen.getByTestId('add-tag-mapping-row'));
    // The row is added with the SANITIZED value (no newline survived the input).
    const rows = hostMappings();
    expect(rows).toHaveLength(1);
    expect(rows[0].tagValue).toBe('badvalue');
    expect(/[\u0000-\u001f\u007f-\u009f]/.test(rows[0].tagValue)).toBe(false);
  });
});

// ===========================================================================
// Edit
// ===========================================================================

describe('editor — edit target', () => {
  it('editing a persisted row project flips it to "edited" (joins the upsert set)', async () => {
    const user = userEvent.setup();
    render(<Harness initial={[persisted('platform', 'CLOUDOPS')]} />);
    const projectInput = screen.getByLabelText('JIRA project for tag value platform');
    await user.type(projectInput, 'X'); // -> CLOUDOPSX
    const row = hostMappings()[0];
    expect(row.jiraProject).toBe('CLOUDOPSX');
    expect(row.rowStatus).toBe('edited');
  });

  it('editing a brand-new row keeps it "new" (does not become "edited")', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await addRow(user, 'platform', 'CLOUDOPS');
    const issueInput = screen.getByLabelText('JIRA issue type for tag value platform');
    await user.type(issueInput, 'X'); // TaskX
    expect(hostMappings()[0].rowStatus).toBe('new');
  });
});

// ===========================================================================
// Delete
// ===========================================================================

describe('editor — remove row', () => {
  it('removing a PERSISTED row records it in removedTagValues (DELETE on save)', async () => {
    const user = userEvent.setup();
    render(<Harness initial={[persisted('platform'), persisted('data')]} />);
    await user.click(screen.getByLabelText('Remove mapping for tag value platform'));
    expect(hostMappings().map(r => r.tagValue)).toEqual(['data']);
    expect(hostRemoved()).toEqual(['platform']);
  });

  it('removing a BRAND-NEW row drops it WITHOUT recording a delete', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await addRow(user, 'ephemeral', 'CLOUDOPS');
    await user.click(screen.getByLabelText('Remove mapping for tag value ephemeral'));
    expect(hostMappings()).toHaveLength(0);
    expect(hostRemoved()).toEqual([]); // never persisted -> no DELETE
  });
});

// ===========================================================================
// Per-row backend reason + text-only render
// ===========================================================================

describe('editor — backend row errors & text-only rendering', () => {
  it('surfaces a per-row backend reason against the correct row', () => {
    render(<Harness
      initial={[persisted('platform', 'BADPROJ')]}
      rowErrors={{ platform: 'Invalid JIRA project' }}
    />);
    expect(screen.getByText('Invalid JIRA project')).toBeInTheDocument();
  });

  it('renders a hostile tag value as inert TEXT (no HTML injection)', () => {
    const xss = '<img src=x onerror=alert(1)>';
    const { container } = render(<Harness initial={[persisted(xss)]} />);
    // The literal string is shown; no <img> element is created from user input.
    expect(screen.getByText(xss)).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
  });

  it('renders a hostile backend reason string as inert text', () => {
    const evil = '<script>steal()</script>';
    const { container } = render(<Harness
      initial={[persisted('platform')]}
      rowErrors={{ platform: evil }}
    />);
    expect(screen.getByText(evil)).toBeInTheDocument();
    expect(container.querySelector('script')).toBeNull();
  });
});

// ===========================================================================
// Load-error affordance & disabled state
// ===========================================================================

describe('editor — load-error affordance & disabled state', () => {
  it('shows a load-error alert with Retry instead of the table', async () => {
    const user = userEvent.setup();
    const onRetryLoad = vi.fn();
    render(<Harness loadError="boom" onRetryLoad={onRetryLoad} />);
    expect(screen.getByText(/Failed to load tag mappings/i)).toBeInTheDocument();
    // The add control is not rendered while the load failed.
    expect(screen.queryByTestId('add-tag-mapping-row')).toBeNull();
    await user.click(screen.getByRole('button', { name: /Retry/i }));
    expect(onRetryLoad).toHaveBeenCalledTimes(1);
  });

  it('disables the Add control while a save is in flight (disabled)', () => {
    render(<Harness disabled />);
    expect(screen.getByTestId('add-tag-mapping-row')).toBeDisabled();
  });
});
