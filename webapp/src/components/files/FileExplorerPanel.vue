<script setup lang="ts">
/**
 * FileExplorerPanel — file tree content for the side panel Files tab.
 *
 * Panel chrome (resize handle, close button) lives in WorkspaceSidePanel;
 * this component only renders the tree, upload zone and context menu.
 */
import { ref, watch, onMounted } from 'vue'
import type { FileNode } from '@/types'
import { useFileExplorerStore } from '@/stores/fileExplorer'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { RefreshCw } from '@lucide/vue'
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
</script>

<template>
  <div class="flex h-full flex-col bg-card" data-testid="file-explorer-panel">
    <!-- Header -->
    <div class="flex shrink-0 items-center justify-between border-b border-border px-3 py-1.5">
      <span class="text-xs font-medium text-foreground">Files</span>
      <Button
        variant="ghost"
        size="icon-sm"
        class="h-6 w-6"
        title="Refresh"
        @click="handleRefresh"
      >
        <RefreshCw :size="12" />
      </Button>
    </div>

    <!-- File tree with drag & drop upload -->
    <FileUploadZone
      :workspace-id="workspaceId"
      target-path="/workspace"
      class="min-h-0 flex-1"
      @uploaded="handleRefresh"
    >
      <ScrollArea class="h-full">
        <FileTree
          :nodes="store.tree"
          :workspace-id="workspaceId"
          @select="handleSelect"
          @contextmenu="handleContextMenu"
        />
      </ScrollArea>
    </FileUploadZone>

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
