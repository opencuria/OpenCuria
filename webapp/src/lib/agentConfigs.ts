/**
 * Shared agent-config fetch so settings surfaces reuse one in-flight request.
 */

import { listAgentConfigs } from '@/services/harness.api'
import type { AgentConfig } from './harnessAgents'
import { providerCatalogContext } from './providerCatalog'

let inflight: Promise<AgentConfig[]> | null = null
let cachedContext = ''

/** Load agent configs, coalescing concurrent and repeat callers. */
export function loadAgentConfigsCached(): Promise<AgentConfig[]> {
  const context = providerCatalogContext()
  if (inflight == null || cachedContext !== context) {
    cachedContext = context
    const request = listAgentConfigs().catch((error: unknown) => {
      if (inflight === request) inflight = null
      throw error
    })
    inflight = request
  }
  return inflight
}

/** Drop the cached promise so the next load refetches. */
export function invalidateAgentConfigs(): void {
  inflight = null
}

/** Drop the cached promise (tests only). */
export function resetAgentConfigsCache(): void {
  inflight = null
}
