/**
 * Vitest coverage for the shared tag-mapping LOGIC module
 * (`dashboard/src/components/tagMappings.ts`): the single, un-forked
 * persistence + validation implementation shared by the wizard and the
 * RoutingEditModal.
 *
 * Coverage:
 *   persistTagMappings — DELETE-first then single upsert POST; 404-on-DELETE is
 *     idempotent success; transport failure short-circuits into transportError
 *     and does NOT report success; honest {created,updated,validationErrors,deleted}
 *     triad surfaced verbatim from the POST body; POST body shape is the
 *     {mappings:[{tagValue,jiraProject,jiraIssueType}]} the backend accepts;
 *     tag value URL-encoded on the DELETE path.
 *   validateTagValueClient / validateIssueTypeClient — advisory mirrors of the
 *     server gate (V1 empty, V4 length 256, V5 control chars); the
 *     client is UX-only (mirrors, not the authority).
 *   getUpsertRows — only 'new'/'edited' rows go on the wire (unchanged omitted).
 *   hasTagMappingClientErrors — blocks save on empty/invalid/oversized rows.
 *   MAX_TAG_VALUE_LEN mirrors the backend constant (256) exactly.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../api', () => ({ apiFetch: vi.fn() }));

import { apiFetch } from '../api';
import {
  persistTagMappings,
  validateTagValueClient,
  validateIssueTypeClient,
  getUpsertRows,
  hasTagMappingClientErrors,
  MAX_TAG_VALUE_LEN,
  TagMappingRow,
} from '../components/tagMappings';

const mockApiFetch = vi.mocked(apiFetch);

/** Calls that hit a given path (optionally filtered by HTTP method). */
function callsTo(path: string, method?: string) {
  return mockApiFetch.mock.calls.filter(c => {
    const p = c[0] as string;
    const m = ((c[1] as any)?.method) ?? 'GET';
    return p === path && (method ? m === method : true);
  });
}

beforeEach(() => vi.clearAllMocks());

// ===========================================================================
// persistTagMappings — save wiring
// ===========================================================================

describe('persistTagMappings — save wiring', () => {
  it('POSTs the {mappings:[{tagValue,jiraProject,jiraIssueType}]} shape to /config/routing/tags', async () => {
    mockApiFetch.mockResolvedValue({ created: 2, updated: 0, validationErrors: [] });
    const rows = [
      { tagValue: 'platform', jiraProject: 'CLOUDOPS', jiraIssueType: 'Task' },
      { tagValue: 'data', jiraProject: 'DATAOPS', jiraIssueType: 'Bug' },
    ];
    const result = await persistTagMappings(rows, []);

    const post = callsTo('/config/routing/tags', 'POST');
    expect(post).toHaveLength(1);
    const body = JSON.parse((post[0][1] as any).body);
    expect(body).toEqual({
      mappings: [
        { tagValue: 'platform', jiraProject: 'CLOUDOPS', jiraIssueType: 'Task' },
        { tagValue: 'data', jiraProject: 'DATAOPS', jiraIssueType: 'Bug' },
      ],
    });
    expect(result.created).toBe(2);
    expect(result.transportError).toBeUndefined();
  });

  it('surfaces the honest {created,updated,validationErrors} triad verbatim from the POST body', async () => {
    mockApiFetch.mockResolvedValue({
      created: 1,
      updated: 1,
      validationErrors: [{ tagValue: 'bad', jiraProject: 'NOPE', reason: 'Invalid JIRA project' }],
    });
    const result = await persistTagMappings(
      [{ tagValue: 'a', jiraProject: 'P', jiraIssueType: 'Task' }], []);
    expect(result.created).toBe(1);
    expect(result.updated).toBe(1);
    expect(result.validationErrors).toEqual([
      { tagValue: 'bad', jiraProject: 'NOPE', reason: 'Invalid JIRA project' },
    ]);
    expect(result.transportError).toBeUndefined();
  });

  it('runs DELETEs FIRST then a single upsert POST (rename overlap window eliminated)', async () => {
    const order: string[] = [];
    mockApiFetch.mockImplementation(async (path: string, opts?: any) => {
      order.push(`${(opts?.method) ?? 'GET'} ${path}`);
      return { created: 1, updated: 0, validationErrors: [] };
    });
    await persistTagMappings(
      [{ tagValue: 'new', jiraProject: 'P', jiraIssueType: 'Task' }],
      ['old-a', 'old-b'],
    );
    expect(order).toEqual([
      'DELETE /config/routing/tags/old-a',
      'DELETE /config/routing/tags/old-b',
      'POST /config/routing/tags',
    ]);
  });

  it('URL-encodes the tag value on the DELETE path', async () => {
    mockApiFetch.mockResolvedValue({});
    await persistTagMappings([], ['team/ops+dev']);
    const del = callsTo('/config/routing/tags/team%2Fops%2Bdev', 'DELETE');
    expect(del).toHaveLength(1);
  });

  it('treats a 404 on DELETE as idempotent success (mapping already absent)', async () => {
    mockApiFetch.mockImplementation(async (_path: string, opts?: any) => {
      if ((opts?.method) === 'DELETE') throw new Error('API 404: not found');
      return { created: 0, updated: 0, validationErrors: [] };
    });
    const result = await persistTagMappings(
      [{ tagValue: 'x', jiraProject: 'P', jiraIssueType: 'Task' }], ['gone']);
    expect(result.deleted).toBe(1);
    expect(result.transportError).toBeUndefined();
    // POST still runs after the idempotent delete.
    expect(callsTo('/config/routing/tags', 'POST')).toHaveLength(1);
  });

  it('short-circuits on a non-404 DELETE transport error (does NOT POST, does NOT claim success)', async () => {
    mockApiFetch.mockImplementation(async (_path: string, opts?: any) => {
      if ((opts?.method) === 'DELETE') throw new Error('API 500: boom');
      return { created: 99, updated: 0, validationErrors: [] };
    });
    const result = await persistTagMappings(
      [{ tagValue: 'x', jiraProject: 'P', jiraIssueType: 'Task' }], ['doomed']);
    expect(result.transportError).toBeTruthy();
    expect(result.created).toBe(0);                     // POST never ran
    expect(callsTo('/config/routing/tags', 'POST')).toHaveLength(0);
  });

  it('records a POST transport failure in transportError without reporting created/updated', async () => {
    mockApiFetch.mockRejectedValue(new Error('API 500: server error'));
    const result = await persistTagMappings(
      [{ tagValue: 'x', jiraProject: 'P', jiraIssueType: 'Task' }], []);
    expect(result.transportError).toBeTruthy();
    expect(result.created).toBe(0);
    expect(result.updated).toBe(0);
  });

  it('skips the POST entirely when there are no upsert rows (empty-skip; deletes only)', async () => {
    mockApiFetch.mockResolvedValue({});
    const result = await persistTagMappings([], ['only-a-delete']);
    expect(callsTo('/config/routing/tags', 'POST')).toHaveLength(0);
    expect(callsTo('/config/routing/tags/only-a-delete', 'DELETE')).toHaveLength(1);
    expect(result.deleted).toBe(1);
  });
});

// ===========================================================================
// Client-side validators (advisory mirror of server gate)
// ===========================================================================

describe('client validators (advisory mirror of server gate)', () => {
  it('MAX_TAG_VALUE_LEN mirrors the backend _MAX_TAG_VALUE_LEN (256)', () => {
    expect(MAX_TAG_VALUE_LEN).toBe(256);
  });

  it('validateTagValueClient — V1 empty', () => {
    expect(validateTagValueClient('')).toMatch(/required/i);
    expect(validateTagValueClient('   ')).toMatch(/required/i);
  });

  it('validateTagValueClient — V4 length (256 ok, 257 rejected)', () => {
    expect(validateTagValueClient('a'.repeat(256))).toBeNull();
    expect(validateTagValueClient('a'.repeat(257))).toMatch(/too long/i);
  });

  it('validateTagValueClient — V5 control chars (newline, tab, NUL, DEL, C1)', () => {
    for (const bad of ['a\nb', 'a\tb', 'a\x00b', 'a\x7fb', 'a\x9fb']) {
      expect(validateTagValueClient(bad)).toMatch(/line breaks or control/i);
    }
  });

  it('validateTagValueClient — accepts real AWS tag values verbatim', () => {
    for (const ok of ['platform', 'Team=platform/prod-1', 'My Team: Prod']) {
      expect(validateTagValueClient(ok)).toBeNull();
    }
  });

  it('validateIssueTypeClient — length + control chars', () => {
    expect(validateIssueTypeClient('Task')).toBeNull();
    expect(validateIssueTypeClient('T'.repeat(129))).toMatch(/too long/i);
    expect(validateIssueTypeClient('Ta\nsk')).toMatch(/line breaks or control/i);
  });
});

// ===========================================================================
// getUpsertRows / hasTagMappingClientErrors — reconciliation helpers
// ===========================================================================

describe('reconciliation helpers', () => {
  const rows: TagMappingRow[] = [
    { tagValue: 'p', jiraProject: 'A', jiraIssueType: 'Task', rowStatus: 'persisted' },
    { tagValue: 'n', jiraProject: 'B', jiraIssueType: 'Task', rowStatus: 'new' },
    { tagValue: 'e', jiraProject: 'C', jiraIssueType: 'Bug', rowStatus: 'edited' },
  ];

  it('getUpsertRows returns only new/edited rows (unchanged persisted omitted from the wire)', () => {
    const upserts = getUpsertRows(rows).map(r => r.tagValue).sort();
    expect(upserts).toEqual(['e', 'n']);
  });

  it('hasTagMappingClientErrors is false for a clean set', () => {
    expect(hasTagMappingClientErrors(rows)).toBe(false);
  });

  it('hasTagMappingClientErrors flags an empty project', () => {
    expect(hasTagMappingClientErrors([
      { tagValue: 'p', jiraProject: '  ', jiraIssueType: 'Task', rowStatus: 'new' },
    ])).toBe(true);
  });

  it('hasTagMappingClientErrors flags a control-char tag value that slipped through', () => {
    expect(hasTagMappingClientErrors([
      { tagValue: 'bad\nvalue', jiraProject: 'A', jiraIssueType: 'Task', rowStatus: 'new' },
    ])).toBe(true);
  });
});
