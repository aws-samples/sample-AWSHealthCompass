// Single source of truth for user-facing product branding.
//
// Introduced in Session 41 (Resolve → Compass, Alpha → Beta rename) so the
// product name and phase live in exactly one place instead of being scattered
// as string literals across App.tsx, Login.tsx, index.html, etc.
//
// NOTE: this is BRANDING ONLY. It is intentionally unrelated to:
//   - the `resolve_core` Python module name (internal import, unchanged),
//   - the standardized-event schema `source: "resolve"` data-contract value,
//   - the `resolve-campaign` JIRA label,
//   - `.resolve()` / `platformResolver` / `resolvePlatformContext` identifiers.

/** Product name shown to users (nav identity, login, headings). */
export const APP_NAME = 'Compass';

/** Release phase shown to users. */
export const APP_PHASE = 'Beta';

/** Full product title, e.g. for the browser tab / document title. */
export const APP_TITLE = `${APP_NAME} ${APP_PHASE}`;
