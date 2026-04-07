<script setup lang="ts">
import { ref, watch, onMounted } from 'vue'
import type { FileNode } from '@/types'
import { useFileExplorerStore } from '@/stores/fileExplorer'
import { UiButton, UiScrollArea } from '@/components/ui'
import { RefreshCw, ChevronRight } from 'lucide-vue-next'
import FileTree from './FileTree.vue'
import FileContextMenu from './FileContextMenu.vue'
import FileUploadZone from './FileUploadZone.vue'

const props = defineProps<{
  workspaceId: string
}>()

const store = useFileExplorerStore()

// Context menu state
const contextMenu = ref<{
  x: number
  y: number
  node: FileNode
} | null>(null)

// Drag resize state
const isDragging = ref(false)

const panelWidth = ref(store.panelWidth)

// Load root on first open
onMounted(() => {
  if (store.tree.length === 0) {
    store.fetchDirectory(props.workspaceId, '/workspace')
  }
})

// Re-fetch when workspace changes
watch(
  () => props.workspaceId,
  () => {
    store.reset()
    store.open()
    store.fetchDirectory(props.workspaceId, '/workspace')
  },
)

function handleSelect(path: string): void {
  store.selectFile(path, props.workspaceId)
}

function handleContextMenu(event: MouseEvent, node: FileNode): void {
  contextMenu.value = {
    x: event.clientX,
    y: event.clientY,
    node,
  }
}

function handleDownload(path: string): void {
  store.downloadFile(props.workspaceId, path)
}

function handleRefresh(): void {
  store.refreshAll(props.workspaceId)
}

function onDragStart(e: MouseEvent): void {
  e.preventDefault()
  isDragging.value = true
  const startX = e.clientX
  const startWidth = panelWidth.value

  const onMove = (ev: MouseEvent) => {
    const delta = startX - ev.clientX
    panelWidth.value = Math.max(200, Math.min(startWidth + delta, 600))
  }

  const onUp = () => {
    isDragging.value = false
    store.panelWidth = panelWidth.value
    document.removeEventListener('mousemove', onMove)
    document.removeEventListener('mouseup', onUp)
  }

  document.addEventListener('mousemove', onMove)
  document.addEventListener('mouseup', onUp)
}
</script>

<template>
  <div
    class="flex h-full border-l border-border bg-surface shrink-0 relative transition-all duration-200"
    :style="{ width: `${panelWidth}px` }"
  >
    <!-- Drag handle (left edge) -->
    <div
      class="w-1 hover:bg-primary cursor-col-resize shrink-0 transition-colors"
      @mousedown="onDragStart"
    />

    <!-- Close toggle -->
    <UiButton
      variant="secondary"
      size="icon-sm"
      class="absolute -left-3 top-3 z-10 w-6 h-6 rounded-full bg-surface border border-border hover:bg-surface-hover transition-colors flex items-center justify-center"
      title="Close files"
      @click="store.close()"
    >
      <ChevronRight :size="14" />
    </UiButton>

    <div class="flex flex-col flex-1 min-w-0">
      <!-- Header -->
      <div class="flex items-center justify-between px-3 py-2 border-b border-border shrink-0">
        <span class="text-sm font-medium text-fg">Files</span>
        <UiButton variant="ghost" size="icon-sm" title="Refresh" @click="handleRefresh">
          <RefreshCw :size="14" />
        </UiButton>
      </div>

      <!-- File tree with drag & drop upload -->
      <FileUploadZone
        :workspace-id="workspaceId"
        target-path="/workspace"
        class="flex-1 min-h-0"
        @uploaded="handleRefresh"
      >
        <UiScrollArea class="h-full">
          <FileTree
            :nodes="store.tree"
            :workspace-id="workspaceId"
            @select="handleSelect"
            @contextmenu="handleContextMenu"
          />
        </UiScrollArea>
      </FileUploadZone>
    </div>

    <!-- Context menu -->
    <FileContextMenu
      v-if="contextMenu"
      :node="contextMenu.node"
      :x="contextMenu.x"
      :y="contextMenu.y"
      @close="contextMenu = null"
      @download="handleDownload"
      @copy-path="contextMenu = null"
    />
  </div>
</template>
