import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import ProviderConfigTab from './ProviderConfigTab.vue'
import ProviderConnectionDialog from './ProviderConnectionDialog.vue'
import ModelPicker from '@/components/common/ModelPicker.vue'
import * as harnessApi from '@/services/harness.api'
import * as providerCatalog from '@/lib/providerCatalog'
import { useAuthStore } from '@/stores/auth'
import { ApiRequestError } from '@/services/api'
import { toast } from 'vue-sonner'
import type { ProviderAccount } from '@/services/harness.api'
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
    listProviderAccounts: vi.fn(),
    listClaudeModels: vi.fn(),
  }
})

const account: ProviderAccount = {
  id: 'account-openrouter-1',
  provider: 'openrouter',
  label: 'Personal',
  scope: 'personal',
  connected: true,
  is_active: true,
  can_manage: true,
  api_key_hint: '••••cdef',
}
const catalog: ProviderModel[] = [
  {
    id: 'openrouter/model-big',
    name: 'Big',
    provider: 'openrouter',
    reasoning_efforts: ['high'],
    default_effort: 'high',
    supports_tools: true,
    context_length: 128000,
    max_output_tokens: 8000,
  },
]
const stubs = {
  ProviderConnectionDialog: {
    name: 'ProviderConnectionDialog',
    props: ['open', 'provider', 'accounts', 'isAdmin'],
    emits: ['update:open', 'changed'],
    template: '<div data-testid="provider-account-dialog" />',
  },
}

function mountTab(admin = false) {
  setActivePinia(createPinia())
  const auth = useAuthStore()
  auth.organizations = [
    { id: 'org-1', name: 'Org', slug: 'org', role: admin ? 'admin' : 'member', created_at: '' },
  ]
  auth.activeOrganizationId = 'org-1'
  return mount(ProviderConfigTab, { global: { stubs } })
}

const config = {
  default_model: '',
  small_model: 'openrouter/model-big',
  computer_use_model: '',
  default_effort: '',
  small_effort: '',
  computer_use_effort: '',
}

describe('ProviderConfigTab', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.spyOn(providerCatalog, 'loadProviderModelsCached').mockResolvedValue(catalog)
    vi.mocked(harnessApi.getProviderConfig).mockResolvedValue(config)
    vi.mocked(harnessApi.saveProviderConfig).mockResolvedValue(config)
    vi.mocked(harnessApi.listProviderAccounts).mockResolvedValue([account])
    vi.mocked(harnessApi.listClaudeModels).mockResolvedValue([])
  })

  it('shows account counts and active label while keeping the shared model picker', async () => {
    const wrapper = mountTab()
    await flushPromises()
    expect(harnessApi.listProviderAccounts).toHaveBeenCalledOnce()
    expect(wrapper.find('[data-testid="provider-account-count-openrouter"]').text()).toBe(
      '1 account',
    )
    expect(wrapper.find('[data-testid="provider-detail-openrouter"]').text()).toContain(
      'Personal · Personal',
    )
    expect(wrapper.find('[data-testid="provider-status-openrouter"]').text()).toBe('Active')
    expect(wrapper.findAllComponents(ModelPicker)).toHaveLength(1)
  })

  it('saves only the changed small model and preserves legacy defaults', async () => {
    const wrapper = mountTab()
    await flushPromises()
    const picker = wrapper.findComponent(ModelPicker)
    await picker.vm.$emit('update:model', 'openrouter/model-next')
    await flushPromises()

    expect(wrapper.find('[data-testid="defaults-status"]').text()).toBe('Unsaved changes')
    await wrapper.find('[data-testid="save-default-models"]').trigger('click')
    await flushPromises()

    expect(harnessApi.saveProviderConfig).toHaveBeenCalledWith({
      small_model: 'openrouter/model-next',
      small_effort: '',
      default_model: '',
      computer_use_model: '',
      default_effort: '',
      computer_use_effort: '',
    })
    expect(wrapper.find('[data-testid="defaults-status"]').text()).toBe('All changes saved')
  })

  it('shows None when accounts exist but none are selected', async () => {
    vi.mocked(harnessApi.listProviderAccounts).mockResolvedValue([
      { ...account, is_active: false },
      { ...account, id: 'another', label: 'Work', is_active: false },
    ])
    const wrapper = mountTab()
    await flushPromises()
    expect(wrapper.find('[data-testid="provider-detail-openrouter"]').text()).toContain(
      '2 accounts · None selected',
    )
  })

  it('prompts to connect when a connected account exists but no account is selected', async () => {
    vi.mocked(harnessApi.listProviderAccounts).mockResolvedValue([{ ...account, is_active: false }])
    const wrapper = mountTab()
    await flushPromises()

    expect(wrapper.find('[data-testid="defaults-no-provider-hint"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="provider-status-openrouter"]').exists()).toBe(false)
  })

  it('opens account management and passes provider accounts and org-admin status', async () => {
    const wrapper = mountTab(true)
    await flushPromises()
    await wrapper.find('[data-testid="provider-manage-openrouter"]').trigger('click')
    const dialog = wrapper.findComponent(ProviderConnectionDialog)
    expect(dialog.props('provider')).toBe('openrouter')
    expect(dialog.props('accounts')).toEqual([account])
    expect(dialog.props('isAdmin')).toBe(true)
  })

  it('refreshes accounts after account changes', async () => {
    const wrapper = mountTab()
    await flushPromises()
    await wrapper.find('[data-testid="provider-manage-openrouter"]').trigger('click')
    wrapper.findComponent(ProviderConnectionDialog).vm.$emit('changed')
    await flushPromises()
    expect(harnessApi.listProviderAccounts).toHaveBeenCalledTimes(2)
  })

  it('keeps accounts visible when provider defaults have not been created yet', async () => {
    vi.mocked(harnessApi.getProviderConfig).mockRejectedValue(
      new ApiRequestError(404, 'Not Found', 'not_found'),
    )
    const wrapper = mountTab()
    await flushPromises()

    expect(wrapper.find('[data-testid="provider-row-openrouter"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="provider-detail-openrouter"]').text()).toContain('Personal')
    expect(wrapper.find('[data-testid="defaults-status"]').text()).toBe('All changes saved')
  })

  it('surfaces default-model save failures through notifications', async () => {
    vi.mocked(harnessApi.saveProviderConfig).mockRejectedValue(new Error('backend down'))
    const wrapper = mountTab()
    await flushPromises()
    const picker = wrapper.findComponent(ModelPicker)
    await picker.vm.$emit('update:model', 'openrouter/other')
    await wrapper.find('[data-testid="save-default-models"]').trigger('click')
    await flushPromises()

    expect(toast.error).toHaveBeenCalledWith('Failed to save default models', {
      description: 'backend down',
      duration: 8000,
    })
    expect(wrapper.find('[data-testid="defaults-status"]').text()).toBe('Unsaved changes')
  })

  it('ignores stale organization loads when the user changes organization quickly', async () => {
    let resolveFirst!: (rows: ProviderAccount[]) => void
    vi.mocked(harnessApi.listProviderAccounts)
      .mockImplementationOnce(() => new Promise((resolve) => (resolveFirst = resolve)))
      .mockResolvedValueOnce([{ ...account, id: 'org-b', label: 'Org B' }])
    const wrapper = mountTab()
    await flushPromises()
    const auth = useAuthStore()
    auth.organizations = [
      { id: 'org-1', name: 'Org', slug: 'org', role: 'admin', created_at: '' },
      { id: 'org-2', name: 'Other', slug: 'other', role: 'member', created_at: '' },
    ]
    auth.setActiveOrganization('org-2')
    await flushPromises()
    resolveFirst([account])
    await flushPromises()

    expect(wrapper.find('[data-testid="provider-detail-openrouter"]').text()).toContain('Org B')
    expect(wrapper.find('[data-testid="provider-detail-openrouter"]').text()).not.toContain(
      'Personal · Personal',
    )
    expect(wrapper.findComponent(ProviderConnectionDialog).props('isAdmin')).toBe(false)

    auth.setActiveOrganization('org-1')
    await flushPromises()
    await wrapper.find('[data-testid="provider-manage-openrouter"]').trigger('click')
    expect(wrapper.findComponent(ProviderConnectionDialog).props('open')).toBe(true)
    auth.setActiveOrganization('org-2')
    await flushPromises()
    expect(wrapper.findComponent(ProviderConnectionDialog).props('open')).toBe(false)
  })
})
