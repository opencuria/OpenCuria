import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  armComposerTransition,
  clearComposerTransition,
  consumeComposerTransition,
  isComposerTransitionPending,
  prefersReducedMotion,
} from './composerTransition'

function makeRect(overrides: Partial<DOMRect> = {}): DOMRect {
  return {
    x: 10,
    y: 20,
    left: 10,
    top: 20,
    right: 310,
    bottom: 100,
    width: 300,
    height: 80,
    toJSON: () => ({}),
    ...overrides,
  } as DOMRect
}

function stubMatchMedia(matches: boolean): void {
  vi.stubGlobal(
    'matchMedia',
    vi.fn().mockReturnValue({ matches }),
  )
}

afterEach(() => {
  clearComposerTransition()
  vi.unstubAllGlobals()
})

describe('composerTransition', () => {
  it('is not pending by default', () => {
    expect(isComposerTransitionPending()).toBe(false)
    expect(consumeComposerTransition()).toBeNull()
  })

  it('returns the armed rect once and then clears it', () => {
    const rect = makeRect()
    armComposerTransition(rect)

    expect(isComposerTransitionPending()).toBe(true)
    expect(consumeComposerTransition()).toBe(rect)
    expect(isComposerTransitionPending()).toBe(false)
    expect(consumeComposerTransition()).toBeNull()
  })

  it('clears an armed transition without consuming', () => {
    armComposerTransition(makeRect())
    clearComposerTransition()

    expect(isComposerTransitionPending()).toBe(false)
    expect(consumeComposerTransition()).toBeNull()
  })

  it('does not arm when reduced motion is requested', () => {
    stubMatchMedia(true)

    expect(prefersReducedMotion()).toBe(true)
    armComposerTransition(makeRect())
    expect(isComposerTransitionPending()).toBe(false)
  })

  it('arms when reduced motion is not requested', () => {
    stubMatchMedia(false)

    expect(prefersReducedMotion()).toBe(false)
    armComposerTransition(makeRect())
    expect(isComposerTransitionPending()).toBe(true)
  })

  it('treats a missing matchMedia (jsdom) as no preference', () => {
    expect(prefersReducedMotion()).toBe(false)
  })
})
