import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

import DesktopSurface from './DesktopSurface.vue'
import { useDesktopStore } from '@/stores/desktop'
import { sidebarDesktopHost, modalDesktopHost } from '@/lib/desktopSurfaceHost'
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

const uiStubs = {
  Button: {
    template: '<button v-bind="$attrs" :title="title"><slot /></button>',
    props: ['title'],
  },
  LoadingSpinner: { template: '<div />' },
}

let sidebarHost: HTMLElement
let modalHost: HTMLElement
const wrappers: VueWrapper[] = []

function mountSurface() {
  const wrapper = mount(DesktopSurface, {
    props: { workspaceId: 'ws-1' },
    global: { stubs: uiStubs },
  })
  wrappers.push(wrapper)
  return wrapper
}

describe('DesktopSurface', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    localStorage.clear()
    localStorage.setItem('kern_access_token', 'tok')
    class ResizeObserverStub {
      observe(): void {}
      disconnect(): void {}
      unobserve(): void {}
    }
    vi.stubGlobal('ResizeObserver', ResizeObserverStub)

    document.body.innerHTML = ''
    sidebarHost = document.createElement('div')
    modalHost = document.createElement('div')
    document.body.append(sidebarHost, modalHost)
    sidebarDesktopHost.value = sidebarHost
    modalDesktopHost.value = modalHost

    getDesktopStatus.mockResolvedValue({
      active: true,
      proxy_url: '/ws/desktop/ws-1/',
      viewer_held: false,
      computer_use_active: false,
    })
    startDesktop.mockResolvedValue({ task_id: 'task-1' })
    stopDesktop.mockResolvedValue({ task_id: 'task-2' })
  })

  afterEach(() => {
    while (wrappers.length) wrappers.pop()?.unmount()
    sidebarDesktopHost.value = null
    modalDesktopHost.value = null
    document.body.innerHTML = ''
  })

  it('teleports the iframe into the sidebar host by default', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')

    mountSurface()
    await nextTick()

    const iframe = sidebarHost.querySelector('iframe')
    expect(iframe).toBeTruthy()
    expect(iframe?.getAttribute('src')).toContain('/ws/desktop/ws-1/')
    expect(iframe?.getAttribute('src')).toContain('token=tok')
    expect(modalHost.querySelector('iframe')).toBeNull()
  })

  it('moves the same iframe element into the modal host without remounting', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')

    mountSurface()
    await nextTick()
    const before = sidebarHost.querySelector('iframe')
    expect(before).toBeTruthy()

    store.open()
    await nextTick()

    const after = modalHost.querySelector('iframe')
    expect(after).toBeTruthy()
    expect(after).toBe(before)
    expect(sidebarHost.querySelector('iframe')).toBeNull()

    store.close()
    await nextTick()
    expect(sidebarHost.querySelector('iframe')).toBe(before)
  })

  it('keeps the iframe alive offscreen when no visible host exists', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    sidebarDesktopHost.value = null

    const wrapper = mountSurface()
    await nextTick()

    const iframe = wrapper.find('iframe')
    expect(iframe.exists()).toBe(true)
  })

  it('auto-starts the session when the modal opens', async () => {
    const store = useDesktopStore()
    mountSurface()
    await flushPromises()
    expect(startDesktop).not.toHaveBeenCalled()

    store.open()
    await flushPromises()

    expect(getDesktopStatus).toHaveBeenCalledWith('ws-1')
    expect(startDesktop).toHaveBeenCalledWith('ws-1')
  })

  it('shows the computer-use overlay over the surface', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    store.setComputerUseActive(true)

    mountSurface()
    await nextTick()

    expect(sidebarHost.textContent).toContain('Computer-use is controlling this desktop')
  })

  it('hides the iframe behind a placeholder until it has loaded', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')

    mountSurface()
    await nextTick()

    const iframe = sidebarHost.querySelector('iframe')!
    expect(iframe.classList.contains('opacity-0')).toBe(true)
    expect(sidebarHost.querySelector('[data-testid="desktop-surface-loading"]')).toBeTruthy()

    iframe.dispatchEvent(new Event('load'))
    await nextTick()

    expect(iframe.classList.contains('opacity-0')).toBe(false)
    expect(sidebarHost.querySelector('[data-testid="desktop-surface-loading"]')).toBeNull()
  })

  it('stops the session on unmount', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')

    const wrapper = mountSurface()
    await flushPromises()
    wrapper.unmount()
    await flushPromises()

    expect(stopDesktop).toHaveBeenCalledWith('ws-1')
  })
})
