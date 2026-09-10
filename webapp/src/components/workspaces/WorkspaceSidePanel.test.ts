import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

import WorkspaceSidePanel from './WorkspaceSidePanel.vue'
import { useSidePanelStore } from '@/stores/sidePanel'
import {
  DEFAULT_PANEL_WIDTH,
  MIN_PANEL_WIDTH,
  PANEL_WIDTH_STORAGE_KEY,
} from '@/lib/sidePanel'

const tabStubs = {
  WorkspaceTerminal: { template: '<div data-testid="stub-terminal" />', props: ['workspaceId'] },
  SidePanelDesktop: { template: '<div data-testid="stub-desktop" />', props: ['workspaceId'] },
  FileExplorerPanel: { template: '<div data-testid="stub-files" />', props: ['workspaceId'] },
  GitPanel: { template: '<div data-testid="side-panel-git" />', props: ['workspaceId'] },
}

function stubWideLayout(): void {
  Object.defineProperty(window, 'innerWidth', { value: 1280, configurable: true })
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: query.includes('min-width: 1024px'),
    media: query,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
    onchange: null,
  }))
  HTMLElement.prototype.setPointerCapture = vi.fn()
  HTMLElement.prototype.releasePointerCapture = vi.fn()
  HTMLElement.prototype.hasPointerCapture = vi.fn(() => true)
}

function mountPanel() {
  return mount(WorkspaceSidePanel, {
    props: { workspaceId: 'ws-1' },
    global: { stubs: tabStubs },
  })
}

async function dispatchPointer(
  element: Element,
  type: string,
  init: PointerEventInit = {},
): Promise<void> {
  element.dispatchEvent(new PointerEvent(type, { bubbles: true, ...init }))
  await nextTick()
}

describe('WorkspaceSidePanel', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    stubWideLayout()
  })

  it('renders all four tabs and shows the active tab content', () => {
    const store = useSidePanelStore()
    store.open('git')
    const wrapper = mountPanel()

    expect(wrapper.find('[data-testid="side-panel-tab-git"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="side-panel-tab-desktop"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="side-panel-tab-terminal"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="side-panel-tab-files"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="side-panel-git"]').isVisible()).toBe(true)
  })

  it('reflects tab switches from the store', async () => {
    const store = useSidePanelStore()
    store.open('git')
    const wrapper = mountPanel()

    store.setTab('files')
    await nextTick()
    expect(wrapper.find('[data-testid="stub-files"]').exists()).toBe(true)
    expect(
      wrapper.find('[data-testid="side-panel-tab-files"]').attributes('data-active'),
    ).toBeDefined()
  })

  it('switches tabs via the tab bar', async () => {
    const store = useSidePanelStore()
    store.open('terminal')
    const wrapper = mountPanel()

    await wrapper.find('[data-testid="side-panel-tab-files"]').trigger('mousedown')
    expect(store.activeTab).toBe('files')
  })

  it('closes the panel via the mobile close button', async () => {
    const store = useSidePanelStore()
    store.open('terminal')
    const wrapper = mountPanel()

    // The close button is only visible below lg (panel overlays the header toggle there).
    await wrapper.find('[data-testid="side-panel-close"]').trigger('click')
    expect(store.isOpen).toBe(false)
  })

  it('lazily mounts tab contents on first activation', async () => {
    const store = useSidePanelStore()
    store.open('git')
    const wrapper = mountPanel()

    expect(wrapper.find('[data-testid="stub-terminal"]').exists()).toBe(false)

    store.setTab('terminal')
    await nextTick()
    expect(wrapper.find('[data-testid="stub-terminal"]').exists()).toBe(true)

    // Kept alive (hidden) after switching away
    store.setTab('git')
    await nextTick()
    const terminal = wrapper.find('[data-testid="stub-terminal"]')
    expect(terminal.exists()).toBe(true)
    expect(terminal.isVisible()).toBe(false)
  })

  it('renders at the persisted width and resizes via the drag handle', async () => {
    const store = useSidePanelStore()
    store.open('terminal')
    const wrapper = mountPanel()

    expect(wrapper.get('[data-testid="workspace-side-panel"]').attributes('style')).toContain(
      `width: ${DEFAULT_PANEL_WIDTH}px`,
    )

    const handle = wrapper.get('[data-testid="side-panel-resize-handle"]')
    await dispatchPointer(handle.element, 'pointerdown', { clientX: 1000, pointerId: 1 })
    await dispatchPointer(handle.element, 'pointermove', { clientX: 800, pointerId: 1 })
    expect(store.width).toBe(480)

    await dispatchPointer(handle.element, 'pointerup', { pointerId: 1 })
    expect(localStorage.getItem(PANEL_WIDTH_STORAGE_KEY)).toBe('480')
  })

  it('can resize the panel up to three fifths of the viewport', async () => {
    Object.defineProperty(window, 'innerWidth', { value: 1920, configurable: true })
    const store = useSidePanelStore()
    store.open('desktop')
    const wrapper = mountPanel()

    const handle = wrapper.get('[data-testid="side-panel-resize-handle"]')
    await dispatchPointer(handle.element, 'pointerdown', { clientX: 800, pointerId: 1 })
    await dispatchPointer(handle.element, 'pointermove', { clientX: 768, pointerId: 1 })
    expect(store.width).toBe(1152)

    await dispatchPointer(handle.element, 'pointermove', { clientX: 0, pointerId: 1 })
    expect(store.width).toBe(1152)
  })

  it('clamps the width and resets it on double-click', async () => {
    const store = useSidePanelStore()
    store.open('terminal')
    const wrapper = mountPanel()

    const handle = wrapper.get('[data-testid="side-panel-resize-handle"]')
    await dispatchPointer(handle.element, 'pointerdown', { clientX: 1280, pointerId: 1 })
    await dispatchPointer(handle.element, 'pointermove', { clientX: 1270, pointerId: 1 })
    expect(store.width).toBe(MIN_PANEL_WIDTH)
    await dispatchPointer(handle.element, 'pointerup', { pointerId: 1 })

    await handle.trigger('dblclick')
    expect(store.width).toBe(DEFAULT_PANEL_WIDTH)
    expect(localStorage.getItem(PANEL_WIDTH_STORAGE_KEY)).toBe(String(DEFAULT_PANEL_WIDTH))
  })
})
