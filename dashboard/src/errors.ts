/**
 * Shared API error formatting (STORY-115).
 *
 * Single source of truth for turning an `apiFetch` error into user-friendly
 * copy. Consumed by both `modals/DispatchEditModal.tsx` and
 * `ConfigurationWizard.tsx` so the two dispatch surfaces render identical
 * error messages and cannot diverge.
 */

/** Parse an API error for user-friendly display. */
export function parseApiError(error: unknown): string {
  if (!(error instanceof Error)) return 'An unexpected error occurred.';
  const msg = error.message;
  const match = msg.match(/^API (\d+): (.+)$/s);
  if (!match) return 'Unable to reach the server. Check your network connection.';
  const status = parseInt(match[1], 10);
  const body = match[2].substring(0, 200);
  if (status === 400) return body;
  if (status === 403) return "You don't have permission to modify this configuration.";
  if (status === 409) return 'Configuration was modified by another user. Close and reopen to see the latest.';
  if (status === 429) return 'Too many requests. Please wait and try again.';
  if (status >= 500) return 'An unexpected error occurred. Please try again.';
  return body;
}
