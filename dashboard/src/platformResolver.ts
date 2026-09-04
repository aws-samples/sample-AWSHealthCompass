/**
 * Platform-context resolver (STORY-139).
 *
 * Pure classification of the STORY-136 `platforms` array (top-level on
 * `GET /config/summary`) into the booleans + single label-platform the
 * dashboard needs. This is the FRONTEND MIRROR of STORY-136's
 * operative-single-platform rule (STORY-136 §4.3) — it CONSUMES that rule,
 * it does not re-derive platform enablement from the legacy scalar
 * (`config.platform`) or from any other source.
 *
 * Operative-single-platform rule (owned by STORY-136 §4.3, reused verbatim):
 *   ServiceNow is the operative single platform IFF platforms == ["servicenow"].
 *   Everything else (["jira"], ["jira","servicenow"], defensive-empty/absent)
 *   resolves to JIRA for the single app-wide label context.
 *
 * ACCESS PATH (Dumbledore §0 correction): the field is `config.platforms`
 * TOP-LEVEL — NOT `config.data.platforms`. The wire body from
 * `handle_config_summary` is bare (no `data` envelope).
 *
 * FILE NAME: this module is deliberately named `platformResolver.ts` (not
 * `platformContext.ts`) to avoid a case-only collision with the existing
 * `PlatformContext.tsx` on case-insensitive filesystems. It remains a
 * separate, pure, unit-testable module per Dumbledore §1.2 (which permitted a
 * separate file or co-location; a separate file was chosen for testability).
 *
 * Invariant: after STORY-139, no component reads `config.platform` (scalar)
 * for a platform decision — every decision flows through this helper (or, in
 * the routing modal, the identically-derived `platforms` array). The scalar
 * remains only as a legacy wire field on the type.
 */

import type { OnboardingConfig } from './types';
import type { Platform } from './platformLabels';

export interface PlatformContext {
  /** The resolved platforms array (source of truth per STORY-136). */
  platforms: string[];
  /** True when JIRA is among the enabled platforms. */
  jiraEnabled: boolean;
  /** True when ServiceNow is among the enabled platforms. */
  snowEnabled: boolean;
  /**
   * The single app-wide label context. Dual (["jira","servicenow"]) and
   * jira-only both resolve to 'jira'; only ["servicenow"] resolves to
   * 'servicenow'. Per-row/per-ticket dual labeling is out of scope.
   */
  labelPlatform: Platform;
}

/**
 * Classify the resolved `config.platforms` array into the label context and
 * enablement booleans consumed across the dashboard.
 *
 * Defensive-only fallback: STORY-136 guarantees a non-empty `platforms[]` on
 * any live 200. The fallback below covers ONLY a null config or an old cached
 * response that predates STORY-136 — it prefers `platforms`, and treats the
 * legacy scalar as a last-resort legacy signal (never the primary source).
 */
export function resolvePlatformContext(config: OnboardingConfig | null): PlatformContext {
  const platforms: string[] =
    config?.platforms ?? (config?.platform ? [config.platform] : ['jira']);

  const jiraEnabled = platforms.includes('jira');
  const snowEnabled = platforms.includes('servicenow');

  // Operative-single-platform (STORY-136 §4.3), consumed not re-derived:
  // only platforms == ["servicenow"] flips the app-wide label to ServiceNow.
  const labelPlatform: Platform =
    platforms.length === 1 && platforms[0] === 'servicenow' ? 'servicenow' : 'jira';

  return { platforms, jiraEnabled, snowEnabled, labelPlatform };
}
