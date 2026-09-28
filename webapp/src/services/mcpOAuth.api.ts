import type { McpOAuthConnectOut, McpOAuthStatus } from '@/types'
import { getConfig } from './config'

function requestHeaders(): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const token = localStorage.getItem('kern_access_token')
  const orgId = localStorage.getItem('kern_active_org_id')
  if (token) headers.Authorization = `Bearer ${token}`
  if (orgId) headers['X-Organization-Id'] = orgId
  return headers
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${getConfig().apiBaseUrl}${path}`, {
    ...init,
    credentials: 'include',
    headers: { ...requestHeaders(), ...init.headers },
  })
  if (response.status === 204) return undefined as T
  const body = await response.json()
  if (!response.ok) {
    throw new Error(typeof body.detail === 'string' ? body.detail : 'OAuth request failed')
  }
  return body as T
}

function oauthPath(pluginId: string, serverId: string): string {
  return `/mcp-oauth/${encodeURIComponent(pluginId)}/mcp-servers/${encodeURIComponent(serverId)}/oauth`
}

export function getMcpOAuthStatus(pluginId: string, serverId: string): Promise<McpOAuthStatus> {
  return request(`${oauthPath(pluginId, serverId)}/status/`)
}

export function startMcpOAuth(
  pluginId: string,
  serverId: string,
  serviceId: string,
  organizationCredential: boolean,
): Promise<McpOAuthConnectOut> {
  return request(`${oauthPath(pluginId, serverId)}/connect/`, {
    method: 'POST',
    body: JSON.stringify({ service_id: serviceId, organization_credential: organizationCredential }),
  })
}

export function disconnectMcpOAuth(
  pluginId: string,
  serverId: string,
  serviceId: string,
  organizationCredential: boolean,
): Promise<void> {
  const query = new URLSearchParams({
    service_id: serviceId,
    organization_credential: String(organizationCredential),
  })
  return request(`${oauthPath(pluginId, serverId)}/disconnect/?${query}`, { method: 'DELETE' })
}

/** Reject anything except a credential-free HTTPS authorization endpoint. */
export function validateAuthorizationUrl(value: string): string {
  const url = new URL(value)
  if (url.protocol !== 'https:' || !url.hostname || url.username || url.password) {
    throw new Error('The OAuth provider returned an unsafe authorization URL.')
  }
  return url.toString()
}

/** Navigate this browser tab so the flow-binding cookie reaches the callback. */
export function navigateToAuthorization(url: string): void {
  window.location.assign(validateAuthorizationUrl(url))
}
