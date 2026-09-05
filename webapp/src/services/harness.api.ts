/**
 * Harness REST API service (M7, additive).
 *
 * Wraps the M6 backend contracts in `backend/apps/harness/api.py`:
 * session list/create, follow-up message, abort, message parts, todos,
 * and permission resolution (`once|always|reject`).
 */

import type {
  HarnessMessage,
  HarnessPermissionRequest,
  HarnessPermissionResponse,
  HarnessSession,
  HarnessSessionCreateIn,
  HarnessTodo,
} from '@/types/harness'
import { get, post } from './api'

export interface HarnessSessionOut extends HarnessSession {}

export interface HarnessPartsResponse {
  session: HarnessSession
  messages: HarnessMessage[]
}

export interface HarnessPermissionOut {
  request_id: string
  decision: string
  remember: string
}

export function listHarnessSessions(workspaceId: string): Promise<HarnessSession[]> {
  return get<HarnessSession[]>(`/workspaces/${workspaceId}/harness/sessions/`)
}

export function createHarnessSession(
  workspaceId: string,
  data: HarnessSessionCreateIn,
): Promise<HarnessSession> {
  return post<HarnessSession>(`/workspaces/${workspaceId}/harness/sessions/`, {
    prompt: data.prompt,
    agent_name: data.agent_name ?? 'build',
    mode: data.mode ?? 'build',
    model: data.model ?? '',
  })
}

export function sendHarnessMessage(
  sessionId: string,
  prompt: string,
): Promise<HarnessSession> {
  return post<HarnessSession>(`/harness/sessions/${sessionId}/message`, { prompt })
}

export function abortHarnessSession(sessionId: string): Promise<HarnessSession> {
  return post<HarnessSession>(`/harness/sessions/${sessionId}/abort`)
}

export function listHarnessParts(sessionId: string): Promise<HarnessPartsResponse> {
  return get<HarnessPartsResponse>(`/harness/sessions/${sessionId}/parts`)
}

export function listHarnessTodos(sessionId: string): Promise<HarnessTodo[]> {
  return get<HarnessTodo[]>(`/harness/sessions/${sessionId}/todos`)
}

export function resolveHarnessPermission(
  sessionId: string,
  request: HarnessPermissionRequest,
  response: HarnessPermissionResponse,
): Promise<HarnessPermissionOut> {
  return post<HarnessPermissionOut>(
    `/harness/sessions/${sessionId}/permissions/${request.request_id}`,
    { response },
  )
}
