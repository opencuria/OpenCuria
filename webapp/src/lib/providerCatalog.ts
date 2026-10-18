/**
 * Shared provider catalog fetch so chat surfaces reuse one in-flight request.
 */

import { HARNESS_ENGINE_CONNECTION_CHANGED_EVENT, listProviderModels } from '@/services/harness.api'
import type { ProviderModel } from './harnessModels'

interface CatalogCacheEntry {
  context: string
  promise: Promise<ProviderModel[]>
}

let cached: CatalogCacheEntry | null = null

function currentContext(): string {
  const userId = (() => {
    try {
      const user = JSON.parse(localStorage.getItem('kern_user') ?? 'null') as {
        id?: string | number
      } | null
      return user?.id == null ? '' : String(user.id)
    } catch {
      return ''
    }
  })()
  return `${userId}:${localStorage.getItem('kern_active_org_id') ?? ''}`
}

/** Load provider models, coalescing concurrent and repeat callers per organization. */
export function loadProviderModelsCached(): Promise<ProviderModel[]> {
  const context = currentContext()
  if (cached?.context === context) return cached.promise

  const entry: CatalogCacheEntry = {
    context,
    promise: listProviderModels(),
  }
  cached = entry
  entry.promise = entry.promise.catch((error: unknown) => {
    if (cached === entry) cached = null
    throw error
  })
  return entry.promise
}

/** Drop the cached promise so the next load refetches. */
export function invalidateProviderCatalog(): void {
  cached = null
}

if (typeof window !== 'undefined') {
  window.addEventListener(HARNESS_ENGINE_CONNECTION_CHANGED_EVENT, invalidateProviderCatalog)
}

/** Clear user-context provider data when auth state is discarded. */
export function resetProviderCatalogCache(): void {
  cached = null
}

/** Refresh mounted chat surfaces after user or organization context changes. */
export function notifyProviderCatalogContextChanged(): void {
  resetProviderCatalogCache()
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new Event(HARNESS_ENGINE_CONNECTION_CHANGED_EVENT))
  }
}
