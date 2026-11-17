import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  PROMPT_FLIGHT_MS,
  armPromptTransition,
  consumePromptTransition,
  playPromptTransition,
} from './promptTransition'

const rect = (top: number) => ({ top, left: 0, width: 100, height: 20 }) as DOMRect

describe('promptTransition', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    consumePromptTransition()
  })

  it('hands an armed rect to exactly one bubble', () => {
    armPromptTransition(rect(500))
    expect(consumePromptTransition()?.top).toBe(500)
    expect(consumePromptTransition()).toBeNull()
  })

  it('lets an unclaimed rect expire', () => {
    vi.useFakeTimers()
    armPromptTransition(rect(500))
    vi.advanceTimersByTime(3_001)
    expect(consumePromptTransition()).toBeNull()
  })

  it('arms nothing when motion is reduced', () => {
    vi.stubGlobal('matchMedia', () => ({ matches: true }))
    armPromptTransition(rect(500))
    expect(consumePromptTransition()).toBeNull()
  })

  it('rises from the composer once the transcript has its new end', () => {
    const frames: FrameRequestCallback[] = []
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      frames.push(callback)
      return frames.length
    })
    const el = document.createElement('div')
    document.body.append(el)
    el.getBoundingClientRect = () => rect(120)
    const animate = vi.fn()
    el.animate = animate

    playPromptTransition(el, rect(520))
    expect(el.style.opacity).toBe('0')
    frames[0]!(0)

    expect(el.style.opacity).toBe('')
    expect(animate).toHaveBeenCalledWith(
      expect.arrayContaining([
        expect.objectContaining({ transform: 'translateY(400px) scale(0.97)', opacity: 0 }),
        expect.objectContaining({ transform: 'none', opacity: 1 }),
      ]),
      expect.anything(),
    )
    // A longer way takes a little longer.
    const duration = animate.mock.calls[0]![1].duration as number
    expect(duration).toBeGreaterThan(PROMPT_FLIGHT_MS)
    expect(duration).toBeLessThan(600)
    el.remove()
  })
})
