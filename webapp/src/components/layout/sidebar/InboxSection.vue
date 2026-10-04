<script setup lang="ts">
/**
 * Shared inbox for pending gates and unread, idle plan/build chats.
 */
import type { HarnessConversation } from '@/types/harness'
import { useWorkspaceStore } from '@/stores/workspaces'
import ConversationRow from './ConversationRow.vue'

const workspaceStore = useWorkspaceStore()

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
  <section v-if="props.conversations.length > 0" data-testid="inbox-section" class="px-2">
    <div class="flex h-7 items-center gap-1.5">
      <span class="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
        Inbox
      </span>
      <span
        data-testid="inbox-count"
        class="ml-auto inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-primary/10 px-1.5 text-[11px] font-semibold text-sidebar-accent-foreground"
      >
        {{ props.conversations.length }}
      </span>
    </div>
    <div class="flex flex-col gap-0.5">
      <ConversationRow
        v-for="conversation in props.conversations"
        :key="conversation.session_id"
        :conversation="conversation"
        :actions-disabled="workspaceStore.isWorkspaceTransitioning(conversation.workspace_id)"
        :active="props.activeSessionId === conversation.session_id"
        inbox
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
