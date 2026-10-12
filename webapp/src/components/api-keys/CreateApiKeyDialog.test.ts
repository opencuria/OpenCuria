import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import { useNotificationStore } from '@/stores/notifications'
import CreateApiKeyDialog from './CreateApiKeyDialog.vue'

const apiKeyStore = vi.hoisted(() => ({
  availablePermissions: [] as Array<{ group: string; value: string; description: string }>,
  fetchAvailablePermissions: vi.fn(),
  createKey: vi.fn(),
}))
vi.mock('@/stores/apiKeys', () => ({ useApiKeyStore: () => apiKeyStore }))

const dialogStubs = {
  Dialog: { template: '<div><slot /></div>' },
  DialogTrigger: { template: '<div><slot /></div>' },
  DialogContent: { template: '<div><slot /></div>' },
  DialogHeader: { template: '<div><slot /></div>' },
  DialogTitle: { template: '<div><slot /></div>' },
  DialogDescription: { template: '<div><slot /></div>' },
  DialogBody: { template: '<div><slot /></div>' },
  DialogFooter: { template: '<div><slot /></div>' },
}

describe('CreateApiKeyDialog key copy', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    apiKeyStore.createKey.mockResolvedValue({
      id: 'key-1', name: 'Integration', key_prefix: 'oc_', is_active: true,
      created_at: '2026-01-01T00:00:00Z', last_used_at: null, expires_at: null,
      permissions: [], key: 'secret-api-key',
    })
  })

  it('copies a newly created API key with silent feedback', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const notificationInfo = vi.spyOn(useNotificationStore(), 'info')
    const wrapper = mount(CreateApiKeyDialog, { global: { stubs: dialogStubs } })

    await wrapper.find('#create-api-key-form').find('input').setValue('Integration')
    await wrapper.find('#create-api-key-form').trigger('submit')
    await flushPromises()
    const copy = wrapper.findComponent({ name: 'CopyButton' })
    expect(copy.props('label')).toBe('API key')

    await copy.trigger('click')
    await flushPromises()

    expect(writeText).toHaveBeenCalledWith('secret-api-key')
    expect(copy.attributes('aria-label')).toBe('Copied')
    expect(notificationInfo).not.toHaveBeenCalled()
  })
})
