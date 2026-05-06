<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useFileExplorerStore } from '@/stores/fileExplorer'
import type { FileNode } from '@/types'
import { Folder, Image, Video, File as FileIcon, ChevronLeft, Upload } from 'lucide-vue-next'
import { classifyWorkspaceFile } from '@/lib/workspaceFileRefs'

const props = defineProps<{
  workspaceId: string
  disabled?: boolean
}>()

const emit = defineEmits<{
  select: [path: string, filename: string]
  close: []
  upload: [file: File]
}>()

const uploadInputRef = ref<HTMLInputElement | null>(null)

function triggerUpload(): void {
  if (props.disabled) return
  uploadInputRef.value?.click()
}

function handleUploadInput(e: Event): void {
  if (props.disabled) return
  const files = Array.from((e.target as HTMLInputElement).files ?? [])
  for (const file of files) {
    emit('upload', file)
  }
  ;(e.target as HTMLInputElement).value = ''
}

const fileExplorer = useFileExplorerStore()
const currentPath = ref('/workspace')

watch(
  () => props.workspaceId,
  (id) => {
    if (id) fileExplorer.fetchDirectory(id, currentPath.value)
  },
  { immediate: true },
)

const currentNodes = computed<FileNode[]>(() => {
  if (currentPath.value === '/workspace') return fileExplorer.tree
  return findNodes(fileExplorer.tree, currentPath.value)
})

function findNodes(nodes: FileNode[], path: string): FileNode[] {
  for (const node of nodes) {
    if (node.path === path) return node.children ?? []
    if (node.children) {
      const found = findNodes(node.children, path)
      if (found.length || node.path === path) return found
    }
  }
  return []
}

function navigate(node: FileNode): void {
  currentPath.value = node.path
  if (!node.children) {
    fileExplorer.fetchDirectory(props.workspaceId, node.path)
  }
}

function goUp(): void {
  const parts = currentPath.value.split('/')
  if (parts.length > 2) {
    parts.pop()
    currentPath.value = parts.join('/')
  }
}

function selectFile(node: FileNode): void {
  if (props.disabled) return
  emit('select', node.path, node.name)
}
</script>

<template>
  <div
    class="w-72 max-w-[min(18rem,calc(100vw-1.5rem))] max-h-64 glass-strong rounded-[var(--radius-md)] flex flex-col overflow-hidden"
  >
    <!-- Header -->
    <div class="flex items-center gap-1.5 px-2.5 py-2 border-b border-border shrink-0">
      <button
        v-if="currentPath !== '/workspace'"
        class="flex items-center gap-1 text-xs text-muted-fg hover:text-fg transition-colors"
        @click="goUp"
      >
        <ChevronLeft :size="13" />
      </button>
      <span class="text-xs text-muted-fg truncate flex-1">{{ currentPath }}</span>
    </div>

    <!-- File list -->
    <div class="overflow-y-auto flex-1 py-1">
      <div
        v-if="fileExplorer.loadingPaths.has(currentPath)"
        class="px-3 py-3 text-xs text-muted-fg text-center"
      >
        Loading…
      </div>
      <template v-else>
        <button
          v-for="node in currentNodes"
          :key="node.path"
          class="flex items-center gap-2 w-full px-3 py-1.5 text-xs text-left transition-colors"
            :class="[
               node.type === 'directory'
                 ? props.disabled
                   ? 'text-fg/60 cursor-default'
                   : 'text-fg hover:bg-surface-hover cursor-pointer'
                 : props.disabled
                   ? 'text-primary/60 cursor-default'
                   : 'text-primary hover:bg-surface-hover cursor-pointer',
             ]"
            :disabled="props.disabled"
            @click="node.type === 'directory' ? navigate(node) : selectFile(node)"
          >
            <Folder v-if="node.type === 'directory'" :size="13" class="shrink-0" />
            <Image v-else-if="classifyWorkspaceFile(node.name) === 'image'" :size="13" class="shrink-0" />
            <Video v-else-if="classifyWorkspaceFile(node.name) === 'video'" :size="13" class="shrink-0" />
            <FileIcon v-else :size="13" class="shrink-0" />
            <span class="truncate flex-1">{{ node.name }}</span>
          </button>

        <div v-if="!currentNodes.length" class="px-3 py-3 text-xs text-muted-fg text-center">
          No files found
        </div>
      </template>
    </div>

    <!-- Footer: hint + upload button -->
    <div class="px-3 py-1.5 border-t border-border flex items-center justify-between gap-2 shrink-0">
      <span class="text-[10px] text-muted-fg">
        {{ props.disabled ? 'Start workspace to insert or upload files' : 'Click file to insert its path' }}
      </span>
      <button
        type="button"
        class="flex items-center gap-1 text-[10px] text-primary hover:text-primary/80 transition-colors shrink-0"
        title="Upload file from your computer"
        :disabled="props.disabled"
        :class="props.disabled ? 'opacity-50 cursor-not-allowed' : ''"
        @click="triggerUpload"
      >
        <Upload :size="11" />
        Upload
      </button>
      <input
        ref="uploadInputRef"
        type="file"
        multiple
        class="hidden"
        @change="handleUploadInput"
      />
    </div>
  </div>
</template>
