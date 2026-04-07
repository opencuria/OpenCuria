<script setup lang="ts">
import { Bot, CheckCircle2, XCircle } from 'lucide-vue-next'
import { WorkspaceStatus, SessionStatus, type Conversation } from '@/types'
import StartNewChatDialog from '@/components/workspaces/StartNewChatDialog.vue'

interface Props {
  idleConvs: Conversation[]
  workingConvs: Conversation[]
  doneConvs: Conversation[]
  formatTimeAgo: (isoString: string) => string
  workspaceStatusVariant: (status: WorkspaceStatus) => 'success' | 'warning' | 'error' | 'muted'
}

defineProps<Props>()
defineEmits<{
  conversationClick: [conv: Conversation]
}>()
</script>

<template>
  <div class="hidden lg:grid grid-cols-3 gap-4 p-4 lg:p-6 flex-1 min-h-0">
    <!-- Column 1: Available -->
    <div class="flex flex-col min-h-0 rounded-lg border border-border bg-surface overflow-hidden">
      <div
        class="px-4 py-2.5 border-b border-border flex items-center justify-between shrink-0"
      >
        <span class="text-sm font-semibold text-muted-fg">Available</span>
        <span
          class="text-xs text-muted-fg bg-muted rounded-full px-2 py-0.5 font-medium tabular-nums"
        >
          {{ idleConvs.length }}
        </span>
      </div>
      <div class="flex-1 overflow-y-auto p-2 flex flex-col gap-2">
        <!-- New Chat Button -->
        <StartNewChatDialog>
          <template #trigger>
            <button
              class="w-full p-3 rounded-lg border-2 border-dashed border-border hover:border-primary hover:bg-primary/5 transition-all flex items-center justify-center gap-2 text-muted-fg hover:text-primary"
            >
              <Bot :size="16" />
              <span class="text-sm font-medium">New Chat</span>
            </button>
          </template>
        </StartNewChatDialog>
        <p
          v-if="idleConvs.length === 0"
          class="py-8 text-center text-xs text-muted-fg"
        >
          No available chats
        </p>
        <button
          v-for="conv in idleConvs"
          :key="`idle-${conv.workspace_id}-${conv.chat_id ?? 'ws'}`"
          class="w-full text-left p-3 rounded-lg border border-border bg-background hover:bg-surface-hover transition-colors"
          @click="$emit('conversationClick', conv)"
        >
          <div class="flex items-start gap-2.5">
            <div
              class="w-7 h-7 rounded-full bg-muted flex items-center justify-center shrink-0 mt-0.5"
            >
              <Bot :size="13" class="text-muted-fg" />
            </div>
            <div class="flex-1 min-w-0">
              <div class="flex items-center justify-between gap-1 mb-0.5">
                <span class="text-xs font-semibold text-fg truncate">
                  {{ conv.workspace_name || conv.workspace_id.slice(0, 12) + '…' }}
                </span>
                <span class="text-xs text-muted-fg shrink-0 tabular-nums">
                  {{ formatTimeAgo(conv.updated_at) }}
                </span>
              </div>
              <p class="text-xs text-muted-fg truncate mb-1.5">
                {{ conv.last_session?.prompt ?? 'No messages yet' }}
              </p>
              <span class="text-xs text-muted-fg">{{ conv.agent_type }}</span>
            </div>
          </div>
        </button>
      </div>
    </div>

    <!-- Column 2: In Progress -->
    <div class="flex flex-col min-h-0 rounded-lg border border-warning/40 bg-surface overflow-hidden">
      <div
        class="px-4 py-2.5 border-b border-warning/30 flex items-center justify-between shrink-0 bg-warning/10"
      >
        <div class="flex items-center gap-2">
          <span class="relative flex h-2 w-2 shrink-0">
            <span
              class="animate-ping absolute inline-flex h-full w-full rounded-full bg-warning opacity-75"
            />
            <span class="relative inline-flex rounded-full h-2 w-2 bg-warning" />
          </span>
          <span class="text-sm font-semibold text-warning">In Progress</span>
        </div>
        <span
          class="text-xs text-warning bg-warning/20 rounded-full px-2 py-0.5 font-medium tabular-nums"
        >
          {{ workingConvs.length }}
        </span>
      </div>
      <div class="flex-1 overflow-y-auto p-2 flex flex-col gap-2">
        <p
          v-if="workingConvs.length === 0"
          class="py-8 text-center text-xs text-muted-fg"
        >
          No agents working right now
        </p>
        <button
          v-for="conv in workingConvs"
          :key="`working-${conv.workspace_id}-${conv.chat_id ?? 'ws'}`"
          class="w-full text-left p-3 rounded-lg border border-warning/40 bg-warning/5 hover:bg-warning/10 transition-colors"
          @click="$emit('conversationClick', conv)"
        >
          <div class="flex items-start gap-2.5">
            <div
              class="w-7 h-7 rounded-full bg-warning/20 flex items-center justify-center shrink-0 mt-0.5"
            >
              <span class="relative flex h-3 w-3">
                <span
                  class="animate-ping absolute inline-flex h-full w-full rounded-full bg-warning opacity-75"
                />
                <span class="relative inline-flex rounded-full h-3 w-3 bg-warning" />
              </span>
            </div>
            <div class="flex-1 min-w-0">
              <div class="flex items-center justify-between gap-1 mb-0.5">
                <span class="text-xs font-bold text-fg truncate">
                  {{ conv.workspace_name || conv.workspace_id.slice(0, 12) + '…' }}
                </span>
                <span class="text-xs text-muted-fg shrink-0 tabular-nums">
                  {{ formatTimeAgo(conv.updated_at) }}
                </span>
              </div>
              <p class="text-xs text-warning/80 truncate mb-1.5">
                {{ conv.last_session?.prompt ?? '' }}
              </p>
              <span class="text-xs text-muted-fg">{{ conv.agent_type }}</span>
            </div>
          </div>
        </button>
      </div>
    </div>

    <!-- Column 3: Done / Unread -->
    <div class="flex flex-col min-h-0 rounded-lg border border-success/40 bg-surface overflow-hidden">
      <div
        class="px-4 py-2.5 border-b border-success/30 flex items-center justify-between shrink-0 bg-success/10"
      >
        <div class="flex items-center gap-2">
          <CheckCircle2 :size="14" class="text-success" />
          <span class="text-sm font-semibold text-success">Done</span>
        </div>
        <span
          class="text-xs text-success bg-success/20 rounded-full px-2 py-0.5 font-medium tabular-nums"
        >
          {{ doneConvs.length }}
        </span>
      </div>
      <div class="flex-1 overflow-y-auto p-2 flex flex-col gap-2">
        <p
          v-if="doneConvs.length === 0"
          class="py-8 text-center text-xs text-muted-fg"
        >
          No unread results yet
        </p>
        <button
          v-for="conv in doneConvs"
          :key="`done-${conv.workspace_id}-${conv.chat_id ?? 'ws'}`"
          class="w-full text-left p-3 rounded-lg border border-success/30 bg-success/5 hover:bg-success/10 transition-colors relative overflow-hidden"
          @click="$emit('conversationClick', conv)"
        >
          <!-- Unread accent -->
          <div class="absolute left-0 top-0 bottom-0 w-0.5 bg-success" />
          <div class="flex items-start gap-2.5">
            <div
              class="w-7 h-7 rounded-full bg-success/20 flex items-center justify-center shrink-0 mt-0.5"
            >
              <CheckCircle2
                v-if="conv.last_session?.status === SessionStatus.COMPLETED"
                :size="13"
                class="text-success"
              />
              <XCircle v-else :size="13" class="text-error" />
            </div>
            <div class="flex-1 min-w-0">
              <div class="flex items-center justify-between gap-1 mb-0.5">
                <span class="text-xs font-bold text-fg truncate">
                  {{ conv.workspace_name || conv.workspace_id.slice(0, 12) + '…' }}
                </span>
                <div class="flex items-center gap-1.5 shrink-0">
                  <span class="w-1.5 h-1.5 rounded-full bg-primary" />
                  <span class="text-xs text-muted-fg tabular-nums">
                    {{ formatTimeAgo(conv.updated_at) }}
                  </span>
                </div>
              </div>
              <p class="text-xs text-muted-fg truncate mb-1.5">
                {{ conv.last_session?.prompt ?? '' }}
              </p>
              <span class="text-xs text-muted-fg">{{ conv.agent_type }}</span>
            </div>
          </div>
        </button>
      </div>
    </div>
  </div>
</template>
