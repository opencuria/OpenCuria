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
  HarnessQuestionRequest,
  HarnessSession,
  HarnessSessionCreateIn,
  HarnessSessionMode,
  HarnessSessionPatchIn,
  HarnessTodo,
} from '@/types/harness'
import type { ProviderId, ProviderModel } from '@/lib/harnessModels'
import { ApiRequestError, get, post, put, del, patch, requestWithStatus } from './api'

export interface HarnessProviderConfig {
  base_url: string
  default_model: string
  small_model: string
  computer_use_model: string
  default_effort: string
  small_effort: string
  computer_use_effort: string
  has_api_key: boolean
  api_key_hint: string
}

export interface HarnessProviderConfigIn {
  api_key?: string
  base_url?: string
  default_model?: string
  small_model?: string
  computer_use_model?: string
  default_effort?: string
  small_effort?: string
  computer_use_effort?: string
}

export interface ProviderConnection {
  provider: ProviderId
  connected: boolean
  base_url?: string
  api_key_hint?: string
  account_id?: string
  region?: string
  auth_method?: 'access_keys' | 'bearer' | ''
}

export interface ProviderConnectionUpsertIn {
  api_key?: string
  base_url?: string
  auth_method?: string
  region?: string
  access_key_id?: string
  secret_access_key?: string
  session_token?: string
  bearer_token?: string
}

export interface ChatGptOAuthStart {
  user_code: string
  verification_url: string
  interval: number
  expires_in: number
}

export interface ChatGptOAuthStatus {
  status: 'pending' | 'connected' | 'expired' | 'denied' | 'no_flow'
  account_id?: string
}

export interface HarnessSessionOut extends HarnessSession {}

export interface HarnessPartsResponse {
  session: HarnessSession
  messages: HarnessMessage[]
  permissions?: HarnessPermissionRequest[]
  questions?: HarnessQuestionRequest[]
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
    reasoning_effort: data.reasoning_effort ?? '',
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
          reasoning_effort: data.reasoning_effort ?? '',
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

export function listProviderModels(): Promise<ProviderModel[]> {
  return get<ProviderModel[]>('/provider-config/models/')
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
    computer_use_model: data.computer_use_model ?? '',
    default_effort: data.default_effort ?? '',
    small_effort: data.small_effort ?? '',
    computer_use_effort: data.computer_use_effort ?? '',
  })
}

export function deleteProviderConfig(): Promise<void> {
  return del<void>('/provider-config/')
}

export function listProviderConnections(): Promise<ProviderConnection[]> {
  return get<ProviderConnection[]>('/provider-config/providers/')
}

export function saveProviderConnection(
  provider: ProviderId,
  payload: ProviderConnectionUpsertIn,
): Promise<ProviderConnection> {
  return put<ProviderConnection>(`/provider-config/providers/${provider}/`, payload)
}

export function deleteProviderConnection(provider: ProviderId): Promise<void> {
  return del<void>(`/provider-config/providers/${provider}/`)
}

export function startChatGptOAuth(): Promise<ChatGptOAuthStart> {
  return post<ChatGptOAuthStart>('/provider-config/providers/chatgpt/oauth/start/')
}

export async function getChatGptOAuthStatus(): Promise<ChatGptOAuthStatus> {
  const result = await requestWithStatus<ChatGptOAuthStatus>(
    'GET',
    '/provider-config/providers/chatgpt/oauth/status/',
  )
  if (result.status === 404) {
    return { status: 'no_flow', account_id: result.data.account_id ?? '' }
  }
  if (result.status === 410) {
    return {
      status: result.data.status === 'denied' ? 'denied' : 'expired',
      account_id: result.data.account_id ?? '',
    }
  }
  if (!result.ok) {
    throw new ApiRequestError(
      result.status,
      typeof result.data === 'object' && result.data && 'detail' in result.data
        ? String((result.data as { detail?: string }).detail)
        : 'OAuth status request failed',
      typeof result.data === 'object' && result.data && 'code' in result.data
        ? String((result.data as { code?: string }).code)
        : 'error',
    )
  }
  return result.data
}

export function cancelChatGptOAuth(): Promise<void> {
  return post<void>('/provider-config/providers/chatgpt/oauth/cancel/')
}

export function listHarnessConversations(): Promise<HarnessConversation[]> {
  return get<HarnessConversation[]>('/harness/conversations/')
}

export function markHarnessSessionRead(sessionId: string): Promise<void> {
  return post<void>(`/harness/sessions/${sessionId}/read`)
}

export function markHarnessSessionUnread(sessionId: string): Promise<void> {
  return post<void>(`/harness/sessions/${sessionId}/unread`)
}
