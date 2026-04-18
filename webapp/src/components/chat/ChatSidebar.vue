<script setup lang="ts">
import { ref } from 'vue'
import type { Chat } from '@/types'
import { PENDING_CHAT_ID } from '@/stores/workspaces'
import { UiButton, UiInput, UiScrollArea } from '@/components/ui'
import { Plus, MessageSquare, Pencil, Trash2, Check, X, ChevronLeft, ChevronRight } from 'lucide-vue-next'

const props = defineProps<{
  chats: Chat[]
  activeChatId: string | null
  mobileOpen?: boolean
  overlayMode?: boolean
}>()

const emit = defineEmits<{
  select: [chatId: string]
  create: []
  rename: [chatId: string, name: string]
  delete: [chatId: string]
  close: []
}>()

const editingChatId = ref<string | null>(null)
const editName = ref('')
const isCollapsed = ref(true)

function startRename(chat: Chat): void {
  editingChatId.value = chat.id
  editName.value = chat.name
}

function cancelRename(): void {
  editingChatId.value = null
  editName.value = ''
}

function confirmRename(): void {
  if (!editingChatId.value || !editName.value.trim()) return
  emit('rename', editingChatId.value, editName.value.trim())
  editingChatId.value = null
  editName.value = ''
}

function handleSelect(chatId: string): void {
  emit('select', chatId)
  // Close mobile overlay after selecting
  emit('close')
}

function formatDate(dateStr: string): string {
  const date = new Date(dateStr)
  const now = new Date()
  const diff = now.getTime() - date.getTime()
  const hours = Math.floor(diff / (1000 * 60 * 60))
  if (hours < 1) return 'just now'
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  if (days < 7) return `${days}d ago`
  return date.toLocaleDateString()
}
</script>

<template>
  <!-- Mobile overlay backdrop -->
  <div
    v-if="mobileOpen"
    class="fixed inset-0 z-40 bg-black/50"
    :class="props.overlayMode ? '' : 'md:hidden'"
    @click="emit('close')"
  />

  <!-- Sidebar: fixed overlay on mobile, inline on desktop -->
  <div
    class="flex flex-col h-full border-r border-border bg-surface shrink-0 relative transition-all duration-200"
    :class="[
      !props.overlayMode && isCollapsed ? 'md:w-12' : 'md:w-64',
      mobileOpen
        ? 'fixed left-0 top-0 bottom-0 z-50 w-72 flex'
        : props.overlayMode
          ? 'hidden'
          : 'hidden md:flex',
    ]"
  >
    <!-- Collapse toggle button (desktop only) -->
    <button
      v-if="!props.overlayMode"
      class="hidden md:flex absolute -right-3 top-3 z-10 w-6 h-6 rounded-full bg-surface border border-border hover:bg-surface-hover transition-colors items-center justify-center"
      @click="isCollapsed = !isCollapsed"
      :title="isCollapsed ? 'Expand chats' : 'Collapse chats'"
    >
      <component :is="isCollapsed ? ChevronRight : ChevronLeft" :size="14" />
    </button>

    <!-- Header -->
    <div
      class="flex items-center justify-between px-3 py-3 border-b border-border"
      :class="!props.overlayMode && isCollapsed && !mobileOpen ? 'md:hidden' : ''"
    >
      <span class="text-sm font-medium text-fg">Chats</span>
      <div class="flex items-center gap-1">
        <UiButton variant="ghost" size="icon-sm" title="New chat" @click="emit('create')">
          <Plus :size="16" />
        </UiButton>
        <button
          class="w-7 h-7 rounded-full hover:bg-muted transition-colors flex items-center justify-center"
          :class="props.overlayMode ? '' : 'md:hidden'"
          @click="emit('close')"
        >
          <X :size="14" />
        </button>
      </div>
    </div>

    <!-- Collapsed state (desktop only): just icon + create -->
    <div
      v-if="!props.overlayMode && isCollapsed && !mobileOpen"
      class="hidden md:flex flex-col items-center py-3 border-b border-border gap-2"
    >
      <MessageSquare :size="18" class="text-muted-fg" />
      <UiButton variant="ghost" size="icon-sm" @click="emit('create')" title="New chat">
        <Plus :size="16" />
      </UiButton>
    </div>

    <!-- Chat list (expanded on desktop, always shown on mobile overlay) -->
    <UiScrollArea v-if="props.overlayMode || !isCollapsed || mobileOpen" class="flex-1">
      <div class="flex flex-col gap-0.5 p-1.5">
        <div
          v-for="chat in chats"
          :key="chat.id"
          class="group flex items-center gap-2 rounded-lg px-2.5 py-2 cursor-pointer transition-colors text-sm"
          :class="
            chat.id === activeChatId
              ? 'bg-primary/10 text-primary'
              : 'text-fg hover:bg-muted'
          "
          @click="handleSelect(chat.id)"
        >
          <!-- Editing state -->
          <template v-if="editingChatId === chat.id">
            <UiInput
              v-model="editName"
              class="h-7 text-xs flex-1"
              maxlength="255"
              @keydown.enter.prevent="confirmRename"
              @keydown.esc.prevent="cancelRename"
              @click.stop
            />
            <UiButton variant="ghost" size="icon-sm" class="shrink-0 h-6 w-6" @click.stop="confirmRename">
              <Check :size="12" />
            </UiButton>
            <UiButton variant="ghost" size="icon-sm" class="shrink-0 h-6 w-6" @click.stop="cancelRename">
              <X :size="12" />
            </UiButton>
          </template>

          <!-- Normal state -->
          <template v-else>
            <MessageSquare :size="14" class="shrink-0" :class="chat.id === PENDING_CHAT_ID ? 'opacity-40' : 'opacity-60'" />
            <div class="flex-1 min-w-0">
              <div class="truncate text-xs font-medium" :class="chat.id === PENDING_CHAT_ID ? 'italic text-muted-fg' : ''">
                {{ chat.name || 'New Chat' }}
              </div>
              <div class="text-[10px] text-muted-fg truncate">
                <template v-if="chat.id === PENDING_CHAT_ID">
                  <span>Send a message to create</span>
                </template>
                <template v-else>
                  <template v-if="chat.agent_type">
                    <span class="capitalize">{{ chat.agent_type }}</span>
                    ·
                  </template>
                  {{ chat.session_count }} msg{{ chat.session_count !== 1 ? 's' : '' }}
                  · {{ formatDate(chat.created_at) }}
                </template>
              </div>
            </div>

            <!-- Action buttons (visible on hover, hidden for pending chats) -->
            <div v-if="chat.id !== PENDING_CHAT_ID" class="flex opacity-0 group-hover:opacity-100 transition-opacity shrink-0">
              <UiButton
                variant="ghost"
                size="icon-sm"
                class="h-6 w-6"
                @click.stop="startRename(chat)"
              >
                <Pencil :size="11" />
              </UiButton>
              <UiButton
                variant="ghost"
                size="icon-sm"
                class="h-6 w-6 text-error hover:text-error"
                @click.stop="emit('delete', chat.id)"
              >
                <Trash2 :size="11" />
              </UiButton>
            </div>
          </template>
        </div>

        <!-- Empty state -->
        <div
          v-if="!chats.length"
          class="flex flex-col items-center gap-2 py-8 text-muted-fg"
        >
          <MessageSquare :size="24" class="opacity-40" />
          <span class="text-xs">No chats yet</span>
        </div>
      </div>
    </UiScrollArea>

    <!-- Collapsed chat list (desktop only): just dots -->
    <UiScrollArea v-else class="flex-1">
      <div class="flex flex-col gap-1 p-2 items-center">
        <div
          v-for="chat in chats"
          :key="chat.id"
          class="w-8 h-8 rounded-lg flex items-center justify-center cursor-pointer transition-colors"
          :class="
            chat.id === activeChatId
              ? 'bg-primary/10 text-primary'
              : 'text-muted-fg hover:bg-muted'
          "
          @click="handleSelect(chat.id)"
          :title="chat.name || 'Untitled chat'"
        >
          <MessageSquare :size="14" />
        </div>
      </div>
    </UiScrollArea>
  </div>
</template>
