<script setup lang="ts">
import { ref, watch, nextTick, computed, onMounted } from 'vue'
import type { Session } from '@/types'
import ChatMessage from './ChatMessage.vue'
import { ScrollArea } from '@/components/ui/scroll-area'
import { MessageSquare } from '@lucide/vue'
import { isSessionActive } from '@/lib/sessionState'

const props = defineProps<{
  sessions: Session[]
  isMultiChat?: boolean
  workspaceId?: string
}>()

const emit = defineEmits<{
  toggleReadState: [sessionId: string]
}>()

const scrollContainer = ref<InstanceType<typeof ScrollArea> | null>(null)

const sortedSessions = computed(() =>
  [...props.sessions].sort(
    (a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime(),
  ),
)

const lastOutput = computed(() => {
  const last = sortedSessions.value[sortedSessions.value.length - 1]
  return last?.output?.length ?? 0
})

async function scrollToBottom(): Promise<void> {
  await nextTick()
  const el = scrollContainer.value?.$el?.querySelector('[data-slot="scroll-area-viewport"]') as HTMLElement | null
  if (el) {
    el.scrollTop = el.scrollHeight
  }
}

onMounted(scrollToBottom)

watch([() => sortedSessions.value.length, lastOutput], scrollToBottom)

const hasActiveSession = computed(() =>
  sortedSessions.value.some((s) => isSessionActive(s.status)),
)

defineExpose({ hasActiveSession })
</script>

<template>
  <ScrollArea ref="scrollContainer" class="min-h-0 h-full flex-1 px-3 sm:px-6 py-4">
    <div v-if="sortedSessions.length" class="flex flex-col gap-6 w-full max-w-3xl mx-auto">
      <ChatMessage
        v-for="session in sortedSessions"
        :key="session.id"
        :session="session"
        :workspace-id="workspaceId"
        @toggle-read-state="emit('toggleReadState', $event)"
      />
    </div>

    <div
      v-else
      class="flex flex-col items-center justify-center py-12 px-6 text-center h-full"
    >
      <div class="mb-4 text-muted-foreground">
        <MessageSquare :size="40" />
      </div>
      <h3 class="text-lg font-medium text-foreground mb-1">
        {{ isMultiChat ? 'No messages in this chat' : 'No conversations yet' }}
      </h3>
      <p class="text-sm text-muted-foreground max-w-sm">
        {{ isMultiChat ? 'Send a prompt to start this conversation.' : 'Send a prompt to start interacting with the agent.' }}
      </p>
    </div>
  </ScrollArea>
</template>
