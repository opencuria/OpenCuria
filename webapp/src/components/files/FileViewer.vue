<script setup lang="ts">
import { computed } from 'vue'
import { useFileExplorerStore } from '@/stores/fileExplorer'
import { Button } from '@/components/ui/button'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import { X, Download, AlertTriangle, FileX } from '@lucide/vue'

const props = defineProps<{
  workspaceId: string
}>()

const emit = defineEmits<{
  close: []
}>()

const store = useFileExplorerStore()

const file = computed(() => store.viewingFile)

const pathParts = computed(() => {
  if (!file.value) return []
  return file.value.path.split('/').filter(Boolean)
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
  <div class="flex flex-col flex-1 min-h-0">
    <div class="flex items-center justify-between px-4 py-2 border-b border-border bg-card shrink-0">
      <div class="flex items-center gap-1 text-xs text-muted-foreground min-w-0 overflow-hidden">
        <span
          v-for="(part, i) in pathParts"
          :key="i"
          class="flex items-center gap-1 shrink-0"
        >
          <span v-if="i > 0" class="text-border">/</span>
          <span :class="i === pathParts.length - 1 ? 'text-foreground font-medium' : ''">
            {{ part }}
          </span>
        </span>
      </div>
      <div class="flex items-center gap-1 shrink-0">
        <Button variant="ghost" size="icon-sm" title="Download" @click="handleDownload">
          <Download :size="14" />
        </Button>
        <Button variant="ghost" size="icon-sm" title="Close" @click="handleClose">
          <X :size="14" />
        </Button>
      </div>
    </div>

    <div class="flex-1 overflow-auto">
      <div v-if="store.isLoadingContent" class="flex items-center justify-center h-full">
        <LoadingSpinner :size="24" />
      </div>

      <div
        v-else-if="file?.mediaType === 'image'"
        class="flex items-center justify-center h-full p-4 bg-checkerboard"
      >
        <img
          :src="imageDataUrl!"
          :alt="file.path"
          class="max-w-full max-h-full object-contain rounded shadow-sm"
        />
      </div>

      <div
        v-else-if="file?.mediaType === 'pdf'"
        class="flex flex-col h-full"
      >
        <iframe
          :src="pdfDataUrl!"
          class="flex-1 w-full border-0"
          title="PDF preview"
        />
      </div>

      <div
        v-else-if="file?.mediaType === 'binary'"
        class="flex flex-col items-center justify-center h-full gap-3 text-muted-foreground"
      >
        <FileX :size="32" class="opacity-40" />
        <span class="text-sm">Binary file — cannot display</span>
      </div>

      <template v-else-if="file">
        <div
          v-if="file.truncated"
          class="flex items-center gap-2 px-4 py-2 bg-amber-500/10 text-amber-600 dark:text-amber-400 text-xs border-b border-amber-500/20"
        >
          <AlertTriangle :size="14" />
          File truncated at 5 MB
        </div>

        <pre class="font-mono text-xs text-foreground p-4 whitespace-pre-wrap break-words">{{ file.content }}</pre>
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
