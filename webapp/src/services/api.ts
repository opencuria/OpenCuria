/**
 * Shared REST transport for the webapp.
 *
 * Authentication, retries, response parsing, and structured errors are owned by
 * `@opencuria/client`; the Socket.IO adapter shares its auth context.
 */
import { ApiClient, ApiRequestError, type FetchLike } from '@opencuria/client/api'
import { clearQuestionInputCaches } from '@/lib/questionInputCache'
import { getConfig } from './config'

export { ApiRequestError }

const ACCESS_TOKEN_KEY = 'kern_access_token'
const REFRESH_TOKEN_KEY = 'kern_refresh_token'
const ORGANIZATION_ID_KEY = 'kern_active_org_id'
let client: ApiClient | null = null
let clientBaseUrl = ''

function getApiBaseUrl(): string {
  return getConfig().apiBaseUrl
}

/** Share REST authentication and organization context with the realtime transport. */
export function getApiClient(): ApiClient {
  const apiBaseUrl = getApiBaseUrl()
  if (!client || clientBaseUrl !== apiBaseUrl) {
    clientBaseUrl = apiBaseUrl
    client = new ApiClient({
      apiBaseUrl,
      getAccessToken: () => localStorage.getItem(ACCESS_TOKEN_KEY),
      getRefreshToken: () => localStorage.getItem(REFRESH_TOKEN_KEY),
      setTokens: (tokens) => {
        localStorage.setItem(ACCESS_TOKEN_KEY, tokens.access_token)
        localStorage.setItem(REFRESH_TOKEN_KEY, tokens.refresh_token)
      },
      getOrganizationId: () => localStorage.getItem(ORGANIZATION_ID_KEY),
      onAuthenticationExpired: () => {
        clearQuestionInputCaches()
        localStorage.removeItem(ACCESS_TOKEN_KEY)
        localStorage.removeItem(REFRESH_TOKEN_KEY)
        localStorage.removeItem(ORGANIZATION_ID_KEY)
        window.location.href = '/login'
      },
      // Resolve fetch when a request is made, so test spies/polyfills installed
      // after module import retain the same behavior as the former wrapper.
      fetch: ((...args) => globalThis.fetch(...args)) as FetchLike,
      credentials: 'same-origin',
      refreshCredentials: 'same-origin',
    })
  }
  return client
}

function getAuthHeaders(): Record<string, string> {
  const headers: Record<string, string> = {}
  const token = localStorage.getItem(ACCESS_TOKEN_KEY)
  const organizationId = localStorage.getItem(ORGANIZATION_ID_KEY)
  if (token) headers.Authorization = `Bearer ${token}`
  if (organizationId) headers['X-Organization-Id'] = organizationId
  return headers
}

/**
 * HTTP desktop copy must finish inside Safari's original trusted key handler.
 * Keep this exceptional synchronous transport out of normal API actions.
 */
export function postDuringClipboardGesture<T>(path: string, body: unknown, context: Window): T {
  const win = context as Window & typeof globalThis
  const xhr = new win.XMLHttpRequest()
  xhr.open('POST', `${getApiBaseUrl()}${path}`, false)
  for (const [key, value] of Object.entries({
    'Content-Type': 'application/json',
    ...getAuthHeaders(),
  })) {
    xhr.setRequestHeader(key, value)
  }
  xhr.send(JSON.stringify(body))
  // Never refresh/retry a non-idempotent selection copy inside this gesture.
  if (xhr.status < 200 || xhr.status >= 300) {
    throw new ApiRequestError(xhr.status, 'Unable to copy the desktop selection')
  }
  return JSON.parse(xhr.responseText) as T
}

export function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  return getApiClient().requestWithCredentials<T>('GET', path, undefined, 'same-origin', signal)
}

export function post<T>(
  path: string,
  body?: unknown,
  credentials?: RequestCredentials,
  signal?: AbortSignal,
): Promise<T> {
  return getApiClient().postWithCredentials<T>(path, body, credentials, signal)
}

export function del<T>(path: string, credentials?: RequestCredentials): Promise<T> {
  return getApiClient().deleteWithCredentials<T>(path, credentials)
}

export function patch<T>(path: string, body?: unknown): Promise<T> {
  return getApiClient().requestWithCredentials<T>('PATCH', path, body, 'same-origin')
}

export function put<T>(path: string, body?: unknown): Promise<T> {
  return getApiClient().requestWithCredentials<T>('PUT', path, body, 'same-origin')
}

/** Fetch JSON and return HTTP status without throwing for non-2xx responses. */
export function requestWithStatus<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<{ ok: boolean; status: number; data: T }> {
  return getApiClient().requestWithStatus<T>(method, path, body, undefined, 'same-origin')
}
