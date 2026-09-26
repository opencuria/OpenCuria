/**
 * Shared provider catalog fetch so chat surfaces reuse one in-flight request.
 */

import { listProviderModels } from '@/services/harness.api'
import type { ProviderModel } from './harnessModels'

interface CatalogCacheEntry {
  context: string
  promise: Promise<ProviderModel[]>
}

let cached: CatalogCacheEntry | null = null

function currentContext(): string {
  return localStorage.getItem('kern_active_org_id') ?? ''
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

/** Drop the cached promise (tests only). */
export function resetProviderCatalogCache(): void {
  cached = null
}
