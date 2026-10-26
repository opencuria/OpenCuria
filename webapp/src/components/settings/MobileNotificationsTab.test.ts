import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import MobileNotificationsTab from './MobileNotificationsTab.vue'
import { useAuthStore } from '@/stores/auth'
import * as api from '@/services/notifications.api'
import * as credentialsApi from '@/services/credentials.api'

vi.mock('@/services/notifications.api', () => ({
  getPushConfiguration: vi.fn(),
  savePushConfiguration: vi.fn(),
}))
vi.mock('@/services/credentials.api', () => ({ listCredentials: vi.fn() }))
vi.mock('vue-sonner', () => ({ toast: { error: vi.fn() } }))
enableAutoUnmount(afterEach)
afterEach(() => vi.useRealTimers())

const configuration = {
  ios: false,
  android: false,
  live_activities: false,
  apns_credential_id: null,
  fcm_credential_id: null,
}
const Select = defineComponent({
  props: ['modelValue', 'disabled'],
  emits: ['update:modelValue'],
  setup(props, { emit, slots }) {
    return () =>
      h(
        'select',
        {
          disabled: props.disabled,
          value: props.modelValue,
          onChange: (event: Event) =>
            emit('update:modelValue', (event.target as HTMLSelectElement).value),
        },
        slots.default?.(),
      )
  },
})
const SelectItem = defineComponent({
  props: ['value'],
  setup(props, { slots }) {
    return () => h('option', { value: props.value }, slots.default?.())
  },
})
const stubs = {
  Select,
  SelectItem,
  SelectTrigger: { template: '<span />' },
  SelectContent: { template: '<slot />' },
}

describe('mobile notification setup', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    const auth = useAuthStore()
    auth.activeOrganizationId = 'org'
    auth.organizations = [
      { id: 'org', name: 'Organization', slug: 'org', role: 'admin', created_at: 'now' },
    ]
    vi.useFakeTimers()
    vi.clearAllMocks()
    vi.mocked(api.getPushConfiguration).mockResolvedValue({ ...configuration })
    vi.mocked(api.savePushConfiguration).mockImplementation(async (input) => ({
      ...configuration,
      ...input,
    }))
    vi.mocked(credentialsApi.listCredentials).mockResolvedValue([
      { id: 'apns', name: 'Apple key', scope: 'organization', service_slug: 'mobile-push-apns' },
      { id: 'personal', name: 'Personal key', scope: 'personal', service_slug: 'mobile-push-apns' },
      { id: 'fcm', name: 'Firebase key', scope: 'organization', service_slug: 'mobile-push-fcm' },
    ] as Awaited<ReturnType<typeof credentialsApi.listCredentials>>)
  })

  it('offers organization credentials and autosaves references without writing on load', async () => {
    const wrapper = mount(MobileNotificationsTab, { global: { stubs } })
    await flushPromises()
    await vi.advanceTimersByTimeAsync(500)
    expect(api.savePushConfiguration).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('Personal key')
    await wrapper.findAll('select')[0]!.setValue('apns')
    await vi.advanceTimersByTimeAsync(500)
    await flushPromises()
    expect(api.savePushConfiguration).toHaveBeenCalledWith({
      apns_credential_id: 'apns',
      fcm_credential_id: null,
    })
    expect(wrapper.text()).toContain('All changes saved')
  })

  it('keeps a provider validation failure visible and lets the admin retry', async () => {
    vi.mocked(api.savePushConfiguration).mockRejectedValueOnce(
      new Error('Invalid APNS credential JSON or private key'),
    )
    const wrapper = mount(MobileNotificationsTab, { global: { stubs } })
    await flushPromises()
    await wrapper.findAll('select')[0]!.setValue('apns')
    await vi.advanceTimersByTimeAsync(500)
    await flushPromises()
    expect(wrapper.text()).toContain('Invalid APNS credential')
    const retry = wrapper.findAll('button').find((button) => button.text() === 'Retry')!
    await retry.trigger('click')
    await flushPromises()
    expect(api.savePushConfiguration).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('All changes saved')
  })
})
