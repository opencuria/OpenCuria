import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { toast } from 'vue-sonner'

import ProviderConfigTab from './ProviderConfigTab.vue'
import ProviderConnectionDialog from './ProviderConnectionDialog.vue'
import ModelPicker from '@/components/common/ModelPicker.vue'
import * as harnessApi from '@/services/harness.api'
import * as providerCatalog from '@/lib/providerCatalog'
import type { ProviderModel } from '@/lib/harnessModels'

vi.mock('vue-sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}))

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

const catalog: ProviderModel[] = [
  {
    id: 'openrouter/model-big',
    name: 'Big',
    provider: 'openrouter',
    reasoning_efforts: ['low', 'medium', 'high'],
    default_effort: 'medium',
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
  {
    id: 'openai-compatible/ui-tars-1.5-7b',
    name: 'ui-tars-1.5-7b',
    provider: 'openai-compatible',
    reasoning_efforts: [],
    default_effort: '',
    supports_tools: false,
    context_length: 0,
    max_output_tokens: 0,
  },
]

const stubs = {
  ProviderConnectionDialog: {
    name: 'ProviderConnectionDialog',
    props: ['open', 'provider', 'connection'],
    emits: ['update:open', 'changed', 'connected'],
    template: '<div data-testid="provider-connection-dialog" />',
  },
  DropdownMenu: { template: '<div><slot /></div>' },
  DropdownMenuTrigger: { template: '<div><slot /></div>' },
  DropdownMenuContent: { template: '<div><slot /></div>' },
  DropdownMenuItem: {
    template: '<button type="button" @click="$emit(\'select\')"><slot /></button>',
  },
  DropdownMenuSub: { template: '<div><slot /></div>' },
  DropdownMenuSubTrigger: { template: '<div><slot /></div>' },
  DropdownMenuSubContent: { template: '<div><slot /></div>' },
}

function mountTab() {
  setActivePinia(createPinia())
  return mount(ProviderConfigTab, {
    global: { stubs },
  })
}

describe('ProviderConfigTab', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.clearAllMocks()
    vi.spyOn(providerCatalog, 'loadProviderModelsCached').mockResolvedValue(catalog)
    getProviderConfigMock.mockResolvedValue({
      base_url: 'https://openrouter.ai/api/v1',
      default_model: 'openrouter/model-big',
      small_model: 'openrouter/model-big',
      computer_use_model: 'openrouter/model-big',
      default_effort: 'high',
      small_effort: '',
      computer_use_effort: 'high',
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
      {
        provider: 'openai-compatible',
        connected: true,
        base_url: 'https://my-host:8000/v1',
        models: ['ui-tars-1.5-7b'],
      },
    ])
    saveProviderConfigMock.mockImplementation(async (data) => ({
      base_url: 'https://openrouter.ai/api/v1',
      default_model: data.default_model ?? '',
      small_model: data.small_model ?? '',
      computer_use_model: data.computer_use_model ?? '',
      default_effort: data.default_effort ?? '',
      small_effort: data.small_effort ?? '',
      computer_use_effort: data.computer_use_effort ?? '',
      has_api_key: true,
      api_key_hint: '••••cdef',
    }))
  })

  it('renders provider rows and one shared small-model selector after load', async () => {
    const wrapper = mountTab()
    await flushPromises()

    expect(getProviderConfigMock).toHaveBeenCalled()
    expect(listProviderConnectionsMock).toHaveBeenCalled()
    expect(wrapper.find('[data-testid="provider-row-openrouter"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="provider-row-chatgpt"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="provider-row-amazon-bedrock"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="provider-row-openai-compatible"]').exists()).toBe(true)
    const pickers = wrapper.findAllComponents(ModelPicker)
    expect(pickers).toHaveLength(1)
    expect(pickers[0]?.props()).toMatchObject({
      inputId: 'provider-small-model',
      model: 'openrouter/model-big',
      variant: 'field',
      manualFallback: true,
      allowDefault: true,
      defaultModelLabel: 'None',
      defaultOptionLabel: 'None',
      allowEffortDefault: true,
      defaultEffortLabel: 'Provider default',
      effortFallback: 'model-default',
      disabled: false,
    })
    expect(wrapper.find('[data-testid="provider-status-openrouter"]').text()).toBe('Connected')
    expect(wrapper.find('[data-testid="provider-status-chatgpt"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="provider-detail-openrouter"]').text()).toContain('••••cdef')
    expect(wrapper.find('[data-testid="provider-detail-chatgpt"]').text()).toContain(
      'ChatGPT Plus or Pro',
    )
  })

  it('shows catalog model counts per provider', async () => {
    const wrapper = mountTab()
    await flushPromises()

    expect(wrapper.find('[data-testid="provider-model-count-openrouter"]').text()).toBe('1 models')
    expect(wrapper.find('[data-testid="provider-model-count-chatgpt"]').text()).toBe('1 models')
    expect(wrapper.find('[data-testid="provider-model-count-openai-compatible"]').text()).toBe(
      '1 models',
    )
    expect(wrapper.find('[data-testid="provider-model-count-amazon-bedrock"]').exists()).toBe(false)
  })

  it('shows the compatible endpoint base URL and model count in the row detail', async () => {
    const wrapper = mountTab()
    await flushPromises()

    expect(wrapper.find('[data-testid="provider-status-openai-compatible"]').text()).toBe(
      'Connected',
    )
    expect(wrapper.find('[data-testid="provider-detail-openai-compatible"]').text()).toContain(
      'https://my-host:8000/v1',
    )
    expect(wrapper.find('[data-testid="provider-detail-openai-compatible"]').text()).toContain(
      '1 model',
    )
  })

  it('keeps save disabled until the small model changes, then saves with toast', async () => {
    const wrapper = mountTab()
    await flushPromises()

    const saveButton = wrapper.find('[data-testid="save-default-models"]')
    expect(saveButton.attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-testid="defaults-status"]').text()).toBe('All changes saved')

    // Pick the ChatGPT model with the shared selector.
    await wrapper.find('[data-testid="composer-model-chatgpt/gpt-5"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="defaults-status"]').text()).toBe('Unsaved changes')
    expect(saveButton.attributes('disabled')).toBeUndefined()

    await saveButton.trigger('click')
    await flushPromises()

    expect(saveProviderConfigMock).toHaveBeenCalledWith({
      small_model: 'chatgpt/gpt-5',
      small_effort: '',
      default_model: 'openrouter/model-big',
      computer_use_model: 'openrouter/model-big',
      default_effort: 'high',
      computer_use_effort: 'high',
    })
    expect(toast.success).toHaveBeenCalledWith('Default models saved', { duration: 5000 })
    expect(wrapper.find('[data-testid="defaults-status"]').text()).toBe('All changes saved')
  })

  it('surfaces save failures as an error toast', async () => {
    saveProviderConfigMock.mockRejectedValue(new Error('boom'))
    const wrapper = mountTab()
    await flushPromises()

    await wrapper.find('[data-testid="composer-model-chatgpt/gpt-5"]').trigger('click')
    await wrapper.find('[data-testid="save-default-models"]').trigger('click')
    await flushPromises()

    expect(toast.error).toHaveBeenCalledWith('Failed to save default models', {
      description: 'boom',
      duration: 8000,
    })
  })

  it('opens the connection dialog from the manage button', async () => {
    const wrapper = mountTab()
    await flushPromises()

    await wrapper.find('[data-testid="provider-manage-chatgpt"]').trigger('click')
    await flushPromises()

    const dialog = wrapper.findComponent(ProviderConnectionDialog)
    expect(dialog.props('open')).toBe(true)
    expect(dialog.props('provider')).toBe('chatgpt')
  })

  it('refreshes state and closes the dialog on changed', async () => {
    const wrapper = mountTab()
    await flushPromises()

    await wrapper.find('[data-testid="provider-manage-openrouter"]').trigger('click')
    await flushPromises()

    const dialog = wrapper.findComponent(ProviderConnectionDialog)
    dialog.vm.$emit('changed')
    await flushPromises()

    expect(getProviderConfigMock).toHaveBeenCalledTimes(2)
    expect(wrapper.findComponent(ProviderConnectionDialog).props('open')).toBe(false)
  })

  it('marks dirty and saves when effort changes on the small picker', async () => {
    const wrapper = mountTab()
    await flushPromises()

    const combos = wrapper.findAllComponents(ModelPicker)
    expect(combos.length).toBe(1)
    const defaultCombo = combos[0]
    // Small combo binds small_effort='' initially.
    expect(defaultCombo?.props('effort')).toBe('')

    const saveButton = wrapper.find('[data-testid="save-default-models"]')
    expect(saveButton.attributes('disabled')).toBeDefined()

    await defaultCombo?.vm.$emit('update:effort', 'high')
    await flushPromises()

    expect(wrapper.find('[data-testid="defaults-status"]').text()).toBe('Unsaved changes')
    expect(saveButton.attributes('disabled')).toBeUndefined()

    await saveButton.trigger('click')
    await flushPromises()

    expect(saveProviderConfigMock).toHaveBeenCalledWith({
      small_model: 'openrouter/model-big',
      small_effort: 'high',
      default_model: 'openrouter/model-big',
      computer_use_model: 'openrouter/model-big',
      default_effort: 'high',
      computer_use_effort: 'high',
    })
  })

  it('normalizes both small-model defaults before saving and retains legacy defaults', async () => {
    const wrapper = mountTab()
    await flushPromises()

    const picker = wrapper.findComponent(ModelPicker)
    picker.vm.$emit('update:model', ' openrouter/model-big ')
    picker.vm.$emit('update:effort', ' high ')
    await flushPromises()
    await wrapper.find('[data-testid="save-default-models"]').trigger('click')
    await flushPromises()

    expect(saveProviderConfigMock).toHaveBeenCalledWith({
      small_model: 'openrouter/model-big',
      small_effort: 'high',
      default_model: 'openrouter/model-big',
      computer_use_model: 'openrouter/model-big',
      default_effort: 'high',
      computer_use_effort: 'high',
    })
    expect(picker.props('model')).toBe('openrouter/model-big')
    expect(picker.props('effort')).toBe('high')
    expect(wrapper.find('[data-testid="defaults-status"]').text()).toBe('All changes saved')
  })

  it('permits manual defaults without a connected provider and never auto-selects a model', async () => {
    vi.mocked(providerCatalog.loadProviderModelsCached).mockResolvedValue([])
    getProviderConfigMock.mockResolvedValue({
      base_url: '',
      default_model: '',
      small_model: '',
      computer_use_model: '',
      default_effort: '',
      small_effort: '',
      computer_use_effort: '',
      has_api_key: false,
      api_key_hint: '',
    })
    listProviderConnectionsMock.mockResolvedValue([
      { provider: 'openrouter', connected: false },
      { provider: 'chatgpt', connected: false },
      { provider: 'amazon-bedrock', connected: false },
      { provider: 'openai-compatible', connected: false },
    ])
    const wrapper = mountTab()
    await flushPromises()

    expect(wrapper.findAllComponents(ModelPicker)).toHaveLength(1)
    const picker = wrapper.findComponent(ModelPicker)
    expect(picker.props('model')).toBe('')
    expect(picker.props('effort')).toBe('')
    expect(wrapper.find('[data-testid="defaults-no-provider-hint"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="save-default-models"]').attributes('disabled')).toBeDefined()
    expect(saveProviderConfigMock).not.toHaveBeenCalled()

    await wrapper.find('input[placeholder="provider/model-id"]').setValue('custom/manual-model')
    await flushPromises()
    expect(picker.props('model')).toBe('custom/manual-model')
    await wrapper.find('[data-testid="save-default-models"]').trigger('click')
    await flushPromises()

    expect(saveProviderConfigMock).toHaveBeenCalledWith({
      small_model: 'custom/manual-model',
      small_effort: '',
      default_model: '',
      computer_use_model: '',
      default_effort: '',
      computer_use_effort: '',
    })
  })

  it('shows a hint when no provider is connected', async () => {
    listProviderConnectionsMock.mockResolvedValue([
      { provider: 'openrouter', connected: false },
      { provider: 'chatgpt', connected: false },
      { provider: 'amazon-bedrock', connected: false },
      { provider: 'openai-compatible', connected: false },
    ])
    const wrapper = mountTab()
    await flushPromises()

    expect(wrapper.find('[data-testid="defaults-no-provider-hint"]').exists()).toBe(true)
  })

  it('keeps connections visible when the defaults row is missing (404)', async () => {
    const { ApiRequestError } = await import('@/services/api')
    getProviderConfigMock.mockRejectedValue(new ApiRequestError(404, 'Not Found', 'not_found'))
    const wrapper = mountTab()
    await flushPromises()

    expect(wrapper.find('[data-testid="provider-row-openrouter"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="provider-status-openrouter"]').text()).toBe('Connected')
    expect(wrapper.find('[data-testid="defaults-no-provider-hint"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="defaults-status"]').text()).toBe('All changes saved')
  })
})
