<script setup lang="ts">
import { computed } from 'vue'
import { useFileExplorerStore } from '@/stores/fileExplorer'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import {
  X,
  Download,
  AlertTriangle,
  FileX,
  FileText,
  FileCode2,
  Image as ImageIcon,
  FileType2,
} from '@lucide/vue'

const props = defineProps<{
  workspaceId: string
}>()

const emit = defineEmits<{
  close: []
}>()

const store = useFileExplorerStore()

const file = computed(() => store.viewingFile)

const fileName = computed(() => file.value?.path.split('/').pop() ?? '')
const directoryPath = computed(() => {
  if (!file.value) return ''
  return file.value.path.split('/').slice(0, -1).join('/') || '/'
})

const CODE_EXTENSIONS = new Set([
  'js', 'ts', 'jsx', 'tsx', 'vue', 'py', 'go', 'rs', 'java', 'c', 'h', 'cpp',
  'hpp', 'cs', 'rb', 'php', 'swift', 'kt', 'sh', 'bash', 'zsh', 'sql', 'html',
  'css', 'scss', 'json', 'yaml', 'yml', 'toml', 'xml', 'md', 'dockerfile',
])

const fileExtension = computed(() => {
  const dot = fileName.value.lastIndexOf('.')
  return dot >= 0 ? fileName.value.slice(dot + 1).toLowerCase() : ''
})

const fileIcon = computed(() => {
  if (!file.value) return FileText
  switch (file.value.mediaType) {
    case 'image':
      return ImageIcon
    case 'pdf':
      return FileType2
    case 'binary':
      return FileX
    default:
      return CODE_EXTENSIONS.has(fileExtension.value) ? FileCode2 : FileText
  }
})

const fileSizeLabel = computed(() => {
  const size = file.value?.size
  if (size == null) return null
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
})

const mediaTypeLabel = computed(() => {
  if (!file.value) return ''
  switch (file.value.mediaType) {
    case 'image':
      return 'Image'
    case 'pdf':
      return 'PDF'
    case 'binary':
      return 'Binary'
    default:
      return fileExtension.value ? fileExtension.value.toUpperCase() : 'Text'
  }
})

/** Line numbers are rendered for reasonably sized files only. */
const LINE_NUMBER_LIMIT = 5000

const contentLines = computed(() => {
  if (!file.value || file.value.mediaType !== 'text') return null
  const lines = file.value.content.split('\n')
  if (lines.length > LINE_NUMBER_LIMIT) return null
  return lines
})

const lineNumberWidth = computed(() => {
  if (!contentLines.value) return 0
  return String(contentLines.value.length).length
})

const imageDataUrl = computed(() => {
  if (!file.value || file.value.mediaType !== 'image') return null
  return `data:${file.value.mimeType};base64,${file.value.rawBase64}`
})

const pdfDataUrl = computed(() => {
  if (!file.value || file.value.mediaType !== 'pdf') return null
  return `data:application/pdf;base64,${file.value.rawBase64}`
})

function handleClose(): void {
  store.closeFileViewer()
  emit('close')
}

function handleDownload(): void {
  if (!file.value) return
  store.downloadFile(props.workspaceId, file.value.path)
}
</script>

<template>
  <div class="flex min-h-0 flex-1 flex-col" data-testid="file-viewer">
    <!-- Header -->
    <div class="flex shrink-0 items-center gap-3 border-b border-border bg-card px-4 py-2">
      <div
        class="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-xs)] border border-border bg-muted/50 text-muted-foreground"
      >
        <component :is="fileIcon" :size="15" />
      </div>
      <div class="min-w-0 flex-1">
        <div class="truncate text-sm font-medium text-foreground" data-testid="file-viewer-name">
          {{ fileName || 'Loading…' }}
        </div>
        <div class="truncate text-xs text-muted-foreground" data-testid="file-viewer-path">
          {{ directoryPath }}
        </div>
      </div>
      <span
        v-if="file"
        class="hidden shrink-0 rounded-full border border-border px-2 py-0.5 text-[10px] font-medium text-muted-foreground sm:inline-block"
      >
        {{ mediaTypeLabel }}<template v-if="fileSizeLabel"> · {{ fileSizeLabel }}</template>
      </span>
      <div class="flex shrink-0 items-center gap-1">
        <Button
          variant="ghost"
          size="icon-sm"
          title="Download"
          :disabled="!file"
          data-testid="file-viewer-download"
          @click="handleDownload"
        >
          <Download :size="14" />
        </Button>
        <Button
          variant="ghost"
          size="icon-sm"
          title="Close"
          data-testid="file-viewer-close"
          @click="handleClose"
        >
          <X :size="14" />
        </Button>
      </div>
    </div>

    <!-- Content -->
    <div class="min-h-0 flex-1 overflow-auto bg-background">
      <!-- Loading -->
      <div v-if="store.isLoadingContent" class="space-y-2 p-4" data-testid="file-viewer-loading">
        <Skeleton v-for="i in 12" :key="i" class="h-3.5" :style="{ width: `${88 - (i % 4) * 14}%` }" />
      </div>

      <!-- Image -->
      <div
        v-else-if="file?.mediaType === 'image'"
        class="flex h-full items-center justify-center bg-checkerboard p-4"
      >
        <img
          :src="imageDataUrl!"
          :alt="file.path"
          class="max-h-full max-w-full rounded object-contain shadow-sm"
        />
      </div>

      <!-- PDF -->
      <div v-else-if="file?.mediaType === 'pdf'" class="flex h-full flex-col">
        <iframe :src="pdfDataUrl!" class="w-full flex-1 border-0" title="PDF preview" />
      </div>

      <!-- Binary -->
      <div
        v-else-if="file?.mediaType === 'binary'"
        class="flex h-full flex-col items-center justify-center gap-3 text-muted-foreground"
      >
        <FileX :size="32" class="opacity-40" />
        <span class="text-sm">Binary file — cannot display</span>
        <span v-if="fileSizeLabel" class="text-xs">{{ fileSizeLabel }}</span>
      </div>

      <!-- Text -->
      <template v-else-if="file">
        <div
          v-if="file.truncated"
          class="flex items-center gap-2 border-b border-amber-500/20 bg-amber-500/10 px-4 py-2 text-xs text-amber-600 dark:text-amber-400"
        >
          <AlertTriangle :size="14" />
          File truncated at 5 MB
        </div>

        <div class="p-3">
          <div
            class="overflow-hidden rounded-[var(--radius-xs)] border border-border bg-muted/30"
            data-testid="file-viewer-code"
          >
            <div v-if="contentLines" class="overflow-x-auto py-2">
              <div
                v-for="(line, index) in contentLines"
                :key="index"
                class="flex font-mono text-xs leading-5"
              >
                <span
                  class="shrink-0 select-none border-r border-border/60 pr-3 text-right text-muted-foreground/50"
                  :style="{ width: `${lineNumberWidth + 2}ch`, marginRight: '0.75rem' }"
                >{{ index + 1 }}</span>
                <span class="whitespace-pre text-foreground">{{ line || ' ' }}</span>
              </div>
            </div>
            <pre
              v-else
              class="overflow-x-auto p-4 font-mono text-xs leading-5 whitespace-pre text-foreground"
            >{{ file.content }}</pre>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.bg-checkerboard {
  background-color: #1a1a1a;
  background-image:
    linear-gradient(45deg, #2a2a2a 25%, transparent 25%),
    linear-gradient(-45deg, #2a2a2a 25%, transparent 25%),
    linear-gradient(45deg, transparent 75%, #2a2a2a 75%),
    linear-gradient(-45deg, transparent 75%, #2a2a2a 75%);
  background-size: 20px 20px;
  background-position: 0 0, 0 10px, 10px -10px, -10px 0px;
}
</style>
