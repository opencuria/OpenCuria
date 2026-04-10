import { computed, toValue, type MaybeRefOrGetter } from 'vue'

export function useChatOptionsCache(
  workspaceId: MaybeRefOrGetter<string>,
  chatId?: MaybeRefOrGetter<string | null | undefined>,
) {
  const cacheKey = computed(() => {
    const resolvedWorkspaceId = toValue(workspaceId)
    const resolvedChatId = toValue(chatId) || 'default'
    return `chat-options-${resolvedWorkspaceId}-${resolvedChatId}`
  })

  const loadFromCache = (): Record<string, string> => {
    if (typeof window === 'undefined') return {}

    const cached = localStorage.getItem(cacheKey.value)
    if (!cached) return {}

    try {
      const parsed = JSON.parse(cached)
      return parsed && typeof parsed === 'object' ? parsed as Record<string, string> : {}
    } catch {
      localStorage.removeItem(cacheKey.value)
      return {}
    }
  }

  const saveToCache = (options: Record<string, string>): void => {
    if (typeof window === 'undefined') return

    if (Object.keys(options).length > 0) {
      localStorage.setItem(cacheKey.value, JSON.stringify(options))
    } else {
      localStorage.removeItem(cacheKey.value)
    }
  }

  const clearCache = (): void => {
    if (typeof window === 'undefined') return
    localStorage.removeItem(cacheKey.value)
  }

  return {
    loadFromCache,
    saveToCache,
    clearCache,
  }
}
