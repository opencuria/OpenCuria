import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import WorkspaceDesktop from './WorkspaceDesktop.vue'
import { useDesktopStore } from '@/stores/desktop'
import * as workspacesApi from '@/services/workspaces.api'

vi.mock('@/services/workspaces.api', () => ({
  getDesktopStatus: vi.fn(),
  startDesktop: vi.fn(),
  stopDesktop: vi.fn(),
  takeDesktopControl: vi.fn(),
  writeDesktopClipboard: vi.fn(),
  readDesktopClipboard: vi.fn(),
}))

vi.mock('@/services/config', () => ({
  getConfig: () => ({ apiBaseUrl: '', wsBaseUrl: '' }),
}))

vi.mock('@/services/socket', () => ({
  onEvent: vi.fn(() => () => {}),
}))

const getDesktopStatus = vi.mocked(workspacesApi.getDesktopStatus)
const startDesktop = vi.mocked(workspacesApi.startDesktop)
const stopDesktop = vi.mocked(workspacesApi.stopDesktop)
const takeDesktopControl = vi.mocked(workspacesApi.takeDesktopControl)

describe('WorkspaceDesktop leases', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    class ResizeObserverStub {
      observe(): void {}
      disconnect(): void {}
      unobserve(): void {}
    }
    vi.stubGlobal('ResizeObserver', ResizeObserverStub)
    getDesktopStatus.mockResolvedValue({
      active: true,
      proxy_url: '/ws/desktop/ws-1/',
      viewer_held: false,
      computer_use_active: true,
    })
    startDesktop.mockResolvedValue({ task_id: 'task-1' })
    stopDesktop.mockResolvedValue({ task_id: 'task-2' })
    takeDesktopControl.mockResolvedValue({ aborted_session_ids: ['cu-1'] })
  })

  it('always posts start to acquire the viewer lease', async () => {
    const store = useDesktopStore()
    store.open()
    mount(WorkspaceDesktop, {
      props: { workspaceId: 'ws-1' },
      global: {
        stubs: {
          Button: { template: '<button v-bind="$attrs"><slot /></button>' },
          Select: { template: '<div><slot /></div>' },
          SelectTrigger: { template: '<div />' },
          SelectValue: { template: '<div />' },
          SelectContent: { template: '<div />' },
          SelectItem: { template: '<div />' },
          LoadingSpinner: { template: '<div />' },
        },
      },
    })
    await flushPromises()

    expect(getDesktopStatus).toHaveBeenCalledWith('ws-1')
    expect(startDesktop).toHaveBeenCalledWith('ws-1')
    expect(store.computerUseActive).toBe(true)
  })

  it('shows a take-control overlay while computer-use is active', async () => {
    const store = useDesktopStore()
    store.open()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    store.setComputerUseActive(true)

    const wrapper = mount(WorkspaceDesktop, {
      props: { workspaceId: 'ws-1' },
      global: {
        stubs: {
          Button: { template: '<button v-bind="$attrs"><slot /></button>' },
          Select: { template: '<div><slot /></div>' },
          SelectTrigger: { template: '<div />' },
          SelectValue: { template: '<div />' },
          SelectContent: { template: '<div />' },
          SelectItem: { template: '<div />' },
          LoadingSpinner: { template: '<div />' },
        },
      },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('Computer-use is controlling this desktop')
    const takeControl = wrapper.findAll('button').find((button) => {
      return button.text() === 'Take control'
    })
    expect(takeControl).toBeTruthy()
    await takeControl?.trigger('click')
    await flushPromises()
    expect(takeDesktopControl).toHaveBeenCalledWith('ws-1')
    expect(store.computerUseActive).toBe(false)
  })

  it('releases the viewer lease on close without treating it as process death', async () => {
    const store = useDesktopStore()
    store.open()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    store.setComputerUseActive(true)

    const wrapper = mount(WorkspaceDesktop, {
      props: { workspaceId: 'ws-1' },
      global: {
        stubs: {
          Button: {
            template: '<button v-bind="$attrs" :title="title"><slot /></button>',
            props: ['title'],
          },
          Select: { template: '<div><slot /></div>' },
          SelectTrigger: { template: '<div />' },
          SelectValue: { template: '<div />' },
          SelectContent: { template: '<div />' },
          SelectItem: { template: '<div />' },
          LoadingSpinner: { template: '<div />' },
        },
      },
    })
    await flushPromises()

    const close = wrapper.findAll('button').find((button) => {
      return button.attributes('title') === 'Close desktop panel'
    })
    await close?.trigger('click')
    await flushPromises()

    expect(stopDesktop).toHaveBeenCalledWith('ws-1')
    expect(store.isOpen).toBe(false)
    expect(store.isConnected).toBe(false)
  })

  it('does not expose live screen-size or rotate controls', async () => {
    const store = useDesktopStore()
    store.open()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')

    const wrapper = mount(WorkspaceDesktop, {
      props: { workspaceId: 'ws-1' },
      global: {
        stubs: {
          Button: { template: '<button v-bind="$attrs"><slot /></button>' },
          LoadingSpinner: { template: '<div />' },
        },
      },
    })
    await flushPromises()

    expect(wrapper.text()).not.toContain('Screen size')
    expect(wrapper.text()).not.toContain('Rotate')
    expect(wrapper.get('iframe').attributes('src')).toContain('resize=scale')
  })
})
