<script setup lang="ts">
/** Shared workspace navigation and chat history, online first. */
import { computed } from 'vue'
import { CheckCheck, MessageSquare, Plus } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import {
  groupConversationsByWorkspace,
  WORKSPACE_CONVERSATION_PAGE_SIZE,
} from '@/lib/conversationGroups'
import type { Workspace } from '@/types'
import type { HarnessConversation } from '@/types/harness'
import ConversationWorkspaceGroup from './ConversationWorkspaceGroup.vue'

const props = defineProps<{
  workspaces: Workspace[]
  conversations: HarnessConversation[]
  totalCount: number
  activeSessionId: string | null
  activeWorkspaceId: string | null
}>()

const emit = defineEmits<{
  open: [workspaceId: string]
  'open-all': []
  create: []
  select: [conversation: HarnessConversation]
  rename: [conversation: HarnessConversation, title: string]
  delete: [conversation: HarnessConversation]
  'mark-read': [conversation: HarnessConversation]
  'mark-unread': [conversation: HarnessConversation]
  'mark-all-read': []
}>()

// Controlled by ChatSidebar so closing the mobile drawer does not reset pagination.
const visibleCounts = defineModel<Record<string, number>>('visibleCounts', { default: () => ({}) })

function setVisibleCount(workspaceId: string, count: number): void {
  visibleCounts.value = { ...visibleCounts.value, [workspaceId]: count }
}

const groups = computed(() => groupConversationsByWorkspace(props.workspaces, props.conversations))
const hasUnread = computed(() => props.conversations.some((conversation) => conversation.unread))
</script>

<template>
  <section data-testid="workspace-conversation-list" class="min-w-0 px-2">
    <div class="flex h-7 items-center gap-1">
      <span class="mr-auto text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
        Workspaces
      </span>
      <Tooltip v-if="hasUnread">
        <TooltipTrigger as-child>
          <Button
            variant="ghost"
            size="icon-xs"
            class="text-muted-foreground"
            data-testid="mark-all-read"
            aria-label="Mark all as read"
            @click="emit('mark-all-read')"
          >
            <CheckCheck />
          </Button>
        </TooltipTrigger>
        <TooltipContent>Mark all as read</TooltipContent>
      </Tooltip>
      <Button
        variant="ghost"
        size="icon-xs"
        class="text-muted-foreground"
        data-testid="workspaces-create"
        aria-label="Manage workspaces"
        @click="emit('create')"
      >
        <Plus />
      </Button>
    </div>

    <div
      v-if="props.conversations.length === 0"
      class="flex items-center gap-2 px-2 py-2 text-xs text-muted-foreground"
    >
      <MessageSquare class="size-3.5 shrink-0 opacity-60" />
      <span>No chats yet — start with New chat</span>
    </div>

    <div class="flex flex-col gap-3 pt-1">
      <ConversationWorkspaceGroup
        v-for="group in groups"
        :key="group.workspaceId"
        :group="group"
        :visible-count="visibleCounts[group.workspaceId] ?? WORKSPACE_CONVERSATION_PAGE_SIZE"
        @update:visible-count="setVisibleCount(group.workspaceId, $event)"
        :active-session-id="props.activeSessionId"
        :active-workspace-id="props.activeWorkspaceId"
        @open="emit('open', $event)"
        @select="emit('select', $event)"
        @rename="(row, title) => emit('rename', row, title)"
        @delete="emit('delete', $event)"
        @mark-read="emit('mark-read', $event)"
        @mark-unread="emit('mark-unread', $event)"
      />
    </div>

    <Button
      variant="ghost"
      size="sm"
      data-testid="all-workspaces"
      class="mt-2 h-7 w-full justify-start rounded-xl px-2 text-xs font-normal text-muted-foreground"
      @click="emit('open-all')"
    >
      All workspaces ({{ props.totalCount }})
    </Button>
  </section>
</template>
