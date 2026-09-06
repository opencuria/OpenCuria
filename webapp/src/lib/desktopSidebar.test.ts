import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import {
  DEFAULT_SIDEBAR_WIDTH,
  MAX_SIDEBAR_WIDTH,
  MIN_SIDEBAR_WIDTH,
  SIDEBAR_WIDTH_STORAGE_KEY,
  clampSidebarWidth,
  loadSidebarWidth,
  saveSidebarWidth,
} from './desktopSidebar'

describe('desktopSidebar', () => {
  beforeEach(() => {
    localStorage.clear()
    Object.defineProperty(window, 'innerWidth', { value: 1280, configurable: true })
  })

  afterEach(() => {
    localStorage.clear()
  })

  it('clamps to the minimum width', () => {
    expect(clampSidebarWidth(100, 1280)).toBe(MIN_SIDEBAR_WIDTH)
  })

  it('clamps to the maximum width', () => {
    expect(clampSidebarWidth(900, 2000)).toBe(MAX_SIDEBAR_WIDTH)
  })

  it('caps width at 60 percent of the viewport', () => {
    expect(clampSidebarWidth(640, 800)).toBe(480)
  })

  it('keeps the minimum when 60 percent is smaller', () => {
    expect(clampSidebarWidth(200, 400)).toBe(MIN_SIDEBAR_WIDTH)
  })

  it('returns the default for non-finite values', () => {
    expect(clampSidebarWidth(Number.NaN, 1280)).toBe(DEFAULT_SIDEBAR_WIDTH)
  })

  it('round-trips a saved width through localStorage', () => {
    saveSidebarWidth(400)
    expect(localStorage.getItem(SIDEBAR_WIDTH_STORAGE_KEY)).toBe('400')
    expect(loadSidebarWidth()).toBe(400)
  })

  it('clamps values before saving', () => {
    saveSidebarWidth(100)
    expect(localStorage.getItem(SIDEBAR_WIDTH_STORAGE_KEY)).toBe(String(MIN_SIDEBAR_WIDTH))
  })

  it('falls back to the default for invalid stored values', () => {
    localStorage.setItem(SIDEBAR_WIDTH_STORAGE_KEY, 'nope')
    expect(loadSidebarWidth()).toBe(DEFAULT_SIDEBAR_WIDTH)
  })

  it('clamps stored values on load', () => {
    localStorage.setItem(SIDEBAR_WIDTH_STORAGE_KEY, '900')
    expect(loadSidebarWidth()).toBe(MAX_SIDEBAR_WIDTH)
  })

  it('caps a stored width against the current viewport', () => {
    Object.defineProperty(window, 'innerWidth', { value: 800, configurable: true })
    localStorage.setItem(SIDEBAR_WIDTH_STORAGE_KEY, '640')
    expect(loadSidebarWidth()).toBe(480)
  })
})
