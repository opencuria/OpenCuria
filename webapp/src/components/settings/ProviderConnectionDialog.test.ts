import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

import ProviderConnectionDialog from './ProviderConnectionDialog.vue'
import * as harnessApi from '@/services/harness.api'
import type { ProviderAccount } from '@/services/harness.api'

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return {
    ...actual,
    createProviderAccount: vi.fn(),
    updateProviderAccount: vi.fn(),
    deleteProviderAccount: vi.fn(),
    selectProviderAccount: vi.fn(),
    startChatGptOAuth: vi.fn(),
    getChatGptOAuthStatus: vi.fn(),
    cancelChatGptOAuth: vi.fn(),
  }
})

const personal: ProviderAccount = {
  id: 'private-uuid',
  provider: 'openrouter',
  label: 'Personal',
  scope: 'personal',
  connected: true,
  is_active: true,
  can_manage: true,
  api_key_hint: '••••cdef',
  base_url: 'https://openrouter.ai/api/v1',
}
const shared: ProviderAccount = {
  id: 'shared-uuid',
  provider: 'openrouter',
  label: 'Org key',
  scope: 'organization',
  connected: true,
  is_active: false,
  can_manage: false,
}
const stubs = {
  Dialog: { template: '<div><slot /></div>', props: ['open'] },
  DialogContent: { template: '<div><slot /></div>' },
  DialogHeader: { template: '<div><slot /></div>' },
  DialogTitle: { template: '<div><slot /></div>' },
  DialogDescription: { template: '<div><slot /></div>' },
  DialogBody: { template: '<div><slot /></div>' },
  DialogFooter: { template: '<div><slot /></div>' },
}
function mountDialog(
  provider: ProviderAccount['provider'],
  accounts: ProviderAccount[] = [],
  isAdmin = false,
) {
  return mount(ProviderConnectionDialog, {
    props: { open: true, provider, accounts, isAdmin },
    global: { stubs },
  })
}

describe('ProviderConnectionDialog account management', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    vi.mocked(harnessApi.createProviderAccount).mockResolvedValue(personal)
    vi.mocked(harnessApi.updateProviderAccount).mockResolvedValue(personal)
    vi.mocked(harnessApi.deleteProviderAccount).mockResolvedValue(undefined)
    vi.mocked(harnessApi.selectProviderAccount).mockResolvedValue(undefined)
    vi.mocked(harnessApi.startChatGptOAuth).mockResolvedValue({
      flow_id: 'flow-current',
      user_code: 'ABCD-1234',
      verification_url: 'https://openai.test/device',
      interval: 1,
      expires_in: 600,
    })
    vi.mocked(harnessApi.getChatGptOAuthStatus).mockResolvedValue({ status: 'pending' })
    vi.mocked(harnessApi.cancelChatGptOAuth).mockResolvedValue(undefined)
  })

  it('lists multiple accounts, lets a member select a shared account, and supports explicit none', async () => {
    const wrapper = mountDialog('openrouter', [personal, shared])
    expect(wrapper.text()).toContain('Personal')
    expect(wrapper.text()).toContain('Org key')
    expect(wrapper.find('[data-testid="edit-account-shared-uuid"]').exists()).toBe(false)
    await wrapper
      .findAll('button')
      .find((button) => button.text() === 'Use')!
      .trigger('click')
    await flushPromises()
    expect(harnessApi.selectProviderAccount).toHaveBeenCalledWith('openrouter', 'shared-uuid')

    await wrapper.find('[data-testid="select-no-account"]').trigger('click')
    await flushPromises()
    expect(harnessApi.selectProviderAccount).toHaveBeenLastCalledWith('openrouter', null)
  })

  it('creates named personal accounts and leaves blank secrets blank to preserve them', async () => {
    const wrapper = mountDialog('openrouter')
    await wrapper.find('[data-testid="add-provider-account"]').trigger('click')
    await wrapper.find('#account-label').setValue('Home')
    await wrapper.find('#openrouter-api-key').setValue('')
    await wrapper.find('[data-testid="save-provider-account"]').trigger('click')
    await flushPromises()
    expect(harnessApi.createProviderAccount).toHaveBeenCalledWith('openrouter', {
      label: 'Home',
      shared: false,
      api_key: '',
      base_url: 'https://openrouter.ai/api/v1',
    })
  })

  it('allows admins to create shared accounts and edits exactly one selected UUID', async () => {
    const wrapper = mountDialog('openrouter', [personal], true)
    await wrapper.find('[data-testid="edit-account-private-uuid"]').trigger('click')
    await wrapper.find('#account-label').setValue('Personal 2')
    await wrapper
      .findAll('button')
      .find((button) => button.text() === 'Back')!
      .trigger('click')
    await flushPromises()
    expect(harnessApi.updateProviderAccount).toHaveBeenCalledWith('openrouter', 'private-uuid', {
      label: 'Personal 2',
      shared: false,
      api_key: '',
      base_url: 'https://openrouter.ai/api/v1',
    })

    wrapper.unmount()
    const createWrapper = mountDialog('openrouter', [], true)
    await createWrapper.find('[data-testid="add-provider-account"]').trigger('click')
    await createWrapper.find('#account-label').setValue('Org Production')
    createWrapper.findComponent({ name: 'Checkbox' }).vm.$emit('update:modelValue', true)
    await createWrapper.find('[data-testid="save-provider-account"]').trigger('click')
    await flushPromises()
    expect(harnessApi.createProviderAccount).toHaveBeenLastCalledWith(
      'openrouter',
      expect.objectContaining({
        label: 'Org Production',
        shared: true,
      }),
    )
  })

  it('keeps shared accounts read-only for members while allowing selection', async () => {
    const wrapper = mountDialog('openrouter', [shared])

    expect(wrapper.find('[data-testid="edit-account-shared-uuid"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="delete-account"]').exists()).toBe(false)
    await wrapper
      .findAll('button')
      .find((button) => button.text() === 'Use')!
      .trigger('click')
    await flushPromises()

    expect(harnessApi.selectProviderAccount).toHaveBeenCalledWith('openrouter', 'shared-uuid')
    expect(harnessApi.updateProviderAccount).not.toHaveBeenCalled()
    expect(harnessApi.deleteProviderAccount).not.toHaveBeenCalled()
  })

  it('autosaves successive edits without overwriting input or closing the account form', async () => {
    vi.useFakeTimers()
    const wrapper = mountDialog('openrouter', [personal])
    try {
      let finishFirst!: (value: ProviderAccount) => void
      vi.mocked(harnessApi.updateProviderAccount).mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finishFirst = resolve
          }),
      )
      await wrapper.get('[data-testid="edit-account-private-uuid"]').trigger('click')
      expect(wrapper.find('[data-testid="save-provider-account"]').exists()).toBe(false)
      await wrapper.get('#account-label').setValue('First')
      await vi.advanceTimersByTimeAsync(500)
      await wrapper.get('#account-label').setValue('Latest')
      await vi.advanceTimersByTimeAsync(500)
      expect(harnessApi.updateProviderAccount).toHaveBeenCalledTimes(1)
      finishFirst({ ...personal, label: 'First' })
      await flushPromises()
      expect(harnessApi.updateProviderAccount).toHaveBeenCalledTimes(2)
      expect(harnessApi.updateProviderAccount).toHaveBeenLastCalledWith(
        'openrouter',
        personal.id,
        expect.objectContaining({ label: 'Latest' }),
      )
      expect((wrapper.get('#account-label').element as HTMLInputElement).value).toBe('Latest')
      expect(wrapper.text()).toContain('All changes saved')
    } finally {
      wrapper.unmount()
      vi.useRealTimers()
    }
  })

  it('normalizes compatible model lists while retaining a blank saved API key', async () => {
    const compatible: ProviderAccount = {
      ...personal,
      id: 'compatible-uuid',
      provider: 'openai-compatible',
      base_url: 'https://local.example/v1',
      models: ['old-model'],
    }
    const wrapper = mountDialog('openai-compatible', [compatible])
    await wrapper.find('[data-testid="edit-account-compatible-uuid"]').trigger('click')
    await wrapper.find('#compat-models').setValue(' model-a  \nmodel-a\nmodel-b \n')
    await wrapper
      .findAll('button')
      .find((button) => button.text() === 'Back')!
      .trigger('click')
    await flushPromises()

    expect(harnessApi.updateProviderAccount).toHaveBeenCalledWith(
      'openai-compatible',
      'compatible-uuid',
      {
        label: 'Personal',
        shared: false,
        api_key: '',
        base_url: 'https://local.example/v1',
        models: ['model-a', 'model-b'],
      },
    )
  })

  it('retains invalid edits on an account that still needs authentication', async () => {
    vi.useFakeTimers()
    const account: ProviderAccount = {
      ...personal,
      provider: 'claude-agent',
      connected: false,
      auth_type: 'api_token',
    }
    const wrapper = mountDialog('claude-agent', [account])
    try {
      await wrapper.get('[data-testid="edit-account-private-uuid"]').trigger('click')
      await wrapper.get('#account-label').setValue('Pending connection')
      await vi.advanceTimersByTimeAsync(500)
      expect(wrapper.text()).toContain('Check your input')
      expect(harnessApi.updateProviderAccount).not.toHaveBeenCalled()
      await wrapper
        .findAll('button')
        .find((button) => button.text() === 'Back')!
        .trigger('click')
      expect(wrapper.find('#account-label').exists()).toBe(true)
      await wrapper.get('#claude-token').setValue('replacement-token')
      await vi.advanceTimersByTimeAsync(500)
      await flushPromises()
      expect(harnessApi.updateProviderAccount).toHaveBeenCalledWith('claude-agent', account.id, {
        label: 'Pending connection',
        shared: false,
        auth_type: 'api_token',
        token: 'replacement-token',
      })
    } finally {
      wrapper.unmount()
      vi.useRealTimers()
    }
  })

  it('requires account labels and base URLs and never submits on invalid form', async () => {
    const wrapper = mountDialog('openai-compatible')
    await wrapper.find('[data-testid="add-provider-account"]').trigger('click')
    await wrapper.find('#account-label').setValue('Local')
    await wrapper.find('[data-testid="save-provider-account"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Base URL is required')
    expect(harnessApi.createProviderAccount).not.toHaveBeenCalled()
  })

  it('confirms deletion with explicit no-fallback interruption warning', async () => {
    const wrapper = mountDialog('openrouter', [personal])
    await wrapper.find('[data-testid="edit-account-private-uuid"]').trigger('click')
    await wrapper.find('[data-testid="delete-account"]').trigger('click')
    expect(wrapper.find('[data-testid="account-confirm"]').text()).toContain(
      'They will not switch to another account',
    )
    await wrapper.find('[data-testid="confirm-account-action"]').trigger('click')
    await flushPromises()
    expect(harnessApi.deleteProviderAccount).toHaveBeenCalledWith('openrouter', 'private-uuid')
  })

  it('requires admin confirmation before unsharing and transfers the exact account to personal scope', async () => {
    const organizationAccount = { ...personal, scope: 'organization' as const }
    const wrapper = mountDialog('openrouter', [organizationAccount], true)
    await wrapper.find('[data-testid="edit-account-private-uuid"]').trigger('click')
    await wrapper.find('[data-testid="unshare-account"]').trigger('click')
    expect(wrapper.find('[data-testid="account-confirm"]').text()).toContain(
      'transfers this account to you',
    )
    await wrapper.find('[data-testid="confirm-account-action"]').trigger('click')
    await flushPromises()
    expect(harnessApi.updateProviderAccount).toHaveBeenCalledWith(
      'openrouter',
      'private-uuid',
      expect.objectContaining({
        label: 'Personal',
        shared: false,
      }),
    )
  })

  it('creates Claude accounts through account API and preserves blank auth secrets', async () => {
    const claude: ProviderAccount = {
      ...personal,
      id: 'claude-uuid',
      provider: 'claude-agent',
      auth_type: 'api_token',
    }
    vi.mocked(harnessApi.updateProviderAccount).mockResolvedValue(claude)
    const wrapper = mountDialog('claude-agent', [claude])
    await wrapper.find('[data-testid="edit-account-claude-uuid"]').trigger('click')
    await wrapper.find('#account-label').setValue('Claude account')
    await wrapper
      .findAll('button')
      .find((button) => button.text() === 'Back')!
      .trigger('click')
    await flushPromises()
    expect(harnessApi.updateProviderAccount).toHaveBeenCalledWith('claude-agent', 'claude-uuid', {
      label: 'Claude account',
      shared: false,
      auth_type: 'api_token',
      token: '',
    })
  })

  it('treats a cancelled OAuth flow as idle rather than expired', async () => {
    vi.useFakeTimers()
    vi.mocked(harnessApi.getChatGptOAuthStatus).mockResolvedValue({ status: 'cancelled' })
    const wrapper = mountDialog('chatgpt')
    try {
      await wrapper.find('[data-testid="add-provider-account"]').trigger('click')
      await wrapper.find('#account-label').setValue('Login')
      await wrapper.find('[data-testid="chatgpt-connect"]').trigger('click')
      await flushPromises()
      await vi.advanceTimersByTimeAsync(1000)
      await flushPromises()

      expect(harnessApi.getChatGptOAuthStatus).toHaveBeenCalledWith('flow-current')
      expect(wrapper.text()).not.toContain('authorization code expired')
      expect(wrapper.text()).not.toContain('Authorization was denied')
      expect(wrapper.find('[data-testid="chatgpt-connect"]').exists()).toBe(true)
    } finally {
      wrapper.unmount()
      vi.useRealTimers()
    }
  })

  it('scopes OAuth start, polling and cancellation to this dialog flow and target account', async () => {
    const chatAccount: ProviderAccount = {
      ...personal,
      id: 'chat-account-uuid',
      provider: 'chatgpt',
    }
    const wrapper = mountDialog('chatgpt', [chatAccount])
    await wrapper.find('[data-testid="edit-account-chat-account-uuid"]').trigger('click')
    await wrapper.find('#account-label').setValue('ChatGPT Work')
    await wrapper.find('[data-testid="chatgpt-connect"]').trigger('click')
    await flushPromises()
    expect(harnessApi.startChatGptOAuth).toHaveBeenCalledWith({
      label: 'ChatGPT Work',
      shared: false,
      account_id: 'chat-account-uuid',
    })
    expect(wrapper.find('[data-testid="chatgpt-user-code"]').text()).toBe('ABCD-1234')
    await wrapper.setProps({ open: false })
    await flushPromises()
    expect(harnessApi.cancelChatGptOAuth).toHaveBeenCalledWith('flow-current')
  })
})
