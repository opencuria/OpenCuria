/**
 * Agent REST API service.
 */

import type { Agent } from '@/types'
import { get } from './api'

export function listAgents(workspaceId?: string): Promise<Agent[]> {
  const query = workspaceId ? `?workspace_id=${encodeURIComponent(workspaceId)}` : ''
  return get<Agent[]>(`/agents/${query}`)
}
