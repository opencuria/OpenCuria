import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  createDesktopReconnectBackoff,
  createPausableTimer,
  isTrustedDesktopMessage,
  parseDesktopConnectionStatus,
} from './desktopSurfaceRecovery'

describe('desktop surface recovery helpers', () => {
  afterEach(() => vi.useRealTimers())

  it('accepts only supported KasmVNC connection status messages', () => {
    expect(parseDesktopConnectionStatus({ action: 'connection_state', value: 'connected' })).toBe(
      'connected',
    )
    expect(
      parseDesktopConnectionStatus({ action: 'connection_state', value: 'disconnected' }),
    ).toBe('disconnected')
    expect(parseDesktopConnectionStatus({ action: 'connection_state', value: 'connecting' })).toBe(
      'connecting',
    )
    expect(parseDesktopConnectionStatus({ action: 'other', value: 'connected' })).toBeNull()
    expect(
      parseDesktopConnectionStatus({ action: 'connection_state', value: 'connected\ntoken' }),
    ).toBeNull()
    expect(parseDesktopConnectionStatus(null)).toBeNull()
  })

  it('authenticates messages using both iframe source and resolved iframe origin', () => {
    const iframe = document.createElement('iframe')
    iframe.src = 'https://app.example/desktop/viewer?token=secret'
    document.body.append(iframe)
    const source = iframe.contentWindow
    const pageUrl = 'https://app.example/workspaces/1'

    expect(
      isTrustedDesktopMessage({ source, origin: 'https://app.example' }, iframe, pageUrl),
    ).toBe(true)
    expect(
      isTrustedDesktopMessage({ source: window, origin: 'https://app.example' }, iframe, pageUrl),
    ).toBe(false)
    expect(
      isTrustedDesktopMessage({ source, origin: 'https://attacker.example' }, iframe, pageUrl),
    ).toBe(false)
    expect(
      isTrustedDesktopMessage({ source: null, origin: 'https://app.example' }, iframe, pageUrl),
    ).toBe(false)
  })

  it('pauses timeout duration while the document is hidden', () => {
    vi.useFakeTimers()
    const callback = vi.fn()
    const timer = createPausableTimer(callback)

    timer.start(1_000)
    vi.advanceTimersByTime(400)
    timer.pause()
    vi.advanceTimersByTime(10_000)
    expect(callback).not.toHaveBeenCalled()

    timer.resume()
    vi.advanceTimersByTime(599)
    expect(callback).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1)
    expect(callback).toHaveBeenCalledOnce()
    expect(timer.pending).toBe(false)
  })

  it('uses capped exponential retries and stops after three attempts', () => {
    vi.useFakeTimers()
    const retry = vi.fn()
    const exhausted = vi.fn()
    const recovery = createDesktopReconnectBackoff(retry, exhausted)

    recovery.schedule()
    expect(recovery.attempts).toBe(1)
    vi.advanceTimersByTime(999)
    expect(retry).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1)
    expect(retry).toHaveBeenCalledTimes(1)

    recovery.schedule()
    expect(recovery.attempts).toBe(2)
    vi.advanceTimersByTime(2_000)
    expect(retry).toHaveBeenCalledTimes(2)

    recovery.schedule()
    expect(recovery.attempts).toBe(3)
    vi.advanceTimersByTime(4_000)
    expect(retry).toHaveBeenCalledTimes(3)

    recovery.schedule()
    expect(exhausted).toHaveBeenCalledOnce()
    expect(recovery.attempts).toBe(3)
  })

  it('resets retries after a successful connection and supports immediate user retry', () => {
    vi.useFakeTimers()
    const retry = vi.fn()
    const exhausted = vi.fn()
    const recovery = createDesktopReconnectBackoff(retry, exhausted)

    recovery.schedule()
    expect(recovery.attempts).toBe(1)
    recovery.reset()
    expect(recovery.attempts).toBe(0)

    recovery.schedule()
    recovery.retryNow()
    expect(retry).toHaveBeenCalledOnce()
    expect(recovery.attempts).toBe(0)
    expect(exhausted).not.toHaveBeenCalled()
  })
})
