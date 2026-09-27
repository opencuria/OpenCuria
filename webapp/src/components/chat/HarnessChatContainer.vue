<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { Skeleton } from '@/components/ui/skeleton'
import { MessageSquare } from '@lucide/vue'
import type { HarnessMessage } from '@/types/harness'
import { sortHarnessMessages } from '@/lib/harnessReducer'
import HarnessMessageView from './HarnessMessageView.vue'

const props = defineProps<{
  messages: HarnessMessage[]
  loading?: boolean
  /** Backend `message_id` of the currently streaming assistant turn. */
  streamingMessageId?: string | null
  /** @deprecated Prefer `streamingMessageId`; kept for compat. */
  streamingSessionId?: string | null
  childSessionIds?: Record<string, string>
  /** Hide per-message edit/fork actions (busy run or subagent session). */
  disabled?: boolean
}>()

const emit = defineEmits<{
  openSubtask: [childSessionId: string]
  edit: [messageId: string, text: string]
  fork: [messageId: string]
}>()

const scrollEl = ref<HTMLElement | null>(null)
/** Stick-to-bottom only while the user is already at the bottom. */
const stickToBottom = ref(true)

function isNearBottom(el: HTMLElement): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight < 80
}

function onScroll(): void {
  const el = scrollEl.value
  if (!el) return
  stickToBottom.value = isNearBottom(el)
}

async function scrollToBottom(force = false): Promise<void> {
  await nextTick()
  const el = scrollEl.value
  if (!el) return
  if (force || stickToBottom.value) {
    el.scrollTop = el.scrollHeight
  }
}

/**
 * Backend `position` order with creation-order fallback. Sorting is
 * stable: optimistic `local-*` rows without a position keep mutual order
 * and sit after positioned rows (see `sortHarnessMessages`).
 */
const sortedMessages = computed(() => sortHarnessMessages(props.messages))

const resolvedStreamingId = computed(() => {
  // Deterministic per-turn anchor: the backend `message_id` of the live
  // turn. Only the matching assistant may show the streaming cursor and
  // Thinking gaps — never the previous answer. The legacy session-id
  // fallback resolves to the latest running assistant (used by older
  // callers/tests) but never to a completed turn.
  if (props.streamingMessageId) {
    const match = sortedMessages.value.find(
      (message) => message.id === props.streamingMessageId,
    )
    if (match?.role === 'assistant' && match.completed_at == null) return match.id
    return null
  }
  if (!props.streamingSessionId) return null
  const messages = sortedMessages.value
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i]
    if (
      message?.role === 'assistant' &&
      message.session_id === props.streamingSessionId &&
      message.completed_at == null
    ) {
      return message.id
    }
  }
  return null
})

const lastMessageLength = computed(() => {
  const last = sortedMessages.value[sortedMessages.value.length - 1]
  if (!last) return 0
  return last.content.length + last.parts.reduce((n, p) => n + p.output.length, 0)
})

const lastMessageId = computed(
  () => sortedMessages.value[sortedMessages.value.length - 1]?.id ?? null,
)

onMounted(() => {
  const el = scrollEl.value
  el?.addEventListener('scroll', onScroll, { passive: true })
  void scrollToBottom(true)
})

onUnmounted(() => {
  scrollEl.value?.removeEventListener('scroll', onScroll)
})

watch([() => sortedMessages.value.length, lastMessageLength], () => {
  void scrollToBottom()
})

// Session switch: snap to bottom. Turn starts (`busy`), subtask starts,
// and part deltas must NOT force a scroll reset — the length watcher
// above sticks only while the user is already at the bottom.
watch(
  () => props.messages[0]?.session_id ?? null,
  () => {
    stickToBottom.value = true
    void scrollToBottom(true)
  },
)

// A brand-new trailing message while pinned keeps the anchor pinned
// (covers optimistic follow-up + fresh busy shell without resetting
// scroll on intermediate start/subagent events).
watch(lastMessageId, () => {
  if (stickToBottom.value) void scrollToBottom()
})
</script>

<template>
  <div
    ref="scrollEl"
    class="min-h-0 h-full flex-1 overflow-x-hidden overflow-y-auto px-3 sm:px-6 py-4"
  >
    <!-- The skeleton only covers the true initial load: once messages
         exist, background refreshes (fetchSessions/subagents) must not
         hide the chat behind skeletons. -->
    <div
      v-if="loading && sortedMessages.length === 0"
      class="mx-auto flex w-full max-w-3xl flex-col gap-4"
    >
      <Skeleton class="h-16 w-full" />
      <Skeleton class="h-24 w-full" />
      <Skeleton class="h-12 w-2/3" />
    </div>
    <div v-else-if="sortedMessages.length" class="mx-auto flex w-full max-w-3xl flex-col gap-6">
      <HarnessMessageView
        v-for="message in sortedMessages"
        :key="message.id"
        :message="message"
        :streaming="message.id === resolvedStreamingId"
        :child-session-ids="childSessionIds"
        :disabled="disabled"
        @open-subtask="emit('openSubtask', $event)"
        @edit="(messageId, text) => emit('edit', messageId, text)"
        @fork="(messageId) => emit('fork', messageId)"
      />
    </div>
    <div v-else class="flex h-full flex-col items-center justify-center px-6 py-12 text-center">
      <div class="mb-4 text-muted-foreground">
        <MessageSquare :size="40" />
      </div>
      <h3 class="mb-1 text-lg font-medium text-foreground">Start a chat</h3>
      <p class="max-w-sm text-sm text-muted-foreground">
        Send a message below to begin a new harness session.
      </p>
    </div>
  </div>
</template>
