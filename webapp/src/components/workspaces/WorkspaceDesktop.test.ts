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
}))

vi.mock('@/services/config', () => ({
  getConfig: () => ({ apiBaseUrl: '', wsBaseUrl: '' }),
}))

vi.mock('@/services/socket', () => ({
  onEvent: vi.fn(() => () => {}),
}))

const stopDesktop = vi.mocked(workspacesApi.stopDesktop)

const dialogStub = {
  name: 'DialogStub',
  props: ['open', 'modal'],
  template: '<div data-testid="dialog-root" :data-modal="modal"><slot /></div>',
}
const dialogContentStub = {
  name: 'DialogContentStub',
  emits: ['interactOutside'],
  template: '<div><slot /></div>',
}

const uiStubs = {
  Button: {
    template: '<button v-bind="$attrs" :title="title"><slot /></button>',
    props: ['title'],
  },
  LoadingSpinner: { template: '<div />' },
  Dialog: dialogStub,
  DialogContent: dialogContentStub,
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

  it('keeps the native iframe interactive while preserving ordinary outside-dismiss behavior', () => {
    const store = useDesktopStore()
    store.open()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')

    const wrapper = mountDesktop()
    const dialog = wrapper.findComponent(dialogStub)
    const content = wrapper.findComponent(dialogContentStub)
    expect(dialog.props('modal')).toBe(false)

    const surface = document.createElement('div')
    surface.dataset.testid = 'desktop-surface'
    const iframe = document.createElement('iframe')
    surface.append(iframe)
    document.body.append(surface)

    const nativeFrameClick = new MouseEvent('pointerdown', { bubbles: true })
    Object.defineProperty(nativeFrameClick, 'target', { value: iframe })
    const nativeOutsideEvent = new CustomEvent('interactOutside', { detail: { originalEvent: nativeFrameClick }, cancelable: true })
    content.vm.$emit('interactOutside', nativeOutsideEvent)
    expect(nativeOutsideEvent.defaultPrevented).toBe(true)

    const regularOutsideClick = new MouseEvent('pointerdown', { bubbles: true })
    Object.defineProperty(regularOutsideClick, 'target', { value: document.body })
    const regularOutsideEvent = new CustomEvent('interactOutside', { detail: { originalEvent: regularOutsideClick }, cancelable: true })
    content.vm.$emit('interactOutside', regularOutsideEvent)
    expect(regularOutsideEvent.defaultPrevented).toBe(false)
    surface.remove()
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
    const { getDesktopStatus } = await import('@/services/workspaces.api')
    vi.mocked(getDesktopStatus).mockResolvedValue({
      active: false,
      proxy_url: null,
      viewer_held: false,
      computer_use_active: false,
    })

    const wrapper = mountDesktop()
    await flushPromises()

    await wrapper.get('[data-testid="desktop-modal-stop"]').trigger('click')
    await flushPromises()

    expect(stopDesktop).toHaveBeenCalledWith('ws-1')
    expect(store.isConnected).toBe(false)
  })

  it('does not render the removed VM/local clipboard controls', () => {
    const store = useDesktopStore()
    store.open()
    store.setConnected('ws-1', '/ws/desktop/ws-1/')

    const wrapper = mountDesktop()

    expect(wrapper.find('[title="Copy VM clipboard to local clipboard"]').exists()).toBe(false)
    expect(wrapper.find('[title="Paste local clipboard into VM clipboard"]').exists()).toBe(false)
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
