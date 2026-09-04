/**
 * Shared dispatch-window wire contract (STORY-115).
 *
 * This is the ONLY definition of the `POST /config/dispatch` request shape.
 * Both `ConfigurationWizard.tsx` and `modals/DispatchEditModal.tsx` import
 * these types and route their save body through `buildDispatchBody()` so the
 * two dispatch forms cannot diverge again.
 *
 * Field names/casing MUST match the API handler (`lambdas/api/dispatch_handlers.py`),
 * which reads camelCase: ruleId, eventTypePattern, eventCategories, enabled.
 */

/** Dispatch preset mode. */
export type DispatchMode = 'all' | 'ple_only' | 'custom';

/** Actionability filter applied on top of the dispatch mode. */
export type ActionabilityFilter = 'all_actionable' | 'action_required_only';

/** A single custom dispatch rule. */
export interface DispatchRule {
  ruleId: string;
  /** Server-validated against ^AWS_[A-Z0-9_]+\*?$ */
  eventTypePattern: string;
  /** Subset of ['scheduledChange', 'accountNotification'] */
  eventCategories: string[];
  enabled: boolean;
}

/** The `POST /config/dispatch` request body. */
export interface DispatchConfigRequest {
  mode: DispatchMode;
  actionabilityFilter: ActionabilityFilter;
  /** Present (mapped) only when mode === 'custom'; omitted otherwise. */
  rules?: DispatchRule[];
}

/**
 * Serialize dispatch state into the canonical wire body.
 *
 * - `rules` is omitted entirely unless `mode === 'custom'`.
 * - Each rule is copied field-by-field to the four canonical keys, in order.
 *   This is deliberate (NOT a spread) to prevent mass-assignment of any extra
 *   in-memory properties into the request body (security invariant per Snape).
 * - Key order is fixed (mode, actionabilityFilter, rules; ruleId, eventTypePattern,
 *   eventCategories, enabled) to satisfy the byte-identical-payload acceptance
 *   criterion.
 *
 * This function performs NO validation — server-side validation remains
 * authoritative.
 */
export function buildDispatchBody(
  mode: DispatchMode,
  actionabilityFilter: ActionabilityFilter,
  rules: DispatchRule[],
): DispatchConfigRequest {
  return {
    mode,
    actionabilityFilter,
    rules:
      mode === 'custom'
        ? rules.map(r => ({
            ruleId: r.ruleId,
            eventTypePattern: r.eventTypePattern,
            eventCategories: r.eventCategories,
            enabled: r.enabled,
          }))
        : undefined,
  };
}
