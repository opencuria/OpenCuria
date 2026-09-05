<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { Skeleton } from '@/components/ui/skeleton'
import { MessageSquare } from '@lucide/vue'
import type { HarnessMessage, HarnessTodo } from '@/types/harness'
import HarnessMessageView from './HarnessMessageView.vue'
import HarnessTodoList from './HarnessTodoList.vue'

const props = defineProps<{
  messages: HarnessMessage[]
  todos?: HarnessTodo[]
  loading?: boolean
  streamingSessionId?: string | null
  childSessionIds?: Record<string, string>
}>()

const emit = defineEmits<{
  openSubtask: [childSessionId: string]
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

const sortedMessages = computed(() =>
  [...props.messages].sort((a, b) => {
    const aTime = a.created_at ? new Date(a.created_at).getTime() : Number.POSITIVE_INFINITY
    const bTime = b.created_at ? new Date(b.created_at).getTime() : Number.POSITIVE_INFINITY
    if (aTime !== bTime) return aTime - bTime
    return 0
  }),
)

const streamingMessageId = computed(() => {
  if (!props.streamingSessionId) return null
  const messages = sortedMessages.value
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i]
    if (message?.role === 'assistant' && message.session_id === props.streamingSessionId) {
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

watch(
  () => props.messages[0]?.session_id ?? null,
  () => {
    stickToBottom.value = true
    void scrollToBottom(true)
  },
)
</script>

<template>
  <div
    ref="scrollEl"
    class="min-h-0 h-full flex-1 overflow-x-hidden overflow-y-auto px-3 sm:px-6 py-4"
  >
    <div v-if="loading" class="mx-auto flex w-full max-w-3xl flex-col gap-4">
      <Skeleton class="h-16 w-full" />
      <Skeleton class="h-24 w-full" />
      <Skeleton class="h-12 w-2/3" />
    </div>
    <div v-else-if="sortedMessages.length" class="mx-auto flex w-full max-w-3xl flex-col gap-6">
      <HarnessTodoList v-if="todos && todos.length" :todos="todos" />
      <HarnessMessageView
        v-for="message in sortedMessages"
        :key="message.id"
        :message="message"
        :streaming="message.id === streamingMessageId"
        :child-session-ids="childSessionIds"
        @open-subtask="emit('openSubtask', $event)"
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
