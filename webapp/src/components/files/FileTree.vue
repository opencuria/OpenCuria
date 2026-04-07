<script setup lang="ts">
import type { FileNode } from '@/types'
import FileTreeNode from './FileTreeNode.vue'

defineProps<{
  nodes: FileNode[]
  workspaceId: string
}>()

const emit = defineEmits<{
  select: [path: string]
  contextmenu: [event: MouseEvent, node: FileNode]
}>()
</script>

<template>
  <div class="py-1">
    <FileTreeNode
      v-for="node in nodes"
      :key="node.path"
      :node="node"
      :depth="0"
      :workspace-id="workspaceId"
      @select="emit('select', $event)"
      @contextmenu="(e: MouseEvent, n: FileNode) => emit('contextmenu', e, n)"
    />

    <!-- Empty state -->
    <div
      v-if="!nodes.length"
      class="flex flex-col items-center gap-2 py-8 text-muted-fg"
    >
      <span class="text-xs">No files found</span>
    </div>
  </div>
</template>
