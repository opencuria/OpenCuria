<script setup lang="ts">
/** One workspace's chat history with independent four-row pagination. */
import { computed, watch } from 'vue'
import { useWorkspaceStore } from '@/stores/workspaces'
import { Loader2 } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import {
  isOperatingWorkspace,
  WORKSPACE_CONVERSATION_PAGE_SIZE,
  type WorkspaceConversationGroup,
} from '@/lib/conversationGroups'
import type { HarnessConversation } from '@/types/harness'
import ConversationRow from './ConversationRow.vue'

const props = defineProps<{
  group: WorkspaceConversationGroup
  activeSessionId: string | null
  activeWorkspaceId: string | null
}>()

const emit = defineEmits<{
  open: [workspaceId: string]
  select: [conversation: HarnessConversation]
  rename: [conversation: HarnessConversation, title: string]
  delete: [conversation: HarnessConversation]
  'mark-read': [conversation: HarnessConversation]
  'mark-unread': [conversation: HarnessConversation]
}>()

const visibleCount = defineModel<number>('visibleCount', {
  default: WORKSPACE_CONVERSATION_PAGE_SIZE,
})
const visibleConversations = computed(() => props.group.conversations.slice(0, visibleCount.value))
const nextPageSize = computed(() =>
  Math.min(WORKSPACE_CONVERSATION_PAGE_SIZE, props.group.conversations.length - visibleCount.value),
)
const workspaceStore = useWorkspaceStore()
const actionsDisabled = computed(() =>
  workspaceStore.isWorkspaceTransitioning(props.group.workspaceId),
)
const transitionLabel = computed(() =>
  workspaceStore.getWorkspaceTransitionLabel(props.group.workspaceId),
)
const operating = computed(() =>
  Boolean(
    transitionLabel.value || (props.group.workspace && isOperatingWorkspace(props.group.workspace)),
  ),
)
const busy = computed(
  () =>
    props.group.workspace?.has_active_session ||
    props.group.conversations.some((conversation) => conversation.status === 'busy'),
)
const statusLabel = computed(
  () =>
    transitionLabel.value ??
    (operating.value ? 'In progress' : props.group.online ? 'Online' : 'Offline'),
)

// Also runs when history arrives after the route or a refresh changes its order.
watch(
  () => props.group.conversations.findIndex((row) => row.session_id === props.activeSessionId),
  (index) => {
    if (index < visibleCount.value) return
    visibleCount.value =
      Math.ceil((index + 1) / WORKSPACE_CONVERSATION_PAGE_SIZE) * WORKSPACE_CONVERSATION_PAGE_SIZE
  },
  { immediate: true },
)
</script>

<template>
  <section
    data-testid="workspace-conversation-group"
    :data-workspace-id="props.group.workspaceId"
    :data-online="props.group.online"
    class="flex min-w-0 flex-col gap-0.5"
  >
    <Button
      variant="ghost"
      size="sm"
      data-testid="workspace-row"
      :aria-label="`Open workspace ${props.group.name}`"
      :title="`${props.group.name} — ${statusLabel}`"
      class="h-7 w-full justify-start gap-1.5 rounded-xl px-2 text-left"
      :class="props.activeWorkspaceId === props.group.workspaceId ? 'bg-primary/10' : ''"
      @click="emit('open', props.group.workspaceId)"
    >
      <span
        data-testid="workspace-status"
        class="size-1.5 shrink-0 rounded-full"
        :class="
          operating
            ? 'bg-amber-500'
            : props.group.online
              ? 'bg-green-500'
              : 'bg-muted-foreground/40'
        "
      />
      <span class="min-w-0 flex-1 truncate text-[13px] font-semibold">{{ props.group.name }}</span>
      <span v-if="transitionLabel" class="text-xs text-muted-foreground">{{
        transitionLabel
      }}</span>
      <Loader2
        v-if="busy || operating"
        data-testid="workspace-busy"
        class="size-3 shrink-0 animate-spin text-primary"
      />
    </Button>

    <div class="min-w-0 pl-2">
      <ConversationRow
        v-for="conversation in visibleConversations"
        :key="conversation.session_id"
        :conversation="conversation"
        :actions-disabled="actionsDisabled"
        :active="props.activeSessionId === conversation.session_id"
        @select="emit('select', $event)"
        @rename="(row, title) => emit('rename', row, title)"
        @delete="emit('delete', $event)"
        @mark-read="emit('mark-read', $event)"
        @mark-unread="emit('mark-unread', $event)"
      />
      <p
        v-if="props.group.conversations.length === 0"
        class="px-2 py-1.5 text-xs text-muted-foreground"
      >
        No chats yet
      </p>
      <Button
        v-if="nextPageSize > 0"
        variant="ghost"
        size="sm"
        data-testid="show-more-chats"
        :aria-label="`Show ${nextPageSize} more chats in ${props.group.name}`"
        class="h-7 w-full justify-start rounded-xl px-2 text-xs font-normal text-muted-foreground"
        @click="visibleCount += WORKSPACE_CONVERSATION_PAGE_SIZE"
      >
        Show {{ nextPageSize }} more {{ nextPageSize === 1 ? 'chat' : 'chats' }}
      </Button>
    </div>
  </section>
</template>
