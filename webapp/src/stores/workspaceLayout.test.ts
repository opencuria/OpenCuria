import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { useWorkspaceLayoutStore } from './workspaceLayout'
import { useSidePanelStore } from './sidePanel'
import {
  CHAT_SIDE_STORAGE_KEY,
  DEFAULT_NAVIGATION_WIDTH,
  LEFT_CHAT_WIDTH_STORAGE_KEY,
  MIN_LEFT_CHAT_WIDTH,
  MIN_NAVIGATION_WIDTH,
  MIN_WORKSPACE_CONTENT_WIDTH,
  NAVIGATION_WIDTH_STORAGE_KEY,
} from '@/lib/workspaceLayout'
import { DEFAULT_PANEL_WIDTH, MIN_PANEL_WIDTH } from '@/lib/sidePanel'

async function setViewport(width: number): Promise<void> {
  Object.defineProperty(window, 'innerWidth', { value: width, configurable: true })
  window.dispatchEvent(new Event('resize'))
  await nextTick()
}

beforeEach(async () => {
  localStorage.clear()
  await setViewport(1440)
  setActivePinia(createPinia())
})

afterEach(() => vi.restoreAllMocks())

describe('workspace layout', () => {
  it('remembers the selected chat side and independent navigation/chat widths', () => {
    const layout = useWorkspaceLayoutStore()
    expect(layout.chatSide).toBe('right')
    expect(layout.leftWidth).toBe(DEFAULT_NAVIGATION_WIDTH)
    layout.setLeftWidth(300)
    layout.persistLeftWidth()
    layout.sidebarChatOpen = true
    layout.moveChat()
    expect(layout.leftWidth).toBe(DEFAULT_PANEL_WIDTH)
    layout.setLeftWidth(420)
    layout.persistLeftWidth()
    expect(localStorage.getItem(CHAT_SIDE_STORAGE_KEY)).toBe('left')
    expect(localStorage.getItem(NAVIGATION_WIDTH_STORAGE_KEY)).toBe('300')
    expect(localStorage.getItem(LEFT_CHAT_WIDTH_STORAGE_KEY)).toBe('420')

    setActivePinia(createPinia())
    const reopened = useWorkspaceLayoutStore()
    expect(reopened.chatSide).toBe('left')
    expect(reopened.leftWidth).toBe(300)
    reopened.sidebarChatOpen = true
    expect(reopened.leftWidth).toBe(420)
    reopened.sidebarChatOpen = false
    expect(reopened.leftWidth).toBe(300)
  })

  it('reserves the middle area when either sidebar grows', () => {
    const layout = useWorkspaceLayoutStore()
    layout.rightPanelVisible = true
    layout.setLeftWidth(2000)
    expect(layout.leftWidth).toBe(640)
    layout.setRightWidth(2000)
    expect(layout.rightWidth).toBe(400)
    expect(1440 - layout.leftWidth - layout.rightWidth).toBe(MIN_WORKSPACE_CONTENT_WIDTH)

    layout.setLeftWidth(100)
    expect(layout.leftWidth).toBe(MIN_NAVIGATION_WIDTH)
    layout.setRightWidth(2000)
    layout.setLeftWidth(2000)
    expect(1440 - layout.leftWidth - layout.rightWidth).toBe(MIN_WORKSPACE_CONTENT_WIDTH)
  })

  it('reclamps both panels on window resize without overwriting preferred widths', async () => {
    const layout = useWorkspaceLayoutStore()
    layout.sidebarChatOpen = true
    layout.moveChat()
    layout.rightPanelVisible = true
    layout.setLeftWidth(600)
    layout.persistLeftWidth()
    layout.setRightWidth(600)
    await setViewport(1024)
    expect(layout.leftWidth).toBe(304)
    expect(layout.rightWidth).toBe(MIN_PANEL_WIDTH)
    expect(1024 - layout.leftWidth - layout.rightWidth).toBe(MIN_WORKSPACE_CONTENT_WIDTH)
    expect(localStorage.getItem(LEFT_CHAT_WIDTH_STORAGE_KEY)).toBe('600')
    await setViewport(1440)
    expect(layout.leftWidth).toBe(600)
  })

  it('keeps the small-screen layout while remembering the desktop dock', async () => {
    localStorage.setItem(CHAT_SIDE_STORAGE_KEY, 'left')
    const layout = useWorkspaceLayoutStore()
    layout.sidebarChatOpen = true
    expect(layout.leftChatDocked).toBe(true)
    await setViewport(900)
    expect(layout.leftChatDocked).toBe(false)
    expect(layout.chatSide).toBe('left')
    await setViewport(1440)
    expect(layout.leftChatDocked).toBe(true)
  })

  it('restores navigation collapse and clamps the left chat minimum separately', () => {
    const layout = useWorkspaceLayoutStore()
    layout.navigationOpen = false
    expect(layout.leftWidth).toBe(48)
    layout.sidebarChatOpen = true
    layout.moveChat()
    layout.setLeftWidth(1)
    expect(layout.leftWidth).toBe(MIN_LEFT_CHAT_WIDTH)
    layout.sidebarChatOpen = false
    expect(layout.leftWidth).toBe(48)
  })

  it('falls back on corrupt preferences and tolerates unavailable storage', () => {
    localStorage.setItem(CHAT_SIDE_STORAGE_KEY, 'bottom')
    localStorage.setItem(NAVIGATION_WIDTH_STORAGE_KEY, 'NaN')
    localStorage.setItem(LEFT_CHAT_WIDTH_STORAGE_KEY, '-10')
    const layout = useWorkspaceLayoutStore()
    expect(layout.chatSide).toBe('right')
    expect(layout.leftWidth).toBe(DEFAULT_NAVIGATION_WIDTH)
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('Disabled')
    })
    layout.sidebarChatOpen = true
    expect(() => layout.moveChat()).not.toThrow()
    layout.setLeftWidth(450)
    expect(() => layout.persistLeftWidth()).not.toThrow()
    expect(layout.leftWidth).toBe(450)
  })

  it('limits right-panel rendering for a narrow viewport even with a large saved width', async () => {
    const layout = useWorkspaceLayoutStore()
    useSidePanelStore().setWidth(800)
    layout.rightPanelVisible = true
    await setViewport(1100)
    expect(1100 - layout.leftWidth - layout.rightWidth).toBeGreaterThanOrEqual(
      MIN_WORKSPACE_CONTENT_WIDTH,
    )
    layout.resetLeftWidth()
    expect(layout.leftWidth).toBe(DEFAULT_NAVIGATION_WIDTH)
  })
})
