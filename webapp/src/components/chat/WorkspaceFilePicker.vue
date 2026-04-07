<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useFileExplorerStore } from '@/stores/fileExplorer'
import type { FileNode } from '@/types'
import { Folder, Image, Video, File, ChevronLeft, Upload } from 'lucide-vue-next'

const props = defineProps<{
  workspaceId: string
}>()

const emit = defineEmits<{
  select: [path: string, filename: string]
  close: []
  upload: [file: File]
}>()

const uploadInputRef = ref<HTMLInputElement | null>(null)

function triggerUpload(): void {
  uploadInputRef.value?.click()
}

function handleUploadInput(e: Event): void {
  const files = Array.from((e.target as HTMLInputElement).files ?? []).filter((f) =>
    f.type.startsWith('image/') || f.type.startsWith('video/'),
  )
  for (const file of files) {
    emit('upload', file)
  }
  ;(e.target as HTMLInputElement).value = ''
}

const IMAGE_EXTENSIONS = new Set([
  'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'ico', 'avif', 'tiff', 'tif',
])

function isImage(name: string): boolean {
  const ext = name.split('.').pop()?.toLowerCase() ?? ''
  return IMAGE_EXTENSIONS.has(ext)
}

const VIDEO_EXTENSIONS = new Set([
  'mp4', 'webm', 'ogg', 'ogv', 'mov', 'm4v', 'avi', 'mkv', 'wmv', 'flv', 'mpeg',
  'mpg', '3gp', '3g2', 'ts', 'm2ts',
])

function isVideo(name: string): boolean {
  const ext = name.split('.').pop()?.toLowerCase() ?? ''
  return VIDEO_EXTENSIONS.has(ext)
}

function isMedia(name: string): boolean {
  return isImage(name) || isVideo(name)
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
  if (!isMedia(node.name)) return
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
                ? 'text-fg hover:bg-surface-hover cursor-pointer'
                : isMedia(node.name)
                  ? 'text-primary hover:bg-surface-hover cursor-pointer'
                : 'text-muted-fg cursor-default opacity-50',
            ]"
            :disabled="node.type === 'file' && !isMedia(node.name)"
            @click="node.type === 'directory' ? navigate(node) : selectFile(node)"
          >
            <Folder v-if="node.type === 'directory'" :size="13" class="shrink-0" />
            <Image v-else-if="isImage(node.name)" :size="13" class="shrink-0" />
            <Video v-else-if="isVideo(node.name)" :size="13" class="shrink-0" />
            <File v-else :size="13" class="shrink-0" />
            <span class="truncate flex-1">{{ node.name }}</span>
          </button>

        <div v-if="!currentNodes.length" class="px-3 py-3 text-xs text-muted-fg text-center">
          No files found
        </div>
      </template>
    </div>

    <!-- Footer: hint + upload button -->
    <div class="px-3 py-1.5 border-t border-border flex items-center justify-between gap-2 shrink-0">
      <span class="text-[10px] text-muted-fg">Click media to insert its path</span>
      <button
        type="button"
        class="flex items-center gap-1 text-[10px] text-primary hover:text-primary/80 transition-colors shrink-0"
        title="Upload image or video from your computer"
        @click="triggerUpload"
      >
        <Upload :size="11" />
        Upload
      </button>
      <input
        ref="uploadInputRef"
        type="file"
        accept="image/*,video/*"
        multiple
        class="hidden"
        @change="handleUploadInput"
      />
    </div>
  </div>
</template>
