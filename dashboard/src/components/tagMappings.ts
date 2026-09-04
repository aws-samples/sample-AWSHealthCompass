/**
 * STORY-125 (RT-03): shared logic for the tag-value → routing-target mapping
 * editor. Kept separate from the React component so the wizard and the
 * RoutingEditModal share ONE implementation of validation, reconciliation, and
 * persistence (no forked save path).
 *
 * Security note: client-side validation here is ADVISORY only (Snape SR-125-9).
 * The authoritative length/charset guard lives server-side in
 * `lambdas/api/tag_routing_handlers.py`; a hostile caller can bypass this UI
 * entirely. These mirrors give fast inline feedback and MUST stay in sync with
 * the backend constants (SR-125-15 / interface-review NOTE-A / NOTE-B).
 */
import { apiFetch } from '../api';
import { parseApiError } from '../errors';

/** One editable mapping row. `rowStatus`/client-only fields are never sent to the API. */
export interface TagMappingRow {
  tagValue: string;
  jiraProject: string;
  jiraIssueType: string;
  /**
   * STORY-140: ServiceNow target fields — used only when snowEnabled. Default
   * snowRecordType is 'change_request'. Optional snowAssignmentGroupName flows
   * through additively if present.
   */
  snowAssignmentGroupId?: string;
  snowAssignmentGroupName?: string;
  snowRecordType?: string;
  /** 'persisted' = loaded from GET; 'new' = added this session; 'edited' = persisted row whose target changed. */
  rowStatus: 'persisted' | 'new' | 'edited';
}

/** Per-row failure returned by POST /config/routing/tags (keyed by tagValue). */
export interface TagMappingValidationError {
  tagValue: string;
  jiraProject?: string;
  /** STORY-140: structured field association for SNOW-branch per-row errors. */
  field?: string;
  /** STORY-140: structured error code (e.g. CFG_SNOW_GROUP_NOT_FOUND). */
  code?: string;
  reason: string;
}

/** Honest outcome of a tag-mapping save (RT-14): upsert triad + delete count + transport error. */
export interface TagMappingsSaveResult {
  created: number;
  updated: number;
  validationErrors: TagMappingValidationError[];
  deleted: number;
  /** Set when the POST or a non-404 DELETE threw; the caller must NOT report success. */
  transportError?: string;
}

// SR-125-15 / NOTE-A: MUST match the backend `_MAX_TAG_VALUE_LEN` (256) exactly.
export const MAX_TAG_VALUE_LEN = 256;
// SR-125-2 / NOTE-B: mirror the backend control-char predicate [\x00-\x1F\x7F-\x9F].
const CONTROL_CHAR_RE = /[\u0000-\u001f\u007f-\u009f]/;
const MAX_ISSUE_TYPE_LEN = 128; // mirrors backend _MAX_ISSUE_TYPE_LEN (SR-125-14)
// STORY-140: advisory client mirror of the backend _SNOW_GROUP_ID_RE
// (^[a-f0-9]{32}$). Fast inline feedback ONLY — backend is authoritative.
const SNOW_GROUP_ID_RE = /^[a-f0-9]{32}$/;

/**
 * STORY-140: advisory client-side validation of a ServiceNow assignment-group
 * sys_id. Returns a user-facing message, or null when acceptable. Backend
 * (`validate_snow_routing_fields` + existence check) is authoritative.
 */
export function validateSnowGroupIdClient(value: string): string | null {
  const v = value.trim();
  if (!v) return 'Assignment group sys_id is required.';
  if (!SNOW_GROUP_ID_RE.test(v)) return 'Assignment group must be a 32-character lowercase hex sys_id.';
  return null;
}

/**
 * Advisory client-side validation of a tag value (V1/V4/V5). Returns a
 * user-facing message, or null when acceptable. Backend is authoritative.
 */
export function validateTagValueClient(value: string): string | null {
  const v = value.trim();
  if (!v) return 'Tag value is required.';
  if (CONTROL_CHAR_RE.test(v)) return "Tag value can't contain line breaks or control characters.";
  if (v.length > MAX_TAG_VALUE_LEN) return `Tag value is too long (max ${MAX_TAG_VALUE_LEN} characters).`;
  return null;
}

/** Advisory client-side validation of the issue type (mirrors SR-125-14, LOW). */
export function validateIssueTypeClient(value: string): string | null {
  const v = value.trim();
  if (CONTROL_CHAR_RE.test(v)) return "Issue type can't contain line breaks or control characters.";
  if (v.length > MAX_ISSUE_TYPE_LEN) return `Issue type is too long (max ${MAX_ISSUE_TYPE_LEN} characters).`;
  return null;
}

/** Rows that need an upsert POST (created or target-edited). Unchanged rows are omitted. */
export function getUpsertRows(rows: TagMappingRow[]): TagMappingRow[] {
  return rows.filter(r => r.rowStatus === 'new' || r.rowStatus === 'edited');
}

/**
 * True if any row has an unresolved client-side error (blocks save; UX only).
 * Duplicate tag values are prevented at add-time, so this checks empty/invalid
 * target and any tag value that slipped through (defense-in-depth).
 *
 * STORY-140: platform-aware. Under SNOW-only (`snowEnabled && !jiraEnabled`)
 * the row gates on `snowAssignmentGroupId` instead of `jiraProject`. Default
 * (jiraEnabled) preserves the pre-epic JIRA gate byte-for-byte (AC-140.6).
 */
export function hasTagMappingClientErrors(
  rows: TagMappingRow[],
  snowEnabled = false,
  jiraEnabled = true,
): boolean {
  const snowOnly = snowEnabled && !jiraEnabled;
  return rows.some(r => {
    if (validateTagValueClient(r.tagValue) !== null) return true;
    if (snowOnly) {
      return validateSnowGroupIdClient(r.snowAssignmentGroupId ?? '') !== null;
    }
    // JIRA (jira-only or dual) — unchanged pre-epic gate.
    return !r.jiraProject.trim() || validateIssueTypeClient(r.jiraIssueType) !== null;
  });
}

/**
 * Persist tag mappings against the existing endpoints. Runs DELETEs FIRST
 * (AD-3: eliminates the rename old+new overlap window), then one upsert POST.
 * A 404 on DELETE is treated as idempotent success (already absent). A
 * transport failure short-circuits and is returned in `transportError` so the
 * caller surfaces it and does not claim the mappings were persisted.
 *
 * STORY-140: the per-row upsert body is platform-aware. When `snowEnabled`,
 * each row carries `snowAssignmentGroupId`/`snowRecordType` (+ name if present).
 * When `!jiraEnabled` (SNOW-only), the `jiraProject`/`jiraIssueType` fields are
 * omitted. Default (`snowEnabled=false, jiraEnabled=true`) is byte-identical to
 * the pre-epic JIRA-only body (AC-140.6). The DELETE-first flow is unchanged —
 * it is target-agnostic (keyed on the `TAG_ROUTING#{value}` pk).
 */
export async function persistTagMappings(
  upsertRows: TagMappingRow[],
  removedTagValues: string[],
  snowEnabled = false,
  jiraEnabled = true,
): Promise<TagMappingsSaveResult> {
  const result: TagMappingsSaveResult = { created: 0, updated: 0, validationErrors: [], deleted: 0 };

  // Deletes first — POST upserts and never removes, so removals need DELETE.
  for (const tagValue of removedTagValues) {
    try {
      await apiFetch(`/config/routing/tags/${encodeURIComponent(tagValue)}`, { method: 'DELETE' });
      result.deleted += 1;
    } catch (e: unknown) {
      // 404 = mapping already absent = success for an idempotent removal.
      if (e instanceof Error && /^API 404:/.test(e.message)) {
        result.deleted += 1;
        continue;
      }
      result.transportError = parseApiError(e);
      return result;
    }
  }

  // Single upsert POST for created/edited rows.
  if (upsertRows.length > 0) {
    try {
      const resp = await apiFetch('/config/routing/tags', {
        method: 'POST',
        body: JSON.stringify({
          mappings: upsertRows.map(r => {
            const row: Record<string, string> = { tagValue: r.tagValue };
            // JIRA fields: included unless SNOW-only (!jiraEnabled).
            if (jiraEnabled) {
              row.jiraProject = r.jiraProject;
              row.jiraIssueType = r.jiraIssueType;
            }
            // SNOW fields: included when snowEnabled.
            if (snowEnabled) {
              row.snowAssignmentGroupId = r.snowAssignmentGroupId ?? '';
              row.snowRecordType = r.snowRecordType || 'change_request';
              if (r.snowAssignmentGroupName) {
                row.snowAssignmentGroupName = r.snowAssignmentGroupName;
              }
            }
            return row;
          }),
        }),
      });
      result.created = resp?.created ?? 0;
      result.updated = resp?.updated ?? 0;
      result.validationErrors = resp?.validationErrors ?? [];
    } catch (e: unknown) {
      result.transportError = parseApiError(e);
    }
  }

  return result;
}
