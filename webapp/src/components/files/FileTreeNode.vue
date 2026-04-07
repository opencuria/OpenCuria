<script setup lang="ts">
import { computed } from 'vue'
import type { FileNode } from '@/types'
import { useFileExplorerStore } from '@/stores/fileExplorer'
import {
  ChevronRight,
  Folder,
  FolderOpen,
  FileText,
  File as FileIcon,
} from 'lucide-vue-next'

const props = defineProps<{
  node: FileNode
  depth: number
  workspaceId: string
}>()

const emit = defineEmits<{
  select: [path: string]
  contextmenu: [event: MouseEvent, node: FileNode]
}>()

const store = useFileExplorerStore()

const isExpanded = computed(() => store.expandedPaths.has(props.node.path))
const isSelected = computed(() => store.selectedPath === props.node.path)
const isLoading = computed(() => store.loadingPaths.has(props.node.path))
const isDirectory = computed(() => props.node.type === 'directory')

const indent = computed(() => `${props.depth * 16}px`)

function humanSize(bytes: number): string {
  if (bytes === 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(1024))
  return `${(bytes / Math.pow(1024, i)).toFixed(i > 0 ? 1 : 0)} ${units[i]}`
}

function handleClick(): void {
  if (isDirectory.value) {
    store.toggleExpand(props.node.path, props.workspaceId)
  } else {
    emit('select', props.node.path)
  }
}

function handleContextMenu(e: MouseEvent): void {
  e.preventDefault()
  emit('contextmenu', e, props.node)
}

function handleDragOver(e: DragEvent): void {
  if (isDirectory.value) {
    e.preventDefault()
    e.stopPropagation()
  }
}

function handleDrop(e: DragEvent): void {
  if (!isDirectory.value) return
  e.preventDefault()
  e.stopPropagation()
  // Bubble up to FileUploadZone which handles the actual file processing
}
</script>

<template>
  <div>
    <div
      class="flex items-center gap-1 py-0.5 px-2 cursor-pointer text-sm rounded-sm transition-colors group"
      :class="{
        'bg-primary/10 text-primary': isSelected,
        'hover:bg-muted': !isSelected,
        'opacity-60': isLoading,
      }"
      :style="{ paddingLeft: indent }"
      @click="handleClick"
      @contextmenu="handleContextMenu"
      @dragover="handleDragOver"
      @drop="handleDrop"
    >
      <!-- Directory chevron -->
      <ChevronRight
        v-if="isDirectory"
        :size="14"
        class="shrink-0 transition-transform"
        :class="{ 'rotate-90': isExpanded }"
      />
      <span v-else class="w-3.5 shrink-0" />

      <!-- Icon -->
      <component
        :is="isDirectory ? (isExpanded ? FolderOpen : Folder) : (node.name.match(/\.(ts|js|vue|py|md|json|yaml|yml|toml|css|html|sh|sql)$/) ? FileText : FileIcon)"
        :size="14"
        class="shrink-0"
        :class="isDirectory ? 'text-amber-500' : 'text-muted-fg'"
      />

      <!-- Name -->
      <span class="truncate flex-1 text-xs">{{ node.name }}</span>

      <!-- Size (files only) -->
      <span
        v-if="!isDirectory && node.size > 0"
        class="text-[10px] text-muted-fg shrink-0 opacity-0 group-hover:opacity-100 transition-opacity"
      >
        {{ humanSize(node.size) }}
      </span>
    </div>

    <!-- Children (recursive) -->
    <template v-if="isDirectory && isExpanded && node.children">
      <FileTreeNode
        v-for="child in node.children"
        :key="child.path"
        :node="child"
        :depth="depth + 1"
        :workspace-id="workspaceId"
        @select="emit('select', $event)"
        @contextmenu="(e: MouseEvent, n: FileNode) => emit('contextmenu', e, n)"
      />
    </template>

    <!-- Loading indicator for directory -->
    <div
      v-if="isDirectory && isExpanded && isLoading && !node.children?.length"
      class="text-xs text-muted-fg py-1"
      :style="{ paddingLeft: `${(depth + 1) * 16 + 8}px` }"
    >
      Loading...
    </div>
  </div>
</template>
