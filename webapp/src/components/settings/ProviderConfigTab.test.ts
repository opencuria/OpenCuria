import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

import ProviderConfigTab from './ProviderConfigTab.vue'
import * as harnessApi from '@/services/harness.api'
import * as providerCatalog from '@/lib/providerCatalog'
import type { ProviderModel } from '@/lib/harnessModels'

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return {
    ...actual,
    getProviderConfig: vi.fn(),
    saveProviderConfig: vi.fn(),
    listProviderConnections: vi.fn(),
    saveProviderConnection: vi.fn(),
    deleteProviderConnection: vi.fn(),
    startChatGptOAuth: vi.fn(),
    getChatGptOAuthStatus: vi.fn(),
    cancelChatGptOAuth: vi.fn(),
  }
})

const getProviderConfigMock = vi.mocked(harnessApi.getProviderConfig)
const saveProviderConfigMock = vi.mocked(harnessApi.saveProviderConfig)
const listProviderConnectionsMock = vi.mocked(harnessApi.listProviderConnections)
const saveProviderConnectionMock = vi.mocked(harnessApi.saveProviderConnection)
const deleteProviderConnectionMock = vi.mocked(harnessApi.deleteProviderConnection)
const startChatGptOAuthMock = vi.mocked(harnessApi.startChatGptOAuth)
const getChatGptOAuthStatusMock = vi.mocked(harnessApi.getChatGptOAuthStatus)
const cancelChatGptOAuthMock = vi.mocked(harnessApi.cancelChatGptOAuth)

const catalog: ProviderModel[] = [
  {
    id: 'openrouter/model-big',
    name: 'Big',
    provider: 'openrouter',
    reasoning_efforts: ['high'],
    default_effort: 'high',
    supports_tools: true,
    context_length: 128_000,
    max_output_tokens: 8_192,
  },
  {
    id: 'chatgpt/gpt-5',
    name: 'GPT 5',
    provider: 'chatgpt',
    reasoning_efforts: [],
    default_effort: '',
    supports_tools: true,
    context_length: 0,
    max_output_tokens: 0,
  },
]

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
  Popover: { template: '<div><slot /></div>' },
  PopoverTrigger: { template: '<div><slot /></div>' },
  PopoverContent: { template: '<div><slot /></div>' },
  Command: { template: '<div><slot /></div>' },
  CommandInput: { template: '<input />' },
  CommandList: { template: '<div><slot /></div>' },
  CommandEmpty: { template: '<div><slot /></div>' },
  CommandGroup: { template: '<div><slot /></div>' },
  CommandItem: {
    template: '<button type="button" @click="$emit(\'select\')"><slot /></button>',
  },
}

function mountTab() {
  return mount(ProviderConfigTab, {
    global: { stubs: dialogStubs },
  })
}

describe('ProviderConfigTab', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.spyOn(providerCatalog, 'loadProviderModelsCached').mockResolvedValue(catalog)
    getProviderConfigMock.mockResolvedValue({
      base_url: 'https://openrouter.ai/api/v1',
      default_model: 'openrouter/model-big',
      small_model: 'chatgpt/gpt-5',
      computer_use_model: 'openrouter/model-big',
      has_api_key: true,
      api_key_hint: '••••cdef',
    })
    listProviderConnectionsMock.mockResolvedValue([
      {
        provider: 'openrouter',
        connected: true,
        base_url: 'https://openrouter.ai/api/v1',
        api_key_hint: '••••cdef',
      },
      { provider: 'chatgpt', connected: false },
      { provider: 'amazon-bedrock', connected: false },
    ])
    saveProviderConfigMock.mockResolvedValue({
      base_url: 'https://openrouter.ai/api/v1',
      default_model: 'openrouter/model-big',
      small_model: 'chatgpt/gpt-5',
      computer_use_model: 'openrouter/model-big',
      has_api_key: true,
      api_key_hint: '••••cdef',
    })
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

  it('renders provider cards and default model pickers after load', async () => {
    const wrapper = mountTab()
    await flushPromises()

    expect(getProviderConfigMock).toHaveBeenCalled()
    expect(listProviderConnectionsMock).toHaveBeenCalled()
    expect(wrapper.find('[data-testid="provider-card-openrouter"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="provider-card-chatgpt"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="provider-card-amazon-bedrock"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="provider-default-model-trigger"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Connected')
    expect(wrapper.text()).toContain('Not connected')
  })

  it('saves default models through saveProviderConfig', async () => {
    const wrapper = mountTab()
    await flushPromises()

    await wrapper.find('[data-testid="save-default-models"]').trigger('click')
    await flushPromises()

    expect(saveProviderConfigMock).toHaveBeenCalledWith({
      default_model: 'openrouter/model-big',
      small_model: 'chatgpt/gpt-5',
      computer_use_model: 'openrouter/model-big',
    })
  })

  it('saves OpenRouter connection from the manage dialog', async () => {
    const wrapper = mountTab()
    await flushPromises()

    await wrapper.find('[data-testid="provider-manage-openrouter"]').trigger('click')
    await flushPromises()
    await wrapper.find('#openrouter-api-key').setValue('sk-or-new')
    await wrapper.find('[data-testid="save-openrouter"]').trigger('click')
    await flushPromises()

    expect(saveProviderConnectionMock).toHaveBeenCalledWith('openrouter', {
      api_key: 'sk-or-new',
      base_url: 'https://openrouter.ai/api/v1',
    })
  })

  it('runs ChatGPT OAuth polling until connected', async () => {
    vi.useFakeTimers()
    getChatGptOAuthStatusMock
      .mockResolvedValueOnce({ status: 'pending' })
      .mockResolvedValueOnce({ status: 'connected', account_id: 'acct-42' })
    listProviderConnectionsMock
      .mockResolvedValueOnce([
        { provider: 'openrouter', connected: true, api_key_hint: '••••cdef' },
        { provider: 'chatgpt', connected: false },
        { provider: 'amazon-bedrock', connected: false },
      ])
      .mockResolvedValueOnce([
        { provider: 'openrouter', connected: true, api_key_hint: '••••cdef' },
        { provider: 'chatgpt', connected: true, account_id: 'acct-42' },
        { provider: 'amazon-bedrock', connected: false },
      ])

    const wrapper = mountTab()
    await flushPromises()

    await wrapper.find('[data-testid="provider-manage-chatgpt"]').trigger('click')
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
    expect(wrapper.text()).toContain('acct-42')
    vi.useRealTimers()
  })

  it('shows ChatGPT OAuth error state on denied status', async () => {
    vi.useFakeTimers()
    getChatGptOAuthStatusMock.mockResolvedValue({ status: 'denied' })

    const wrapper = mountTab()
    await flushPromises()

    await wrapper.find('[data-testid="provider-manage-chatgpt"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-testid="chatgpt-connect"]').trigger('click')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()

    expect(wrapper.text()).toContain('denied')
    vi.useRealTimers()
  })

  it('switches Bedrock auth method tabs and saves bearer payload', async () => {
    listProviderConnectionsMock.mockResolvedValue([
      {
        provider: 'openrouter',
        connected: true,
        api_key_hint: '••••cdef',
      },
      { provider: 'chatgpt', connected: false },
      {
        provider: 'amazon-bedrock',
        connected: true,
        region: 'us-east-1',
        auth_method: 'bearer',
      },
    ])
    saveProviderConnectionMock.mockResolvedValue({
      provider: 'amazon-bedrock',
      connected: true,
      region: 'us-west-2',
      auth_method: 'bearer',
    })

    const wrapper = mountTab()
    await flushPromises()

    expect(wrapper.find('[data-testid="bedrock-tab-bearer"]').exists()).toBe(false)

    await wrapper.find('[data-testid="provider-manage-amazon-bedrock"]').trigger('click')
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
  })

  it('disconnects a provider after confirmation', async () => {
    const wrapper = mountTab()
    await flushPromises()

    await wrapper.find('[data-testid="provider-manage-openrouter"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-testid="disconnect-openrouter"]').trigger('click')
    await wrapper.find('[data-testid="confirm-disconnect-openrouter"]').trigger('click')
    await flushPromises()

    expect(deleteProviderConnectionMock).toHaveBeenCalledWith('openrouter')
  })
})
