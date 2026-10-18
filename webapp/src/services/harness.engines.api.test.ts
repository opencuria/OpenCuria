import { afterEach, describe, expect, it, vi } from 'vitest'
import * as api from './api'
import {
  createHarnessSession,
  deleteClaudeConnection,
  getClaudeConnection,
  listClaudeModels,
  listHarnessEngines,
  saveClaudeConnection,
  createProviderAccount,
  deleteProviderAccount,
  listProviderAccounts,
  selectProviderAccount,
  startChatGptOAuth,
  getChatGptOAuthStatus,
  cancelChatGptOAuth,
  updateProviderAccount,
} from './harness.api'

const engines = [
  { id: 'native', name: 'OpenCuria', modes: ['build', 'plan'], connected: true },
  { id: 'claude', name: 'Claude Agent', modes: ['build', 'plan'], connected: false },
]
const models = [
  {
    id: 'sonnet',
    name: 'Claude Sonnet',
    provider: 'claude',
    reasoning_efforts: ['high'],
    default_effort: 'high',
    supports_tools: true,
    context_length: 200_000,
    max_output_tokens: 16_000,
  },
]

afterEach(() => vi.restoreAllMocks())

describe('harness engine API', () => {
  it('lists engines and keeps Claude models on their dedicated catalog endpoint', async () => {
    const get = vi.spyOn(api, 'get').mockResolvedValueOnce(engines).mockResolvedValueOnce(models)
    await expect(listHarnessEngines()).resolves.toEqual(engines)
    await expect(listClaudeModels()).resolves.toEqual(models)
    expect(get).toHaveBeenNthCalledWith(1, '/harness/engines/')
    expect(get).toHaveBeenNthCalledWith(2, '/harness/engines/claude/models/')
  })

  it('persists the selected engine only when creating a Claude session', async () => {
    const post = vi.spyOn(api, 'post').mockResolvedValue({ id: 'session-1' })
    await createHarnessSession('ws-1', { prompt: 'hello', harness_id: 'claude', mode: 'plan' })
    expect(post).toHaveBeenCalledWith('/workspaces/ws-1/harness/sessions/', {
      prompt: 'hello',
      agent_name: 'build',
      mode: 'plan',
      model: '',
      reasoning_effort: '',
      skill_ids: [],
      harness_id: 'claude',
    })
    post.mockClear()
    await createHarnessSession('ws-1', { prompt: 'hello', harness_id: 'native' })
    expect(post).toHaveBeenCalledWith('/workspaces/ws-1/harness/sessions/', {
      prompt: 'hello',
      agent_name: 'build',
      mode: 'build',
      model: '',
      reasoning_effort: '',
      skill_ids: [],
      harness_id: 'native',
    })
  })

  it('reads and saves safe connection metadata without reshaping token payloads', async () => {
    const connection = {
      id: 'personal-1',
      auth_type: 'api_token' as const,
      label: 'Personal',
      connected: true,
      created_at: '2026-10-08T00:00:00Z',
      updated_at: '2026-10-08T00:00:00Z',
    }
    const get = vi.spyOn(api, 'get').mockResolvedValue(connection)
    const put = vi.spyOn(api, 'put').mockResolvedValue(connection)
    await expect(getClaudeConnection()).resolves.toEqual(connection)
    await expect(
      saveClaudeConnection({ auth_type: 'api_token', token: 'secret', label: 'Personal' }),
    ).resolves.toEqual(connection)
    expect(get).toHaveBeenCalledWith('/harness/engines/claude/connection/')
    expect(put).toHaveBeenCalledWith('/harness/engines/claude/connection/', {
      auth_type: 'api_token',
      token: 'secret',
      label: 'Personal',
    })
  })

  it('deletes the personal Claude connection', async () => {
    const del = vi.spyOn(api, 'del').mockResolvedValue(undefined)
    await expect(deleteClaudeConnection()).resolves.toBeUndefined()
    expect(del).toHaveBeenCalledWith('/harness/engines/claude/connection/')
  })

  it('uses account UUIDs for provider account changes and sends null for no selection', async () => {
    const get = vi.spyOn(api, 'get').mockResolvedValue([])
    const post = vi.spyOn(api, 'post').mockResolvedValue({ id: 'new', provider: 'claude-agent' })
    const put = vi.spyOn(api, 'put').mockResolvedValue(undefined)
    const del = vi.spyOn(api, 'del').mockResolvedValue(undefined)
    await listProviderAccounts()
    await createProviderAccount('claude-agent', {
      label: 'Personal',
      shared: false,
      auth_type: 'api_token',
      token: 'secret',
    })
    await updateProviderAccount('claude-agent', 'account-uuid', {
      label: 'Updated',
      shared: false,
      token: '',
    })
    await deleteProviderAccount('claude-agent', 'account-uuid')
    await selectProviderAccount('claude-agent', null)
    expect(get).toHaveBeenCalledWith('/provider-config/accounts/')
    expect(post).toHaveBeenCalledWith('/provider-config/accounts/claude-agent/', {
      label: 'Personal',
      shared: false,
      auth_type: 'api_token',
      token: 'secret',
    })
    expect(put).toHaveBeenCalledWith('/provider-config/accounts/claude-agent/account-uuid/', {
      label: 'Updated',
      shared: false,
      token: '',
    })
    expect(del).toHaveBeenCalledWith('/provider-config/accounts/claude-agent/account-uuid/')
    expect(put).toHaveBeenLastCalledWith('/provider-config/selection/claude-agent/', {
      account_id: null,
    })
  })

  it('scopes device OAuth status and cancel to the dialog flow id', async () => {
    const post = vi.spyOn(api, 'post').mockResolvedValue({ flow_id: 'flow-id' })
    const originalFetch = globalThis.fetch
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ status: 'connected', connection_id: 'target-uuid' }), {
        status: 200,
      }),
    )
    const flow = await startChatGptOAuth({ label: 'Work', shared: true, account_id: 'target-uuid' })
    expect(flow.flow_id).toBe('flow-id')
    await getChatGptOAuthStatus('flow-id/one')
    await cancelChatGptOAuth('flow-id/one')
    expect(post).toHaveBeenCalledWith('/provider-config/providers/chatgpt/oauth/start/', {
      label: 'Work',
      shared: true,
      account_id: 'target-uuid',
    })
    expect(fetchSpy).toHaveBeenCalledWith(
      expect.stringContaining(
        '/provider-config/providers/chatgpt/oauth/status/?flow_id=flow-id%2Fone',
      ),
      expect.objectContaining({ method: 'GET' }),
    )
    expect(post).toHaveBeenLastCalledWith('/provider-config/providers/chatgpt/oauth/cancel/', {
      flow_id: 'flow-id/one',
    })
    fetchSpy.mockRestore()
    globalThis.fetch = originalFetch
  })
})
