<script setup lang="ts">
/**
 * WorkspaceSidePanel — collapsible, horizontally resizable panel docked to
 * the right of the workspace chat.
 *
 * Hosts the Git (productive backend integration), Desktop (live
 * preview), Terminal and Files tabs. The panel is a full-height column:
 * its tab bar spans the full panel width at the top (same bg-card surface
 * as the content), so the chat header buttons always sit left of the
 * panel. Closing happens via the
 * side-panel toggle in the chat header; below the lg breakpoint (where the
 * panel overlays the chat full-width and covers that toggle) the tab bar
 * shows its own close button. Tab contents are lazily mounted and kept
 * alive via v-show so the terminal session and desktop stream survive tab
 * switches and panel collapse.
 */
import { computed, defineAsyncComponent, onBeforeUnmount, ref, watch } from 'vue'
import type { CSSProperties } from 'vue'
import { useSidePanelStore } from '@/stores/sidePanel'
import type { SidePanelTab } from '@/lib/sidePanel'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Button } from '@/components/ui/button'
import { GitBranch, Monitor, TerminalSquare, FolderTree, PanelRightClose, X } from '@lucide/vue'

const WorkspaceTerminal = defineAsyncComponent(() => import('./WorkspaceTerminal.vue'))
const SidePanelDesktop = defineAsyncComponent(() => import('./SidePanelDesktop.vue'))
const FileExplorerPanel = defineAsyncComponent(
  () => import('@/components/files/FileExplorerPanel.vue'),
)
const GitPanel = defineAsyncComponent(() => import('@/components/git/GitPanel.vue'))

const props = withDefaults(
  defineProps<{
    workspaceId: string
    chatOpen?: boolean
  }>(),
  { chatOpen: false },
)

const emit = defineEmits<{ 'close-chat': [] }>()

const sidePanel = useSidePanelStore()

const tabs = [
  { id: 'git', label: 'Git', icon: GitBranch },
  { id: 'desktop', label: 'Desktop', icon: Monitor },
  { id: 'terminal', label: 'Terminal', icon: TerminalSquare },
  { id: 'files', label: 'Files', icon: FolderTree },
] as const

// Lazily mount tab contents on first activation, then keep them alive.
const mountedTabs = ref<Set<SidePanelTab>>(new Set())
watch(
  () => [sidePanel.activeTab, sidePanel.isOpen, props.chatOpen] as const,
  ([tab, open, chatOpen]) => {
    if (open && !chatOpen) mountedTabs.value.add(tab)
  },
  { immediate: true },
)

function handleTabChange(value: string | number): void {
  sidePanel.setTab(value as SidePanelTab)
}

// --- Horizontal resize (left edge) ---

const isResizing = ref(false)
const lgQuery = window.matchMedia('(min-width: 1024px)')
const isWideLayout = ref(lgQuery.matches)

function onWideLayoutChange(event: MediaQueryListEvent): void {
  isWideLayout.value = event.matches
}
lgQuery.addEventListener('change', onWideLayoutChange)

const panelStyle = computed<CSSProperties>(() => {
  if (!isWideLayout.value) return {}
  return { width: `${sidePanel.width}px` }
})

function onResizePointerDown(event: PointerEvent): void {
  if (!isWideLayout.value) return
  event.preventDefault()
  isResizing.value = true
  const handle = event.currentTarget as HTMLElement
  handle.setPointerCapture?.(event.pointerId)
  sidePanel.setWidth(window.innerWidth - event.clientX)
}

function onResizePointerMove(event: PointerEvent): void {
  if (!isResizing.value) return
  sidePanel.setWidth(window.innerWidth - event.clientX)
}

function onResizePointerUp(event: PointerEvent): void {
  if (!isResizing.value) return
  isResizing.value = false
  const handle = event.currentTarget as HTMLElement
  if (handle.hasPointerCapture?.(event.pointerId)) {
    handle.releasePointerCapture(event.pointerId)
  }
  sidePanel.persistWidth()
}

function onResizeDoubleClick(): void {
  sidePanel.resetWidth()
}

onBeforeUnmount(() => {
  lgQuery.removeEventListener('change', onWideLayoutChange)
})
</script>

<template>
  <div
    class="relative flex h-full min-h-0 min-w-0 shrink-0 flex-col border-l border-border bg-card"
    :class="[
      { 'select-none': isResizing },
      chatOpen
        ? 'max-lg:h-1/2 max-lg:w-full max-lg:border-t max-lg:border-l-0'
        : 'max-lg:fixed max-lg:inset-y-0 max-lg:right-0 max-lg:z-30 max-lg:w-full',
    ]"
    :style="panelStyle"
    data-testid="workspace-side-panel"
  >
    <!-- Resize handle (left edge) -->
    <div
      data-testid="side-panel-resize-handle"
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize panel"
      title="Drag to resize panel"
      class="absolute -left-0.5 top-0 z-30 hidden h-full w-1 cursor-col-resize touch-none bg-transparent transition-colors hover:bg-primary lg:block"
      :class="{ 'bg-primary': isResizing }"
      @pointerdown="onResizePointerDown"
      @pointermove="onResizePointerMove"
      @pointerup="onResizePointerUp"
      @pointercancel="onResizePointerUp"
      @dblclick="onResizeDoubleClick"
    />

    <!-- Tab bar (full panel width; closing happens via the chat header toggle) -->
    <div
      v-show="!chatOpen"
      class="flex shrink-0 items-center gap-1 border-b border-border px-2 py-1.5"
    >
      <Tabs
        :model-value="sidePanel.activeTab"
        class="min-w-0 flex-1"
        @update:model-value="handleTabChange"
      >
        <TabsList class="w-full">
          <TabsTrigger
            v-for="tab in tabs"
            :key="tab.id"
            :value="tab.id"
            class="min-w-0 gap-1.5"
            :data-testid="`side-panel-tab-${tab.id}`"
          >
            <component :is="tab.icon" />
            <span class="truncate">{{ tab.label }}</span>
          </TabsTrigger>
        </TabsList>
      </Tabs>
      <!-- Below lg the panel overlays the chat header toggle, so it needs its own close button -->
      <Button
        variant="ghost"
        size="icon-sm"
        class="h-6 w-6 shrink-0 lg:hidden"
        title="Close panel"
        data-testid="side-panel-close"
        @click="sidePanel.close()"
      >
        <PanelRightClose :size="14" />
      </Button>
    </div>

    <!-- Tab contents (lazy mounted, kept alive) -->
    <div v-show="!chatOpen" class="min-h-0 flex-1" data-testid="workspace-side-panel-tools">
      <GitPanel
        v-if="mountedTabs.has('git')"
        v-show="sidePanel.activeTab === 'git'"
        :workspace-id="props.workspaceId"
      />
      <SidePanelDesktop
        v-if="mountedTabs.has('desktop')"
        v-show="sidePanel.activeTab === 'desktop'"
        :workspace-id="props.workspaceId"
        class="h-full"
      />
      <WorkspaceTerminal
        v-if="mountedTabs.has('terminal')"
        v-show="sidePanel.activeTab === 'terminal'"
        :workspace-id="props.workspaceId"
      />
      <FileExplorerPanel
        v-if="mountedTabs.has('files')"
        v-show="sidePanel.activeTab === 'files'"
        :workspace-id="props.workspaceId"
      />
    </div>

    <section
      v-show="chatOpen"
      class="absolute inset-0 z-20 flex min-h-0 min-w-0 flex-col bg-background"
      aria-label="Workspace chat"
      data-testid="workspace-sidebar-chat"
    >
      <header
        class="flex h-10 min-w-0 shrink-0 items-center gap-1 border-b border-border bg-card px-3"
      >
        <div class="flex min-w-0 flex-1 items-center gap-1 text-xs font-medium">
          <slot name="chat-header">Chat</slot>
        </div>
        <Button
          variant="ghost"
          size="icon-sm"
          class="size-6 shrink-0"
          title="Close chat"
          aria-label="Close chat"
          data-testid="sidebar-chat-close"
          @click="emit('close-chat')"
          ><X :size="14"
        /></Button>
      </header>
      <div class="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden"><slot name="chat" /></div>
    </section>
  </div>
</template>
