import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useSidePanelStore } from './sidePanel'
import { useTerminalStore } from './terminal'
import {
  DEFAULT_PANEL_TAB,
  DEFAULT_PANEL_WIDTH,
  MIN_PANEL_WIDTH,
  PANEL_STATE_STORAGE_KEY,
  PANEL_WIDTH_STORAGE_KEY,
} from '@/lib/sidePanel'

describe('sidePanel store', () => {
  beforeEach(() => {
    localStorage.clear()
    Object.defineProperty(window, 'innerWidth', { value: 1280, configurable: true })
    setActivePinia(createPinia())
  })

  it('starts closed on the default tab', () => {
    const store = useSidePanelStore()
    expect(store.isOpen).toBe(false)
    expect(store.activeTab).toBe(DEFAULT_PANEL_TAB)
    expect(store.hasOpened).toBe(false)
    expect(store.width).toBe(DEFAULT_PANEL_WIDTH)
  })

  it('restores the persisted open state and tab', () => {
    localStorage.setItem(
      PANEL_STATE_STORAGE_KEY,
      JSON.stringify({ isOpen: true, activeTab: 'files' }),
    )
    const store = useSidePanelStore()
    expect(store.isOpen).toBe(true)
    expect(store.activeTab).toBe('files')
    expect(store.hasOpened).toBe(true)
  })

  it('toggles visibility and persists it', () => {
    const store = useSidePanelStore()
    store.toggle()
    expect(store.isOpen).toBe(true)
    expect(store.hasOpened).toBe(true)
    expect(JSON.parse(localStorage.getItem(PANEL_STATE_STORAGE_KEY)!)).toMatchObject({
      isOpen: true,
    })
    store.toggle()
    expect(store.isOpen).toBe(false)
    expect(JSON.parse(localStorage.getItem(PANEL_STATE_STORAGE_KEY)!)).toMatchObject({
      isOpen: false,
    })
  })

  it('opens directly on a given tab', () => {
    const store = useSidePanelStore()
    store.open('desktop')
    expect(store.isOpen).toBe(true)
    expect(store.activeTab).toBe('desktop')
  })

  it('switches tabs and persists the selection', () => {
    const store = useSidePanelStore()
    store.open('git')
    store.setTab('files')
    expect(store.activeTab).toBe('files')
    expect(JSON.parse(localStorage.getItem(PANEL_STATE_STORAGE_KEY)!)).toMatchObject({
      activeTab: 'files',
    })
  })

  it('opens the terminal store when the terminal tab is activated', () => {
    const store = useSidePanelStore()
    const terminal = useTerminalStore()
    expect(terminal.isOpen).toBe(false)
    store.open('terminal')
    expect(terminal.isOpen).toBe(true)
  })

  it('clamps and persists the panel width', () => {
    const store = useSidePanelStore()
    store.setWidth(100)
    expect(store.width).toBe(MIN_PANEL_WIDTH)
    store.setWidth(440)
    store.persistWidth()
    expect(localStorage.getItem(PANEL_WIDTH_STORAGE_KEY)).toBe('440')
  })

  it('resets the width to the default', () => {
    const store = useSidePanelStore()
    store.setWidth(600)
    store.resetWidth()
    expect(store.width).toBe(DEFAULT_PANEL_WIDTH)
    expect(localStorage.getItem(PANEL_WIDTH_STORAGE_KEY)).toBe(String(DEFAULT_PANEL_WIDTH))
  })
})
