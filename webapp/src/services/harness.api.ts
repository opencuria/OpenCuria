/** Shared chat API bindings and web settings endpoints. */

import { createHarnessApi } from '@opencuria/client/harness'
import { createAgentSettingsApi, createProviderAccountsApi } from '@opencuria/client/settings'
import { getApiClient } from './api'

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

export type { ComputerUseConfig, ProviderAccount, ProviderAccountInput } from '@opencuria/client'

/** Notify mounted chat surfaces after an account or selection mutation. */
export function notifyProviderAccountsChanged(): void {
  if (typeof window === 'undefined') return
  window.dispatchEvent(new Event(PROVIDER_ACCOUNTS_CHANGED_EVENT))
}

export const {
  list: listProviderAccounts,
  create: createProviderAccount,
  update: updateProviderAccount,
  delete: deleteProviderAccount,
  select: selectProviderAccount,
  startChatGptOAuth,
  chatGptOAuthStatus: getChatGptOAuthStatus,
  cancelChatGptOAuth,
} = createProviderAccountsApi(getApiClient)

export const {
  getSubagentConfig,
  saveSubagentConfig,
  getComputerUseConfig,
  saveComputerUseConfig,
} = createAgentSettingsApi(getApiClient)
