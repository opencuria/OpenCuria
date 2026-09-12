<script setup lang="ts">
/**
 * ⌘K command palette: actions, chats, and workspaces with grouped keyboard nav.
 */
import { computed, nextTick, ref, watch, type Component } from 'vue'
import { useRouter } from 'vue-router'
import {
  BookOpen,
  CircleAlert,
  Layers,
  MessageSquare,
  Plus,
  Search,
  Settings,
} from '@lucide/vue'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import {
  conversationTitle,
  extractActionRequired,
  extractActiveConversations,
  selectSidebarWorkspaces,
  countableWorkspaces,
} from '@/lib/conversationGroups'
import { useHarnessConversationStore } from '@/stores/harnessConversations'
import { useWorkspaceStore } from '@/stores/workspaces'
import type { HarnessConversation } from '@/types/harness'
import type { Workspace } from '@/types'

const props = withDefaults(
  defineProps<{
    open?: boolean
  }>(),
  { open: false },
)

const emit = defineEmits<{
  'update:open': [open: boolean]
}>()

interface PaletteItem {
  id: string
  group: string
  label: string
  description?: string
  icon: Component
  attention?: boolean
  run: () => void
}

const router = useRouter()
const conversationStore = useHarnessConversationStore()
const workspaceStore = useWorkspaceStore()

const query = ref('')
const activeIndex = ref(0)

const actionItems: PaletteItem[] = [
  {
    id: 'action-new-chat',
    group: 'Actions',
    label: 'New chat',
    icon: Plus,
    run: () => {
      closeAndRun(() => {
        void router.push('/')
      })
    },
  },
  {
    id: 'action-workspaces',
    group: 'Actions',
    label: 'Manage workspaces',
    icon: Layers,
    run: () => {
      closeAndRun(() => {
        void router.push('/workspaces')
      })
    },
  },
  {
    id: 'action-settings',
    group: 'Actions',
    label: 'Settings',
    icon: Settings,
    run: () => {
      closeAndRun(() => {
        window.dispatchEvent(new CustomEvent('opencuria:open-settings'))
      })
    },
  },
  {
    id: 'action-docs',
    group: 'Actions',
    label: 'Docs',
    icon: BookOpen,
    run: () => {
      closeAndRun(() => {
        void router.push('/docs')
      })
    },
  },
]

function closeAndRun(action: () => void): void {
  emit('update:open', false)
  action()
}

function openConversation(conversation: HarnessConversation): void {
  closeAndRun(() => {
    void conversationStore.markAsRead(conversation.session_id)
    void router.push({
      path: `/workspaces/${conversation.workspace_id}`,
      query: { session: conversation.session_id },
    })
  })
}

function openWorkspace(workspace: Workspace): void {
  closeAndRun(() => {
    void router.push({ path: `/workspaces/${workspace.id}` })
  })
}

function chatItem(conversation: HarnessConversation, group: string): PaletteItem {
  const attention = Boolean(conversation.needs_attention)
  return {
    id: `chat-${conversation.session_id}`,
    group,
    label: conversationTitle(conversation),
    description: conversation.workspace_name,
    icon: attention ? CircleAlert : MessageSquare,
    attention,
    run: () => openConversation(conversation),
  }
}

function workspaceItem(workspace: Workspace): PaletteItem {
  return {
    id: `workspace-${workspace.id}`,
    group: 'Workspaces',
    label: workspace.name,
    icon: Layers,
    run: () => openWorkspace(workspace),
  }
}

const items = computed<PaletteItem[]>(() => {
  const q = query.value.trim().toLowerCase()
  const conversations = conversationStore.conversations
  const workspaces = countableWorkspaces(workspaceStore.workspaces)

  if (!q) {
    const actionRequired = extractActionRequired(conversations)
    const actionRequiredIds = new Set(actionRequired.map((row) => row.session_id))
    const active = extractActiveConversations(conversations)
    const featuredIds = new Set([
      ...actionRequiredIds,
      ...active.map((row) => row.session_id),
    ])
    const recent = conversations
      .filter((row) => !featuredIds.has(row.session_id))
      .slice(0, 8)
    const sidebarWorkspaces = selectSidebarWorkspaces(workspaces, conversations, 5)
    return [
      ...actionItems,
      ...actionRequired.map((row) => chatItem(row, 'Action required')),
      ...active.map((row) => chatItem(row, 'Active')),
      ...recent.map((row) => chatItem(row, 'Recently used')),
      ...sidebarWorkspaces.map(workspaceItem),
    ]
  }

  const chats = conversations.filter(
    (row) =>
      conversationTitle(row).toLowerCase().includes(q) ||
      row.workspace_name.toLowerCase().includes(q),
  )
  const matchedWorkspaces = workspaces.filter((workspace) =>
    workspace.name.toLowerCase().includes(q),
  )
  const matchedActions = actionItems.filter(
    (item) =>
      item.label.toLowerCase().includes(q) || item.id.toLowerCase().includes(q),
  )
  return [
    ...chats.map((row) => chatItem(row, 'Chats')),
    ...matchedWorkspaces.map(workspaceItem),
    ...matchedActions.map((item) => ({ ...item, group: 'Actions' })),
  ]
})

const groups = computed(() => {
  const result: { heading: string; items: PaletteItem[] }[] = []
  for (const item of items.value) {
    const last = result[result.length - 1]
    if (last && last.heading === item.group) {
      last.items.push(item)
    } else {
      result.push({ heading: item.group, items: [item] })
    }
  }
  return result
})

function setOpen(open: boolean): void {
  emit('update:open', open)
}

function focusInput(): void {
  void nextTick(() => {
    document.querySelector<HTMLInputElement>('[data-testid="command-palette-input"]')?.focus()
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

watch(items, () => {
  if (activeIndex.value > items.value.length - 1) {
    activeIndex.value = Math.max(0, items.value.length - 1)
  }
})

function moveSelection(delta: number): void {
  if (items.value.length === 0) return
  const next = activeIndex.value + delta
  activeIndex.value = (next + items.value.length) % items.value.length
  void nextTick(() => {
    document
      .querySelector(`[data-testid="command-palette-item-${activeIndex.value}"]`)
      ?.scrollIntoView({ block: 'nearest' })
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
    items.value[activeIndex.value]?.run()
  }
}

function itemIndex(item: PaletteItem): number {
  return items.value.findIndex((entry) => entry.id === item.id)
}
</script>

<template>
  <Dialog :open="props.open" @update:open="setOpen">
    <DialogContent class="max-w-xl p-0" :show-close-button="false">
      <DialogHeader class="sr-only">
        <DialogTitle>Search</DialogTitle>
        <DialogDescription>
          Search chats, workspaces, and actions. Press Enter to open the selection.
        </DialogDescription>
      </DialogHeader>

      <div class="flex items-center gap-2 border-b border-border px-4 py-3">
        <Search class="size-4 shrink-0 text-muted-foreground" />
        <Input
          v-model="query"
          data-testid="command-palette-input"
          placeholder="Chats, workspaces, actions…"
          class="h-8 border-0 bg-transparent px-0 shadow-none focus-visible:ring-0"
          aria-label="Search command palette"
          @keydown="handleKeydown"
        />
        <kbd
          class="shrink-0 rounded border border-border bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground"
        >
          ESC
        </kbd>
      </div>

      <div
        class="max-h-[50vh] overflow-y-auto p-1.5"
        role="listbox"
        aria-label="Search results"
        data-testid="command-palette-results"
      >
        <div v-for="group in groups" :key="group.heading" class="mb-1">
          <div class="px-2.5 py-1.5 text-[11px] font-medium text-muted-foreground">
            {{ group.heading }}
          </div>
          <button
            v-for="item in group.items"
            :key="item.id"
            type="button"
            role="option"
            :data-testid="`command-palette-item-${itemIndex(item)}`"
            :aria-selected="itemIndex(item) === activeIndex"
            class="flex w-full items-center gap-2 rounded-xl px-2.5 py-2 text-left transition-colors focus-visible:outline-2 focus-visible:outline-primary"
            :class="itemIndex(item) === activeIndex ? 'bg-primary/10' : 'hover:bg-muted'"
            @click="item.run()"
            @mousemove="activeIndex = itemIndex(item)"
          >
            <component
              :is="item.icon"
              class="size-4 shrink-0"
              :class="item.attention ? 'text-amber-500' : 'text-muted-foreground'"
            />
            <div class="min-w-0 flex-1">
              <div class="truncate text-[13px] font-medium text-foreground">
                {{ item.label }}
              </div>
              <div
                v-if="item.description"
                class="truncate text-[11px] text-muted-foreground"
              >
                {{ item.description }}
              </div>
            </div>
            <span
              v-if="item.attention"
              data-testid="palette-attention-badge"
              class="shrink-0 rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-semibold text-amber-700 dark:text-amber-300"
            >
              Action
            </span>
          </button>
        </div>

        <div
          v-if="items.length === 0"
          class="flex flex-col items-center gap-1 py-8 text-muted-foreground"
        >
          <span class="text-sm">No results</span>
          <span class="text-xs">Try a different search term</span>
        </div>
      </div>

      <div
        class="hidden items-center gap-3 border-t border-border px-4 py-2 text-[11px] text-muted-foreground sm:flex"
      >
        <span>
          <kbd class="rounded border border-border px-1">↑</kbd>
          <kbd class="rounded border border-border px-1">↓</kbd>
          navigate
        </span>
        <span><kbd class="rounded border border-border px-1">↵</kbd> open</span>
        <span><kbd class="rounded border border-border px-1">esc</kbd> close</span>
      </div>
    </DialogContent>
  </Dialog>
</template>
