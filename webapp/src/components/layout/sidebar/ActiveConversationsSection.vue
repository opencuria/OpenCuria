<script setup lang="ts">
/**
 * Compact “Active” block: busy sessions first, then unread. Hidden when empty.
 */
import { computed } from 'vue'
import { CheckCheck } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
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
  'mark-all-read': []
}>()

const unreadCount = computed(
  () => props.conversations.filter((conversation) => conversation.unread).length,
)
</script>

<template>
  <section v-if="props.conversations.length > 0" data-testid="active-section" class="px-2">
    <div class="flex h-7 items-center gap-1">
      <span class="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
        Active
      </span>
      <Tooltip v-if="unreadCount > 0">
        <TooltipTrigger as-child>
          <Button
            variant="ghost"
            size="icon-xs"
            class="ml-auto text-muted-foreground"
            data-testid="mark-all-read"
            aria-label="Mark all as read"
            @click="emit('mark-all-read')"
          >
            <CheckCheck />
          </Button>
        </TooltipTrigger>
        <TooltipContent>Mark all as read</TooltipContent>
      </Tooltip>
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
