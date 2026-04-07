import { computed } from 'vue'

export function useChatInputCache(workspaceId: string, chatId?: string | null) {
  const cacheKey = computed(() => {
    const chatIdStr = chatId || 'default'
    return `chat-input-${workspaceId}-${chatIdStr}`
  })

  const loadFromCache = (): string => {
    if (typeof window === 'undefined') return ''
    const cached = sessionStorage.getItem(cacheKey.value)
    return cached || ''
  }

  const saveToCache = (text: string): void => {
    if (typeof window === 'undefined') return
    if (text.trim()) {
      sessionStorage.setItem(cacheKey.value, text)
    } else {
      sessionStorage.removeItem(cacheKey.value)
    }
  }

  const clearCache = (): void => {
    if (typeof window === 'undefined') return
    sessionStorage.removeItem(cacheKey.value)
  }

  return {
    loadFromCache,
    saveToCache,
    clearCache,
  }
}
