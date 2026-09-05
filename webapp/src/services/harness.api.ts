/**
 * Harness REST API service (M7, additive).
 *
 * Wraps the M6 backend contracts in `backend/apps/harness/api.py`:
 * session list/create, follow-up message, abort, message parts, todos,
 * and permission resolution (`once|always|reject`).
 */

import type {
  HarnessConversation,
  HarnessMessage,
  HarnessMessageIn,
  HarnessPermissionRequest,
  HarnessPermissionResponse,
  HarnessSession,
  HarnessSessionCreateIn,
  HarnessSessionMode,
  HarnessSessionPatchIn,
  HarnessTodo,
} from '@/types/harness'
import { get, post, put, del, patch } from './api'

export interface HarnessProviderConfig {
  base_url: string
  default_model: string
  small_model: string
  has_api_key: boolean
  api_key_hint: string
}

export interface HarnessProviderConfigIn {
  api_key: string
  base_url?: string
  default_model?: string
  small_model?: string
}

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
    skill_ids: data.skill_ids ?? [],
  })
}

export function sendHarnessMessage(
  sessionId: string,
  data: HarnessMessageIn | string,
): Promise<HarnessSession> {
  const body =
    typeof data === 'string'
      ? { prompt: data }
      : {
          prompt: data.prompt,
          mode: data.mode ?? '',
          model: data.model ?? '',
          skill_ids: data.skill_ids ?? [],
        }
  return post<HarnessSession>(`/harness/sessions/${sessionId}/message`, body)
}

export function patchHarnessSession(
  sessionId: string,
  data: HarnessSessionPatchIn,
): Promise<HarnessSession> {
  return patch<HarnessSession>(`/harness/sessions/${sessionId}`, data)
}

export function deleteHarnessSession(sessionId: string): Promise<void> {
  return del<void>(`/harness/sessions/${sessionId}`)
}

export function setSessionMode(
  sessionId: string,
  mode: HarnessSessionMode,
): Promise<HarnessSession> {
  return patch<HarnessSession>(`/harness/sessions/${sessionId}/mode`, { mode })
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

export function resolveHarnessQuestion(
  sessionId: string,
  requestId: string,
  answers: string[],
  reject = false,
): Promise<{ request_id: string; status: string }> {
  return post<{ request_id: string; status: string }>(
    `/harness/sessions/${sessionId}/questions/${requestId}`,
    { answers, reject },
  )
}

export function getProviderConfig(): Promise<HarnessProviderConfig> {
  return get<HarnessProviderConfig>('/provider-config/')
}

export function saveProviderConfig(
  data: HarnessProviderConfigIn,
): Promise<HarnessProviderConfig> {
  return put<HarnessProviderConfig>('/provider-config/', {
    api_key: data.api_key ?? '',
    base_url: data.base_url ?? '',
    default_model: data.default_model ?? '',
    small_model: data.small_model ?? '',
  })
}

export function deleteProviderConfig(): Promise<void> {
  return del<void>('/provider-config/')
}

export function listHarnessConversations(): Promise<HarnessConversation[]> {
  return get<HarnessConversation[]>('/harness/conversations/')
}

export function markHarnessSessionRead(sessionId: string): Promise<void> {
  return post<void>(`/harness/sessions/${sessionId}/read`)
}
