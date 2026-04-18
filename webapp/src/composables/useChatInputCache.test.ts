import { describe, expect, it, beforeEach } from 'vitest'
import { ref } from 'vue'

import { useChatInputCache } from './useChatInputCache'

describe('useChatInputCache', () => {
  beforeEach(() => {
    sessionStorage.clear()
  })

  it('stores and loads drafts for the current workspace/chat pair', () => {
    const workspaceId = ref('workspace-a')
    const chatId = ref<string | null>('chat-1')
    const cache = useChatInputCache(workspaceId, chatId)

    cache.saveToCache('Hallo Welt')

    expect(cache.loadFromCache()).toBe('Hallo Welt')
    expect(sessionStorage.getItem('chat-input-workspace-a-chat-1')).toBe('Hallo Welt')
  })

  it('switches cache keys when the active chat changes', () => {
    const workspaceId = ref('workspace-a')
    const chatId = ref<string | null>('chat-1')
    const cache = useChatInputCache(workspaceId, chatId)

    cache.saveToCache('Draft fuer Chat 1')
    chatId.value = 'chat-2'

    expect(cache.loadFromCache()).toBe('')

    cache.saveToCache('Draft fuer Chat 2')
    chatId.value = 'chat-1'
    expect(cache.loadFromCache()).toBe('Draft fuer Chat 1')

    workspaceId.value = 'workspace-b'
    expect(cache.loadFromCache()).toBe('')
  })
})
