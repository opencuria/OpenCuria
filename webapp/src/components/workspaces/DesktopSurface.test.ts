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
    template:
      '<button v-bind="$attrs" :title="title" @click="$emit(\'click\', $event)"><slot /></button>',
    props: ['title'],
    emits: ['click'],
  },
  LoadingSpinner: { template: '<div />' },
}

let sidebarHost: HTMLElement
let modalHost: HTMLElement
const wrappers: VueWrapper[] = []

function setHostRect(
  host: HTMLElement,
  left: number,
  top: number,
  width: number,
  height: number,
): void {
  vi.spyOn(host, 'getBoundingClientRect').mockReturnValue({
    x: left,
    y: top,
    left,
    top,
    right: left + width,
    bottom: top + height,
    width,
    height,
    toJSON: () => ({}),
  } as DOMRect)
}

function mountSurface() {
  const wrapper = mount(DesktopSurface, {
    attachTo: document.body,
    props: { workspaceId: 'ws-1' },
    global: { stubs: uiStubs },
  })
  wrappers.push(wrapper)
  return wrapper
}

function connectionMessage(
  source: MessageEventSource | null,
  value: string,
  origin = window.location.origin,
) {
  window.dispatchEvent(
    new MessageEvent('message', {
      data: { action: 'connection_state', value },
      source,
      origin,
    }),
  )
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
    setHostRect(sidebarHost, 10, 20, 320, 240)
    setHostRect(modalHost, 50, 60, 800, 450)
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
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => 'visible',
    })
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('keeps one fixed iframe node while overlaying the active host rectangle', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')

    mountSurface()
    await nextTick()

    const surface = document.querySelector('[data-testid="desktop-surface"]') as HTMLElement
    const iframe = surface.querySelector('iframe')
    expect(iframe).toBeTruthy()
    expect(surface.parentElement).not.toBe(sidebarHost)
    expect(iframe?.getAttribute('src')).toContain('/ws/desktop/ws-1/')
    expect(iframe?.getAttribute('src')).toContain('token=tok')
    expect(surface.style.position).toBe('fixed')
    expect(surface.style.left).toBe('10px')
    expect(surface.style.top).toBe('20px')
    expect(surface.style.width).toBe('320px')
    expect(surface.style.height).toBe('240px')

    store.open()
    await nextTick()
    expect(document.querySelector('[data-testid="desktop-surface"] iframe')).toBe(iframe)
    expect(surface.parentElement).not.toBe(modalHost)
    expect(surface.style.left).toBe('50px')
    expect(surface.style.top).toBe('60px')
    expect(surface.style.width).toBe('800px')
    expect(surface.style.height).toBe('450px')

    store.close()
    await nextTick()
    expect(document.querySelector('[data-testid="desktop-surface"] iframe')).toBe(iframe)
    expect(surface.style.left).toBe('10px')
  })

  it('keeps the iframe mounted but hides it when no visible host exists', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    sidebarDesktopHost.value = null

    const wrapper = mountSurface()
    await nextTick()

    expect(wrapper.find('iframe').exists()).toBe(true)
    expect(
      (document.querySelector('[data-testid="desktop-surface"]') as HTMLElement).style.visibility,
    ).toBe('hidden')
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

  it('shows the computer-use overlay over the fixed surface', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    store.setComputerUseActive(true)

    mountSurface()
    await nextTick()

    expect(document.body.textContent).toContain('Computer-use is controlling this desktop')
  })

  it('requires KasmVNC connection status after iframe load', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')

    mountSurface()
    await nextTick()

    const iframe = document.querySelector('iframe')!
    expect(iframe.classList.contains('opacity-0')).toBe(true)
    iframe.dispatchEvent(new Event('load'))
    await nextTick()
    expect(document.querySelector('[data-testid="desktop-surface-loading"]')).toBeTruthy()

    connectionMessage(iframe.contentWindow, 'connected')
    await nextTick()
    expect(iframe.classList.contains('opacity-0')).toBe(false)
    expect(document.querySelector('[data-testid="desktop-surface-loading"]')).toBeNull()
  })

  it('ignores messages from the wrong origin or a different frame', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    mountSurface()
    await nextTick()

    const iframe = document.querySelector('iframe')!
    connectionMessage(iframe.contentWindow, 'connected', 'https://attacker.example')
    connectionMessage(window, 'connected')
    await nextTick()
    expect(iframe.classList.contains('opacity-0')).toBe(true)

    connectionMessage(iframe.contentWindow, 'connected')
    await nextTick()
    expect(iframe.classList.contains('opacity-0')).toBe(false)
  })

  it('ignores a delayed status from an older iframe generation', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    mountSurface()
    await nextTick()
    const oldFrame = document.querySelector('iframe')!

    store.bumpViewer()
    await nextTick()
    const currentFrame = document.querySelector('iframe')!
    expect(currentFrame).not.toBe(oldFrame)
    connectionMessage(oldFrame.contentWindow, 'connected')
    await nextTick()
    expect(currentFrame.classList.contains('opacity-0')).toBe(true)
  })

  it('renders visible Retry on disconnect and retries by bumping the viewer', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    mountSurface()
    await nextTick()
    const iframe = document.querySelector('iframe')!

    connectionMessage(iframe.contentWindow, 'disconnected')
    await nextTick()
    expect(document.querySelector('[data-testid="desktop-surface-retry"]')).toBeTruthy()
    expect(document.body.textContent).toContain('Reconnecting')

    document.querySelector<HTMLButtonElement>('[data-testid="desktop-surface-retry"]')!.click()
    await nextTick()
    expect(store.viewerGeneration).toBe(1)
  })

  it('does not remount a healthy iframe when the tab becomes visible', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    mountSurface()
    await nextTick()
    const iframe = document.querySelector('iframe')!
    connectionMessage(iframe.contentWindow, 'connected')
    await nextTick()

    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => 'visible',
    })
    document.dispatchEvent(new Event('visibilitychange'))
    await nextTick()

    expect(store.viewerGeneration).toBe(0)
    expect(document.querySelector('iframe')).toBe(iframe)
  })

  it('pauses recovery while hidden and does not reload on hidden transitions', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    mountSurface()
    await nextTick()

    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => 'hidden',
    })
    document.dispatchEvent(new Event('visibilitychange'))
    expect(store.viewerGeneration).toBe(0)

    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => 'visible',
    })
    document.dispatchEvent(new Event('visibilitychange'))
    await nextTick()
    expect(store.viewerGeneration).toBe(0)
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

  it('shows connecting content until Kasm reports connected', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')
    mountSurface()
    await nextTick()

    expect(document.querySelector('[data-testid="desktop-surface-loading"]')).toBeTruthy()
    connectionMessage(document.querySelector('iframe')!.contentWindow, 'connecting')
    await nextTick()
    expect(document.querySelector('[data-testid="desktop-surface-loading"]')).toBeTruthy()
  })

  it('remounts the iframe when viewer generation bumps', async () => {
    const store = useDesktopStore()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')

    mountSurface()
    await nextTick()
    const iframe = document.querySelector('iframe')!
    store.bumpViewer()
    await nextTick()

    const nextIframe = document.querySelector('iframe')
    expect(nextIframe).toBeTruthy()
    expect(nextIframe).not.toBe(iframe)
    expect(nextIframe?.classList.contains('opacity-0')).toBe(true)
    expect(document.querySelector('[data-testid="desktop-surface-loading"]')).toBeTruthy()
  })
})
