import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import SidePanelDesktop from './SidePanelDesktop.vue'
import { useDesktopStore } from '@/stores/desktop'
import { sidebarDesktopHost } from '@/lib/desktopSurfaceHost'
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

const uiStubs = {
  Button: {
    template: '<button v-bind="$attrs" :title="title"><slot /></button>',
    props: ['title'],
  },
  LoadingSpinner: { template: '<div />' },
}

function mountPanel() {
  return mount(SidePanelDesktop, {
    props: { workspaceId: 'ws-1' },
    global: { stubs: uiStubs },
  })
}

describe('SidePanelDesktop', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    localStorage.clear()
    sidebarDesktopHost.value = null
    getDesktopStatus.mockResolvedValue({
      active: true,
      proxy_url: '/ws/desktop/ws-1/',
      viewer_held: false,
      computer_use_active: false,
    })
    startDesktop.mockResolvedValue({ task_id: 'task-1' })
  })

  it('auto-starts the desktop session on mount', async () => {
    mountPanel()
    await flushPromises()

    expect(getDesktopStatus).toHaveBeenCalledWith('ws-1')
    expect(startDesktop).toHaveBeenCalledWith('ws-1')
  })

  it('does not auto-start when already connected', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')

    mountPanel()
    await flushPromises()

    expect(startDesktop).not.toHaveBeenCalled()
  })

  it('opens the desktop modal via the maximize button', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')

    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.get('[data-testid="side-panel-desktop-maximize"]').trigger('click')
    expect(store.isOpen).toBe(true)
  })

  it('registers the sidebar host for the persistent surface while connected', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')

    mountPanel()
    await flushPromises()

    expect(sidebarDesktopHost.value).toBeInstanceOf(HTMLElement)
  })

  it('does not open the modal when clicking the interactive viewport', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')

    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.get('[data-testid="side-panel-desktop-host"]').trigger('click')
    expect(store.isOpen).toBe(false)
  })
})
