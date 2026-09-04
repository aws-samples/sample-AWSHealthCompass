/**
 * STORY-140 (RT-10) — ServiceNow tag-routing target persistence: FRONTEND.
 *
 * Owner: Moody (QA). Implementation-flow Step 10. Branch: bugfix/manual-testing.
 *
 * Implements the Dumbledore §9 frontend test set (T18-T23) plus the Snape
 * MUST-140-3 / SR-139-1/2/3 hostile-input inert-text case (STC-9-style) for the
 * tag-routing editor, plus the AC-140.6 JIRA no-regression leg (T22).
 *
 * Consumes the already-landed component under test
 * (`components/TagRoutingMappingsEditor.tsx` + `components/tagMappings.ts`) with
 * the API MOCKED. `snowEnabled`/`jiraEnabled` are the platform gating props the
 * host derives from `resolvePlatformContext(config.platforms)` (STORY-136/139).
 *
 * KNOWN CONSTRAINT: headless Playwright cannot run in this sandbox. Per project
 * norm (STORY-131/132/133/139), UI verification is (a) these deterministic
 * component/unit tests against mocked contracts, and (b) a served-source string
 * audit (T23) reading the actual component source at test time.
 *
 * Authorities asserted against:
 *   - 01_hermione_story.md AC-140.1..6
 *   - 03_dumbledore_design.md §5 (editor), §9 T18-T23
 *   - 04_snape_security.md MUST-140-3, SR-139-1/2/3 (inert text)
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

// persistTagMappings hits ../api — mock it so we exercise the platform-aware
// body construction without a network call (T19/T22 shape checks).
vi.mock('../api', () => ({ apiFetch: vi.fn() }));

import { apiFetch } from '../api';
import TagRoutingMappingsEditor, { snowErrorMessage } from '../components/TagRoutingMappingsEditor';
import {
  TagMappingRow,
  persistTagMappings,
  hasTagMappingClientErrors,
} from '../components/tagMappings';

const mockApiFetch = vi.mocked(apiFetch);

// ---------------------------------------------------------------------------
// Controlled harness — owns row + removed state like the real hosts, and lets
// tests pass the STORY-140 platform gating props.
// ---------------------------------------------------------------------------
function Harness(props: {
  initial?: TagMappingRow[];
  enabled?: boolean;
  tagKey?: string;
  snowEnabled?: boolean;
  jiraEnabled?: boolean;
  rowErrors?: Record<string, string>;
  sectionError?: string | null;
  disabled?: boolean;
}) {
  const [mappings, setMappings] = React.useState<TagMappingRow[]>(props.initial ?? []);
  const [removed, setRemoved] = React.useState<string[]>([]);
  return (
    <div>
      <TagRoutingMappingsEditor
        enabled={props.enabled ?? true}
        tagKey={props.tagKey ?? 'Team'}
        snowEnabled={props.snowEnabled}
        jiraEnabled={props.jiraEnabled}
        mappings={mappings}
        onMappingsChange={setMappings}
        removedTagValues={removed}
        onRemovedChange={setRemoved}
        rowErrors={props.rowErrors}
        sectionError={props.sectionError ?? null}
        disabled={props.disabled}
      />
      <div data-testid="probe-mappings">{JSON.stringify(mappings)}</div>
      <div data-testid="probe-removed">{JSON.stringify(removed)}</div>
    </div>
  );
}

function hostMappings(): TagMappingRow[] {
  return JSON.parse(screen.getByTestId('probe-mappings').textContent || '[]');
}

/** SNOW-only persisted row helper. */
const snowRow = (v: string, id = 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6'): TagMappingRow => ({
  tagValue: v,
  jiraProject: '',
  jiraIssueType: 'Task',
  snowAssignmentGroupId: id,
  snowRecordType: 'change_request',
  rowStatus: 'persisted',
});

/** JIRA-only persisted row helper. */
const jiraRow = (v: string, p = 'CLOUDOPS'): TagMappingRow => ({
  tagValue: v,
  jiraProject: p,
  jiraIssueType: 'Task',
  rowStatus: 'persisted',
});

beforeEach(() => vi.clearAllMocks());

// ===========================================================================
// T18 (AC-140.3) — SNOW-only editor columns
// ===========================================================================

describe('STORY-140 T18 — SNOW-only editor columns (AC-140.3)', () => {
  it('renders "ServiceNow Group" + "Record Type" columns; JIRA columns absent', () => {
    render(<Harness snowEnabled jiraEnabled={false} initial={[snowRow('platform')]} />);
    // Assert against the rendered column headers (robust to Cloudscape/jsdom
    // splitting header text across nodes — getByText can miss a <th> whose
    // text is composed of multiple spans).
    const headers = screen.getAllByRole('columnheader').map(h => h.textContent);
    expect(headers).toContain('ServiceNow Group');
    expect(headers).toContain('Record Type');
    // JIRA columns absent.
    expect(headers).not.toContain('JIRA project');
    expect(headers).not.toContain('Issue type');
    // Add-row: SNOW inputs present, JIRA inputs absent.
    expect(screen.getByLabelText('ServiceNow group for new tag value')).toBeInTheDocument();
    // Cloudscape Select renders more than one node carrying the ariaLabel
    // (trigger + internal label), so assert presence via getAllBy.
    expect(screen.getAllByLabelText('Record type for new tag value').length).toBeGreaterThan(0);
    expect(screen.queryByLabelText('JIRA project for new tag value')).toBeNull();
    expect(screen.queryByLabelText('JIRA issue type for new tag value')).toBeNull();
  });

  it('empty-state copy references a ServiceNow assignment group (not JIRA project) for SNOW-only', () => {
    render(<Harness snowEnabled jiraEnabled={false} tagKey="Team" />);
    expect(screen.getByText(/ServiceNow assignment group/i)).toBeInTheDocument();
    expect(screen.queryByText(/route to a specific JIRA project/i)).toBeNull();
  });
});

// ===========================================================================
// T19 (AC-140.3) — SNOW-only add-row requires sys_id, not JIRA project;
//                  duplicate-tagValue guard still fires
// ===========================================================================

describe('STORY-140 T19 — SNOW-only add-row validation (AC-140.3)', () => {
  it('requires the ServiceNow group sys_id (not a JIRA project) to add a row', async () => {
    const user = userEvent.setup();
    render(<Harness snowEnabled jiraEnabled={false} />);
    await user.type(screen.getByLabelText('New tag value'), 'platform');
    await user.click(screen.getByTestId('add-tag-mapping-row'));
    expect(hostMappings()).toHaveLength(0);
    expect(screen.getByText(/Assignment group sys_id is required/i)).toBeInTheDocument();
    expect(screen.queryByText(/JIRA project is required/i)).toBeNull();
  });

  it('adds a SNOW-only row (tagValue + sys_id + record type) with rowStatus "new"', async () => {
    const user = userEvent.setup();
    render(<Harness snowEnabled jiraEnabled={false} />);
    await user.type(screen.getByLabelText('New tag value'), 'platform');
    await user.type(screen.getByLabelText('ServiceNow group for new tag value'),
      'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6');
    await user.click(screen.getByTestId('add-tag-mapping-row'));
    const rows = hostMappings();
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      tagValue: 'platform',
      snowAssignmentGroupId: 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6',
      snowRecordType: 'change_request',
      jiraProject: '',
      rowStatus: 'new',
    });
  });

  it('duplicate-tagValue guard still fires on the SNOW-only path (collides on TAG_ROUTING#{value})', async () => {
    const user = userEvent.setup();
    render(<Harness snowEnabled jiraEnabled={false} initial={[snowRow('platform')]} />);
    await user.type(screen.getByLabelText('New tag value'), 'platform');
    await user.type(screen.getByLabelText('ServiceNow group for new tag value'),
      'b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6a1');
    await user.click(screen.getByTestId('add-tag-mapping-row'));
    expect(hostMappings()).toHaveLength(1);
    expect(screen.getByText(/already exists/i)).toBeInTheDocument();
  });

  it('persistTagMappings builds a SNOW-only body (snow* included, jira* omitted)', async () => {
    mockApiFetch.mockResolvedValue({ created: 1, updated: 0, validationErrors: [] });
    const rows: TagMappingRow[] = [{
      tagValue: 'platform',
      jiraProject: '',
      jiraIssueType: 'Task',
      snowAssignmentGroupId: 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6',
      snowRecordType: 'incident',
      rowStatus: 'new',
    }];
    await persistTagMappings(rows, [], /*snowEnabled*/ true, /*jiraEnabled*/ false);
    const post = mockApiFetch.mock.calls.find(c => c[0] === '/config/routing/tags');
    const body = JSON.parse((post![1] as any).body);
    expect(body.mappings[0]).toEqual({
      tagValue: 'platform',
      snowAssignmentGroupId: 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6',
      snowRecordType: 'incident',
    });
    expect(body.mappings[0]).not.toHaveProperty('jiraProject');
    expect(body.mappings[0]).not.toHaveProperty('jiraIssueType');
  });

  it('hasTagMappingClientErrors gates on the sys_id (not jiraProject) under SNOW-only', () => {
    expect(hasTagMappingClientErrors(
      [{ tagValue: 'p', jiraProject: '', jiraIssueType: 'Task', snowAssignmentGroupId: '', rowStatus: 'new' }],
      /*snowEnabled*/ true, /*jiraEnabled*/ false)).toBe(true);
    expect(hasTagMappingClientErrors(
      [{ tagValue: 'p', jiraProject: '', jiraIssueType: 'Task',
         snowAssignmentGroupId: 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6', rowStatus: 'new' }],
      true, false)).toBe(false);
  });
});

// ===========================================================================
// T20 (AC-140.1) — structured SNOW error surfacing
// ===========================================================================

describe('STORY-140 T20 — SNOW error surfacing (AC-140.1)', () => {
  it('maps CFG_SNOW_GROUP_NOT_FOUND to a per-row inline message on the correct tagValue', () => {
    const mapped = snowErrorMessage('CFG_SNOW_GROUP_NOT_FOUND', 'raw', 'ffffffffffffffffffffffffffffffff');
    render(<Harness
      snowEnabled jiraEnabled={false}
      initial={[snowRow('platform', 'ffffffffffffffffffffffffffffffff')]}
      rowErrors={{ platform: mapped }}
    />);
    expect(screen.getByText(/was not found in the connected ServiceNow instance/i)).toBeInTheDocument();
  });

  it('snowErrorMessage maps every structured SNOW code to a targeted message', () => {
    expect(snowErrorMessage('CFG_INVALID_SNOW_GROUP_ID', 'raw')).toMatch(/32-character lowercase hex/i);
    expect(snowErrorMessage('CFG_INVALID_SNOW_RECORD_TYPE', 'raw')).toMatch(/Change Request or Incident/i);
    expect(snowErrorMessage('CFG_INVALID_SNOW_GROUP_NAME', 'raw')).toMatch(/128 characters or fewer/i);
    expect(snowErrorMessage('CFG_SNOW_GROUP_NOT_FOUND', 'raw', 'abc')).toMatch(/'abc'.*not found/i);
    expect(snowErrorMessage(undefined, 'raw reason')).toBe('raw reason');
  });

  it('renders CFG_SNOW_NOT_CONFIGURED as a section-level Alert above the table', () => {
    render(<Harness
      snowEnabled jiraEnabled={false}
      sectionError="Connect and validate ServiceNow before saving tag routing. Go to Configuration → Connections."
    />);
    expect(screen.getByText(/Connect and validate ServiceNow before saving tag routing/i)).toBeInTheDocument();
  });
});

// ===========================================================================
// T21 (AC-140.5) — GET hydrate → SNOW target + rowStatus persisted
// ===========================================================================

describe('STORY-140 T21 — round-trip hydrate (AC-140.5)', () => {
  it('a persisted SNOW row displays its sys_id and record type, marked Saved', () => {
    render(<Harness snowEnabled jiraEnabled={false} initial={[snowRow('platform')]} />);
    const idInput = screen.getByLabelText('ServiceNow group for tag value platform') as HTMLInputElement;
    expect(idInput.value).toBe('a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6');
    expect(screen.getByText('Saved')).toBeInTheDocument();
    const rows = hostMappings();
    expect(rows[0].snowAssignmentGroupId).toBe('a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6');
    expect(rows[0].snowRecordType).toBe('change_request');
    expect(rows[0].rowStatus).toBe('persisted');
  });

  it('editing a persisted SNOW target flips the row to "edited" (joins the upsert set)', async () => {
    const user = userEvent.setup();
    render(<Harness snowEnabled jiraEnabled={false} initial={[snowRow('platform')]} />);
    const idInput = screen.getByLabelText('ServiceNow group for tag value platform');
    await user.type(idInput, 'X');
    expect(hostMappings()[0].rowStatus).toBe('edited');
  });
});

// ===========================================================================
// T22 (AC-140.6) — JIRA-only editor byte-identical
// ===========================================================================

describe('STORY-140 T22 — JIRA-only editor unchanged (AC-140.6)', () => {
  it('default props (no snowEnabled) render the JIRA columns and add-row, SNOW absent', () => {
    render(<Harness initial={[jiraRow('platform')]} />);
    const headers = screen.getAllByRole('columnheader').map(h => h.textContent);
    expect(headers).toContain('JIRA project');
    expect(headers).toContain('Issue type');
    expect(headers).not.toContain('ServiceNow Group');
    expect(headers).not.toContain('Record Type');
    expect(screen.getByLabelText('JIRA project for new tag value')).toBeInTheDocument();
    expect(screen.queryByLabelText('ServiceNow group for new tag value')).toBeNull();
  });

  it('JIRA-only add-row requires a JIRA project (pre-epic message)', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.type(screen.getByLabelText('New tag value'), 'platform');
    await user.click(screen.getByTestId('add-tag-mapping-row'));
    expect(hostMappings()).toHaveLength(0);
    expect(screen.getByText(/JIRA project is required/i)).toBeInTheDocument();
  });

  it('JIRA-only persistTagMappings body is byte-identical to pre-epic (no snow* keys)', async () => {
    mockApiFetch.mockResolvedValue({ created: 1, updated: 0, validationErrors: [] });
    const rows: TagMappingRow[] = [{
      tagValue: 'platform', jiraProject: 'CLOUDOPS', jiraIssueType: 'Task', rowStatus: 'new',
    }];
    await persistTagMappings(rows, []);
    const post = mockApiFetch.mock.calls.find(c => c[0] === '/config/routing/tags');
    const body = JSON.parse((post![1] as any).body);
    expect(body.mappings[0]).toEqual({
      tagValue: 'platform', jiraProject: 'CLOUDOPS', jiraIssueType: 'Task',
    });
    expect(body.mappings[0]).not.toHaveProperty('snowAssignmentGroupId');
    expect(body.mappings[0]).not.toHaveProperty('snowRecordType');
  });

  it('explicit jira-only platform (snowEnabled=false, jiraEnabled=true) matches default', () => {
    render(<Harness snowEnabled={false} jiraEnabled initial={[jiraRow('platform')]} />);
    expect(screen.getByText('JIRA project')).toBeInTheDocument();
    expect(screen.queryByText('ServiceNow Group')).toBeNull();
  });
});

// ===========================================================================
// MUST-140-3 / SR-139-1/2/3 (STC-9-style) — hostile input renders INERT
// ===========================================================================

describe('STORY-140 MUST-140-3 — hostile input renders inert text', () => {
  it('a hostile tagValue renders as inert TEXT (no HTML injection)', () => {
    const xss = '<img src=x onerror=alert(1)>';
    const { container } = render(
      <Harness snowEnabled jiraEnabled={false} initial={[snowRow(xss)]} />);
    expect(screen.getByText(xss)).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
  });

  it('a hostile per-row SNOW error message renders as inert text (no script node)', () => {
    const evil = '<script>steal()</script>';
    const { container } = render(<Harness
      snowEnabled jiraEnabled={false}
      initial={[snowRow('platform')]}
      rowErrors={{ platform: evil }}
    />);
    expect(screen.getByText(evil)).toBeInTheDocument();
    expect(container.querySelector('script')).toBeNull();
  });

  it('a hostile CFG_SNOW_NOT_CONFIGURED section message renders as inert text', () => {
    const evil = '<iframe src=javascript:alert(1)></iframe>';
    const { container } = render(<Harness
      snowEnabled jiraEnabled={false} sectionError={evil} />);
    expect(screen.getByText(evil)).toBeInTheDocument();
    expect(container.querySelector('iframe')).toBeNull();
  });

  it('a hostile sys_id interpolated into a not-found message renders inert', () => {
    const evilId = '<b>x</b>';
    const mapped = snowErrorMessage('CFG_SNOW_GROUP_NOT_FOUND', 'raw', evilId);
    const { container } = render(<Harness
      snowEnabled jiraEnabled={false}
      initial={[snowRow('platform')]}
      rowErrors={{ platform: mapped }}
    />);
    const escaped = '<b>x</b>'.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    expect(screen.getByText(new RegExp(escaped))).toBeInTheDocument();
    expect(container.querySelector('b')).toBeNull();
  });
});

// ===========================================================================
// T23 — served-source string audit (sandbox Playwright constraint)
// ===========================================================================

describe('STORY-140 T23 — served-source string audit', () => {
  const here = dirname(fileURLToPath(import.meta.url));
  const editorSrc = readFileSync(
    resolve(here, '../components/TagRoutingMappingsEditor.tsx'), 'utf-8');
  const logicSrc = readFileSync(
    resolve(here, '../components/tagMappings.ts'), 'utf-8');

  it('the SNOW vocabulary is present in the tag editor render path', () => {
    // Editor-authored SNOW column vocabulary + empty-state target label.
    expect(editorSrc).toContain('ServiceNow Group');
    expect(editorSrc).toContain('Record Type');
    expect(editorSrc).toContain('Change Request');
    expect(editorSrc).toContain('Incident');
    expect(editorSrc).toContain('ServiceNow assignment group');
    // The add-row-required message is produced by validateSnowGroupIdClient in
    // the shared logic module (served in the same bundle).
    expect(logicSrc).toContain('Assignment group sys_id is required.');
  });

  it('no JIRA-worded tag-save codes/strings appear as active SNOW-path text', () => {
    expect(editorSrc).not.toContain('CFG_JIRA_NOT_CONFIGURED');
    expect(editorSrc).not.toContain('jiraProject is required');
  });

  it('no HTML/eval sink is present in the editor or logic module (MUST-140-3 / SR-139-1)', () => {
    const forbidden = /dangerouslySetInnerHTML|\binnerHTML\b|insertAdjacentHTML|document\.write|\beval\(|new Function|srcDoc/;
    // Strip block/line comments before scanning: the only expected match is the
    // doc-comment stating the prohibition, which is not an actual sink.
    const strip = (s: string) =>
      s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
    expect(forbidden.test(strip(editorSrc))).toBe(false);
    expect(forbidden.test(strip(logicSrc))).toBe(false);
  });
});
