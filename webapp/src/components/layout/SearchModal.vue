<script setup lang="ts">
/**
 * SearchModal — ⌘K Chat-Suche (Schritt 1 Redesign, angelehnt an OpenWebUI SearchModal).
 *
 * Filtert HarnessConversations nach Titel/Workspace. Enter navigiert wie ein
 * Row-Klick in der ChatSidebar (markAsRead + /workspaces/:id?session=).
 * Mobil ohne Preview-Spalte (nur Ergebnisliste).
 */

import { computed, nextTick, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Search } from '@lucide/vue'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { useHarnessConversationStore } from '@/stores/harnessConversations'
import type { HarnessConversation } from '@/types/harness'

const props = withDefaults(
  defineProps<{
    open?: boolean
  }>(),
  { open: false },
)

const emit = defineEmits<{
  'update:open': [open: boolean]
}>()

const router = useRouter()
const conversationStore = useHarnessConversationStore()

const query = ref('')
const activeIndex = ref(0)
const inputRef = ref<InstanceType<typeof Input> | null>(null)

const results = computed<HarnessConversation[]>(() => {
  const q = query.value.trim().toLowerCase()
  const all = [...conversationStore.conversations].sort(
    (a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
  )
  if (!q) return all.slice(0, 20)
  return all
    .filter(
      (conv) =>
        conv.title.toLowerCase().includes(q) || conv.workspace_name.toLowerCase().includes(q),
    )
    .slice(0, 20)
})

function conversationTitle(conv: HarnessConversation): string {
  return conv.title?.trim() || 'New chat'
}

function formatDate(isoString: string): string {
  return new Date(isoString).toLocaleDateString()
}

function setOpen(open: boolean): void {
  emit('update:open', open)
}

function focusInput(): void {
  void nextTick(() => {
    const el = document.querySelector<HTMLInputElement>('[data-testid="chat-search-input"]')
    el?.focus()
  })
}

watch(
  () => props.open,
  (open) => {
    if (open) {
      query.value = ''
      activeIndex.value = 0
      focusInput()
    }
  },
)

watch(query, () => {
  activeIndex.value = 0
})

watch(results, () => {
  if (activeIndex.value > results.value.length - 1) {
    activeIndex.value = Math.max(0, results.value.length - 1)
  }
})

function moveSelection(delta: number): void {
  if (results.value.length === 0) return
  const next = activeIndex.value + delta
  activeIndex.value = (next + results.value.length) % results.value.length
  void nextTick(() => {
    document
      .querySelector(`[data-testid="chat-search-result-${activeIndex.value}"]`)
      ?.scrollIntoView({ block: 'nearest' })
  })
}

function selectConversation(conv: HarnessConversation): void {
  setOpen(false)
  void conversationStore.markAsRead(conv.session_id)
  void router.push({
    path: `/workspaces/${conv.workspace_id}`,
    query: { session: conv.session_id },
  })
}

function handleKeydown(event: KeyboardEvent): void {
  if (event.key === 'ArrowDown') {
    event.preventDefault()
    moveSelection(1)
  } else if (event.key === 'ArrowUp') {
    event.preventDefault()
    moveSelection(-1)
  } else if (event.key === 'Enter') {
    event.preventDefault()
    const conv = results.value[activeIndex.value]
    if (conv) selectConversation(conv)
  }
}
</script>

<template>
  <Dialog :open="props.open" @update:open="setOpen">
    <DialogContent class="max-w-xl p-0" :show-close-button="false">
      <DialogHeader class="sr-only">
        <DialogTitle>Chats suchen</DialogTitle>
        <DialogDescription>Suche über Titel und Workspace. Enter öffnet den Chat.</DialogDescription>
      </DialogHeader>

      <div class="flex items-center gap-2 border-b border-border px-4 py-3">
        <Search class="size-4 shrink-0 text-muted-foreground" />
        <Input
          ref="inputRef"
          v-model="query"
          data-testid="chat-search-input"
          placeholder="Chats suchen… (Titel, Workspace)"
          class="h-8 border-0 bg-transparent px-0 shadow-none focus-visible:ring-0"
          aria-label="Chats suchen"
          @keydown="handleKeydown"
        />
        <kbd class="shrink-0 rounded border border-border bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
          ESC
        </kbd>
      </div>

      <div class="max-h-[50vh] overflow-y-auto p-1.5" role="listbox" aria-label="Suchergebnisse">
        <button
          v-for="(conv, index) in results"
          :key="conv.session_id"
          type="button"
          role="option"
          :data-testid="`chat-search-result-${index}`"
          :aria-selected="index === activeIndex"
          class="flex w-full items-center gap-2 rounded-xl px-2.5 py-2 text-left transition-colors focus-visible:outline-2 focus-visible:outline-primary"
          :class="index === activeIndex ? 'bg-primary/10' : 'hover:bg-muted'"
          @click="selectConversation(conv)"
          @mousemove="activeIndex = index"
        >
          <div class="min-w-0 flex-1">
            <div class="truncate text-[13px] font-medium text-foreground">
              {{ conversationTitle(conv) }}
            </div>
            <div class="truncate text-[11px] text-muted-foreground">
              {{ conv.workspace_name }}
            </div>
          </div>
          <span class="shrink-0 text-[11px] text-muted-foreground">
            {{ formatDate(conv.updated_at) }}
          </span>
        </button>

        <div
          v-if="results.length === 0"
          class="flex flex-col items-center gap-1 py-8 text-muted-foreground"
        >
          <span class="text-sm">Keine Treffer</span>
          <span class="text-xs">Anderer Suchbegriff versuchen</span>
        </div>
      </div>

      <div class="hidden items-center gap-3 border-t border-border px-4 py-2 text-[11px] text-muted-foreground sm:flex">
        <span><kbd class="rounded border border-border px-1">↑</kbd> <kbd class="rounded border border-border px-1">↓</kbd> navigieren</span>
        <span><kbd class="rounded border border-border px-1">↵</kbd> öffnen</span>
        <span><kbd class="rounded border border-border px-1">esc</kbd> schließen</span>
      </div>
    </DialogContent>
  </Dialog>
</template>
