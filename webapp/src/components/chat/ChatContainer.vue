<script setup lang="ts">
import { ref, watch, nextTick, computed, onMounted } from 'vue'
import type { Session } from '@/types'
import ChatMessage from './ChatMessage.vue'
import { UiEmptyState, UiScrollArea } from '@/components/ui'
import { MessageSquare } from 'lucide-vue-next'
import { isSessionActive } from '@/lib/sessionState'

const props = defineProps<{
  sessions: Session[]
  isMultiChat?: boolean
  workspaceId?: string
}>()

const emit = defineEmits<{
  toggleReadState: [sessionId: string]
}>()

const scrollContainer = ref<InstanceType<typeof UiScrollArea> | null>(null)

// Sort sessions chronologically (oldest first)
const sortedSessions = computed(() =>
  [...props.sessions].sort(
    (a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime(),
  ),
)

// Auto-scroll to bottom when new output arrives
const lastOutput = computed(() => {
  const last = sortedSessions.value[sortedSessions.value.length - 1]
  return last?.output?.length ?? 0
})

async function scrollToBottom(): Promise<void> {
  await nextTick()
  const el = scrollContainer.value?.$el
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
  <UiScrollArea ref="scrollContainer" class="flex-1 px-3 sm:px-6 py-4">
    <div v-if="sortedSessions.length" class="flex flex-col gap-6 w-full max-w-3xl mx-auto">
      <ChatMessage
        v-for="session in sortedSessions"
        :key="session.id"
        :session="session"
        :workspace-id="workspaceId"
        @toggle-read-state="emit('toggleReadState', $event)"
      />
    </div>

    <UiEmptyState
      v-else
      :title="isMultiChat ? 'No messages in this chat' : 'No conversations yet'"
      :description="isMultiChat ? 'Send a prompt to start this conversation.' : 'Send a prompt to start interacting with the agent.'"
      class="h-full"
    >
      <template #icon>
        <MessageSquare :size="40" />
      </template>
    </UiEmptyState>
  </UiScrollArea>
</template>
