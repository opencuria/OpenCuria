<script setup lang="ts">
import { ScrollArea } from '@/components/ui/scroll-area'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import { Bot, CheckCircle2, XCircle } from '@lucide/vue'
import { SessionStatus, WorkspaceStatus, type Conversation } from '@/types'
import StartNewChatDialog from '@/components/workspaces/StartNewChatDialog.vue'
import { isConversationDoneUnread, isConversationRunning } from '@/lib/sessionState'

interface Props {
  conversations: Conversation[]
  loading: boolean
  searchQuery: string
  formatTimeAgo: (isoString: string) => string
  workspaceStatusVariant: (status: WorkspaceStatus) => 'success' | 'warning' | 'error' | 'muted'
}

const props = defineProps<Props>()
const emit = defineEmits<{
  conversationClick: [conv: Conversation]
}>()

function isRunning(conv: Conversation): boolean {
  return isConversationRunning(conv)
}

function isDoneUnread(conv: Conversation): boolean {
  return isConversationDoneUnread(conv)
}
</script>

<template>
  <div class="flex flex-col flex-1 min-h-0">
    <div class="px-4 py-3 border-b border-border bg-background">
      <StartNewChatDialog>
        <template #trigger>
          <button
            class="w-full p-3 rounded-lg border-2 border-dashed border-border hover:border-primary hover:bg-primary/5 transition-all flex items-center justify-center gap-2 text-muted-foreground hover:text-primary"
          >
            <Bot :size="16" />
            <span class="text-sm font-medium">New Chat</span>
          </button>
        </template>
      </StartNewChatDialog>
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
          :key="`${conv.workspace_id}-${conv.chat_id ?? 'ws'}`"
          class="w-full flex items-start gap-3 px-4 py-3 lg:px-6 hover:bg-muted transition-colors border-b border-border last:border-0 text-left relative"
          :class="{
            'bg-amber-500/5 hover:bg-amber-500/10': isRunning(conv),
            'bg-primary/5 hover:bg-primary/10': isDoneUnread(conv),
          }"
          @click="emit('conversationClick', conv)"
        >
          <div
            class="absolute left-0 top-0 bottom-0 w-0.5 rounded-r"
            :class="{
              'bg-amber-500': isRunning(conv),
              'bg-primary': isDoneUnread(conv),
            }"
          />

          <div
            class="flex items-center justify-center w-9 h-9 rounded-full shrink-0 mt-0.5"
            :class="{
              'bg-amber-500/20': isRunning(conv),
              'bg-primary/15': isDoneUnread(conv),
              'bg-muted': conv.is_read || !conv.last_session,
            }"
          >
            <span v-if="isRunning(conv)" class="relative flex h-3 w-3">
              <span
                class="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-500 opacity-75"
              />
              <span class="relative inline-flex rounded-full h-3 w-3 bg-amber-500" />
            </span>
            <CheckCircle2
              v-else-if="isDoneUnread(conv) && conv.last_session?.status === SessionStatus.COMPLETED"
              :size="16"
              class="text-emerald-600 dark:text-emerald-400"
            />
            <XCircle
              v-else-if="isDoneUnread(conv) && conv.last_session?.status === SessionStatus.FAILED"
              :size="16"
              class="text-destructive"
            />
            <Bot v-else :size="16" class="text-muted-foreground" />
          </div>

          <div class="flex-1 min-w-0">
            <div class="flex items-center justify-between gap-2 mb-0.5">
              <span
                class="text-sm font-semibold text-foreground truncate"
                :class="{ 'text-foreground': conv.is_read, 'font-bold': !conv.is_read && conv.last_session }"
              >
                {{ conv.workspace_name || conv.workspace_id.slice(0, 12) + '…' }}
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
              <template v-if="conv.last_session">
                <span
                  v-if="isRunning(conv)"
                  class="relative flex h-2 w-2 shrink-0"
                >
                  <span
                    class="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-500 opacity-75"
                  />
                  <span class="relative inline-flex rounded-full h-2 w-2 bg-amber-500" />
                </span>
                <CheckCircle2
                  v-else-if="conv.last_session.status === SessionStatus.COMPLETED"
                  :size="12"
                  class="text-emerald-600 dark:text-emerald-400 shrink-0"
                />
                <XCircle
                  v-else-if="conv.last_session.status === SessionStatus.FAILED"
                  :size="12"
                  class="text-destructive shrink-0"
                />
                <span
                  class="text-xs truncate"
                  :class="isRunning(conv) ? 'text-amber-600 dark:text-amber-400 font-medium' : 'text-muted-foreground'"
                >
                  {{ conv.last_session.prompt }}
                </span>
              </template>
              <span v-else class="text-xs text-muted-foreground italic">No messages yet</span>
            </div>

            <div class="flex items-center gap-1.5">
              <span class="text-xs text-muted-foreground">{{ conv.agent_type }}</span>
            </div>
          </div>
        </button>
      </div>
    </ScrollArea>
  </div>
</template>
