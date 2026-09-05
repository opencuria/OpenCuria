<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { ScrollArea } from '@/components/ui/scroll-area'
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

const scrollContainer = ref<InstanceType<typeof ScrollArea> | null>(null)
/** Stick-to-bottom only while the user is already at the bottom. */
const stickToBottom = ref(true)

function viewportEl(): HTMLElement | null {
  return (scrollContainer.value?.$el?.querySelector(
    '[data-slot="scroll-area-viewport"]',
  ) ?? null) as HTMLElement | null
}

function isNearBottom(el: HTMLElement): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight < 80
}

function onScroll(): void {
  const el = viewportEl()
  if (!el) return
  stickToBottom.value = isNearBottom(el)
}

async function scrollToBottom(force = false): Promise<void> {
  await nextTick()
  const el = viewportEl()
  if (!el) return
  if (force || stickToBottom.value) {
    el.scrollTop = el.scrollHeight
  }
}

const sortedMessages = computed(() =>
  [...props.messages].sort((a, b) => {
    const aTime = a.created_at ? new Date(a.created_at).getTime() : 0
    const bTime = b.created_at ? new Date(b.created_at).getTime() : 0
    return aTime - bTime
  }),
)

const lastMessageLength = computed(() => {
  const last = sortedMessages.value[sortedMessages.value.length - 1]
  if (!last) return 0
  return last.content.length + last.parts.reduce((n, p) => n + p.output.length, 0)
})

onMounted(() => {
  const el = viewportEl()
  el?.addEventListener('scroll', onScroll, { passive: true })
  void scrollToBottom(true)
})

watch([() => sortedMessages.value.length, lastMessageLength], () => {
  void scrollToBottom()
})
</script>

<template>
  <ScrollArea ref="scrollContainer" class="min-h-0 h-full flex-1 px-3 sm:px-6 py-4">
    <div v-if="loading" class="flex flex-col gap-4 w-full max-w-3xl mx-auto">
      <Skeleton class="h-16 w-full" />
      <Skeleton class="h-24 w-full" />
      <Skeleton class="h-12 w-2/3" />
    </div>
    <div v-else-if="sortedMessages.length" class="flex flex-col gap-6 w-full max-w-3xl mx-auto">
      <HarnessTodoList v-if="todos && todos.length" :todos="todos" />
      <HarnessMessageView
        v-for="message in sortedMessages"
        :key="message.id"
        :message="message"
        :streaming="streamingSessionId !== null && message.session_id === streamingSessionId"
        :child-session-ids="childSessionIds"
        @open-subtask="emit('openSubtask', $event)"
      />
    </div>
    <div v-else class="flex flex-col items-center justify-center py-12 px-6 text-center h-full">
      <div class="mb-4 text-muted-foreground">
        <MessageSquare :size="40" />
      </div>
      <h3 class="text-lg font-medium text-foreground mb-1">No messages yet</h3>
      <p class="text-sm text-muted-foreground max-w-sm">
        Send a prompt to start this harness session.
      </p>
    </div>
  </ScrollArea>
</template>
