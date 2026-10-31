import { afterEach, describe, expect, it, vi } from 'vitest'
import { createSecureUuid } from './secureUuid'

afterEach(() => vi.unstubAllGlobals())

describe('createSecureUuid', () => {
  it('uses the browser UUID API when available', () => {
    const id = '11111111-1111-4111-8111-111111111111'
    const randomUUID = vi.fn(() => id)
    const getRandomValues = vi.fn()
    vi.stubGlobal('crypto', { randomUUID, getRandomValues })

    expect(createSecureUuid()).toBe(id)
    expect(randomUUID).toHaveBeenCalledOnce()
    expect(getRandomValues).not.toHaveBeenCalled()
  })

  it('generates a version 4 UUID with secure randomness when HTTP hides randomUUID', () => {
    const getRandomValues = vi.fn((bytes: Uint8Array) => bytes.fill(255))
    vi.stubGlobal('crypto', { getRandomValues })

    expect(createSecureUuid()).toBe('ffffffff-ffff-4fff-bfff-ffffffffffff')
    expect(getRandomValues).toHaveBeenCalledOnce()
    expect(getRandomValues.mock.calls[0]?.[0]).toBeInstanceOf(Uint8Array)
    expect(getRandomValues.mock.calls[0]?.[0]).toHaveLength(16)
  })

  it('fails explicitly when the browser offers no secure randomness', () => {
    vi.stubGlobal('crypto', undefined)
    expect(createSecureUuid()).toBeNull()
    vi.stubGlobal('crypto', {})
    expect(createSecureUuid()).toBeNull()
  })

  it('handles an unavailable cryptographic implementation without weak random IDs', () => {
    vi.stubGlobal('crypto', {
      getRandomValues: () => {
        throw new Error('Unavailable')
      },
    })
    expect(createSecureUuid()).toBeNull()
  })
})
