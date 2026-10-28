import { afterEach, describe, expect, it, vi } from 'vitest'
import * as api from './api'
import {
  createProviderAccount,
  deleteProviderAccount,
  listProviderAccounts,
  selectProviderAccount,
  startChatGptOAuth,
  getChatGptOAuthStatus,
  cancelChatGptOAuth,
  updateProviderAccount,
} from './harness.api'

afterEach(() => vi.restoreAllMocks())

describe('provider account API', () => {
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
      new Response(JSON.stringify({ status: 'connected', account_id: 'account-42' }), {
        status: 200,
      }),
    )
    const flow = await startChatGptOAuth({ label: 'Work', shared: true, account_id: 'target-uuid' })
    expect(flow.flow_id).toBe('flow-id')
    await expect(getChatGptOAuthStatus('flow-id/one')).resolves.toEqual({
      status: 'connected',
      account_id: 'account-42',
    })
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
