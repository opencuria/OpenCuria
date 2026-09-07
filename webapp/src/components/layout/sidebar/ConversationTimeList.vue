<script setup lang="ts">
/**
 * Time-bucketed conversation list with a 15-row cap and inline expand.
 */
import { computed, ref } from 'vue'
import { MessageSquare } from '@lucide/vue'
import {
  VISIBLE_CONVERSATION_LIMIT,
  capConversationGroups,
  groupConversationsByTime,
} from '@/lib/conversationGroups'
import type { HarnessConversation } from '@/types/harness'
import ConversationRow from './ConversationRow.vue'

const props = defineProps<{
  conversations: HarnessConversation[]
  activeSessionId: string | null
  empty?: boolean
}>()

const emit = defineEmits<{
  select: [conversation: HarnessConversation]
  rename: [conversation: HarnessConversation, title: string]
  delete: [conversation: HarnessConversation]
  'mark-read': [conversation: HarnessConversation]
  'mark-unread': [conversation: HarnessConversation]
}>()

const expanded = ref(false)

const grouped = computed(() => {
  const groups = groupConversationsByTime(props.conversations)
  if (expanded.value) return { groups, hiddenCount: 0 }
  return capConversationGroups(groups, VISIBLE_CONVERSATION_LIMIT)
})
</script>

<template>
  <section data-testid="time-list" class="px-2">
    <div
      v-if="props.empty"
      class="flex items-center gap-2 rounded-xl px-2 py-2 text-xs text-muted-foreground"
    >
      <MessageSquare class="size-3.5 shrink-0 opacity-60" />
      <span>Noch keine Chats — starte mit Neuer Chat</span>
    </div>

    <div v-else class="flex flex-col gap-2">
      <div v-for="group in grouped.groups" :key="group.key" class="flex flex-col gap-0.5">
        <div class="px-2 pt-1 text-[11px] font-medium text-muted-foreground">
          {{ group.label }}
        </div>
        <ConversationRow
          v-for="conversation in group.conversations"
          :key="conversation.session_id"
          :conversation="conversation"
          :active="props.activeSessionId === conversation.session_id"
          @select="emit('select', $event)"
          @rename="(row, title) => emit('rename', row, title)"
          @delete="emit('delete', $event)"
          @mark-read="emit('mark-read', $event)"
          @mark-unread="emit('mark-unread', $event)"
        />
      </div>

      <button
        v-if="grouped.hiddenCount > 0"
        type="button"
        data-testid="show-more-chats"
        class="w-full rounded-xl px-2 py-1.5 text-left text-[12px] text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-2 focus-visible:outline-primary"
        @click="expanded = true"
      >
        {{ grouped.hiddenCount }} weitere Chats anzeigen
      </button>
    </div>
  </section>
</template>
