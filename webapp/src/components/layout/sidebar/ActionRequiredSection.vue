<script setup lang="ts">
/**
 * Top sidebar block for chats waiting on a permission or question gate.
 */
import type { HarnessConversation } from '@/types/harness'
import ConversationRow from './ConversationRow.vue'

const props = defineProps<{
  conversations: HarnessConversation[]
  activeSessionId: string | null
}>()

const emit = defineEmits<{
  select: [conversation: HarnessConversation]
  rename: [conversation: HarnessConversation, title: string]
  delete: [conversation: HarnessConversation]
  'mark-read': [conversation: HarnessConversation]
  'mark-unread': [conversation: HarnessConversation]
}>()
</script>

<template>
  <section
    v-if="props.conversations.length > 0"
    data-testid="action-required-section"
    class="px-2"
  >
    <div class="flex h-7 items-center gap-1.5">
      <span class="text-[11px] font-medium tracking-wide text-amber-600 uppercase dark:text-amber-400">
        Action required
      </span>
      <span
        data-testid="action-required-count"
        class="ml-auto inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-amber-500/15 px-1.5 text-[11px] font-semibold text-amber-700 dark:text-amber-300"
      >
        {{ props.conversations.length }}
      </span>
    </div>
    <div class="flex flex-col gap-0.5">
      <ConversationRow
        v-for="conversation in props.conversations"
        :key="conversation.session_id"
        :conversation="conversation"
        :active="props.activeSessionId === conversation.session_id"
        show-workspace
        @select="emit('select', $event)"
        @rename="(row, title) => emit('rename', row, title)"
        @delete="emit('delete', $event)"
        @mark-read="emit('mark-read', $event)"
        @mark-unread="emit('mark-unread', $event)"
      />
    </div>
  </section>
</template>
