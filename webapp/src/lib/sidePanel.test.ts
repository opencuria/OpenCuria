import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import {
  DEFAULT_PANEL_TAB,
  DEFAULT_PANEL_WIDTH,
  MAX_PANEL_WIDTH,
  MIN_PANEL_WIDTH,
  PANEL_STATE_STORAGE_KEY,
  PANEL_WIDTH_STORAGE_KEY,
  clampPanelWidth,
  loadPanelState,
  loadPanelWidth,
  savePanelState,
  savePanelWidth,
} from './sidePanel'

describe('sidePanel', () => {
  beforeEach(() => {
    localStorage.clear()
    Object.defineProperty(window, 'innerWidth', { value: 1280, configurable: true })
  })

  afterEach(() => {
    localStorage.clear()
  })

  it('clamps to the minimum width', () => {
    expect(clampPanelWidth(100, 1280)).toBe(MIN_PANEL_WIDTH)
  })

  it('clamps to the maximum width', () => {
    expect(clampPanelWidth(900, 2000)).toBe(MAX_PANEL_WIDTH)
  })

  it('caps width at 60 percent of the viewport', () => {
    expect(clampPanelWidth(720, 800)).toBe(480)
  })

  it('keeps the minimum when 60 percent is smaller', () => {
    expect(clampPanelWidth(200, 400)).toBe(MIN_PANEL_WIDTH)
  })

  it('returns the default for non-finite values', () => {
    expect(clampPanelWidth(Number.NaN, 1280)).toBe(DEFAULT_PANEL_WIDTH)
  })

  it('round-trips a saved width through localStorage', () => {
    savePanelWidth(420)
    expect(localStorage.getItem(PANEL_WIDTH_STORAGE_KEY)).toBe('420')
    expect(loadPanelWidth()).toBe(420)
  })

  it('falls back to the default for invalid stored values', () => {
    localStorage.setItem(PANEL_WIDTH_STORAGE_KEY, 'nope')
    expect(loadPanelWidth()).toBe(DEFAULT_PANEL_WIDTH)
  })

  it('clamps a stored width against the current viewport', () => {
    Object.defineProperty(window, 'innerWidth', { value: 800, configurable: true })
    localStorage.setItem(PANEL_WIDTH_STORAGE_KEY, '720')
    expect(loadPanelWidth()).toBe(480)
  })

  it('defaults to a closed panel on the default tab', () => {
    expect(loadPanelState()).toEqual({ isOpen: false, activeTab: DEFAULT_PANEL_TAB })
  })

  it('round-trips the persisted panel state', () => {
    savePanelState({ isOpen: true, activeTab: 'desktop' })
    expect(loadPanelState()).toEqual({ isOpen: true, activeTab: 'desktop' })
  })

  it('falls back to the default tab for unknown stored tabs', () => {
    localStorage.setItem(
      PANEL_STATE_STORAGE_KEY,
      JSON.stringify({ isOpen: true, activeTab: 'subscriptions' }),
    )
    expect(loadPanelState()).toEqual({ isOpen: true, activeTab: DEFAULT_PANEL_TAB })
  })

  it('falls back to defaults for invalid stored state', () => {
    localStorage.setItem(PANEL_STATE_STORAGE_KEY, 'not-json')
    expect(loadPanelState()).toEqual({ isOpen: false, activeTab: DEFAULT_PANEL_TAB })
  })
})
