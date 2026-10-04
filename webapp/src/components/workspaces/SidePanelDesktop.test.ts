import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import SidePanelDesktop from './SidePanelDesktop.vue'
import { useDesktopStore } from '@/stores/desktop'
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

    expect(getDesktopStatus).toHaveBeenCalledWith(workspaceId, desktopViewerClientId)
    expect(startDesktop).toHaveBeenCalledWith(workspaceId, intent())
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
    expect(startDesktop).toHaveBeenCalledExactlyOnceWith(workspaceId, intent())
    expect(store.workspaceId).toBe(workspaceId)
    expect(store.viewerOwned).toBe(true)
  })
})
