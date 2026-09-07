/**
 * Shared OpenRouter catalog fetch so chat surfaces reuse one in-flight request.
 */

import { listProviderModels } from '@/services/harness.api'
import type { ProviderModel } from './harnessModels'

let inflight: Promise<ProviderModel[]> | null = null

/** Load provider models, coalescing concurrent and repeat callers. */
export function loadProviderModelsCached(): Promise<ProviderModel[]> {
  if (inflight == null) {
    inflight = listProviderModels().catch((error: unknown) => {
      inflight = null
      throw error
    })
  }
  return inflight
}

/** Drop the cached promise (tests only). */
export function resetProviderCatalogCache(): void {
  inflight = null
}
