import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

import ProviderConnectionDialog from './ProviderConnectionDialog.vue'
import * as harnessApi from '@/services/harness.api'
import type { ProviderId } from '@/lib/harnessModels'
import type { ProviderConnection } from '@/services/harness.api'

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return {
    ...actual,
    saveProviderConnection: vi.fn(),
    deleteProviderConnection: vi.fn(),
    startChatGptOAuth: vi.fn(),
    getChatGptOAuthStatus: vi.fn(),
    cancelChatGptOAuth: vi.fn(),
  }
})

const saveProviderConnectionMock = vi.mocked(harnessApi.saveProviderConnection)
const deleteProviderConnectionMock = vi.mocked(harnessApi.deleteProviderConnection)
const startChatGptOAuthMock = vi.mocked(harnessApi.startChatGptOAuth)
const getChatGptOAuthStatusMock = vi.mocked(harnessApi.getChatGptOAuthStatus)
const cancelChatGptOAuthMock = vi.mocked(harnessApi.cancelChatGptOAuth)

const dialogStubs = {
  Dialog: {
    template: '<div><slot /></div>',
    props: ['open'],
  },
  DialogContent: { template: '<div><slot /></div>' },
  DialogHeader: { template: '<div><slot /></div>' },
  DialogTitle: { template: '<div><slot /></div>' },
  DialogDescription: { template: '<div><slot /></div>' },
  DialogBody: { template: '<div><slot /></div>' },
  DialogFooter: { template: '<div><slot /></div>' },
}

function mountDialog(provider: ProviderId, connection?: ProviderConnection) {
  return mount(ProviderConnectionDialog, {
    props: { open: true, provider, connection },
    global: { stubs: dialogStubs },
  })
}

describe('ProviderConnectionDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    saveProviderConnectionMock.mockResolvedValue({
      provider: 'openrouter',
      connected: true,
      base_url: 'https://openrouter.ai/api/v1',
      api_key_hint: '••••wxyz',
    })
    deleteProviderConnectionMock.mockResolvedValue(undefined)
    startChatGptOAuthMock.mockResolvedValue({
      user_code: 'ABCD-1234',
      verification_url: 'https://auth.openai.com/codex/device',
      interval: 1,
      expires_in: 600,
    })
    getChatGptOAuthStatusMock.mockResolvedValue({ status: 'pending' })
    cancelChatGptOAuthMock.mockResolvedValue(undefined)
  })

  it('shows a unified status summary for a connected provider', async () => {
    const wrapper = mountDialog('openrouter', {
      provider: 'openrouter',
      connected: true,
      base_url: 'https://openrouter.ai/api/v1',
      api_key_hint: '••••cdef',
    })
    await flushPromises()

    const status = wrapper.find('[data-testid="connection-status"]')
    expect(status.exists()).toBe(true)
    expect(status.text()).toContain('Connected')
    expect(status.text()).toContain('••••cdef')
  })

  it('saves the OpenRouter connection and emits changed', async () => {
    const wrapper = mountDialog('openrouter', {
      provider: 'openrouter',
      connected: true,
      base_url: 'https://openrouter.ai/api/v1',
      api_key_hint: '••••cdef',
    })
    await flushPromises()

    await wrapper.find('#openrouter-api-key').setValue('sk-or-new')
    await wrapper.find('[data-testid="save-openrouter"]').trigger('click')
    await flushPromises()

    expect(saveProviderConnectionMock).toHaveBeenCalledWith('openrouter', {
      api_key: 'sk-or-new',
      base_url: 'https://openrouter.ai/api/v1',
    })
    expect(wrapper.emitted('changed')).toHaveLength(1)
  })

  it('runs ChatGPT OAuth polling until connected', async () => {
    vi.useFakeTimers()
    getChatGptOAuthStatusMock
      .mockResolvedValueOnce({ status: 'pending' })
      .mockResolvedValueOnce({ status: 'connected', account_id: 'acct-42' })

    const wrapper = mountDialog('chatgpt', { provider: 'chatgpt', connected: false })
    await flushPromises()

    await wrapper.find('[data-testid="chatgpt-connect"]').trigger('click')
    await flushPromises()

    expect(startChatGptOAuthMock).toHaveBeenCalled()
    expect(wrapper.find('[data-testid="chatgpt-user-code"]').text()).toBe('ABCD-1234')
    expect(wrapper.text()).toContain('Waiting for authorization')

    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()
    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()

    expect(getChatGptOAuthStatusMock).toHaveBeenCalled()
    expect(wrapper.emitted('connected')).toHaveLength(1)
    expect(wrapper.find('[data-testid="connection-status"]').text()).toContain('acct-42')
    vi.useRealTimers()
  })

  it('cancels a pending ChatGPT OAuth flow when the dialog closes', async () => {
    const wrapper = mountDialog('chatgpt', { provider: 'chatgpt', connected: false })
    await flushPromises()

    await wrapper.find('[data-testid="chatgpt-connect"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="chatgpt-user-code"]').exists()).toBe(true)

    await wrapper.setProps({ open: false })
    await flushPromises()

    expect(cancelChatGptOAuthMock).toHaveBeenCalled()
  })

  it('shows ChatGPT OAuth error state on denied status', async () => {
    vi.useFakeTimers()
    getChatGptOAuthStatusMock.mockResolvedValue({ status: 'denied' })

    const wrapper = mountDialog('chatgpt', { provider: 'chatgpt', connected: false })
    await flushPromises()

    await wrapper.find('[data-testid="chatgpt-connect"]').trigger('click')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()

    expect(wrapper.text()).toContain('denied')
    vi.useRealTimers()
  })

  it('switches Bedrock auth method tabs and saves bearer payload', async () => {
    saveProviderConnectionMock.mockResolvedValue({
      provider: 'amazon-bedrock',
      connected: true,
      region: 'us-west-2',
      auth_method: 'bearer',
    })

    const wrapper = mountDialog('amazon-bedrock', {
      provider: 'amazon-bedrock',
      connected: true,
      region: 'us-east-1',
      auth_method: 'bearer',
    })
    await flushPromises()

    expect(wrapper.find('[data-testid="bedrock-tab-access-keys"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="bedrock-tab-bearer"]').exists()).toBe(true)
    const bearerInput = wrapper.find('#bedrock-bearer-token')
    expect(bearerInput.exists()).toBe(true)
    await bearerInput.setValue('token-abc')
    await wrapper.find('#bedrock-region').setValue('us-west-2')
    await wrapper.find('[data-testid="save-bedrock"]').trigger('click')
    await flushPromises()

    expect(saveProviderConnectionMock).toHaveBeenCalledWith('amazon-bedrock', {
      auth_method: 'bearer',
      region: 'us-west-2',
      bearer_token: 'token-abc',
    })
    expect(wrapper.emitted('changed')).toHaveLength(1)
  })

  it('disconnects a provider after confirmation', async () => {
    const wrapper = mountDialog('openrouter', {
      provider: 'openrouter',
      connected: true,
      base_url: 'https://openrouter.ai/api/v1',
      api_key_hint: '••••cdef',
    })
    await flushPromises()

    await wrapper.find('[data-testid="disconnect-openrouter"]').trigger('click')
    expect(wrapper.find('[data-testid="disconnect-confirm"]').text()).toContain(
      'Disconnect OpenRouter?',
    )
    await wrapper.find('[data-testid="confirm-disconnect-openrouter"]').trigger('click')
    await flushPromises()

    expect(deleteProviderConnectionMock).toHaveBeenCalledWith('openrouter')
    expect(wrapper.emitted('changed')).toHaveLength(1)
  })
})
