/**
 * Shared agent-config fetch so settings surfaces reuse one in-flight request.
 */

import { listAgentConfigs } from '@/services/harness.api'
import type { AgentConfig } from './harnessAgents'

let inflight: Promise<AgentConfig[]> | null = null

/** Load agent configs, coalescing concurrent and repeat callers. */
export function loadAgentConfigsCached(): Promise<AgentConfig[]> {
  if (inflight == null) {
    inflight = listAgentConfigs().catch((error: unknown) => {
      inflight = null
      throw error
    })
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
