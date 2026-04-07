/**
 * Runtime configuration loader.
 *
 * In production, the Docker entrypoint generates a /config.json with
 * backend URLs. In development, Vite env vars (VITE_*) are used as fallback.
 *
 * The config is fetched once at app startup and cached in memory.
 */

export interface RuntimeConfig {
  apiBaseUrl: string
  wsBaseUrl: string
}

let _config: RuntimeConfig | null = null

/**
 * Load runtime configuration.
 *
 * Tries to fetch /config.json first (production). If it fails or returns
 * empty values, falls back to Vite env vars / defaults.
 */
export async function loadConfig(): Promise<RuntimeConfig> {
  if (_config) return _config

  try {
    const res = await fetch('/config.json')
    if (res.ok) {
      const json = await res.json()
      if (json.apiBaseUrl) {
        _config = {
          apiBaseUrl: json.apiBaseUrl,
          wsBaseUrl: json.wsBaseUrl || '',
        }
        return _config
      }
    }
  } catch {
    // Ignore — fall back to env vars / defaults
  }

  // Fallback: Vite build-time env vars or defaults (dev mode)
  _config = {
    apiBaseUrl: import.meta.env.VITE_API_BASE_URL ?? '/api/v1',
    wsBaseUrl: import.meta.env.VITE_WS_BASE_URL ?? '',
  }
  return _config
}

/**
 * Get the cached config synchronously. Must call loadConfig() first.
 */
export function getConfig(): RuntimeConfig {
  if (!_config) {
    // Fallback if accessed before loadConfig() completes
    return {
      apiBaseUrl: import.meta.env.VITE_API_BASE_URL ?? '/api/v1',
      wsBaseUrl: import.meta.env.VITE_WS_BASE_URL ?? '',
    }
  }
  return _config
}
