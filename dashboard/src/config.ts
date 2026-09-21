export interface AppConfig {
  userPoolId: string;
  clientId: string;
  apiUrl: string;
  region: string;
}

let _config: AppConfig | null = null;

export async function loadConfig(): Promise<AppConfig> {
  if (_config) return _config;
  try {
    const resp = await fetch('/config.json');
    if (resp.ok) {
      _config = await resp.json();
      return _config!;
    }
  } catch (e) { /* fall through to env vars for local dev */ }
  _config = {
    userPoolId: import.meta.env.VITE_COGNITO_USER_POOL_ID || '',
    clientId: import.meta.env.VITE_COGNITO_CLIENT_ID || '',
    apiUrl: import.meta.env.VITE_API_URL || '',
    region: 'us-east-1',
  };
  return _config;
}

export function getConfig(): AppConfig {
  if (!_config) throw new Error('Config not loaded — call loadConfig() first');
  return _config;
}
