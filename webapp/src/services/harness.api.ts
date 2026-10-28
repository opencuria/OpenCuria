/** Shared chat API bindings and web settings endpoints. */

import { createHarnessApi } from '@opencuria/client/harness'
import type { ProviderId } from '@/lib/harnessModels'
import { ApiRequestError, get, getApiClient, post, put, del, requestWithStatus } from './api'

export type {
  AgentConfig,
  AgentConfigInput as AgentConfigIn,
  ProviderConfig as HarnessProviderConfig,
  RecentModelUsage,
} from '@opencuria/client'

export const {
  listConversations: listHarnessConversations,
  listSessions: listHarnessSessions,
  createSession: createHarnessSession,
  send: sendHarnessMessage,
  timeline: listHarnessParts,
  getPart: getHarnessPart,
  listTodos: listHarnessTodos,
  abort: abortHarnessSession,
  edit: editHarnessMessage,
  fork: forkHarnessSession,
  patchSession: patchHarnessSession,
  deleteSession: deleteHarnessSession,
  markRead: markHarnessSessionRead,
  markUnread: markHarnessSessionUnread,
  steer: steerHarnessSession,
  withdraw: withdrawHarnessSteer,
  dismissNotice: dismissHarnessNotice,
  resolvePermission: resolveHarnessPermission,
  resolveQuestion: resolveHarnessQuestion,
  setMode: setSessionMode,
  listEngines: listHarnessEngines,
  listEngineModels,
  listAgentConfigs,
  saveAgentConfigs,
  listRecentModels,
  saveRecentModel,
  getDefaultConfig: getProviderConfig,
  saveDefaultConfig: saveProviderConfig,
} = createHarnessApi(() => getApiClient())

export const HARNESS_ENGINE_CONNECTION_CHANGED_EVENT = 'harness:engine-connection-changed'
/** Shared model-catalog and engine-availability refresh signal. */
export const PROVIDER_ACCOUNTS_CHANGED_EVENT = HARNESS_ENGINE_CONNECTION_CHANGED_EVENT

export type AccountProviderId = ProviderId | 'claude-agent'

/** Safe, caller-visible provider account metadata. Secrets are never included. */
export interface ProviderAccount {
  id: string
  provider: AccountProviderId
  label: string
  scope: 'personal' | 'organization'
  connected: boolean
  is_active: boolean
  can_manage: boolean
  base_url?: string
  api_key_hint?: string
  account_id?: string
  region?: string
  auth_method?: string
  models?: string[]
  auth_type?: 'api_token' | 'subscription_token' | string
}

export interface ProviderAccountPayload {
  label: string
  shared: boolean
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
  token?: string
  auth_type?: 'api_token' | 'subscription_token'
}

/** Notify mounted chat surfaces after an account or selection mutation. */
export function notifyProviderAccountsChanged(): void {
  if (typeof window === 'undefined') return
  window.dispatchEvent(new Event(PROVIDER_ACCOUNTS_CHANGED_EVENT))
}

/** Org-wide Computer Use execution limits and optional desktop recording. */
export interface ComputerUseConfig {
  max_steps: number
  enable_recording: boolean
  screenshot_max_dimension: number
}

export type ComputerUseConfigIn = Partial<ComputerUseConfig>

export interface ChatGptOAuthStart {
  flow_id: string
  user_code: string
  verification_url: string
  interval: number
  expires_in: number
}

export interface ChatGptOAuthStatus {
  status: 'pending' | 'connected' | 'expired' | 'denied' | 'cancelled' | 'no_flow'
  account_id?: string
}

export interface ChatGptOAuthStartIn {
  label?: string
  shared?: boolean
  account_id?: string
}

/** List accounts visible in the current user's active organization context. */
export function listProviderAccounts(): Promise<ProviderAccount[]> {
  return get<ProviderAccount[]>('/provider-config/accounts/')
}

/** Create one named personal or admin-shared provider account. */
export function createProviderAccount(
  provider: AccountProviderId,
  payload: ProviderAccountPayload,
): Promise<ProviderAccount> {
  return post<ProviderAccount>(`/provider-config/accounts/${provider}/`, payload)
}

/** Update an account by its opaque account UUID. */
export function updateProviderAccount(
  provider: AccountProviderId,
  accountId: string,
  payload: ProviderAccountPayload,
): Promise<ProviderAccount> {
  return put<ProviderAccount>(
    `/provider-config/accounts/${provider}/${encodeURIComponent(accountId)}/`,
    payload,
  )
}

/** Delete one account by its opaque account UUID. */
export function deleteProviderAccount(
  provider: AccountProviderId,
  accountId: string,
): Promise<void> {
  return del<void>(`/provider-config/accounts/${provider}/${encodeURIComponent(accountId)}/`)
}

/** Select one visible account for this user/provider, or explicitly select none. */
export function selectProviderAccount(
  provider: AccountProviderId,
  accountId: string | null,
): Promise<void> {
  return put<void>(`/provider-config/selection/${provider}/`, { account_id: accountId })
}

/** Org-wide nesting limit, applied to new runs (default 2). */
export interface SubagentConfig {
  max_depth: number
}

/** Strict integer from 1 through 2147483647. */
export interface SubagentConfigIn {
  max_depth: number
}

export function getSubagentConfig(): Promise<SubagentConfig> {
  return get<SubagentConfig>('/subagent-config/')
}

export function saveSubagentConfig(data: SubagentConfigIn): Promise<SubagentConfig> {
  return put<SubagentConfig>('/subagent-config/', data)
}

/**
 * Load the org-wide Computer Use config (defaults when unstored).
 * Mirrors `GET /computer-use-config/`.
 */
export function getComputerUseConfig(): Promise<ComputerUseConfig> {
  return get<ComputerUseConfig>('/computer-use-config/')
}

/**
 * Save (upsert) the org-wide Computer Use config.
 * Accepts a partial payload; the backend merges it over stored values
 * (or defaults when unstored). Mirrors `PUT /computer-use-config/`.
 */
export function saveComputerUseConfig(data: ComputerUseConfigIn): Promise<ComputerUseConfig> {
  return put<ComputerUseConfig>('/computer-use-config/', data)
}

export function startChatGptOAuth(payload: ChatGptOAuthStartIn): Promise<ChatGptOAuthStart> {
  return post<ChatGptOAuthStart>('/provider-config/providers/chatgpt/oauth/start/', payload)
}

export async function getChatGptOAuthStatus(flowId: string): Promise<ChatGptOAuthStatus> {
  const result = await requestWithStatus<ChatGptOAuthStatus>(
    'GET',
    `/provider-config/providers/chatgpt/oauth/status/?flow_id=${encodeURIComponent(flowId)}`,
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

export function cancelChatGptOAuth(flowId: string): Promise<void> {
  return post<void>('/provider-config/providers/chatgpt/oauth/cancel/', { flow_id: flowId })
}
