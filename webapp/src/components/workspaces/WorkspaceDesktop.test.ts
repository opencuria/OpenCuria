import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import WorkspaceDesktop from './WorkspaceDesktop.vue'
import { useDesktopStore } from '@/stores/desktop'
import { modalDesktopHost } from '@/lib/desktopSurfaceHost'
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

const stopDesktop = vi.mocked(workspacesApi.stopDesktop)

const uiStubs = {
  Button: {
    template: '<button v-bind="$attrs" :title="title"><slot /></button>',
    props: ['title'],
  },
  LoadingSpinner: { template: '<div />' },
  Dialog: { template: '<div><slot /></div>', props: ['open'] },
  DialogContent: { template: '<div><slot /></div>' },
  DialogTitle: { template: '<span><slot /></span>' },
  DialogDescription: { template: '<span><slot /></span>' },
}

function mountDesktop() {
  return mount(WorkspaceDesktop, {
    props: { workspaceId: 'ws-1' },
    global: { stubs: uiStubs },
  })
}

describe('WorkspaceDesktop modal', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    localStorage.clear()
    modalDesktopHost.value = null
    stopDesktop.mockResolvedValue({ task_id: 'task-2' })
  })

  it('sizes the viewport to the desktop aspect ratio', () => {
    const store = useDesktopStore()
    store.open()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')

    const wrapper = mountDesktop()

    const viewport = wrapper.get('[data-testid="desktop-viewport"]')
    expect(viewport.attributes('style')).toContain('aspect-ratio: 1920 / 1080')
    // The width formula itself is covered in desktopGeometry.test.ts;
    // jsdom drops the min()/calc() declaration, so only check the cap
    // override here.
    const modal = wrapper.get('[data-testid="workspace-desktop-modal"]')
    expect(modal.attributes('class')).toContain('sm:max-w-none!')
  })

  it('registers the modal host for the persistent surface while connected', () => {
    const store = useDesktopStore()
    store.open()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')

    mountDesktop()

    expect(modalDesktopHost.value).toBeInstanceOf(HTMLElement)
  })

  it('closes the modal without stopping the session', async () => {
    const store = useDesktopStore()
    store.open()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')

    const wrapper = mountDesktop()
    await flushPromises()

    await wrapper.get('[data-testid="desktop-modal-close"]').trigger('click')
    await flushPromises()

    expect(store.isOpen).toBe(false)
    expect(stopDesktop).not.toHaveBeenCalled()
    expect(store.isConnected).toBe(true)
  })

  it('stops the session via the stop button', async () => {
    const store = useDesktopStore()
    store.open()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')

    const wrapper = mountDesktop()
    await flushPromises()

    await wrapper.get('[data-testid="desktop-modal-stop"]').trigger('click')
    await flushPromises()

    expect(stopDesktop).toHaveBeenCalledWith('ws-1')
    expect(store.isConnected).toBe(false)
  })

  it('shows the not-active state with a start button when disconnected', () => {
    const store = useDesktopStore()
    store.open()

    const wrapper = mountDesktop()

    expect(wrapper.text()).toContain('Desktop session not active')
    expect(wrapper.find('[data-testid="desktop-modal-start"]').exists()).toBe(true)
    expect(modalDesktopHost.value).toBeNull()
  })
})
