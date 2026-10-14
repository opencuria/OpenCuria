import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import WorkspaceDesktop from './WorkspaceDesktop.vue'
import { useDesktopStore } from '@/stores/desktop'
import { modalDesktopHost } from '@/lib/desktopSurfaceHost'
import {
  desktopViewerClientId,
  desktopViewerSession,
  retainDesktopViewer,
  acquireDesktopViewer,
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

const wrappers: VueWrapper[] = []
const originalClipboard = navigator.clipboard
let workspaceId = ''
let index = 0
const intent = () => ({
  viewer_client_id: desktopViewerClientId,
  intent_revision: desktopViewerSession(workspaceId).revision,
})
afterEach(async () => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  await flushPromises()
  vi.unstubAllGlobals()
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: originalClipboard,
  })
})

function mountDesktop() {
  const wrapper = mount(WorkspaceDesktop, {
    props: { workspaceId },
    global: { stubs: uiStubs },
  })
  wrappers.push(wrapper)
  return wrapper
}

describe('WorkspaceDesktop modal', () => {
  beforeEach(() => {
    workspaceId = `WorkspaceDesktop-${++index}`
    setActivePinia(createPinia())
    vi.clearAllMocks()
    localStorage.clear()
    modalDesktopHost.value = null
    stopDesktop.mockResolvedValue({ task_id: 'task-2' })
  })

  it('sizes the viewport to the desktop aspect ratio', () => {
    const store = useDesktopStore()
    store.open()
    store.setConnected(workspaceId, `/ws/desktop/${workspaceId}/`)

    const wrapper = mountDesktop()

    const viewport = wrapper.get('[data-testid="desktop-viewport"]')
    expect(viewport.attributes('style')).toContain('aspect-ratio: 1920 / 1080')
    // The width formula itself is covered in desktopGeometry.test.ts;
    // jsdom drops the min()/calc() declaration, so only check the cap
    // override here.
    const modal = wrapper.get('[data-testid="workspace-desktop-modal"]')
    expect(modal.attributes('class')).toContain('sm:max-w-none!')
    expect(modal.attributes('class')).toContain('z-(--z-desktop-modal)')
    expect(modal.attributes('overlay-class')).toBe('z-(--z-desktop-modal)')
  })

  it('registers the modal host for the persistent surface while connected', () => {
    const store = useDesktopStore()
    store.open()
    store.setConnected(workspaceId, `/ws/desktop/${workspaceId}/`)

    mountDesktop()

    expect(modalDesktopHost.value).toBeInstanceOf(HTMLElement)
  })

  it('closes the modal without stopping the session', async () => {
    const store = useDesktopStore()
    store.open()
    store.setConnected(workspaceId, `/ws/desktop/${workspaceId}/`)

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
    store.setConnected(workspaceId, `/ws/desktop/${workspaceId}/`)
    const owner = {}
    retainDesktopViewer(workspaceId, owner)
    vi.mocked(workspacesApi.startDesktop).mockResolvedValue({ task_id: 'start' })
    vi.mocked(workspacesApi.getDesktopStatus).mockImplementation(async () => ({
      active: true,
      proxy_url: `/ws/desktop/${workspaceId}/`,
      viewer_held: true,
      computer_use_active: false,
      viewer_lease_state: 'held',
      revision: desktopViewerSession(workspaceId).revision,
      epoch: 'epoch',
    }))
    await acquireDesktopViewer(workspaceId)
    vi.mocked(workspacesApi.getDesktopStatus).mockResolvedValue({
      active: false,
      proxy_url: null,
      viewer_held: false,
      computer_use_active: false,
      viewer_lease_state: 'released',
      revision: desktopViewerSession(workspaceId).revision,
      epoch: 'epoch',
    })

    const wrapper = mountDesktop()
    await flushPromises()

    await wrapper.get('[data-testid="desktop-modal-stop"]').trigger('click')
    await flushPromises()

    expect(stopDesktop).toHaveBeenCalledWith(workspaceId, intent())
    expect(store.isConnected).toBe(false)
  })

  it('routes toolbar copy and paste clicks through their explicit clipboard actions', async () => {
    const store = useDesktopStore()
    store.setConnected(workspaceId, `/ws/desktop/${workspaceId}/`)
    vi.mocked(workspacesApi.readDesktopClipboard).mockResolvedValue({ text: 'VM clipboard value' })
    vi.mocked(workspacesApi.writeDesktopClipboard).mockResolvedValue({ text: '' })

    class MockClipboardItem {
      constructor(readonly values: Record<string, unknown>) {}
    }
    const write = vi.fn(async (items: MockClipboardItem[]) => {
      const textBlob = (await items[0]!.values['text/plain']) as Blob
      expect(textBlob).toBeInstanceOf(Blob)
      expect(await textBlob.text()).toBe('VM clipboard value')
    })
    const readText = vi.fn(async () => 'local clipboard value')
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { readText, write },
    })
    vi.stubGlobal('ClipboardItem', MockClipboardItem)

    const wrapper = mountDesktop()
    await wrapper.get('[title="Copy VM clipboard to local clipboard"]').trigger('click')
    await flushPromises()

    expect(workspacesApi.readDesktopClipboard).toHaveBeenCalledWith(workspaceId)
    expect(write).toHaveBeenCalledOnce()

    await wrapper.get('[title="Paste local clipboard into VM clipboard"]').trigger('click')
    await flushPromises()

    expect(readText).toHaveBeenCalledOnce()
    expect(workspacesApi.writeDesktopClipboard).toHaveBeenCalledOnce()
    expect(workspacesApi.writeDesktopClipboard).toHaveBeenCalledWith(
      workspaceId,
      'local clipboard value',
    )
    expect(typeof vi.mocked(workspacesApi.writeDesktopClipboard).mock.calls[0]?.[1]).toBe('string')
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
