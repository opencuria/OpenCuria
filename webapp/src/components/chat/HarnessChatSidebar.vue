<script setup lang="ts">
import { computed, ref } from 'vue'
import type { HarnessSession } from '@/types/harness'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { ScrollArea } from '@/components/ui/scroll-area'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  Plus,
  MessageSquare,
  Pencil,
  Trash2,
  Check,
  X,
  ChevronLeft,
  ChevronRight,
} from '@lucide/vue'

const props = defineProps<{
  sessions: HarnessSession[]
  childSessionsByParent: Record<string, HarnessSession[]>
  activeSessionId: string | null
  mobileOpen?: boolean
}>()

const emit = defineEmits<{
  select: [sessionId: string]
  create: []
  rename: [sessionId: string, title: string]
  delete: [sessionId: string]
  close: []
}>()

const editingSessionId = ref<string | null>(null)
const editTitle = ref('')
const isCollapsed = ref(false)
const deleteTarget = ref<HarnessSession | null>(null)

const orderedSessions = computed(() =>
  [...props.sessions].sort((a, b) => {
    const aTime = a.updated_at ? new Date(a.updated_at).getTime() : 0
    const bTime = b.updated_at ? new Date(b.updated_at).getTime() : 0
    return bTime - aTime
  }),
)

function startRename(session: HarnessSession): void {
  editingSessionId.value = session.id
  editTitle.value = session.title || 'New chat'
}

function cancelRename(): void {
  editingSessionId.value = null
  editTitle.value = ''
}

function confirmRename(): void {
  if (!editingSessionId.value || !editTitle.value.trim()) return
  emit('rename', editingSessionId.value, editTitle.value.trim())
  editingSessionId.value = null
  editTitle.value = ''
}

function handleSelect(sessionId: string): void {
  emit('select', sessionId)
  emit('close')
}

function requestDelete(session: HarnessSession): void {
  deleteTarget.value = session
}

function confirmDelete(): void {
  if (!deleteTarget.value) return
  emit('delete', deleteTarget.value.id)
  deleteTarget.value = null
}

function formatDate(dateStr?: string): string {
  if (!dateStr) return ''
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

function sessionTitle(session: HarnessSession): string {
  return session.title?.trim() || 'New chat'
}
</script>

<template>
  <div
    v-if="mobileOpen"
    class="fixed inset-0 z-40 bg-black/50 md:hidden"
    @click="emit('close')"
  />

  <div
    class="flex h-full min-h-0 shrink-0 flex-col border-r border-border bg-card transition-all duration-200"
    :class="[
      isCollapsed ? 'md:w-12' : 'md:w-64',
      mobileOpen ? 'fixed bottom-0 left-0 top-0 z-50 flex w-72' : 'hidden md:flex',
    ]"
  >
    <button
      class="absolute -right-3 top-3 z-10 hidden h-6 w-6 items-center justify-center rounded-full border border-border bg-card transition-colors hover:bg-muted md:flex"
      :title="isCollapsed ? 'Expand chats' : 'Collapse chats'"
      @click="isCollapsed = !isCollapsed"
    >
      <component :is="isCollapsed ? ChevronRight : ChevronLeft" :size="14" />
    </button>

    <div
      class="flex shrink-0 items-center justify-between border-b border-border px-3 py-3"
      :class="isCollapsed && !mobileOpen ? 'md:hidden' : ''"
    >
      <span class="text-sm font-medium text-foreground">Chats</span>
      <div class="flex items-center gap-1">
        <Button variant="ghost" size="icon-sm" title="New chat" @click="emit('create')">
          <Plus :size="16" />
        </Button>
        <button
          class="flex h-7 w-7 items-center justify-center rounded-full transition-colors hover:bg-muted md:hidden"
          @click="emit('close')"
        >
          <X :size="14" />
        </button>
      </div>
    </div>

    <div
      v-if="isCollapsed && !mobileOpen"
      class="hidden shrink-0 flex-col items-center gap-2 border-b border-border py-3 md:flex"
    >
      <MessageSquare :size="18" class="text-muted-foreground" />
      <Button variant="ghost" size="icon-sm" title="New chat" @click="emit('create')">
        <Plus :size="16" />
      </Button>
    </div>

    <ScrollArea v-if="!isCollapsed || mobileOpen" class="min-h-0 flex-1 overflow-hidden">
      <div class="flex flex-col gap-0.5 p-1.5">
        <template v-for="session in orderedSessions" :key="session.id">
          <div
            class="group flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-2 text-sm transition-colors"
            :class="
              session.id === activeSessionId
                ? 'bg-primary/10 text-primary'
                : 'text-foreground hover:bg-muted'
            "
            @click="handleSelect(session.id)"
          >
            <template v-if="editingSessionId === session.id">
              <Input
                v-model="editTitle"
                class="h-7 flex-1 text-xs"
                maxlength="255"
                @keydown.enter.prevent="confirmRename"
                @keydown.esc.prevent="cancelRename"
                @click.stop
              />
              <Button variant="ghost" size="icon-sm" class="h-6 w-6 shrink-0" @click.stop="confirmRename">
                <Check :size="12" />
              </Button>
              <Button variant="ghost" size="icon-sm" class="h-6 w-6 shrink-0" @click.stop="cancelRename">
                <X :size="12" />
              </Button>
            </template>
            <template v-else>
              <MessageSquare :size="14" class="shrink-0 opacity-60" />
              <div class="min-w-0 flex-1">
                <div class="truncate text-xs font-medium">{{ sessionTitle(session) }}</div>
                <div class="truncate text-[10px] text-muted-foreground">
                  <span class="capitalize">{{ session.mode }}</span>
                  · {{ session.status }}
                  <template v-if="session.updated_at"> · {{ formatDate(session.updated_at) }}</template>
                </div>
              </div>
              <span
                v-if="session.unread"
                class="h-1.5 w-1.5 shrink-0 rounded-full bg-primary"
                data-testid="unread-dot"
              />
              <div class="flex shrink-0 opacity-0 transition-opacity group-hover:opacity-100">
                <Button variant="ghost" size="icon-sm" class="h-6 w-6" @click.stop="startRename(session)">
                  <Pencil :size="11" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  class="h-6 w-6 text-destructive hover:text-destructive"
                  @click.stop="requestDelete(session)"
                >
                  <Trash2 :size="11" />
                </Button>
              </div>
            </template>
          </div>

          <div
            v-for="child in childSessionsByParent[session.id] ?? []"
            :key="child.id"
            class="group ml-4 flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-sm transition-colors"
            :class="
              child.id === activeSessionId
                ? 'bg-primary/10 text-primary'
                : 'text-muted-foreground hover:bg-muted hover:text-foreground'
            "
            @click="handleSelect(child.id)"
          >
            <MessageSquare :size="12" class="shrink-0 opacity-50" />
            <div class="min-w-0 flex-1">
              <div class="truncate text-xs">{{ sessionTitle(child) }}</div>
              <div class="truncate text-[10px] text-muted-foreground">
                Subtask · {{ child.status }}
              </div>
            </div>
          </div>
        </template>

        <div
          v-if="!orderedSessions.length"
          class="flex flex-col items-center gap-2 py-8 text-muted-foreground"
        >
          <MessageSquare :size="24" class="opacity-40" />
          <span class="text-xs">No chats yet</span>
        </div>
      </div>
    </ScrollArea>
  </div>

  <Dialog :open="deleteTarget !== null" @update:open="(open) => { if (!open) deleteTarget = null }">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>Delete chat?</DialogTitle>
        <DialogDescription>
          This will permanently delete
          <span class="font-medium text-foreground">{{ deleteTarget ? sessionTitle(deleteTarget) : '' }}</span>
          and abort any active run.
        </DialogDescription>
      </DialogHeader>
      <DialogFooter>
        <Button variant="outline" @click="deleteTarget = null">Cancel</Button>
        <Button variant="destructive" @click="confirmDelete">Delete</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
