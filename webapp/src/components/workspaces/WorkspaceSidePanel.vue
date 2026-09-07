<script setup lang="ts">
/**
 * WorkspaceSidePanel — collapsible, horizontally resizable panel docked to
 * the right of the workspace chat.
 *
 * Hosts the Git (placeholder), Desktop (live preview), Terminal and Files
 * tabs. Tab contents are lazily mounted and kept alive via v-show so the
 * terminal session and desktop stream survive tab switches and panel
 * collapse. Below the lg breakpoint the panel overlays the chat full-width.
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { CSSProperties } from 'vue'
import { useSidePanelStore } from '@/stores/sidePanel'
import type { SidePanelTab } from '@/lib/sidePanel'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Button } from '@/components/ui/button'
import { GitBranch, Monitor, TerminalSquare, FolderTree, X } from '@lucide/vue'
import WorkspaceTerminal from './WorkspaceTerminal.vue'
import SidePanelDesktop from './SidePanelDesktop.vue'
import FileExplorerPanel from '@/components/files/FileExplorerPanel.vue'

const props = defineProps<{
  workspaceId: string
}>()

const sidePanel = useSidePanelStore()

const tabs = [
  { id: 'git', label: 'Git', icon: GitBranch },
  { id: 'desktop', label: 'Desktop', icon: Monitor },
  { id: 'terminal', label: 'Terminal', icon: TerminalSquare },
  { id: 'files', label: 'Files', icon: FolderTree },
] as const

// Lazily mount tab contents on first activation, then keep them alive.
const mountedTabs = ref<Set<SidePanelTab>>(new Set([sidePanel.activeTab]))
watch(
  () => sidePanel.activeTab,
  (tab) => {
    mountedTabs.value.add(tab)
  },
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
    class="relative flex h-full min-h-0 shrink-0 flex-col border-l border-border bg-card max-lg:fixed max-lg:inset-y-0 max-lg:right-0 max-lg:z-30 max-lg:w-full"
    :class="{ 'select-none': isResizing }"
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
      class="absolute -left-0.5 top-0 z-10 hidden h-full w-1 cursor-col-resize touch-none bg-transparent transition-colors hover:bg-primary lg:block"
      :class="{ 'bg-primary': isResizing }"
      @pointerdown="onResizePointerDown"
      @pointermove="onResizePointerMove"
      @pointerup="onResizePointerUp"
      @pointercancel="onResizePointerUp"
      @dblclick="onResizeDoubleClick"
    />

    <!-- Tab bar -->
    <div class="flex shrink-0 items-center gap-1 border-b border-border px-2 py-1.5">
      <Tabs
        :model-value="sidePanel.activeTab"
        class="min-w-0 flex-1"
        @update:model-value="handleTabChange"
      >
        <TabsList class="h-8 w-full">
          <TabsTrigger
            v-for="tab in tabs"
            :key="tab.id"
            :value="tab.id"
            class="min-w-0 gap-1 px-1.5 text-xs"
            :data-testid="`side-panel-tab-${tab.id}`"
          >
            <component :is="tab.icon" :size="13" />
            <span class="truncate">{{ tab.label }}</span>
          </TabsTrigger>
        </TabsList>
      </Tabs>
      <Button
        variant="ghost"
        size="icon-sm"
        class="h-6 w-6 shrink-0"
        title="Close panel"
        data-testid="side-panel-close"
        @click="sidePanel.close()"
      >
        <X :size="13" />
      </Button>
    </div>

    <!-- Tab contents (lazy mounted, kept alive) -->
    <div class="min-h-0 flex-1">
      <div
        v-show="sidePanel.activeTab === 'git'"
        class="flex h-full flex-col items-center justify-center gap-3 px-6 text-center"
        data-testid="side-panel-git"
      >
        <GitBranch :size="32" class="text-muted-foreground" />
        <div>
          <p class="text-sm font-medium text-foreground">Git integration</p>
          <p class="mt-1 text-xs text-muted-foreground">
            Coming soon — review changes, branches and commits right here.
          </p>
        </div>
      </div>
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
  </div>
</template>
