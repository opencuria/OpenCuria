/**
 * Base HTTP client for the OpenCuria REST API.
 *
 * Uses native `fetch` — no extra dependencies needed.
 * Auth headers (JWT Bearer) and org context (X-Organization-Id)
 * are injected automatically from the auth store.
 */

import type { ApiError } from '@/types'
import { getConfig } from './config'

function getApiBaseUrl(): string {
  return getConfig().apiBaseUrl
}

// ---------------------------------------------------------------------------
// Auth headers — reads JWT + org from localStorage (avoids Pinia import cycle)
// ---------------------------------------------------------------------------

/**
 * Returns auth and org-context headers for every API request.
 *
 * Reads directly from localStorage to avoid circular Pinia imports.
 */
function getAuthHeaders(): Record<string, string> {
  const headers: Record<string, string> = {}

  const token = localStorage.getItem('kern_access_token')
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }

  const orgId = localStorage.getItem('kern_active_org_id')
  if (orgId) {
    headers['X-Organization-Id'] = orgId
  }

  return headers
}

// ---------------------------------------------------------------------------
// Token refresh logic
// ---------------------------------------------------------------------------

let refreshPromise: Promise<boolean> | null = null

export async function tryRefreshToken(): Promise<boolean> {
  // Use a shared promise so multiple concurrent 401s don't trigger
  // multiple refresh calls.
  if (refreshPromise) return refreshPromise

  refreshPromise = (async () => {
    const refreshToken = localStorage.getItem('kern_refresh_token')
    if (!refreshToken) return false

    try {
      const res = await fetch(`${getApiBaseUrl()}/auth/refresh/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refreshToken }),
      })

      if (!res.ok) return false

      const data = await res.json()
      localStorage.setItem('kern_access_token', data.access_token)
      localStorage.setItem('kern_refresh_token', data.refresh_token)
      return true
    } catch {
      return false
    }
  })()

  const result = await refreshPromise
  refreshPromise = null
  return result
}

// ---------------------------------------------------------------------------
// Generic request helper
// ---------------------------------------------------------------------------

export class ApiRequestError extends Error {
  status: number
  code: string

  constructor(status: number, detail: string, code: string = 'error') {
    super(detail)
    this.name = 'ApiRequestError'
    this.status = status
    this.code = code
  }
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const url = `${getApiBaseUrl()}${path}`

  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...getAuthHeaders(),
  }

  let res = await fetch(url, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })

  // On 401, try refreshing the token and retry once
  if (res.status === 401) {
    const refreshed = await tryRefreshToken()
    if (refreshed) {
      const retryHeaders: Record<string, string> = {
        'Content-Type': 'application/json',
        ...getAuthHeaders(),
      }
      res = await fetch(url, {
        method,
        headers: retryHeaders,
        body: body !== undefined ? JSON.stringify(body) : undefined,
      })
    }

    // Still 401 after refresh — clear auth and redirect to login
    if (res.status === 401) {
      localStorage.removeItem('kern_access_token')
      localStorage.removeItem('kern_refresh_token')
      localStorage.removeItem('kern_active_org_id')
      window.location.href = '/login'
      throw new ApiRequestError(401, 'Session expired', 'authentication_error')
    }
  }

  // 204 No Content
  if (res.status === 204) {
    return undefined as T
  }

  const data = await res.json()

  if (!res.ok) {
    const err = data as ApiError
    throw new ApiRequestError(res.status, err.detail ?? 'Unknown error', err.code)
  }

  return data as T
}

// ---------------------------------------------------------------------------
// Convenience methods
// ---------------------------------------------------------------------------

export function get<T>(path: string): Promise<T> {
  return request<T>('GET', path)
}

export function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>('POST', path, body)
}

export function del<T>(path: string): Promise<T> {
  return request<T>('DELETE', path)
}

export function patch<T>(path: string, body?: unknown): Promise<T> {
  return request<T>('PATCH', path, body)
}
