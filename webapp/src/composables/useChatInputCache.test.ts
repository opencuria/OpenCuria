import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useChatInputCache } from './useChatInputCache'

describe('useChatInputCache', () => {
  beforeEach(() => {
    sessionStorage.clear()
    vi.stubGlobal('window', window)
  })

  it('keeps the legacy native key and namespaces Claude drafts by engine', () => {
    const native = useChatInputCache('workspace-1', null, 'native')
    const claude = useChatInputCache('workspace-1', null, 'claude')
    native.saveToCache('native draft')
    claude.saveToCache('Claude draft')

    expect(native.loadFromCache()).toBe('native draft')
    expect(claude.loadFromCache()).toBe('Claude draft')
    expect(sessionStorage.getItem('chat-input-workspace-1-default')).toBe('native draft')
    expect(sessionStorage.getItem('chat-input-workspace-1-default-claude')).toBe('Claude draft')
  })
})
