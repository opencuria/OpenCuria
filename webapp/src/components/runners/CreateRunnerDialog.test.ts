import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import { useNotificationStore } from '@/stores/notifications'
import CreateRunnerDialog from './CreateRunnerDialog.vue'

const runnerStore = vi.hoisted(() => ({ createRunner: vi.fn() }))
vi.mock('@/stores/runners', () => ({ useRunnerStore: () => runnerStore }))
vi.mock('@/services/config', () => ({
  getConfig: () => ({ apiBaseUrl: '/api/v1', wsBaseUrl: 'https://opencuria.example.com' }),
}))

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

describe('CreateRunnerDialog token copy', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    runnerStore.createRunner.mockResolvedValue({ id: 'runner-1', name: 'Runner', api_token: 'secret-runner-token' })
  })

  it('copies a newly created runner token with silent feedback', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const notificationInfo = vi.spyOn(useNotificationStore(), 'info')
    const wrapper = mount(CreateRunnerDialog, { global: { stubs: dialogStubs } })

    await wrapper.find('#create-runner-form').trigger('submit')
    await flushPromises()
    const copy = wrapper.findComponent({ name: 'CopyButton' })
    expect(copy.props('label')).toBe('runner token')

    await copy.trigger('click')
    await flushPromises()

    expect(writeText).toHaveBeenCalledWith('secret-runner-token')
    expect(copy.attributes('aria-label')).toBe('Copied')
    expect(notificationInfo).not.toHaveBeenCalled()
  })

  it('offers a one-line runner install command for this control plane', async () => {
    const wrapper = mount(CreateRunnerDialog, { global: { stubs: dialogStubs } })

    await wrapper.find('#create-runner-form').trigger('submit')
    await flushPromises()
    const copy = wrapper
      .findAllComponents({ name: 'CopyButton' })
      .find((button) => button.props('label') === 'install command')

    expect(copy?.props('text')).toBe(
      'curl -fsSL https://raw.githubusercontent.com/opencuria/OpenCuria/main/install.sh' +
        ' | sudo bash -s -- runner --url https://opencuria.example.com' +
        ' --token secret-runner-token',
    )
  })

  it('shows no install command when registration fails', async () => {
    runnerStore.createRunner.mockResolvedValue(null)
    const wrapper = mount(CreateRunnerDialog, { global: { stubs: dialogStubs } })

    await wrapper.find('#create-runner-form').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).not.toContain('install.sh')
    expect(wrapper.find('#create-runner-form').exists()).toBe(true)
  })
})
