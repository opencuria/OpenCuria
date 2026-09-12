/**
 * Harness REST API service (M7, additive).
 *
 * Wraps the M6 backend contracts in `backend/apps/harness/api.py`:
 * session list/create, follow-up message, abort, message parts, todos,
 * and permission resolution (`once|always|reject`).
 */

import type {
  HarnessConversation,
  HarnessForkIn,
  HarnessMessage,
  HarnessMessageEditIn,
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
import type { AgentConfig as LibAgentConfig } from '@/lib/harnessAgents'
import { ApiRequestError, get, post, put, del, patch, requestWithStatus } from './api'

export type AgentConfig = LibAgentConfig

export interface AgentConfigIn {
  agent: string
  description?: string
  model?: string
  effort?: string
  inherit_model?: boolean
  effort_strategy?: string
}

export interface HarnessProviderConfig {
  base_url: string
  /** @deprecated Replaced by GET /agent-configs/ (agent model/effort settings). */
  default_model: string
  /** @deprecated Replaced by GET /agent-configs/ (agent model/effort settings). */
  small_model: string
  /** @deprecated Replaced by GET /agent-configs/ (agent model/effort settings). */
  computer_use_model: string
  /** @deprecated Replaced by GET /agent-configs/ (agent model/effort settings). */
  default_effort: string
  /** @deprecated Replaced by GET /agent-configs/ (agent model/effort settings). */
  small_effort: string
  /** @deprecated Replaced by GET /agent-configs/ (agent model/effort settings). */
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
  /** Manual model catalog for openai-compatible endpoints without /models. */
  models?: string[]
}

export interface ProviderConnectionUpsertIn {
  api_key?: string
  base_url?: string
  /** Manual model ids (openai-compatible only; explicit [] clears the list). */
  models?: string[]
  auth_method?: string
  region?: string
  access_key_id?: string
  secret_access_key?: string
  session_token?: string
  bearer_token?: string
}

/**
 * Org-wide Agent-S harness parameters (`GET/PUT /agent-s-config/`).
 *
 * `AgentConfig['computeruse']` stays the source for the Agent-S main
 * model/effort; this row only holds harness behavior values plus the
 * separate grounding model. `grounding_model` empty falls back to the
 * run's main model; `model_temperature` null means provider default.
 */
export interface AgentSConfig {
  grounding_model: string
  grounding_width: number
  grounding_height: number
  model_temperature: number | null
  max_steps: number
  max_trajectory_length: number
  enable_reflection: boolean
  enable_code_agent: boolean
  screenshot_max_dimension: number
  action_pre_delay: number
  action_post_delay: number
  wait_delay: number
}

/**
 * Partial Agent-S save payload. The backend merges partial payloads over
 * stored values (or defaults when unstored), so callers send only changed
 * fields. Unknown keys are ignored server-side.
 */
export type AgentSConfigIn = Partial<AgentSConfig>

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

/**
 * Fork a harness session (read-only, works while busy).
 * Mirrors `POST /harness/sessions/{id}/fork` (201 Session).
 */
export function forkHarnessSession(
  sessionId: string,
  data: HarnessForkIn = {},
): Promise<HarnessSession> {
  const body: Record<string, string> = {}
  if (data.message_id) body.message_id = data.message_id
  return post<HarnessSession>(`/harness/sessions/${sessionId}/fork`, body)
}

/**
 * Edit a user message and rerun the session from there.
 * Mirrors `POST /harness/sessions/{id}/messages/{messageId}/edit` (202 Session).
 */
export function editHarnessMessage(
  sessionId: string,
  messageId: string,
  data: HarnessMessageEditIn,
): Promise<HarnessSession> {
  return post<HarnessSession>(`/harness/sessions/${sessionId}/messages/${messageId}/edit`, {
    prompt: data.prompt,
    mode: data.mode ?? '',
    model: data.model ?? '',
    reasoning_effort: data.reasoning_effort ?? '',
    skill_ids: data.skill_ids ?? [],
  })
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

export function listAgentConfigs(): Promise<AgentConfig[]> {
  return get<AgentConfig[]>('/agent-configs/')
}

export function saveAgentConfigs(configs: AgentConfigIn[]): Promise<AgentConfig[]> {
  return put<AgentConfig[]>('/agent-configs/', { configs })
}

export function getProviderConfig(): Promise<HarnessProviderConfig> {
  return get<HarnessProviderConfig>('/provider-config/')
}

export function saveProviderConfig(data: HarnessProviderConfigIn): Promise<HarnessProviderConfig> {
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

/**
 * Load the org-wide Agent-S harness config (defaults when unstored).
 * Mirrors `GET /agent-s-config/`.
 */
export function getAgentSConfig(): Promise<AgentSConfig> {
  return get<AgentSConfig>('/agent-s-config/')
}

/**
 * Save (upsert) the org-wide Agent-S harness config.
 * Accepts a partial payload; the backend merges it over stored values
 * (or defaults when unstored). Mirrors `PUT /agent-s-config/`.
 */
export function saveAgentSConfig(data: AgentSConfigIn): Promise<AgentSConfig> {
  return put<AgentSConfig>('/agent-s-config/', data)
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
