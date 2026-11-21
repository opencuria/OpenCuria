import { computed, toValue, type MaybeRefOrGetter } from 'vue'
import type { HarnessId } from '@opencuria/chat-core/harness'
import { createChatInputCache, type ChatInputScope } from '@opencuria/chat-core/chatInputCache'

/** Drafts live in this tab: they survive a reload, not closing the tab. */
const cache = createChatInputCache(() => (typeof window === 'undefined' ? null : sessionStorage))

export function useChatInputCache(
  workspaceId: MaybeRefOrGetter<string>,
  chatId?: MaybeRefOrGetter<string | null | undefined>,
  harnessId?: MaybeRefOrGetter<HarnessId | null | undefined>,
) {
  const scope = computed<ChatInputScope>(() => ({
    workspaceId: toValue(workspaceId),
    sessionId: toValue(chatId),
    harnessId: toValue(harnessId),
  }))

  return {
    loadFromCache: (): string => cache.load(scope.value),
    saveToCache: (text: string): void => cache.save(scope.value, text),
    clearCache: (): void => cache.clear(scope.value),
  }
}
