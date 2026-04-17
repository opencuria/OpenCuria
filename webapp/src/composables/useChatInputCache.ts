import { computed, toValue, type MaybeRefOrGetter } from 'vue'

export function useChatInputCache(
  workspaceId: MaybeRefOrGetter<string>,
  chatId?: MaybeRefOrGetter<string | null | undefined>,
) {
  const cacheKey = computed(() => {
    const resolvedWorkspaceId = toValue(workspaceId)
    const resolvedChatId = toValue(chatId) || 'default'
    return `chat-input-${resolvedWorkspaceId}-${resolvedChatId}`
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
