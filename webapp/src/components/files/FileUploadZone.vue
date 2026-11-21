<script setup lang="ts">
import { ref } from 'vue'
import { Upload, Loader2 } from '@lucide/vue'
import { canTransferFile, fileTransferLimitMessage } from '@opencuria/chat-core/workspaceFiles'
import { useFileExplorerStore } from '@/stores/fileExplorer'
import { useNotificationStore } from '@/stores/notifications'

const props = defineProps<{
  workspaceId: string
  targetPath: string
}>()

const emit = defineEmits<{
  uploaded: []
}>()

const isDragOver = ref(false)
const isUploading = ref(false)

const fileExplorer = useFileExplorerStore()

let dragCounter = 0

function onDragEnter(e: DragEvent): void {
  e.preventDefault()
  dragCounter++
  isDragOver.value = true
}

function onDragLeave(e: DragEvent): void {
  e.preventDefault()
  dragCounter--
  if (dragCounter <= 0) {
    isDragOver.value = false
    dragCounter = 0
  }
}

function onDragOver(e: DragEvent): void {
  e.preventDefault()
}

async function uploadFiles(fileList: FileList): Promise<void> {
  const files = Array.from(fileList)
  if (files.length === 0) return

  isUploading.value = true
  const notify = useNotificationStore()
  // Failures are toasted by the store; each upload settles on its own.
  await Promise.all(
    files.map((file) => {
      if (!canTransferFile(file.size)) {
        notify.error('Upload failed', `"${file.name}" was skipped. ${fileTransferLimitMessage()}`)
        return null
      }
      return fileExplorer.uploadFile(props.workspaceId, {
        directory: props.targetPath,
        filename: file.name,
        body: file,
        conflict: 'replace',
      })
    }),
  )

  isUploading.value = false
  emit('uploaded')
}

async function onDrop(e: DragEvent): Promise<void> {
  e.preventDefault()
  isDragOver.value = false
  dragCounter = 0

  const files = e.dataTransfer?.files
  if (!files || files.length === 0) return
  await uploadFiles(files)
}
</script>

<template>
  <div
    class="relative h-full"
    @dragenter="onDragEnter"
    @dragleave="onDragLeave"
    @dragover="onDragOver"
    @drop="onDrop"
  >
    <slot />

    <!-- Drop overlay -->
    <div
      v-if="isDragOver"
      class="absolute inset-0 z-40 bg-primary/10 border-2 border-dashed border-primary rounded-lg flex items-center justify-center"
    >
      <div class="flex flex-col items-center gap-2 text-primary">
        <Upload :size="24" />
        <span class="text-sm font-medium">Drop files here</span>
      </div>
    </div>

    <!-- Uploading overlay -->
    <div
      v-if="isUploading"
      class="absolute inset-0 z-40 bg-card/70 rounded-lg flex items-center justify-center"
    >
      <div class="flex flex-col items-center gap-2 text-primary">
        <Loader2 :size="24" class="animate-spin" />
        <span class="text-sm font-medium">Uploading…</span>
      </div>
    </div>
  </div>
</template>
