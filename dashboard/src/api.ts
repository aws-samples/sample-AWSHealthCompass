import { getConfig } from './config';

function getApiUrl(): string {
  return getConfig().apiUrl.replace(/\/+$/, '');
}

// Injected async token provider — set by AuthenticatedApp once auth is ready.
// This replaces the old synchronous sessionStorage.getItem('resolve_id_token') approach.
let tokenProvider: (() => Promise<string | null>) | null = null;

let signOutCallback: (() => void) | null = null;

export function setTokenProvider(provider: () => Promise<string | null>) {
  tokenProvider = provider;
}

export function setSignOutCallback(cb: () => void) { signOutCallback = cb; }

export function isConfigured(): boolean {
  return !!getApiUrl() && !!tokenProvider;
}

export async function apiFetch(path: string, options: RequestInit = {}) {
  if (!tokenProvider) throw new Error('Auth not initialized');

  const token = await tokenProvider();
  if (!token) {
    // Token could not be obtained — session expired or user signed out
    if (signOutCallback) signOutCallback();
    throw new Error('Session expired. Please sign in again.');
  }

  const headers: Record<string, string> = {
    ...(options.headers as Record<string, string> || {}),
    'Authorization': `Bearer ${token}`,
  };
  if (options.body && typeof options.body === 'string') headers['Content-Type'] = 'application/json';

  const res = await fetch(`${getApiUrl()}/api${path}`, { ...options, headers });

  if (res.status === 401) {
    // Token rejected by API — force re-login
    if (signOutCallback) signOutCallback();
    throw new Error('Session expired. Please sign in again.');
  }

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`API ${res.status}: ${text}`);
  }
  return res.json();
}
