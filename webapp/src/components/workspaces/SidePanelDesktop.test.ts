import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import SidePanelDesktop from './SidePanelDesktop.vue'
import { useDesktopStore } from '@/stores/desktop'
import { useNotificationStore } from '@/stores/notifications'
import { sidebarDesktopHost } from '@/lib/desktopSurfaceHost'
import {
  desktopViewerClientId,
  desktopViewerSession,
} from '@/composables/useDesktopSessionCoordinator'
import type { VueWrapper } from '@vue/test-utils'
import * as workspacesApi from '@/services/workspaces.api'

vi.mock('@/services/workspaces.api', () => ({
  getDesktopStatus: vi.fn(),
  startDesktop: vi.fn(),
  stopDesktop: vi.fn(),
  renewDesktop: vi.fn(),
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

const uiStubs = {
  Button: {
    template: '<button v-bind="$attrs" :title="title"><slot /></button>',
    props: ['title'],
  },
  LoadingSpinner: { template: '<div />' },
}

const wrappers: VueWrapper[] = []
let workspaceId = ''
let index = 0
const intent = () => ({
  viewer_client_id: desktopViewerClientId,
  intent_revision: desktopViewerSession(workspaceId).revision,
})
afterEach(async () => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  await flushPromises()
})

function mountPanel() {
  const wrapper = mount(SidePanelDesktop, {
    props: { workspaceId },
    global: { stubs: uiStubs },
  })
  wrappers.push(wrapper)
  return wrapper
}

describe('SidePanelDesktop', () => {
  beforeEach(() => {
    workspaceId = `SidePanelDesktop-${++index}`
    setActivePinia(createPinia())
    vi.clearAllMocks()
    localStorage.clear()
    sidebarDesktopHost.value = null
    getDesktopStatus.mockImplementation(async () => ({
      active: true,
      proxy_url: `/ws/desktop/${workspaceId}/`,
      viewer_held: true,
      computer_use_active: false,
      viewer_lease_state: 'held',
      revision: desktopViewerSession(workspaceId).revision,
      epoch: 'epoch',
    }))
    vi.mocked(workspacesApi.stopDesktop).mockResolvedValue({ task_id: 'stop' })
    startDesktop.mockResolvedValue({ task_id: 'task-1' })
  })

  it('auto-starts the desktop session on mount', async () => {
    mountPanel()
    await flushPromises()

    expect(getDesktopStatus).toHaveBeenCalledWith(
      workspaceId,
      desktopViewerClientId,
      expect.any(AbortSignal),
    )
    expect(startDesktop).toHaveBeenCalledWith(workspaceId, intent(), expect.any(AbortSignal))
  })

  it('does not auto-start when already connected', async () => {
    const store = useDesktopStore()
    store.setConnected(workspaceId, `/ws/desktop/${workspaceId}/`)

    mountPanel()
    await flushPromises()

    expect(startDesktop).not.toHaveBeenCalled()
  })

  it('opens the desktop modal via the maximize button', async () => {
    const store = useDesktopStore()
    store.setConnected(workspaceId, `/ws/desktop/${workspaceId}/`)

    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.get('[data-testid="side-panel-desktop-maximize"]').trigger('click')
    expect(store.isOpen).toBe(true)
  })

  it('registers the sidebar host for the persistent surface while connected', async () => {
    const store = useDesktopStore()
    store.setConnected(workspaceId, `/ws/desktop/${workspaceId}/`)

    mountPanel()
    await flushPromises()

    expect(sidebarDesktopHost.value).toBeInstanceOf(HTMLElement)
  })

  it('offers Take control in the toolbar while computer use drives the desktop', async () => {
    const store = useDesktopStore()
    store.setConnected(workspaceId, `/ws/desktop/${workspaceId}/`)
    vi.mocked(workspacesApi.takeDesktopControl).mockResolvedValue({ aborted_session_ids: ['s'] })
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.find('[data-testid="desktop-take-control"]').exists()).toBe(false)

    store.setComputerUseActive(true)
    await flushPromises()
    const action = wrapper.get('[data-testid="desktop-take-control"]')
    // The viewer host stays free: nothing is placed over the desktop.
    expect(wrapper.get('[data-testid="side-panel-desktop-host"]').text()).toBe('')
    await action.trigger('click')
    await flushPromises()

    expect(workspacesApi.takeDesktopControl).toHaveBeenCalledWith(workspaceId)
    expect(store.computerUseActive).toBe(false)
    expect(wrapper.find('[data-testid="desktop-take-control"]').exists()).toBe(false)
  })

  it('keeps the viewer and reports a toast when taking control fails', async () => {
    const store = useDesktopStore()
    store.setConnected(workspaceId, `/ws/desktop/${workspaceId}/`)
    store.setComputerUseActive(true)
    vi.mocked(workspacesApi.takeDesktopControl).mockRejectedValue(new Error('busy'))
    const notify = vi.spyOn(useNotificationStore(), 'error')
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.get('[data-testid="desktop-take-control"]').trigger('click')
    await flushPromises()

    expect(notify).toHaveBeenCalledWith(
      'Could not take control',
      'Computer Use is still running. Try again.',
    )
    expect(store.computerUseActive).toBe(true)
    expect(wrapper.find('[data-testid="desktop-take-control"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="side-panel-desktop-host"]').exists()).toBe(true)
  })

  it('does not open the modal when clicking the interactive viewport', async () => {
    const store = useDesktopStore()
    store.setConnected(workspaceId, `/ws/desktop/${workspaceId}/`)

    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.get('[data-testid="side-panel-desktop-host"]').trigger('click')
    expect(store.isOpen).toBe(false)
  })
  it('starts the new workspace rather than inheriting the old connecting flags', async () => {
    const store = useDesktopStore()
    store.setConnecting('previous-workspace')
    mountPanel()
    await flushPromises()
    expect(startDesktop).toHaveBeenCalledExactlyOnceWith(
      workspaceId,
      intent(),
      expect.any(AbortSignal),
    )
    expect(store.workspaceId).toBe(workspaceId)
    expect(store.viewerOwned).toBe(true)
  })
})
