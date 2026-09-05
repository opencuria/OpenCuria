<script setup lang="ts">
import { Bot, CheckCircle2 } from '@lucide/vue'
import type { HarnessConversation } from '@/types/harness'
import StartNewHarnessChatDialog from '@/components/workspaces/StartNewHarnessChatDialog.vue'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import { ScrollArea } from '@/components/ui/scroll-area'
import {
  harnessConversationModeLabel,
  harnessConversationPreview,
  isHarnessConversationDoneUnread,
  isHarnessConversationRunning,
} from '@/lib/harnessConversationState'

interface Props {
  conversations: HarnessConversation[]
  loading: boolean
  searchQuery: string
  formatTimeAgo: (isoString: string) => string
}

const props = defineProps<Props>()
const emit = defineEmits<{
  conversationClick: [conv: HarnessConversation]
}>()

function isRunning(conv: HarnessConversation): boolean {
  return isHarnessConversationRunning(conv)
}

function isDoneUnread(conv: HarnessConversation): boolean {
  return isHarnessConversationDoneUnread(conv)
}
</script>

<template>
  <div class="flex flex-col flex-1 min-h-0">
    <div class="px-4 py-3 border-b border-border bg-background">
      <StartNewHarnessChatDialog>
        <template #trigger>
          <button
            type="button"
            class="w-full p-3 rounded-lg border-2 border-dashed border-border hover:border-primary hover:bg-primary/5 transition-all flex items-center justify-center gap-2 text-muted-foreground hover:text-primary"
          >
            <Bot :size="16" />
            <span class="text-sm font-medium">New Chat</span>
          </button>
        </template>
      </StartNewHarnessChatDialog>
    </div>

    <ScrollArea class="flex-1">
      <div
        v-if="loading && conversations.length === 0"
        class="flex items-center justify-center py-16"
      >
        <LoadingSpinner :size="24" />
      </div>

      <div
        v-else-if="conversations.length === 0"
        class="flex flex-col items-center justify-center py-16 text-center px-6"
      >
        <Bot :size="40" class="text-muted-foreground mb-3" />
        <p class="text-sm text-muted-foreground">
          {{
            searchQuery
              ? 'No conversations match your search.'
              : 'No conversations yet. Create a workspace to get started.'
          }}
        </p>
      </div>

      <div v-else>
        <button
          v-for="conv in conversations"
          :key="conv.session_id"
          type="button"
          class="w-full flex items-start gap-3 px-4 py-3 lg:px-6 hover:bg-accent transition-colors border-b border-border last:border-0 text-left relative"
          :class="{
            'bg-warning/5 hover:bg-warning/10': isRunning(conv),
            'bg-primary/5 hover:bg-primary/10': isDoneUnread(conv),
          }"
          @click="emit('conversationClick', conv)"
        >
          <div
            class="absolute left-0 top-0 bottom-0 w-0.5 rounded-r"
            :class="{
              'bg-warning': isRunning(conv),
              'bg-primary': isDoneUnread(conv),
            }"
          />

          <div
            class="flex items-center justify-center w-9 h-9 rounded-full shrink-0 mt-0.5"
            :class="{
              'bg-warning/20': isRunning(conv),
              'bg-primary/15': isDoneUnread(conv),
              'bg-muted': !isRunning(conv) && !isDoneUnread(conv),
            }"
          >
            <span v-if="isRunning(conv)" class="relative flex h-3 w-3">
              <span
                class="animate-ping absolute inline-flex h-full w-full rounded-full bg-warning opacity-75"
              />
              <span class="relative inline-flex rounded-full h-3 w-3 bg-warning" />
            </span>
            <CheckCircle2 v-else-if="isDoneUnread(conv)" :size="16" class="text-success" />
            <Bot v-else :size="16" class="text-muted-foreground" />
          </div>

          <div class="flex-1 min-w-0">
            <div class="flex items-center justify-between gap-2 mb-0.5">
              <span
                class="text-sm font-semibold text-foreground truncate"
                :class="{ 'font-bold': isDoneUnread(conv) }"
              >
                {{ conv.title || conv.workspace_name || conv.workspace_id.slice(0, 12) + '…' }}
              </span>
              <div class="flex items-center gap-1.5 shrink-0">
                <span
                  v-if="isDoneUnread(conv)"
                  class="w-2 h-2 rounded-full bg-primary shrink-0"
                />
                <span class="text-xs text-muted-foreground tabular-nums">
                  {{ formatTimeAgo(conv.updated_at) }}
                </span>
              </div>
            </div>

            <div class="flex items-center gap-1.5 mb-1">
              <span
                class="text-xs truncate"
                :class="isRunning(conv) ? 'text-warning font-medium' : 'text-muted-foreground'"
              >
                {{ harnessConversationPreview(conv) }}
              </span>
            </div>

            <div class="flex items-center gap-1.5">
              <span class="text-xs text-muted-foreground">
                {{ conv.workspace_name }} · {{ harnessConversationModeLabel(conv) }}
              </span>
            </div>
          </div>
        </button>
      </div>
    </ScrollArea>
  </div>
</template>
